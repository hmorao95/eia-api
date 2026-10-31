"""Unit tests for :mod:`eia_api`.

The tests never touch the network: a synthetic in-memory API that mimics the
real EIA route/metadata/data envelope is built once and fed to the client either
by monkeypatching :meth:`EIA._request` (the single HTTP choke point) or by
stubbing the ``requests.Session`` used underneath it.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import fire
import httpx
import pandas as pd
import pytest
import requests

import eia_api.core as eia_core
from eia_api.core import EIA, Route

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #

# Three monthly retail-price rows for one state/sector, as the API returns them
# (all values are strings). Used to exercise parsing, export and pagination.
_ROWS: list[dict[str, Any]] = [
    {
        "period": "2020-01",
        "stateid": "CO",
        "sectorid": "RES",
        "price": "11.0",
        "price-units": "cents/kWh",
    },
    {
        "period": "2020-02",
        "stateid": "CO",
        "sectorid": "RES",
        "price": "12.0",
        "price-units": "cents/kWh",
    },
    {
        "period": "2020-03",
        "stateid": "CO",
        "sectorid": "RES",
        "price": "13.0",
        "price-units": "cents/kWh",
    },
]


def _fake_request(endpoint: str, params: list[tuple[str, str]]) -> dict[str, Any]:
    """Return a canned ``response`` object for each mocked endpoint."""
    lookup = dict(params)  # offset/length appear once, so a dict is enough here
    if not endpoint:
        return {
            "id": "",
            "routes": [
                {
                    "id": "electricity",
                    "name": "Electricity",
                    "description": "Power data",
                }
            ],
        }
    if endpoint == "electricity":
        return {
            "id": "electricity",
            "routes": [
                {
                    "id": "retail-sales",
                    "name": "Retail sales",
                    "description": "Retail electricity sales",
                }
            ],
        }
    if endpoint == "electricity/retail-sales":
        return {
            "id": "retail-sales",
            "routes": [],
            "frequency": [{"id": "monthly", "description": "Monthly"}],
            "facets": [
                {"id": "stateid", "description": "State"},
                {"id": "sectorid", "description": "Sector"},
            ],
            "data": {
                "price": {"alias": "Average retail price", "units": "cents/kWh"},
                "revenue": {"alias": "Revenue", "units": "million dollars"},
            },
            "startPeriod": "2001-01",
            "endPeriod": "2020-03",
        }
    if endpoint == "electricity/retail-sales/data":
        offset = int(lookup.get("offset", "0"))
        length = int(lookup.get("length", "5000"))
        return {
            "total": str(len(_ROWS)),
            "frequency": "monthly",
            "data": _ROWS[offset : offset + length],
        }
    if endpoint == "electricity/retail-sales/facet/stateid":
        return {
            "facets": [
                {"id": "CO", "name": "Colorado"},
                {"id": "CA", "name": "California"},
            ]
        }
    # A petroleum/natural-gas price route: the metadata maps the single "value"
    # column to an empty list rather than an {alias, units} dict.
    if endpoint == "petroleum/pri/spt":
        return {
            "id": "spt",
            "routes": [],
            "frequency": [{"id": "daily", "description": "Daily"}],
            "facets": [{"id": "series", "description": "Series"}],
            "data": {"value": []},
            "startPeriod": "1986-01-02",
            "endPeriod": "2020-03-15",
        }
    if endpoint == "petroleum/pri/spt/data":
        rows = [
            {
                "period": "2020-01-02",
                "series": "RWTC",
                "value": "61.18",
                "value-units": "$/bbl",
            },
            {
                "period": "2020-01-03",
                "series": "RWTC",
                "value": "63.05",
                "value-units": "$/bbl",
            },
        ]
        offset = int(lookup.get("offset", "0"))
        length = int(lookup.get("length", "5000"))
        return {
            "total": str(len(rows)),
            "frequency": "daily",
            "data": rows[offset : offset + length],
        }
    msg = f"unexpected endpoint {endpoint!r}"
    raise AssertionError(msg)


class _FakeResponse:
    """Minimal stand-in for a :class:`requests.Response`."""

    # Read by the client's debug log line after each request.
    status_code = 200

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store the canned JSON ``payload``."""
        self._payload = payload

    def json(self) -> dict[str, Any]:
        """Return the canned payload."""
        return self._payload

    def raise_for_status(self) -> None:
        """No-op; canned responses are always considered successful."""


_ROUTE = "electricity/retail-sales"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EIA:
    """Return a client whose HTTP layer is replaced by the synthetic API."""
    eia = EIA(api_key="test-key", cache_dir=tmp_path)
    monkeypatch.setattr(eia, "_request", _fake_request)
    return eia


@pytest.fixture
def cli(client: EIA, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> eia_core._Cli:
    """The Fire CLI facade, bound to the mocked client."""
    # The facade builds a default client first, whose cache dir lives under
    # the home directory; point that at tmp_path so tests never touch it.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    facade = eia_core._Cli()
    facade._eia = client  # swap the real client for the offline mock
    return facade


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #


def test_route_str() -> None:
    """``Route`` renders as ``id — name``."""
    assert (
        str(Route("retail-sales", "Retail sales", "x")) == "retail-sales — Retail sales"
    )


def test_normalize_route_strips_slashes_and_data() -> None:
    """Surrounding slashes and a trailing /data are removed."""
    assert (
        EIA._normalize_route("/electricity/retail-sales/data/")
        == "electricity/retail-sales"
    )
    assert EIA._normalize_route("electricity") == "electricity"


def test_parse_period_handles_all_frequencies() -> None:
    """Year, month, quarter, date and hour labels parse to period starts."""
    out = EIA._parse_period(
        pd.Series(["2020", "2020-03", "2020-Q2", "2020-03-15", "2020-03-15T05"])
    )
    assert out.iloc[0] == pd.Timestamp("2020-01-01")
    assert out.iloc[1] == pd.Timestamp("2020-03-01")
    assert out.iloc[2] == pd.Timestamp("2020-04-01")  # Q2 -> April
    assert out.iloc[3] == pd.Timestamp("2020-03-15")
    assert out.iloc[4] == pd.Timestamp("2020-03-15 05:00:00")


def test_data_params_encodes_brackets() -> None:
    """Data, facet, sort and pagination params use the API's bracket syntax."""
    params = EIA._data_params(
        columns=["price"],
        facets={"stateid": "CO", "sectorid": ["RES", "COM"]},
        frequency="monthly",
        start="2020-01",
        end="2020-03",
        sort=[("period", "asc")],
        offset=0,
        length=5000,
    )
    assert ("data[]", "price") in params
    assert ("facets[stateid][]", "CO") in params
    assert ("facets[sectorid][]", "RES") in params
    assert ("facets[sectorid][]", "COM") in params
    assert ("frequency", "monthly") in params
    assert ("sort[0][column]", "period") in params
    assert ("sort[0][direction]", "asc") in params
    assert ("length", "5000") in params


def test_params_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing API key raises a helpful error before any request."""
    # An empty key falls back to EIA_API_KEY, so unset it for this test.
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    eia = EIA(api_key="", cache_dir="")
    with pytest.raises(ValueError, match="No EIA API key"):
        eia._params()


def test_api_key_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client falls back to the EIA_API_KEY environment variable."""
    monkeypatch.setenv("EIA_API_KEY", "from-env")
    assert EIA(cache_dir="").api_key == "from-env"


# --------------------------------------------------------------------------- #
# Metadata / browsing
# --------------------------------------------------------------------------- #


def test_browse_top_level(client: EIA) -> None:
    """Browsing the root lists the child routes."""
    frame = client.browse()
    assert list(frame.columns) == ["id", "name", "description"]
    assert frame["id"].tolist() == ["electricity"]


def test_browse_leaf_is_empty(client: EIA) -> None:
    """A dataset (leaf) route has no child routes."""
    assert client.browse("electricity/retail-sales").empty


def test_frequencies(client: EIA) -> None:
    """The dataset's frequencies are listed."""
    assert client.frequencies("electricity/retail-sales")["id"].tolist() == ["monthly"]


def test_facets(client: EIA) -> None:
    """The dataset's facets (filtering dimensions) are listed."""
    assert set(client.facets("electricity/retail-sales")["id"]) == {
        "stateid",
        "sectorid",
    }


def test_data_columns(client: EIA) -> None:
    """The dataset's value columns and units are listed."""
    frame = client.data_columns("electricity/retail-sales")
    assert set(frame["id"]) == {"price", "revenue"}
    assert frame.loc[frame["id"] == "price", "units"].item() == "cents/kWh"


def test_data_columns_handles_listy_metadata(client: EIA) -> None:
    """A column whose metadata is an empty list still lists its id, no crash."""
    frame = client.data_columns("petroleum/pri/spt")
    assert frame["id"].tolist() == ["value"]
    assert frame["alias"].tolist() == [""]
    assert frame["units"].tolist() == [""]


def test_get_data_default_data_on_listy_metadata(client: EIA) -> None:
    """get_data(data=None) works on price routes with empty column metadata."""
    frame = client.get_data("petroleum/pri/spt")
    assert "value" in frame.columns
    assert frame["value"].dtype.kind == "f"  # coerced despite blank metadata
    assert len(frame) == 2


def test_facet_values(client: EIA) -> None:
    """The allowed values of a facet are listed."""
    frame = client.facet_values("electricity/retail-sales", "stateid")
    assert set(frame["id"]) == {"CO", "CA"}


def test_metadata_is_memoised(client: EIA, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated metadata lookups hit the network once."""
    calls = {"n": 0}
    original = client._request

    def _counting(endpoint: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        calls["n"] += 1
        return original(endpoint, params)

    monkeypatch.setattr(client, "_request", _counting)
    client.metadata("electricity/retail-sales")
    client.metadata("electricity/retail-sales")
    assert calls["n"] == 1


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #


def test_get_data_shape_and_types(client: EIA) -> None:
    """The tidy frame has a parsed date, keeps period, and coerces values."""
    frame = client.get_data("electricity/retail-sales", data="price")
    assert list(frame.columns)[:2] == ["date", "period"]
    assert len(frame) == 3
    assert frame["price"].dtype.kind == "f"  # coerced to float
    assert frame["date"].iloc[0] == pd.Timestamp("2020-01-01")


def test_get_data_default_data_selects_all_columns(client: EIA) -> None:
    """``data=None`` requests every value column the dataset exposes."""
    # Only "price" exists in the fake rows, but "revenue" is also requested and
    # simply absent from the payload; the call still succeeds.
    frame = client.get_data("electricity/retail-sales")
    assert "price" in frame.columns


def test_get_data_single_string_data(client: EIA) -> None:
    """A single value column may be passed as a plain string."""
    assert "price" in client.get_data("electricity/retail-sales", data="price").columns


def test_get_data_paginates(client: EIA, monkeypatch: pytest.MonkeyPatch) -> None:
    """A page size below the total triggers multiple requests."""
    calls = {"n": 0}
    original = client._request

    def _counting(endpoint: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        if endpoint.endswith("/data"):
            calls["n"] += 1
        return original(endpoint, params)

    monkeypatch.setattr(client, "_request", _counting)
    frame = client.get_data("electricity/retail-sales", data="price", length=2)
    assert len(frame) == 3  # 2 + 1 across two pages
    assert calls["n"] == 2


def test_get_data_max_rows(client: EIA) -> None:
    """``max_rows`` caps the number of rows returned."""
    frame = client.get_data("electricity/retail-sales", data="price", max_rows=2)
    assert len(frame) == 2


def test_get_data_bad_length_raises(client: EIA) -> None:
    """A non-positive page size raises ``ValueError``."""
    with pytest.raises(ValueError, match="length must be a positive"):
        client.get_data("electricity/retail-sales", data="price", length=0)


def test_get_data_empty_returns_empty(
    client: EIA, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty result yields an empty frame, not an error."""

    def _empty(endpoint: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        if endpoint.endswith("/data"):
            return {"total": "0", "data": []}
        return _fake_request(endpoint, params)

    monkeypatch.setattr(client, "_request", _empty)
    assert client.get_data("electricity/retail-sales", data="price").empty


# --------------------------------------------------------------------------- #
# HTTP and disk cache
# --------------------------------------------------------------------------- #


def test_request_raises_on_api_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A top-level ``error`` field becomes a clear RuntimeError."""
    eia = EIA(api_key="bad", cache_dir=None)  # None disables the disk cache
    payload = {"error": "invalid or missing api_key", "code": 403}
    monkeypatch.setattr(eia.session, "get", lambda *a, **k: _FakeResponse(payload))
    with pytest.raises(RuntimeError, match="invalid or missing api_key"):
        eia._request("electricity", eia._params())


def test_disk_cache_avoids_second_http_hit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A cached response on disk is reused instead of re-requesting."""
    eia = EIA(api_key="k", cache_dir=tmp_path)
    payload = {"response": {"id": "electricity", "routes": []}}
    calls = {"n": 0}

    def _fake_get(*_a: Any, **_k: Any) -> _FakeResponse:
        calls["n"] += 1
        return _FakeResponse(payload)

    monkeypatch.setattr(eia.session, "get", _fake_get)
    first = eia._request("electricity", eia._params())
    second = eia._request("electricity", eia._params())  # served from disk
    assert first == second
    assert calls["n"] == 1


def test_rate_limit_throttles_close_requests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second back-to-back request sleeps to honour requests_per_second."""
    eia = EIA(api_key="k", cache_dir="", requests_per_second=10.0)  # 0.1s apart
    monkeypatch.setattr(
        eia.session, "get", lambda *a, **k: _FakeResponse({"response": {}})
    )
    clock = {"t": 1000.0}
    sleeps: list[float] = []
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds  # a real sleep would advance the clock

    monkeypatch.setattr(time, "sleep", _fake_sleep)

    eia._request("electricity", eia._params())  # first call: no wait
    eia._request("electricity", eia._params())  # immediate second: must wait
    assert len(sleeps) == 1
    assert sleeps[0] == pytest.approx(0.1)


def test_rate_limit_none_disables_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """requests_per_second=None never sleeps, however close the requests."""
    eia = EIA(api_key="k", cache_dir="", requests_per_second=None)
    monkeypatch.setattr(
        eia.session, "get", lambda *a, **k: _FakeResponse({"response": {}})
    )
    monkeypatch.setattr(time, "monotonic", lambda: 1000.0)
    sleeps: list[float] = []
    monkeypatch.setattr(time, "sleep", sleeps.append)

    eia._request("electricity", eia._params())
    eia._request("electricity", eia._params())
    assert sleeps == []


def test_default_session_sends_package_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A session the client creates sends the package User-Agent, not requests'."""
    eia = EIA(api_key="k", cache_dir="")
    sent: list[str | bytes] = []

    def _fake_send(prepared: requests.PreparedRequest, **_k: Any) -> _FakeResponse:
        sent.append(prepared.headers["User-Agent"])
        return _FakeResponse({"response": {}})

    # Stub ``send`` rather than ``get`` so requests still merges the session's
    # headers into the prepared request, exactly as it would on the wire.
    monkeypatch.setattr(eia.session, "send", _fake_send)
    eia._request("electricity", eia._params())
    assert sent == [eia_core._USER_AGENT]


def test_caller_session_headers_left_untouched() -> None:
    """A caller-supplied session is used as-is: its User-Agent is not replaced."""
    session = requests.Session()
    session.headers["User-Agent"] = "my-proxy-agent/2.0"
    before = dict(session.headers)
    eia = EIA(api_key="k", cache_dir="", session=session)
    assert eia.session is session
    assert dict(session.headers) == before


# --------------------------------------------------------------------------- #
# Async client
# --------------------------------------------------------------------------- #


def _async_client(monkeypatch: pytest.MonkeyPatch) -> eia_core.AsyncEIA:
    """An AsyncEIA whose ``_request`` is the offline synthetic API."""
    eia = eia_core.AsyncEIA(api_key="test-key", cache_dir="")

    async def _areq(endpoint: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        return _fake_request(endpoint, list(params))

    monkeypatch.setattr(eia, "_request", _areq)
    return eia


def test_async_api_alias() -> None:
    """``EIA.AsyncAPI`` is the ``AsyncEIA`` class."""
    assert EIA.AsyncAPI is eia_core.AsyncEIA


def test_async_owns_client_by_default() -> None:
    """A client created without an httpx client owns (and will close) one."""
    assert eia_core.AsyncEIA(api_key="k", cache_dir="")._owns_client is True


def test_async_aclose_without_client_is_noop() -> None:
    """Closing before any request was made is a harmless no-op."""
    asyncio.run(eia_core.AsyncEIA(api_key="k", cache_dir="").aclose())


def test_async_default_client_sends_package_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lazily-created httpx client sends the package User-Agent too."""
    eia = eia_core.AsyncEIA(api_key="k", cache_dir="")
    sent: list[str] = []

    async def _fake_send(
        self: httpx.AsyncClient, request: httpx.Request, **_k: Any
    ) -> httpx.Response:
        """Record the outgoing User-Agent and return an empty envelope."""
        sent.append(request.headers["User-Agent"])
        return httpx.Response(200, json={"response": {}}, request=request)

    # Patch the class's ``send`` because the client is only built inside
    # ``_request``; ``get`` still merges the client headers into the request.
    monkeypatch.setattr(httpx.AsyncClient, "send", _fake_send)

    async def _run() -> None:
        """Make one request, then close the client the request created."""
        await eia._request("electricity", eia._params())
        await eia.aclose()

    asyncio.run(_run())
    assert sent == [eia_core._USER_AGENT]


def test_async_browse(monkeypatch: pytest.MonkeyPatch) -> None:
    """Async browse returns the same child-route frame as the sync client."""
    eia = _async_client(monkeypatch)
    frame = asyncio.run(eia.browse())
    assert list(frame.columns) == ["id", "name", "description"]
    assert "electricity" in frame["id"].tolist()


def test_async_get_data_matches_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Async get_data returns the same tidy frame the sync path would."""
    eia = _async_client(monkeypatch)
    frame = asyncio.run(eia.get_data("electricity/retail-sales", data="price"))
    assert list(frame.columns)[:2] == ["date", "period"]
    assert frame["price"].dtype.kind == "f"
    assert len(frame) == 3


def test_async_data_columns_handles_listy_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Async data_columns tolerates empty-list column metadata (no crash)."""
    eia = _async_client(monkeypatch)
    frame = asyncio.run(eia.data_columns("petroleum/pri/spt"))
    assert frame["id"].tolist() == ["value"]


def test_async_get_data_default_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """Async get_data(data=None) resolves its columns from metadata."""
    eia = _async_client(monkeypatch)
    frame = asyncio.run(eia.get_data("petroleum/pri/spt"))
    assert "value" in frame.columns
    assert len(frame) == 2


def test_async_throttle_sleeps(monkeypatch: pytest.MonkeyPatch) -> None:
    """The async throttle awaits a sleep to honour requests_per_second."""
    eia = eia_core.AsyncEIA(api_key="k", cache_dir="", requests_per_second=10.0)
    clock = {"t": 1000.0}
    sleeps: list[float] = []
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock["t"] += seconds

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    async def _run() -> None:
        await eia._throttle()  # first: no wait
        await eia._throttle()  # immediate second: must wait

    asyncio.run(_run())
    assert sleeps == [pytest.approx(0.1)]


# --------------------------------------------------------------------------- #
# Export
# --------------------------------------------------------------------------- #


def test_to_csv(client: EIA, tmp_path: Path) -> None:
    """``to_csv`` writes the tidy extract to disk."""
    path = client.to_csv(
        tmp_path / "out.csv", route="electricity/retail-sales", data="price"
    )
    back = pd.read_csv(path)
    assert "price" in back.columns
    assert len(back) == 3


def test_to_excel(client: EIA, tmp_path: Path) -> None:
    """``to_excel`` writes an .xlsx with plain (time-free) dates."""
    path = client.to_excel(
        tmp_path / "out.xlsx", route="electricity/retail-sales", data="price"
    )
    back = pd.read_excel(path)
    assert "price" in back.columns
    first = back["date"].iloc[0]
    assert (first.hour, first.minute, first.second) == (0, 0, 0)


def test_to_parquet(client: EIA, tmp_path: Path) -> None:
    """``to_parquet`` writes a Parquet file that reads back intact."""
    path = client.to_parquet(
        tmp_path / "out.parquet", route="electricity/retail-sales", data="price"
    )
    back = pd.read_parquet(path)
    assert len(back) == 3


def test_to_json(client: EIA, tmp_path: Path) -> None:
    """``to_json`` writes a list of record objects."""
    path = client.to_json(
        tmp_path / "out.json", route="electricity/retail-sales", data="price"
    )
    back = pd.read_json(path)
    assert "price" in back.columns
    assert len(back) == 3


# --------------------------------------------------------------------------- #
# Incremental update
# --------------------------------------------------------------------------- #


def test_update_csv_first_run_writes_everything(client: EIA, tmp_path: Path) -> None:
    """The first run writes the full extract to a fresh file."""
    path = tmp_path / "prices.csv"
    new = client.update_csv(path, route="electricity/retail-sales", data="price")
    assert path.exists()
    assert len(new) == 3
    assert len(pd.read_csv(path)) == 3


def test_update_csv_appends_only_new(client: EIA, tmp_path: Path) -> None:
    """A later run appends only observations not already stored."""
    path = tmp_path / "prices.csv"
    seed = client.get_data("electricity/retail-sales", data="price").iloc[:1]
    seed.to_csv(path, index=False)

    new = client.update_csv(path, route="electricity/retail-sales", data="price")
    assert len(new) == 2  # the two later months
    assert len(pd.read_csv(path)) == 3


def test_update_csv_idempotent_second_run(client: EIA, tmp_path: Path) -> None:
    """Running twice appends nothing the second time."""
    path = tmp_path / "prices.csv"
    client.update_csv(path, route="electricity/retail-sales", data="price")
    new = client.update_csv(path, route="electricity/retail-sales", data="price")
    assert new.empty


def test_update_csv_overwrites_revision(client: EIA, tmp_path: Path) -> None:
    """A stale stored value is overwritten by the freshly fetched one."""
    path = tmp_path / "prices.csv"
    stale = pd.DataFrame(
        {
            "period": ["2020-01"],
            "stateid": ["CO"],
            "sectorid": ["RES"],
            "price": [999.0],
            "price-units": ["cents/kWh"],
        }
    )
    stale.to_csv(path, index=False)

    changed = client.update_csv(path, route="electricity/retail-sales", data="price")
    # The revised key is surfaced and the file now holds the true value.
    assert "2020-01" in changed["period"].astype(str).tolist()
    written = pd.read_csv(path)
    jan = written[(written["period"] == "2020-01") & (written["stateid"] == "CO")][
        "price"
    ]
    assert jan.item() == pytest.approx(11.0)
    assert len(jan) == 1


def test_new_observations_from_dataframe(client: EIA) -> None:
    """Only rows missing from the given DataFrame are returned."""
    full = client.get_data("electricity/retail-sales", data="price")
    have = full.iloc[:1]
    missing = client.new_observations(
        have, route="electricity/retail-sales", data="price"
    )
    assert len(missing) == 2


def test_new_observations_from_csv_path(client: EIA, tmp_path: Path) -> None:
    """A long CSV path is accepted as the 'already extracted' source."""
    path = tmp_path / "have.csv"
    client.get_data("electricity/retail-sales", data="price").iloc[:1].to_csv(
        path, index=False
    )
    missing = client.new_observations(
        path, route="electricity/retail-sales", data="price"
    )
    assert len(missing) == 2


def test_new_observations_up_to_date_is_empty(client: EIA) -> None:
    """When nothing is missing, an empty frame is returned."""
    full = client.get_data("electricity/retail-sales", data="price")
    assert client.new_observations(
        full, route="electricity/retail-sales", data="price"
    ).empty


def test_new_observations_excludes_revisions_when_disabled(client: EIA) -> None:
    """With include_revisions=False a changed value is not returned."""
    full = client.get_data("electricity/retail-sales", data="price")
    stale = full.copy()
    stale.loc[stale.index[0], "price"] = 99999.0
    missing = client.new_observations(
        stale, route="electricity/retail-sales", data="price", include_revisions=False
    )
    assert missing.empty
    assert not client.new_observations(
        stale, route="electricity/retail-sales", data="price"
    ).empty


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def test_cli_browse(cli: eia_core._Cli) -> None:
    """The browse subcommand prints the child routes."""
    assert "electricity" in fire.Fire(cli, command=["browse"])


def test_cli_facets(cli: eia_core._Cli) -> None:
    """The facets subcommand prints the filtering dimensions."""
    out = fire.Fire(cli, command=["facets", "electricity/retail-sales"])
    assert "stateid" in out


def test_cli_data_columns(cli: eia_core._Cli) -> None:
    """The data-columns subcommand prints the value columns."""
    out = fire.Fire(cli, command=["data_columns", "electricity/retail-sales"])
    assert "price" in out


def test_cli_facet_values(cli: eia_core._Cli) -> None:
    """The facet-values subcommand prints the allowed values."""
    out = fire.Fire(
        cli, command=["facet_values", "electricity/retail-sales", "stateid"]
    )
    assert "Colorado" in out


def test_cli_get_data_prints(cli: eia_core._Cli) -> None:
    """get_data prints a table containing the requested data."""
    out = fire.Fire(
        cli, command=["get_data", "electricity/retail-sales", "--data", "price"]
    )
    assert "price" in out


def test_cli_get_data_with_facets(cli: eia_core._Cli) -> None:
    """get_data accepts a JSON facets mapping on the command line."""
    out = fire.Fire(
        cli,
        command=[
            "get_data",
            "electricity/retail-sales",
            "--data",
            "price",
            "--facets",
            '{"stateid": "CO"}',
        ],
    )
    assert "CO" in out


class _Recorder:
    """Stand-in client that records the facets the CLI passes on."""

    def __init__(self, calls: list[object]) -> None:
        """Keep the list that each call's ``facets`` is appended to."""
        self.calls = calls

    def get_data(self, _route: str, **kwargs: object) -> pd.DataFrame:
        """Record the facets and return an empty frame."""
        self.calls.append(kwargs["facets"])
        return pd.DataFrame()

    def to_csv(self, _path: str, **kwargs: object) -> None:
        """Record the facets."""
        self.calls.append(kwargs["facets"])

    def update_csv(self, _path: str, **kwargs: object) -> pd.DataFrame:
        """Record the facets and report no changed rows."""
        self.calls.append(kwargs["facets"])
        return pd.DataFrame()


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        # A bare leading-zero code is not valid Python, so Fire used to pass
        # the whole argument on as a string instead of a mapping.
        (["--facets", "{generatorid: 01}"], {"generatorid": "01"}),
        (["--facets", "{generatorid: [01, 02]}"], {"generatorid": ["01", "02"]}),
        # An all-zero code used to become the int 0.
        (["--facets", "{generatorid: 00}"], {"generatorid": "00"}),
        # The --flag=value form.
        (["--facets={generatorid: 01}"], {"generatorid": "01"}),
        # Quoted values and plain numbers parse as before.
        (['--facets={"generatorid": "01"}'], {"generatorid": "01"}),
        (["--facets", "{sectorid: [1, 2]}"], {"sectorid": [1, 2]}),
        (["--facets", "{stateid: CO, plantid: 3}"], {"stateid": "CO", "plantid": 3}),
    ],
)
def test_cli_keeps_facet_codes_as_text(
    command: list[str], expected: dict[str, object]
) -> None:
    """Facet codes keep their leading zeros, whichever way they are given."""
    calls: list[object] = []
    cli = eia_core._Cli.__new__(eia_core._Cli)
    cli._eia = _Recorder(calls)  # type: ignore[assignment]
    eia_core._keep_codes_as_text()
    route = "electricity/operating-generator-capacity"
    fire.Fire(cli, command=["get-data", route, *command])
    # The flag before the positional route.
    fire.Fire(cli, command=["get-data", *command, route])
    assert calls == [expected, expected]


def test_cli_keeps_positional_and_writer_facet_codes(tmp_path: Path) -> None:
    """Positional facets and the writers' facets keep leading zeros too."""
    calls: list[object] = []
    cli = eia_core._Cli.__new__(eia_core._Cli)
    cli._eia = _Recorder(calls)  # type: ignore[assignment]
    eia_core._keep_codes_as_text()
    route = "electricity/operating-generator-capacity"
    # Positionally: route, data, facets.
    fire.Fire(cli, command=["get-data", route, "nameplate", "{generatorid: 01}"])
    out = str(tmp_path / "gen.csv")
    fire.Fire(cli, command=["to-csv", out, route, "--facets", "{generatorid: 01}"])
    fire.Fire(cli, command=["update-csv", out, route, "--facets={generatorid: 01}"])
    assert calls == [{"generatorid": "01"}] * 3


def test_every_command_with_facets_keeps_codes_as_text() -> None:
    """Each command taking ``facets`` gets the leading-zero-safe parser."""
    eia_core._keep_codes_as_text()
    commands = [
        "get_data",
        "to_csv",
        "to_excel",
        "to_parquet",
        "to_json",
        "update_csv",
    ]
    for name in commands:
        named = fire.decorators.GetParseFns(getattr(eia_core._Cli, name))["named"]
        assert named == {"facets": eia_core._facets_literal}
    assert not fire.decorators.GetParseFns(eia_core._Cli.facet_values)["named"]


def test_main_keeps_facet_codes_as_text(monkeypatch: pytest.MonkeyPatch) -> None:
    """The console entry point applies the facet handling itself."""
    calls: list[object] = []
    monkeypatch.setattr(eia_core, "EIA", lambda: _Recorder(calls))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "eia-api",
            "--log-level",
            "WARNING",
            "get-data",
            "--facets",
            "{generatorid: 01}",
            "electricity/operating-generator-capacity",
        ],
    )
    eia_core.main()
    assert calls == [{"generatorid": "01"}]


def test_cli_get_data_out_writes(cli: eia_core._Cli, tmp_path: Path) -> None:
    """get_data --out writes a CSV and reports the destination."""
    out = tmp_path / "cli.csv"
    msg = fire.Fire(
        cli,
        command=[
            "get_data",
            "electricity/retail-sales",
            "--data",
            "price",
            "--out",
            str(out),
        ],
    )
    assert out.exists()
    assert "Wrote" in msg


def test_cli_get_data_out_xlsx(cli: eia_core._Cli, tmp_path: Path) -> None:
    """get_data --out with an .xlsx name writes a real Excel workbook."""
    out = tmp_path / "cli.xlsx"
    fire.Fire(
        cli,
        command=[
            "get_data",
            "electricity/retail-sales",
            "--data",
            "price",
            "--out",
            str(out),
        ],
    )
    assert "price" in pd.read_excel(out).columns


def test_cli_to_csv(cli: eia_core._Cli, tmp_path: Path) -> None:
    """The to_csv subcommand writes the file and reports it."""
    out = tmp_path / "cli.csv"
    msg = fire.Fire(
        cli,
        command=[
            "to_csv",
            str(out),
            "--route",
            "electricity/retail-sales",
            "--data",
            "price",
        ],
    )
    assert out.exists()
    assert "Wrote" in msg


def test_cli_to_parquet(cli: eia_core._Cli, tmp_path: Path) -> None:
    """The to_parquet subcommand writes a Parquet file."""
    out = tmp_path / "cli.parquet"
    fire.Fire(
        cli,
        command=[
            "to_parquet",
            str(out),
            "--route",
            "electricity/retail-sales",
            "--data",
            "price",
        ],
    )
    assert "price" in pd.read_parquet(out).columns


def test_cli_to_json(cli: eia_core._Cli, tmp_path: Path) -> None:
    """The to_json subcommand writes JSON records."""
    out = tmp_path / "cli.json"
    fire.Fire(
        cli,
        command=[
            "to_json",
            str(out),
            "--route",
            "electricity/retail-sales",
            "--data",
            "price",
        ],
    )
    assert "price" in pd.read_json(out).columns


def test_cli_update_csv(cli: eia_core._Cli, tmp_path: Path) -> None:
    """The update_csv subcommand writes the file and reports the count."""
    out = tmp_path / "prices.csv"
    msg = fire.Fire(
        cli,
        command=[
            "update_csv",
            str(out),
            "--route",
            "electricity/retail-sales",
            "--data",
            "price",
        ],
    )
    assert out.exists()
    assert "3 new/revised rows" in msg


class _RecordingEIA:
    """Stand-in client that records the keyword arguments each method gets."""

    def __init__(self) -> None:
        """Start with no recorded calls."""
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __getattr__(self, name: str) -> Callable[..., pd.DataFrame]:
        """Return a recorder for any client method (``get_data``, ``to_csv``...)."""

        def _method(*_args: Any, **kwargs: Any) -> pd.DataFrame:
            self.calls.append((name, kwargs))
            return pd.DataFrame()

        return _method


@pytest.mark.parametrize(
    ("command", "method"),
    [
        (["get_data", _ROUTE], "get_data"),
        (["get_data", _ROUTE, "--out", "o.csv"], "to_csv"),
        (["get_data", _ROUTE, "--out", "o.xlsx"], "to_excel"),
        (["to_csv", "o.csv", _ROUTE], "to_csv"),
        (["to_excel", "o.xlsx", _ROUTE], "to_excel"),
        (["to_parquet", "o.parquet", _ROUTE], "to_parquet"),
        (["to_json", "o.json", _ROUTE], "to_json"),
        (["update_csv", "o.csv", _ROUTE], "update_csv"),
    ],
)
def test_cli_bare_number_bounds_reach_the_client_as_text(
    cli: eia_core._Cli,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: list[str],
    method: str,
) -> None:
    """Fire turns ``--start 2024`` into an int; the client still gets ``"2024"``."""
    monkeypatch.chdir(tmp_path)
    recorder = _RecordingEIA()
    monkeypatch.setattr(cli, "_eia", recorder)
    fire.Fire(cli, command=[*command, "--start", "2024", "--end", "202406"])
    [(name, kwargs)] = recorder.calls
    assert name == method
    assert kwargs["start"] == "2024"
    assert kwargs["end"] == "202406"


def test_cli_omitted_bounds_stay_none(
    cli: eia_core._Cli, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bounds left off the command line reach the client as ``None``."""
    recorder = _RecordingEIA()
    monkeypatch.setattr(cli, "_eia", recorder)
    fire.Fire(cli, command=["get_data", _ROUTE])
    [(_, kwargs)] = recorder.calls
    assert kwargs["start"] is None
    assert kwargs["end"] is None


def test_cli_bare_number_bounds_share_the_library_cache(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A CLI call with ``--start 2024`` is served from the library call's cache."""
    eia = EIA(api_key="k", cache_dir=tmp_path)
    calls = {"n": 0}

    def _fake_get(_url: str, params: list[tuple[str, str]], **_k: Any) -> _FakeResponse:
        calls["n"] += 1
        endpoint = _url.removeprefix(eia_core._BASE_URL).strip("/")
        return _FakeResponse({"response": _fake_request(endpoint, params)})

    monkeypatch.setattr(eia.session, "get", _fake_get)
    eia.get_data(_ROUTE, data="price", start="2024")
    after_library = calls["n"]
    facade = eia_core._Cli.__new__(eia_core._Cli)
    facade._eia = eia
    fire.Fire(
        facade, command=["get_data", _ROUTE, "--data", "price", "--start", "2024"]
    )
    assert calls["n"] == after_library


def test_cli_out_defaults_to_outputs_folder(
    cli: eia_core._Cli, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare output filename is written into the outputs/ folder."""
    monkeypatch.chdir(tmp_path)
    msg = fire.Fire(
        cli,
        command=[
            "to_csv",
            "prices.csv",
            "--route",
            "electricity/retail-sales",
            "--data",
            "price",
        ],
    )
    assert (tmp_path / "outputs" / "prices.csv").exists()
    assert "outputs" in msg


def test_cli_reports_errors_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An expected error exits 1 with a concise message, not a traceback."""
    monkeypatch.setattr(sys, "argv", ["eia-api", "browse", "electricity"])
    # No API key configured anywhere -> ValueError from _params, caught by main.
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    with pytest.raises(SystemExit) as excinfo:
        eia_core.main()
    assert excinfo.value.code == 1
    assert "Error: No EIA API key" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _reset_package_logger() -> Iterator[None]:
    """Undo any CLI logging setup a test made, so tests stay independent."""
    yield
    log = logging.getLogger("eia_api")
    for handler in list(log.handlers):
        if getattr(handler, eia_core._CLI_HANDLER_FLAG, False):
            log.removeHandler(handler)
            handler.close()
    log.setLevel(logging.NOTSET)


def test_request_logs_without_the_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Request, cache and status lines are logged, and the key never is."""
    canary = "super-canary-key-123"
    eia = EIA(api_key=canary, cache_dir="", requests_per_second=None)
    monkeypatch.setattr(
        eia.session, "get", lambda *a, **k: _FakeResponse({"response": {}})
    )
    with caplog.at_level(logging.DEBUG, logger="eia_api"):
        eia.metadata("electricity")
    assert "GET electricity" in caplog.text
    assert "HTTP 200 for electricity" in caplog.text
    assert canary not in caplog.text


def test_cache_hit_is_logged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A response served from the disk cache says so at DEBUG."""
    eia = EIA(api_key="k", cache_dir=tmp_path, requests_per_second=None)
    monkeypatch.setattr(
        eia.session, "get", lambda *a, **k: _FakeResponse({"response": {}})
    )
    eia._request("electricity", eia._params())
    with caplog.at_level(logging.DEBUG, logger="eia_api"):
        eia._request("electricity", eia._params())
    assert "Cache hit: electricity" in caplog.text


@pytest.mark.parametrize(
    "logger_name", ["urllib3.connectionpool", "urllib3.util.retry", "httpx"]
)
def test_http_library_logs_are_redacted(
    logger_name: str, caplog: pytest.LogCaptureFixture
) -> None:
    """URLs that urllib3/httpx log with the key in the query come out masked."""
    canary = "another-canary-456"
    EIA(api_key=canary, cache_dir="")
    with caplog.at_level(logging.DEBUG, logger=logger_name):
        logging.getLogger(logger_name).info(
            "GET https://api.eia.gov/v2/?api_key=%s", canary
        )
    assert canary not in caplog.text
    assert "api_key=***" in caplog.text


def test_get_data_logs_summary(client: EIA, caplog: pytest.LogCaptureFixture) -> None:
    """get_data reports rows and pages at INFO, each page at DEBUG."""
    with caplog.at_level(logging.DEBUG, logger="eia_api"):
        client.get_data("electricity/retail-sales", data="price", length=2)
    assert "Fetched 3 rows from electricity/retail-sales in 2 page(s)" in caplog.text
    assert "page 2, 1 rows" in caplog.text


def test_async_get_data_logs_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The async client logs the same INFO summary as the sync one."""
    aeia = _async_client(monkeypatch)
    with caplog.at_level(logging.INFO, logger="eia_api"):
        asyncio.run(aeia.get_data("electricity/retail-sales", data="price"))
    assert "Fetched 3 rows from electricity/retail-sales" in caplog.text


def test_update_csv_logs_each_outcome(
    client: EIA, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """update_csv logs creating, then finding nothing new."""
    path = tmp_path / "prices.csv"
    with caplog.at_level(logging.INFO, logger="eia_api"):
        client.update_csv(path, route="electricity/retail-sales", data="price")
        client.update_csv(path, route="electricity/retail-sales", data="price")
    assert f"Created {path} with 3 rows" in caplog.text
    assert f"{path} is already up to date" in caplog.text


def test_configure_logging_rejects_unknown_level() -> None:
    """A misspelled level is a clear error, not a silent default."""
    with pytest.raises(ValueError, match="log_level must be one of"):
        eia_core._configure_logging("loud")


def test_configure_logging_does_not_stack_handlers() -> None:
    """Reconfiguring replaces the CLI handlers instead of adding more."""
    eia_core._configure_logging("INFO")
    eia_core._configure_logging("DEBUG")
    log = logging.getLogger("eia_api")
    ours = [h for h in log.handlers if getattr(h, eia_core._CLI_HANDLER_FLAG, False)]
    assert len(ours) == 1
    assert log.level == logging.DEBUG


def test_configure_logging_console_goes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Console log lines go to stderr so stdout stays pipeable."""
    eia_core._configure_logging("INFO")
    logging.getLogger("eia_api").info("hello from the log")
    captured = capsys.readouterr()
    assert "hello from the log" in captured.err
    assert "hello from the log" not in captured.out


def test_configure_logging_file_gets_debug(tmp_path: Path) -> None:
    """The log file records DEBUG lines even when the console is quieter."""
    log_file = tmp_path / "run.log"
    eia_core._configure_logging("WARNING", str(log_file))
    logging.getLogger("eia_api").debug("detail for the file")
    text = log_file.read_text(encoding="utf-8")
    assert "eia_api - DEBUG - detail for the file" in text


def test_log_file_traceback_has_no_color_codes(tmp_path: Path) -> None:
    """A traceback colored for the console is written plain to the log file."""
    log_file = tmp_path / "run.log"
    eia_core._configure_logging("DEBUG", str(log_file))
    for handler in logging.getLogger("eia_api").handlers:
        formatter = getattr(handler, "formatter", None)
        if formatter is not None and hasattr(formatter, "force_color"):
            formatter.force_color = True  # color the console even off a tty
    with pytest.raises(ValueError, match="log_level") as info:
        eia_core._configure_logging("loud")  # fails before touching handlers
    logging.getLogger("eia_api").debug("failed", exc_info=info.value)
    text = log_file.read_text(encoding="utf-8")
    assert "ValueError: log_level must be one of" in text
    assert "\x1b[" not in text


@pytest.mark.parametrize(
    "command",
    [
        ["--log-level", "info", "browse"],
        ["browse", "--log-level", "info"],
    ],
)
def test_cli_log_level_flag(
    command: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--log-level works before or after the subcommand."""
    monkeypatch.setattr(EIA, "_request", lambda _self, e, p: _fake_request(e, p))
    monkeypatch.setenv("EIA_API_KEY", "k")
    out = fire.Fire(eia_core._Cli, command=command)
    assert "electricity" in out
    assert logging.getLogger("eia_api").level == logging.INFO
    # Log lines (none at INFO for browse) never land on stdout.
    assert "INFO" not in capsys.readouterr().out


def test_redaction_masks_registered_key_outside_urls(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A registered key is masked even when it is not in a query string."""
    canary = "plain-text-canary-789"
    EIA(api_key=canary, cache_dir="")
    with caplog.at_level(logging.DEBUG, logger="eia_api"):
        logging.getLogger("eia_api").debug("oops, key was %s", canary)
    assert canary not in caplog.text
    assert "oops, key was ***" in caplog.text


def test_short_keys_are_not_registered() -> None:
    """A tiny key is not masked as plain text, which would mangle other words."""
    EIA(api_key="k", cache_dir="")
    assert "k" not in eia_core._REDACT_FILTER.secrets


def test_main_writes_traceback_to_log_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """On an expected error the log file keeps the full traceback."""
    log_file = tmp_path / "eia.log"
    monkeypatch.setattr(sys, "argv", ["eia-api", "--log-file", str(log_file), "browse"])
    monkeypatch.delenv("EIA_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        eia_core.main()
    text = log_file.read_text(encoding="utf-8")
    assert "Command failed" in text
    assert "Traceback" in text


# --------------------------------------------------------------------------- #
# Packaging metadata
# --------------------------------------------------------------------------- #


def _file_version(path: Path, pattern: str) -> str:
    """Return the first version captured by ``pattern`` in ``path``."""
    import re

    match = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None, f"no version found in {path}"
    return match.group(1)


def test_pyproject_and_citation_versions_agree() -> None:
    """The pyproject.toml and CITATION.cff versions must stay in lockstep."""
    root = Path(__file__).resolve().parents[1]
    pyproject = _file_version(root / "pyproject.toml", r'^version = "([^"]+)"')
    citation = _file_version(root / "CITATION.cff", r'^version:\s*"?([^"\s]+)"?')
    assert pyproject == citation


# --------------------------------------------------------------------------- #
# Tidy output, polars, incremental updates in the tidy shape
# --------------------------------------------------------------------------- #

_MULTI = "electricity/state-profiles"

# Two measures per row, facet label columns after each id, a measure with no
# "-units" companion ("customers") and a non-numeric cell ("(s)"). Rows are
# deliberately out of order so the tidy sort is exercised.
_MULTI_ROWS: list[dict[str, Any]] = [
    {
        "period": "2021",
        "stateid": "TX",
        "stateDescription": "Texas",
        "sectorid": "RES",
        "sectorName": "residential",
        "price": "12.5",
        "price-units": "cents/kWh",
        "sales": "(s)",
        "sales-units": "MWh",
        "customers": "100",
    },
    {
        "period": "2020",
        "stateid": "TX",
        "stateDescription": "Texas",
        "sectorid": "RES",
        "sectorName": "residential",
        "price": "12.0",
        "price-units": "cents/kWh",
        "sales": "5.0",
        "sales-units": "MWh",
        "customers": "90",
    },
    {
        "period": "2020",
        "stateid": "CO",
        "stateDescription": "Colorado",
        "sectorid": "RES",
        "sectorName": "residential",
        "price": "11.0",
        "price-units": "cents/kWh",
        "sales": "3.0",
        "sales-units": "MWh",
        "customers": "50",
    },
]

# Numeric-looking facet ids: a CSV round-trip would turn "3" into 3 and "01"
# into 1, so the incremental helpers must compare keys as strings.
_PLANT_ROUTE = "electricity/facility-fuel"
_PLANT_ROWS: list[dict[str, Any]] = [
    {"period": "2020-01", "plantid": "3", "gen": "1.5", "gen-units": "MWh"},
    {"period": "2020-01", "plantid": "01", "gen": "2.5", "gen-units": "MWh"},
]

# Value columns of the extra synthetic routes (served as their metadata).
_EXTRA_MEASURES = {"price", "sales", "customers", "gen"}


def _tidy_request(
    rows: dict[str, list[dict[str, Any]]],
) -> Callable[[str, list[tuple[str, str]]], dict[str, Any]]:
    """Extend the synthetic API with extra data routes serving ``rows``."""

    def _request(endpoint: str, params: list[tuple[str, str]]) -> dict[str, Any]:
        route = endpoint.removesuffix("/data")
        if route in rows and endpoint.endswith("/data"):
            data = rows[route]
            return {"total": str(len(data)), "data": list(data)}
        if route in rows:
            columns = {c for r in rows[route] for c in r} & _EXTRA_MEASURES
            return {"routes": [], "data": {c: {"alias": c} for c in sorted(columns)}}
        return _fake_request(endpoint, params)

    return _request


@pytest.fixture
def tclient(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EIA:
    """A client whose synthetic API also serves the multi-measure routes."""
    eia = EIA(api_key="test-key", cache_dir=tmp_path / "cache")
    rows = {_MULTI: _MULTI_ROWS, _PLANT_ROUTE: _PLANT_ROWS}
    monkeypatch.setattr(eia, "_request", _tidy_request(rows))
    return eia


def _assert_tidy_dtypes(frame: pd.DataFrame, dims: list[str]) -> None:
    """Check the exact tidy columns, their order and dtypes."""
    assert list(frame.columns) == ["date", *dims, "value", "unit"]
    assert frame["date"].dtype == "datetime64[ns]"
    for col in dims:
        assert frame[col].dtype == "string", col
    assert frame["value"].dtype == "float64"
    assert frame["unit"].dtype == "string"


def test_snake_case_names() -> None:
    """API column names become snake_case dimension names."""
    assert eia_core._snake("stateDescription") == "state_description"
    assert eia_core._snake("area-name") == "area_name"
    assert eia_core._snake("stateid") == "stateid"


def test_get_data_default_shape_unchanged_by_new_options(client: EIA) -> None:
    """Without the new flags the result keeps the raw API columns."""
    frame = client.get_data(_ROUTE, data="price")
    assert list(frame.columns) == [
        "date",
        "period",
        "stateid",
        "sectorid",
        "price",
        "price-units",
    ]


def test_get_data_tidy_single_measure(client: EIA) -> None:
    """A single-measure query still carries the ``variable`` dimension."""
    frame = client.get_data(_ROUTE, data="price", tidy=True)
    _assert_tidy_dtypes(frame, ["stateid", "sectorid", "variable"])
    assert frame["variable"].unique().tolist() == ["price"]
    assert frame["value"].tolist() == [11.0, 12.0, 13.0]
    assert frame["unit"].unique().tolist() == ["cents/kWh"]
    assert frame["date"].iloc[0] == pd.Timestamp("2020-01-01")


def test_get_data_tidy_multi_measure(tclient: EIA) -> None:
    """Measures melt into ``variable``; labels become ``<facet>_name``."""
    frame = tclient.get_data(_MULTI, data=["price", "sales", "customers"], tidy=True)
    dims = ["stateid", "stateid_name", "sectorid", "sectorid_name", "variable"]
    _assert_tidy_dtypes(frame, dims)
    assert len(frame) == 3 * len(_MULTI_ROWS)
    # Sorted by the dimensions, then date, with a fresh index.
    expected = frame.sort_values([*dims, "date"]).reset_index(drop=True)
    pd.testing.assert_frame_equal(frame, expected)
    assert frame.index.tolist() == list(range(len(frame)))
    assert frame["stateid"].iloc[0] == "CO"
    assert frame["stateid_name"].iloc[0] == "Colorado"
    # Each measure takes its unit from its own "-units" column ...
    units = frame.groupby("variable")["unit"].first()
    assert units["price"] == "cents/kWh"
    assert units["sales"] == "MWh"
    # ... and a measure without one gets <NA>.
    assert frame.loc[frame["variable"] == "customers", "unit"].isna().all()
    # A non-numeric cell becomes NaN, and its row is kept.
    sales_tx = frame[(frame["variable"] == "sales") & (frame["stateid"] == "TX")]
    assert sales_tx["value"].isna().tolist() == [False, True]


def test_to_tidy_uses_lone_units_column() -> None:
    """Routes with a single ``units`` column take the unit from it."""
    raw = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-02"]),
            "period": ["2020-01-02"],
            "series": ["RWTC"],
            "value": [61.0],
            "units": ["$/bbl"],
        }
    )
    tidy = eia_core._to_tidy(raw, ["value"])
    assert list(tidy.columns) == ["date", "series", "variable", "value", "unit"]
    assert tidy["unit"].tolist() == ["$/bbl"]


def test_to_tidy_blank_unit_is_na() -> None:
    """An empty unit string is reported as missing."""
    raw = pd.DataFrame(
        {
            "date": pd.to_datetime(["2020-01-01"]),
            "x": ["a"],
            "v": ["1"],
            "v-units": [""],
        }
    )
    assert eia_core._to_tidy(raw, ["v"])["unit"].isna().all()


def test_get_data_tidy_empty(client: EIA, monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty result still has the tidy columns and dtypes."""
    monkeypatch.setattr(client, "_request", lambda *_a: {"total": "0", "data": []})
    frame = client.get_data(_ROUTE, data="price", tidy=True)
    assert frame.empty
    _assert_tidy_dtypes(frame, ["variable"])


def test_get_data_as_polars(tclient: EIA) -> None:
    """``as_polars`` returns a polars frame with the same columns."""
    pl = pytest.importorskip("polars")
    pandas_frame = tclient.get_data(_MULTI, data=["price", "sales"], tidy=True)
    polars_frame = tclient.get_data(
        _MULTI, data=["price", "sales"], tidy=True, as_polars=True
    )
    assert isinstance(polars_frame, pl.DataFrame)
    assert polars_frame.columns == list(pandas_frame.columns)
    assert polars_frame.height == len(pandas_frame)
    default = tclient.get_data(_MULTI, data="price", as_polars=True)
    assert isinstance(default, pl.DataFrame)
    assert "price-units" in default.columns


def test_as_polars_missing_dependency(
    client: EIA, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without polars installed, ``as_polars`` explains how to get it."""
    # A None entry in sys.modules makes ``import polars`` raise ImportError.
    monkeypatch.setitem(sys.modules, "polars", None)
    with pytest.raises(ImportError, match=r'pip install "eia-api\[polars\]"'):
        client.get_data(_ROUTE, data="price", as_polars=True)


def test_async_get_data_tidy_matches_sync(
    client: EIA, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The async client returns the same tidy frame as the sync client."""
    eia = _async_client(monkeypatch)
    got = asyncio.run(eia.get_data(_ROUTE, data="price", tidy=True))
    want = client.get_data(_ROUTE, data="price", tidy=True)
    pd.testing.assert_frame_equal(got, want)


def test_async_get_data_as_polars(monkeypatch: pytest.MonkeyPatch) -> None:
    """The async client honours ``as_polars`` too."""
    pl = pytest.importorskip("polars")
    eia = _async_client(monkeypatch)
    got = asyncio.run(eia.get_data(_ROUTE, data="price", tidy=True, as_polars=True))
    assert isinstance(got, pl.DataFrame)
    assert got.columns == ["date", "stateid", "sectorid", "variable", "value", "unit"]


def test_to_parquet_tidy_round_trip(tclient: EIA, tmp_path: Path) -> None:
    """A tidy Parquet extract reads back equal to the frame."""
    kwargs: dict[str, Any] = {"route": _MULTI, "data": ["price", "sales"], "tidy": True}
    path = tclient.to_parquet(tmp_path / "tidy.parquet", **kwargs)
    pd.testing.assert_frame_equal(pd.read_parquet(path), tclient.get_data(**kwargs))


def test_to_parquet_default_round_trip(client: EIA, tmp_path: Path) -> None:
    """A default-shape Parquet extract reads back equal to the frame."""
    path = client.to_parquet(tmp_path / "d.parquet", route=_ROUTE, data="price")
    pd.testing.assert_frame_equal(
        pd.read_parquet(path), client.get_data(_ROUTE, data="price")
    )


def test_update_csv_tidy_first_run_and_idempotent(tclient: EIA, tmp_path: Path) -> None:
    """Tidy update_csv writes everything once, then nothing."""
    path = tmp_path / "tidy.csv"
    first = tclient.update_csv(path, _MULTI, data=["price", "sales"], tidy=True)
    assert len(first) == 6
    assert list(pd.read_csv(path).columns) == list(first.columns)
    before = path.read_bytes()
    again = tclient.update_csv(path, _MULTI, data=["price", "sales"], tidy=True)
    assert again.empty
    assert path.read_bytes() == before  # untouched


def test_update_csv_tidy_appends_only_new(tclient: EIA, tmp_path: Path) -> None:
    """Only rows missing from the tidy CSV are added."""
    path = tmp_path / "tidy.csv"
    full = tclient.get_data(_MULTI, data=["price", "sales"], tidy=True)
    full.iloc[:4].to_csv(path, index=False)
    new = tclient.update_csv(path, _MULTI, data=["price", "sales"], tidy=True)
    assert len(new) == 2
    written = pd.read_csv(path)
    assert len(written) == 6
    # The file comes back sorted by dimensions then date.
    assert written["date"].tolist() == full["date"].dt.strftime("%Y-%m-%d").tolist()


def test_update_csv_tidy_overwrites_revision(tclient: EIA, tmp_path: Path) -> None:
    """A revised tidy value overwrites the stored one; history is kept."""
    path = tmp_path / "tidy.csv"
    tclient.update_csv(path, _MULTI, data="price", tidy=True)
    stored = pd.read_csv(path)
    stored.loc[0, "value"] = 999.0
    # A row the fresh pull no longer has must survive the upsert.
    old = stored.iloc[[0]].assign(date="2019-01-01", value=1.0)
    pd.concat([stored, old]).to_csv(path, index=False)

    changed = tclient.update_csv(path, _MULTI, data="price", tidy=True)
    assert len(changed) == 1
    assert changed["value"].item() == pytest.approx(11.0)
    written = pd.read_csv(path)
    assert len(written) == 4
    co = written[written["stateid"] == "CO"].reset_index(drop=True)
    assert co["date"].tolist() == ["2019-01-01", "2020-01-01"]
    assert co["value"].tolist() == [1.0, 11.0]


def test_new_observations_tidy(tclient: EIA) -> None:
    """new_observations works on a tidy in-memory extract."""
    full = tclient.get_data(_MULTI, data="price", tidy=True)
    missing = tclient.new_observations(full.iloc[1:], _MULTI, data="price", tidy=True)
    pd.testing.assert_frame_equal(missing, full.iloc[:1].reset_index(drop=True))
    assert tclient.new_observations(full, _MULTI, data="price", tidy=True).empty


def test_new_observations_nan_value_is_not_a_revision(tclient: EIA) -> None:
    """A stored NaN that is still NaN is neither new nor revised."""
    full = tclient.get_data(_MULTI, data="sales", tidy=True)
    assert full["value"].isna().any()
    assert tclient.new_observations(full, _MULTI, data="sales", tidy=True).empty


@pytest.mark.parametrize("tidy", [False, True])
def test_update_csv_numeric_looking_keys_round_trip(
    tclient: EIA, tmp_path: Path, *, tidy: bool
) -> None:
    """Facet ids like "01" survive the CSV round-trip; a rerun adds nothing."""
    path = tmp_path / "plants.csv"
    tclient.update_csv(path, _PLANT_ROUTE, data="gen", tidy=tidy)
    assert tclient.update_csv(path, _PLANT_ROUTE, data="gen", tidy=tidy).empty
    assert tclient.new_observations(path, _PLANT_ROUTE, data="gen", tidy=tidy).empty


def test_cli_get_data_tidy(cli: eia_core._Cli) -> None:
    """``get-data --tidy`` prints the tidy columns."""
    text = fire.Fire(cli, command=["get_data", _ROUTE, "--data", "price", "--tidy"])
    header = text.splitlines()[0].split()
    assert header == ["date", "stateid", "sectorid", "variable", "value", "unit"]


def test_cli_to_parquet_tidy(cli: eia_core._Cli, tmp_path: Path) -> None:
    """``to-parquet --tidy`` writes the tidy shape."""
    out = tmp_path / "t.parquet"
    command = ["to-parquet", str(out), "--route", _ROUTE, "--data", "price", "--tidy"]
    fire.Fire(cli, command=command)
    assert list(pd.read_parquet(out).columns)[-2:] == ["value", "unit"]


def test_cli_get_data_out_tidy(cli: eia_core._Cli, tmp_path: Path) -> None:
    """``get-data --out x.csv --tidy`` writes the tidy shape."""
    out = tmp_path / "t.csv"
    command = ["get-data", _ROUTE, "--data", "price", "--out", str(out), "--tidy"]
    fire.Fire(cli, command=command)
    assert "variable" in pd.read_csv(out).columns


def test_cli_update_csv_tidy(cli: eia_core._Cli, tmp_path: Path) -> None:
    """``update-csv --tidy`` creates a tidy CSV, then reports nothing new."""
    out = tmp_path / "t.csv"
    command = ["update-csv", str(out), "--route", _ROUTE, "--data", "price", "--tidy"]
    assert "3 new/revised rows" in fire.Fire(cli, command=command)
    assert "unit" in pd.read_csv(out).columns
    assert "0 new/revised rows" in fire.Fire(cli, command=command)


# --------------------------------------------------------------------------- #
# cache_dir: DEFAULT_CACHE_DIR (the default) versus None (no cache)
# --------------------------------------------------------------------------- #

_CLIENT_CLASSES = [EIA, eia_core.AsyncEIA]
_ClientClass = type[EIA] | type[eia_core.AsyncEIA]


def test_default_cache_dir_is_exported() -> None:
    """The sentinel is public, and it is the constructors' default."""
    import eia_api

    assert eia_api.DEFAULT_CACHE_DIR is eia_core.DEFAULT_CACHE_DIR
    assert "DEFAULT_CACHE_DIR" in eia_api.__all__
    assert repr(eia_api.DEFAULT_CACHE_DIR) == "DEFAULT_CACHE_DIR"


@pytest.mark.parametrize("cls", _CLIENT_CLASSES)
def test_no_cache_dir_argument_uses_default_dir(cls: _ClientClass, home: Path) -> None:
    """Omitting ``cache_dir`` caches under ``~/.cache/eia_api`` (patched home)."""
    client = cls(api_key="k")
    assert client.cache_dir == home / ".cache" / "eia_api"
    assert client.cache_dir.is_dir()


@pytest.mark.parametrize("cls", _CLIENT_CLASSES)
def test_default_cache_dir_sentinel_uses_default_dir(
    cls: _ClientClass, home: Path
) -> None:
    """Passing ``DEFAULT_CACHE_DIR`` explicitly is the same as omitting it."""
    client = cls(api_key="k", cache_dir=eia_core.DEFAULT_CACHE_DIR)
    assert client.cache_dir == home / ".cache" / "eia_api"


def test_default_cache_dir_resolved_per_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The home directory is looked up when a client is built, not at import."""
    for name in ("first", "second"):
        monkeypatch.setattr(Path, "home", lambda name=name: tmp_path / name)
        assert EIA(api_key="k").cache_dir == tmp_path / name / ".cache" / "eia_api"


@pytest.mark.parametrize("cls", _CLIENT_CLASSES)
@pytest.mark.parametrize("off", [None, ""])
def test_none_or_empty_cache_dir_disables_cache(
    cls: _ClientClass, off: str | None, home: Path
) -> None:
    """``None`` (and, as before, ``""``) turns the cache off: no dir is created."""
    assert cls(api_key="k", cache_dir=off).cache_dir is None
    assert not any(home.iterdir())


@pytest.mark.parametrize("cls", _CLIENT_CLASSES)
def test_explicit_cache_dir_is_used(
    cls: _ClientClass, tmp_path: Path, home: Path
) -> None:
    """A path (``str`` or ``Path``) is used as given and created."""
    target = tmp_path / "explicit"
    assert cls(api_key="k", cache_dir=target).cache_dir == target
    assert cls(api_key="k", cache_dir=str(target)).cache_dir == target
    assert target.is_dir()
    assert not any(home.iterdir())


_PAYLOAD = {"response": {"id": "electricity", "routes": []}}


def _sync_fetch_twice(monkeypatch: pytest.MonkeyPatch, eia: EIA) -> int:
    """Make the same request twice through a stub session; return HTTP calls."""
    calls: list[int] = []

    def _fake_get(*_a: Any, **_k: Any) -> _FakeResponse:
        """Count the call and return the canned envelope."""
        calls.append(1)
        return _FakeResponse(_PAYLOAD)

    monkeypatch.setattr(eia.session, "get", _fake_get)
    for _ in range(2):
        assert eia._request("electricity", eia._params()) == _PAYLOAD["response"]
    return len(calls)


def _async_fetch_twice(monkeypatch: pytest.MonkeyPatch, eia: eia_core.AsyncEIA) -> int:
    """Make the same async request twice through a stub transport; count sends."""
    calls: list[int] = []

    async def _fake_send(
        _self: httpx.AsyncClient, request: httpx.Request, **_k: Any
    ) -> httpx.Response:
        """Count the call and return the canned envelope."""
        calls.append(1)
        return httpx.Response(200, json=_PAYLOAD, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", _fake_send)

    async def _run() -> None:
        """Request twice, then close the lazily-created client."""
        for _ in range(2):
            assert (
                await eia._request("electricity", eia._params()) == _PAYLOAD["response"]
            )
        await eia.aclose()

    asyncio.run(_run())
    return len(calls)


def test_sync_cache_dir_none_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """With ``cache_dir=None`` every call hits the network and nothing is written."""
    eia = EIA(api_key="k", cache_dir=None, requests_per_second=None)
    assert _sync_fetch_twice(monkeypatch, eia) == 2
    assert not any(home.rglob("*"))


def test_sync_default_cache_writes_under_home(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """By default the response is cached in the default dir and reused."""
    eia = EIA(api_key="k", requests_per_second=None)
    assert _sync_fetch_twice(monkeypatch, eia) == 1
    assert len(list((home / ".cache" / "eia_api").glob("*.json"))) == 1


def test_async_cache_dir_none_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """The async client with ``cache_dir=None`` writes no cache either."""
    eia = eia_core.AsyncEIA(api_key="k", cache_dir=None, requests_per_second=None)
    assert _async_fetch_twice(monkeypatch, eia) == 2
    assert not any(home.rglob("*"))


def test_async_default_cache_writes_under_home(
    monkeypatch: pytest.MonkeyPatch, home: Path
) -> None:
    """The async client caches in the default dir by default."""
    eia = eia_core.AsyncEIA(api_key="k", requests_per_second=None)
    assert _async_fetch_twice(monkeypatch, eia) == 1
    assert len(list((home / ".cache" / "eia_api").glob("*.json"))) == 1


@pytest.mark.parametrize(
    ("command", "cached"),
    [
        (["browse"], True),
        (["--no-cache", "browse"], False),
        (["--no_cache", "browse"], False),
        (["browse", "--no-cache"], False),
        (["--log-level", "INFO", "--no-cache", "browse"], False),
        (["--no-cache", "--log-level", "INFO", "browse"], False),
    ],
)
def test_cli_caches_by_default_and_no_cache_disables(
    command: list[str],
    cached: bool,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    home: Path,
) -> None:
    """The CLI caches in the default dir unless ``--no-cache`` (anywhere) is given."""
    monkeypatch.setenv("EIA_API_KEY", "k")
    built: list[EIA] = []

    class _Recording(EIA):
        """An :class:`EIA` that records each instance the CLI builds."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            """Build the client as usual, then remember it."""
            super().__init__(*args, **kwargs)
            built.append(self)

    monkeypatch.setattr(eia_core, "EIA", _Recording)
    monkeypatch.setattr(
        EIA, "_request", lambda _self, endpoint, params: _fake_request(endpoint, params)
    )
    monkeypatch.setattr(sys, "argv", ["eia-api", *command])
    eia_core.main()
    assert "electricity" in capsys.readouterr().out
    (client,) = built
    default = home / ".cache" / "eia_api"
    assert client.cache_dir == (default if cached else None)
    assert default.is_dir() is cached


def test_bind_no_cache_flag_leaves_fire_flags_alone() -> None:
    """Only bare ``--no-cache`` before ``--`` is rewritten."""
    argv = ["--no-cache", "browse", "--", "--no-cache"]
    assert eia_core._bind_no_cache_flag(argv) == [
        "--no_cache=True",
        "browse",
        "--",
        "--no-cache",
    ]
