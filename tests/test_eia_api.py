"""Unit tests for :mod:`eia_api`.

The tests never touch the network: a synthetic in-memory API that mimics the
real EIA route/metadata/data envelope is built once and fed to the client either
by monkeypatching :meth:`EIA._request` (the single HTTP choke point) or by
stubbing the ``requests.Session`` used underneath it.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from typing import Any

import fire
import pandas as pd
import pytest

import eia_api.core as eia_core
from eia_api.core import EIA, Route

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

    def __init__(self, payload: dict[str, Any]) -> None:
        """Store the canned JSON ``payload``."""
        self._payload = payload

    def json(self) -> dict[str, Any]:
        """Return the canned payload."""
        return self._payload

    def raise_for_status(self) -> None:
        """No-op; canned responses are always considered successful."""


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EIA:
    """Return a client whose HTTP layer is replaced by the synthetic API."""
    eia = EIA(api_key="test-key", cache_dir=tmp_path)
    monkeypatch.setattr(eia, "_request", _fake_request)
    return eia


@pytest.fixture
def cli(client: EIA) -> eia_core._Cli:
    """The Fire CLI facade, bound to the mocked client."""
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


def test_params_without_key_raises() -> None:
    """A missing API key raises a helpful error before any request."""
    eia = EIA(api_key="", cache_dir=None)
    with pytest.raises(ValueError, match="No EIA API key"):
        eia._params()


def test_api_key_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client falls back to the EIA_API_KEY environment variable."""
    monkeypatch.setenv("EIA_API_KEY", "from-env")
    assert EIA(cache_dir=None).api_key == "from-env"


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
    eia = EIA(api_key="bad", cache_dir="")  # "" disables cache; None = default dir
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
