"""
Client and helpers for the U.S. Energy Information Administration (EIA) API v2.

The EIA publishes energy statistics — electricity, petroleum, natural gas, coal,
nuclear, renewables and more — behind a single versioned REST API rooted at
``https://api.eia.gov/v2/``. Unlike a flat "series" API, v2 is organised as a
*tree of routes*: parent routes list child routes, and a leaf dataset exposes a
``/data`` endpoint plus metadata describing its available data columns, facets
(filtering dimensions) and frequencies. Every request carries an ``api_key`` and
the ``/data`` endpoint caps a single response at 5000 rows, so real extracts must
be paginated with ``offset``/``length``.

This module hides all of that behind a small, typed client. Concretely,
:class:`EIA`:

  * authenticates with an API key taken from the constructor or the
    ``EIA_API_KEY`` environment variable,
  * lets you walk the route tree (:meth:`browse`) and inspect a dataset's
    frequencies, facets and data columns (:meth:`metadata`, :meth:`frequencies`,
    :meth:`facets`, :meth:`facet_values`, :meth:`data_columns`),
  * fetches a dataset's rows into a tidy ``pandas`` DataFrame
    (:meth:`get_data`), transparently paginating past the 5000-row limit,
    encoding facet filters, coercing value columns to numbers and adding a parsed
    ``date`` column,
  * exports straight to CSV/Excel/Parquet/JSON (:meth:`to_csv` and friends) and
    keeps a long CSV current with incremental upserts (:meth:`update_csv`,
    :meth:`new_observations`),
  * caches raw JSON responses on disk so repeat calls are cheap.

The module also defines a small :class:`_Cli` facade that `python-fire
<https://github.com/google/python-fire>`_ turns into a command-line interface.

Data source (free API key required):
    https://www.eia.gov/opendata/

Example:
    >>> from eia_api import EIA
    >>> eia = EIA()  # reads EIA_API_KEY from the environment
    >>> eia.browse()  # top-level routes (electricity, petroleum, ...)
    >>> eia.facets("electricity/retail-sales")  # filtering dimensions
    >>> eia.get_data(  # tidy long DataFrame, auto-paginated
    ...     "electricity/retail-sales",
    ...     data="price",
    ...     facets={"stateid": "CO", "sectorid": "RES"},
    ...     frequency="monthly",
    ...     start="2015-01",
    ... )
    >>> eia.to_csv("retail_price.csv", route="electricity/retail-sales", data="price")

Command line:
    eia-api browse electricity
    eia-api facets electricity/retail-sales
    eia-api to-csv price.csv --route electricity/retail-sales --data price
"""

from __future__ import (  # Enables modern (PEP 604 / postponed) annotations.
    annotations,
)

import os  # Reads the EIA_API_KEY environment variable as a key fallback.
import re  # Recognises facet label columns (stateDescription, ...).
import sys  # Reads the CLI's command-line arguments.
import time  # Times each request for the debug log.
from dataclasses import dataclass  # Builds the immutable Route value type.
from pathlib import Path  # Cross-platform filesystem paths for the cache/outputs.
from typing import TYPE_CHECKING, Any, ClassVar, Literal, overload  # Typing helpers.

import httpx  # Async HTTP client backing :class:`AsyncEIA`.
import pandas as pd  # The core tabular data type used throughout the module.
import requests  # HTTP client for calling the EIA REST API.
from wrapkit.cache import (  # Shared disk cache and its default sentinel.
    DEFAULT_CACHE_DIR,
    DefaultCacheDir,
    DiskCache,
    cache_path,
    resolve_cache_dir,
)
from wrapkit.cli import (  # Shared python-fire helpers.
    as_text,
    bind_bool_flags,
    literal_keeping_codes,
    resolve_out,
    run,
    set_parse_fns,
)
from wrapkit.frames import (  # Shared tidy-shape helpers.
    as_polars,
    empty_tidy,
    snake,
    tidy_frame,
)
from wrapkit.http import AsyncThrottle, Throttle  # Request-rate throttles.
from wrapkit.incremental import (  # Shared new/revised-row logic.
    read_existing,
    select_new,
    upsert,
)
from wrapkit.io import write_frame  # Shared CSV/Excel/Parquet/JSON writer.
from wrapkit.log import (  # Shared logging and key redaction.
    RedactFilter,
    configure_logging,
    loggable,
    package_logger,
)

if TYPE_CHECKING:
    # Imported only for type checking to keep the runtime import graph small;
    # these names are used purely in annotations.
    from collections.abc import Iterable, Mapping, Sequence

    # polars is an optional extra, imported lazily at runtime (``_as_polars``).
    import polars as pl

# Only the public surface is exported; helpers and the CLI facade stay private.
__all__ = ["DEFAULT_CACHE_DIR", "EIA", "AsyncEIA", "Route"]

# Root of the versioned API. Every route path is appended to this base, and the
# metadata endpoint for a route is the route itself while its rows live under an
# appended "/data" segment.
_BASE_URL = "https://api.eia.gov/v2/"

# The API refuses to return more than this many rows in a single JSON response,
# so :meth:`EIA.get_data` pages through larger datasets in chunks of this size.
_MAX_LENGTH = 5000

# Environment variable consulted for the API key when one is not passed
# explicitly, so scripts need not hardcode the secret.
_ENV_KEY = "EIA_API_KEY"

# Some EIA endpoints reject requests lacking a browser-like User-Agent, so every
# HTTP session or client this package creates sends this one, to avoid being
# served an error page instead of JSON. Caller-supplied sessions are left as-is.
_USER_AGENT = (
    "Mozilla/5.0 (compatible; eia-api-wrapper/1.0; +https://www.eia.gov/opendata/)"
)

# The package logger. As a library it only emits records; nothing is printed
# until an application (or the CLI, see ``_configure_logging``) attaches a
# handler, e.g. ``logging.getLogger("eia_api").setLevel("DEBUG")``.
_log = package_logger("eia_api")

# Marks the handlers the CLI installs, so reconfiguring replaces them instead of
# stacking duplicates.
_CLI_HANDLER_FLAG = "_eia_api_cli_handler"

# The key travels in the query string, and both HTTP clients log full URLs
# (urllib3 at DEBUG, httpx at INFO). One filter for the process masks any
# ``api_key=<value>`` and every key a client uses (see ``_register_secret``) on
# those loggers and on the package logger. Records are never dropped.
_REDACT_FILTER = RedactFilter(["api_key"]).install("eia_api")

# The type of :data:`DEFAULT_CACHE_DIR`, under its earlier private name.
_DefaultCacheDir = DefaultCacheDir


def _register_secret(api_key: str | None) -> None:
    """
    Register an API key with the redaction filter.

    Args:
        api_key (str | None): The key to scrub from logs; ``None`` or a key
            shorter than eight characters is ignored (the URL pattern still
            masks it).
    """
    _REDACT_FILTER.register(api_key)


def _loggable(params: Sequence[tuple[str, str]]) -> str:
    """
    Render request parameters for a log line, without the API key.

    Args:
        params (Sequence[tuple[str, str]]): Request parameters as (name, value)
            pairs, possibly including ``api_key``.

    Returns:
        str: ``name=value`` pairs joined by ``&``, with ``api_key`` left out.
    """
    return loggable(params, ["api_key"])


def _resolve_cache_dir(cache_dir: str | Path | DefaultCacheDir | None) -> Path | None:
    """
    Turn a ``cache_dir`` argument into the cache directory to use, if any.

    :data:`DEFAULT_CACHE_DIR` maps to ``~/.cache/eia_api`` under the *current*
    home directory; ``None`` and any other falsy value (such as ``""``) disable
    the on-disk cache; anything else is taken as the directory path.

    Args:
        cache_dir (str | Path | DefaultCacheDir | None): The value passed to a
            client constructor.

    Returns:
        Path | None: The cache directory, or ``None`` when caching is off.
    """
    return resolve_cache_dir(cache_dir, "eia_api")


def _cache_path(
    cache_dir: Path | None, endpoint: str, params: Sequence[tuple[str, str]]
) -> Path | None:
    """
    Return the on-disk cache path for a request, or ``None`` when disabled.

    The name is a SHA-256 digest of the endpoint plus the request parameters
    with the API key left out, so the secret never reaches a file name and two
    callers with different keys share one cached payload.

    Args:
        cache_dir (Path | None): Cache directory, or ``None`` if caching is off.
        endpoint (str): The route path being requested (below the base URL).
        params (Sequence[tuple[str, str]]): Request parameters as (name, value)
            pairs, including the ``api_key`` pair.

    Returns:
        Path | None: The cache file path, or ``None`` if caching is disabled.
    """
    return cache_path(cache_dir, endpoint, params, hidden=["api_key"])


def _disk_cache(cache_dir: Path | None, ttl_seconds: float) -> DiskCache:
    """
    Return the response cache for a client's current settings.

    Built per request from the client's ``cache_dir`` and
    ``cache_ttl_seconds``, so changing either attribute takes effect at once.

    Args:
        cache_dir (Path | None): Cache directory, or ``None`` if caching is off.
        ttl_seconds (float): Freshness window in seconds.

    Returns:
        DiskCache: The cache (never hits when ``cache_dir`` is ``None``).
    """
    return DiskCache(cache_dir, ttl_seconds, hidden=["api_key"], logger=_log)


def _routes_frame(meta: Mapping[str, Any]) -> pd.DataFrame:
    """
    Build the child-route DataFrame from a route's metadata object.

    Args:
        meta (Mapping[str, Any]): A decoded metadata ``response`` object.

    Returns:
        pd.DataFrame: Columns ``[id, name, description]``, one row per child
        route (empty when the route is a leaf dataset).
    """
    rows = [
        {
            "id": str(r.get("id", "")),
            "name": str(r.get("name", "")),
            "description": str(r.get("description", "")),
        }
        for r in meta.get("routes", [])
    ]
    return pd.DataFrame(rows, columns=["id", "name", "description"])


def _data_columns_frame(meta: Mapping[str, Any]) -> pd.DataFrame:
    """
    Build the value-column DataFrame from a dataset's metadata object.

    Args:
        meta (Mapping[str, Any]): A decoded metadata ``response`` object.

    Returns:
        pd.DataFrame: Columns ``[id, alias, units]`` (missing pieces blank), one
        row per available value column.
    """
    # Most datasets map each column id to an ``{alias, units}`` dict, but some
    # (e.g. petroleum/natural-gas price routes) carry an empty list as the value
    # instead — the label and unit live on the data rows, not here. So coerce a
    # non-dict ``info`` to blank metadata rather than calling ``.get`` on it,
    # which would raise for such routes.
    cols = meta.get("data", {})
    cols = cols if isinstance(cols, dict) else {}
    rows = [
        {
            "id": key,
            "alias": str(info.get("alias", "")) if isinstance(info, dict) else "",
            "units": str(info.get("units", "")) if isinstance(info, dict) else "",
        }
        for key, info in cols.items()
    ]
    return pd.DataFrame(rows, columns=["id", "alias", "units"])


def _norm_route(route: str) -> str:
    """
    Canonicalise a user-supplied route path (shared by both clients).

    Trims surrounding slashes/whitespace and drops a trailing ``/data`` segment,
    so ``"/electricity/retail-sales/data/"`` and ``"electricity/retail-sales"``
    address the same node. The ``/data`` and ``/facet`` suffixes are appended by
    the client, never by the caller.

    Args:
        route (str): The route path as supplied by the caller.

    Returns:
        str: The cleaned route with no leading/trailing slash and no trailing
        ``/data`` segment.
    """
    cleaned = route.strip().strip("/")
    if cleaned.endswith("/data"):
        cleaned = cleaned[: -len("/data")].strip("/")
    return cleaned


def _parse_period_series(period: pd.Series) -> pd.Series:
    """
    Parse EIA period labels of any frequency into period-start Timestamps.

    Labels vary by frequency: a bare year (``"2020"``), a month (``"2020-03"``),
    a quarter (``"2020-Q1"``), a date (``"2020-03-15"``) or an hour
    (``"2020-03-15T05"``). Quarters map to their first month; everything else
    uses pandas' mixed-format parser. Unparseable labels become ``NaT``.

    Args:
        period (pd.Series): The raw ``period`` column returned by the API.

    Returns:
        pd.Series: Period-start Timestamps, with unparseable labels as ``NaT``.
    """
    s = period.astype("string")
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")

    # Quarterly labels ("2020-Q1") have no calendar day, so map each to the first
    # month of its quarter: Q1->Jan, Q2->Apr, Q3->Jul, Q4->Oct.
    is_q = s.str.contains("Q", case=False, na=False)
    if is_q.any():
        parts = s[is_q].str.extract(r"(\d{4}).*?[Qq]([1-4])")
        months = (parts[1].astype(int) - 1) * 3 + 1
        out.loc[is_q] = pd.to_datetime(
            {"year": parts[0].astype(int), "month": months, "day": 1}
        ).to_numpy()

    # Everything else (year/month/date/hour) parses directly; "mixed" opts in to
    # per-element inference so heterogeneous labels do not warn.
    rest = ~is_q
    if rest.any():
        out.loc[rest] = pd.to_datetime(
            s[rest], errors="coerce", format="mixed"
        ).to_numpy()
    return out


def _encode_data_params(
    *,
    columns: list[str],
    facets: Mapping[str, str | Iterable[str]] | None,
    frequency: str | None,
    start: str | None,
    end: str | None,
    sort: Sequence[tuple[str, str]],
    offset: int,
    length: int,
) -> list[tuple[str, str]]:
    """
    Encode a data query into the API's bracketed query parameters.

    The EIA API expects repeated bracketed keys (``data[]``, ``facets[<id>][]``,
    ``sort[<i>][column]``), represented here as a list of (name, value) pairs so
    the HTTP client sends each occurrence. See :meth:`EIA.get_data` for meanings.

    Args:
        columns (list[str]): Value-column ids for ``data[]``.
        facets (Mapping[str, str | Iterable[str]] | None): Facet filters.
        frequency (str | None): Requested periodicity.
        start (str | None): Inclusive lower period bound.
        end (str | None): Inclusive upper period bound.
        sort (Sequence[tuple[str, str]]): Sort keys.
        offset (int): Row offset.
        length (int): Page size.

    Returns:
        list[tuple[str, str]]: The encoded query parameters (without the key).
    """
    params: list[tuple[str, str]] = []
    if frequency:
        params.append(("frequency", frequency))
    params.extend(("data[]", col) for col in columns)
    for facet_id, values in (facets or {}).items():
        # Accept a single value as a plain string, not only an iterable.
        items = [values] if isinstance(values, str) else list(values)
        params.extend((f"facets[{facet_id}][]", str(v)) for v in items)
    if start:
        params.append(("start", str(start)))
    if end:
        params.append(("end", str(end)))
    for i, (column, direction) in enumerate(sort):
        params.extend(
            [(f"sort[{i}][column]", column), (f"sort[{i}][direction]", direction)]
        )
    params.extend([("offset", str(offset)), ("length", str(length))])
    return params


def _rows_to_frame(rows: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
    """
    Assemble the tidy DataFrame from accumulated rows (shared by both clients).

    Coerces the requested value columns to numbers (the API returns them as
    strings) and inserts a parsed ``date`` column ahead of the raw ``period``. An
    empty result yields an empty frame.

    Args:
        rows (list[dict[str, Any]]): Accumulated raw row dictionaries.
        columns (list[str]): The requested value-column ids to coerce.

    Returns:
        pd.DataFrame: The tidy result described in :meth:`EIA.get_data`.
    """
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    # Value cells arrive as strings; coerce the requested measures to numbers
    # (leaving their "<col>-units" companions as text).
    for col in columns:
        if col in frame.columns:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
    # Add a parsed calendar date ahead of the API's native period label.
    if "period" in frame.columns:
        frame.insert(0, "date", _parse_period_series(frame["period"]))
    return frame


# Suffix of the per-measure unit columns the API pairs with each value column
# (``price`` -> ``price-units``).
_UNITS_SUFFIX = "-units"

# Facet label columns end in "Description"/"Name"/"-name"/"-description"
# (``stateDescription``, ``sectorName``, ``area-name``, ...); the API places each
# one straight after the facet id it describes.
_LABEL_COLUMN = re.compile(r"(description|name)$", re.IGNORECASE)

# The fixed leading and trailing columns of the tidy shape.
_TIDY_VALUE_COLUMNS = ("value", "unit")


_snake = snake


def _dimension_map(frame: pd.DataFrame, columns: Sequence[str]) -> dict[str, str]:
    """
    Map a raw frame's facet columns to their tidy dimension names.

    Every column that is not the date/period, a requested value column or a unit
    column is a facet column. A facet *id* keeps its (snake_cased) name; a label
    column (one ending in ``Description``/``Name``) that directly follows an id
    becomes ``<id>_name``, so ``stateid, stateDescription`` maps to
    ``stateid, stateid_name``. A label with no id in front of it is kept as a
    dimension in its own right.

    Args:
        frame (pd.DataFrame): The default-shape frame from :meth:`EIA.get_data`.
        columns (Sequence[str]): The requested value-column ids.

    Returns:
        dict[str, str]: Raw column name -> tidy dimension name, in column order.
    """
    skip = {"date", "period", "units", *columns}
    mapping: dict[str, str] = {}
    last_id: str | None = None
    for col in map(str, frame.columns):
        if col in skip or col.endswith(_UNITS_SUFFIX):
            continue
        if _LABEL_COLUMN.search(col) and last_id is not None:
            # The label of the facet id just before it; only one label per id.
            mapping[col] = f"{last_id}_name"
            last_id = None
            continue
        mapping[col] = last_id = _snake(col)
    return mapping


def _to_tidy(frame: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    """
    Reshape a default :meth:`EIA.get_data` frame into the shared tidy shape.

    Shared by the sync and async clients. Each requested measure is melted into
    its own block of rows: the facet columns become the dimensions (ids under
    their snake_case names, labels as ``<id>_name``), the measure id goes into a
    ``variable`` column, its number into ``value`` and its unit from the paired
    ``<measure>-units`` column (or a lone ``units`` column) into ``unit``. The
    raw ``period`` label is dropped; ``date`` is its parsed period start.

    Args:
        frame (pd.DataFrame): The default-shape frame from :meth:`EIA.get_data`.
        columns (Sequence[str]): The requested value-column ids.

    Returns:
        pd.DataFrame: Columns ``date``, the dimension columns, ``variable``,
        ``value`` (float64) and ``unit`` (string, ``<NA>`` when absent), sorted
        by the dimensions then ``date`` with a fresh index.
    """
    dims = _dimension_map(frame, columns)
    dim_cols = [*dims.values(), "variable"]
    measures = [c for c in columns if c in frame.columns]
    if frame.empty or not measures:
        # Nothing to melt: return the empty tidy shape with its dtypes.
        return empty_tidy(dim_cols)
    dates = (
        frame["date"]
        if "date" in frame.columns
        else pd.Series(pd.NaT, index=frame.index)
    )
    pieces: list[pd.DataFrame] = []
    for measure in measures:
        part = frame[list(dims)].rename(columns=dims)
        part.insert(0, "date", dates)
        part["variable"] = measure
        part["value"] = pd.to_numeric(frame[measure], errors="coerce")
        # Prefer the measure's own "<measure>-units" column; some routes carry
        # a single "units" column instead; otherwise the unit is unknown.
        unit_col = next(
            (c for c in (f"{measure}{_UNITS_SUFFIX}", "units") if c in frame.columns),
            None,
        )
        part["unit"] = frame[unit_col] if unit_col else None
        pieces.append(part)
    # Shared column order, dtypes (blank units become <NA>) and sort.
    return tidy_frame(pd.concat(pieces, ignore_index=True), dim_cols)


def _as_polars(frame: pd.DataFrame) -> pl.DataFrame:
    """
    Convert a pandas result to a polars DataFrame, importing polars lazily.

    polars is an optional extra, so it is only imported when a caller asks for
    ``as_polars=True``; without it, the ``ImportError`` names the ``polars``
    extra to install.

    Args:
        frame (pd.DataFrame): The pandas result to convert.

    Returns:
        pl.DataFrame: ``polars.from_pandas(frame)``.
    """
    return as_polars(frame, "eia-api")


def _finish(
    frame: pd.DataFrame, columns: Sequence[str], *, tidy: bool, as_polars: bool
) -> pd.DataFrame | pl.DataFrame:
    """
    Apply the opt-in output options to a default-shape result.

    Shared by the sync and async ``get_data``.

    Args:
        frame (pd.DataFrame): The default-shape result.
        columns (Sequence[str]): The requested value-column ids.
        tidy (bool): Reshape into the tidy long shape (see :func:`_to_tidy`).
        as_polars (bool): Return a polars DataFrame instead of pandas.

    Returns:
        pd.DataFrame | pl.DataFrame: The result in the requested shape/library.
    """
    if tidy:
        frame = _to_tidy(frame, columns)
    return _as_polars(frame) if as_polars else frame


@dataclass(frozen=True)
class Route:
    """
    Immutable metadata describing a single node in the EIA API route tree.

    Browsing the API yields a list of child routes, each carrying a short path
    ``id`` (the segment appended to the parent route), a human-readable ``name``
    and a longer ``description``. This small value type pairs those together so
    callers can present or navigate the tree without re-parsing raw JSON. It is
    declared with ``@dataclass(frozen=True)`` to make instances hashable and safe
    to reuse.

    Attributes:
        id (str): Path segment identifying the route, e.g. ``"retail-sales"``.
        name (str): Human-readable route name.
        description (str): Longer prose description of the route's contents.
    """

    id: str
    name: str
    description: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        """
        Return a compact ``"id — name"`` representation of the route.

        Returns:
            str: The route id followed by its name.
        """
        return f"{self.id} — {self.name}"


class EIA:
    """
    Client for browsing and downloading data from the EIA API v2.

    This class is the main entry point of the package. A single instance can
    authenticate, walk the route tree, inspect a dataset's facets and columns,
    and pull its rows into a tidy DataFrame with pagination handled for you.
    Network access and on-disk response caching are managed internally, so most
    callers only ever touch :meth:`browse`, :meth:`facets`, :meth:`facet_values`,
    :meth:`get_data`, the ``to_*`` writers and the incremental helpers.

    Args:
        api_key (str | None): EIA API key. When omitted the ``EIA_API_KEY``
            environment variable is used; if neither is set, calls that hit the
            network raise :class:`ValueError`. Register for a free key at
            https://www.eia.gov/opendata/.
        cache_dir (str | Path | _DefaultCacheDir | None): Directory used to
            cache raw API responses. The default, :data:`~eia_api.DEFAULT_CACHE_DIR`,
            means ``~/.cache/eia_api`` (resolved when the client is built).
            ``None`` (or ``""``) disables the on-disk cache.
        cache_ttl_hours (float): How long a cached response is considered fresh.
            Most EIA series update daily at most, so the 24-hour default rarely
            serves stale data while still avoiding needless requests.
        timeout (float): Per-request timeout in seconds.
        requests_per_second (float | None): Client-side throttle. At most this
            many network requests are issued per second (cache hits are never
            throttled); the client sleeps just enough between calls to stay under
            the cap. Defaults to a gentle 9/s so bulk pagination stays polite to
            the API; pass ``None`` to disable throttling entirely.
        session (requests.Session | None): Optional pre-configured session, for
            example one already set up to route through a corporate proxy. When
            omitted a fresh session is created and given the package's
            browser-like User-Agent. A supplied session is used exactly as
            passed, its headers untouched, so set its User-Agent yourself if the
            API rejects ``python-requests``.
    """

    # The pair of column families that are *not* part of a row's identity: the
    # requested value columns and their paired "<column>-units" strings. Every
    # other column (the period plus the facet columns) forms the natural key used
    # by the incremental helpers to decide which rows are new or revised.
    _UNITS_SUFFIX: ClassVar[str] = "-units"

    # Bound just after :class:`AsyncEIA` is defined (below), so the async client
    # is reachable as ``EIA.AsyncAPI`` for parity with the sync entry point.
    AsyncAPI: ClassVar[type[AsyncEIA]]

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | _DefaultCacheDir | None = DEFAULT_CACHE_DIR,
        cache_ttl_hours: float = 24.0,
        timeout: float = 60.0,
        requests_per_second: float | None = 9.0,
        session: requests.Session | None = None,
    ) -> None:
        """
        Initialise the client and prepare the cache directory and session.

        See the class docstring for a description of each argument. The
        constructor performs no network access; it only resolves the API key,
        sets up the cache location and the HTTP session and initialises the
        in-memory metadata cache. Only a session the constructor creates itself
        gets the browser-like User-Agent; a caller's session keeps its headers.

        Args:
            api_key (str | None): API key, or ``None`` to read ``EIA_API_KEY``.
            cache_dir (str | Path | _DefaultCacheDir | None): Cache directory;
                :data:`~eia_api.DEFAULT_CACHE_DIR` for ``~/.cache/eia_api``, ``None``
                (or ``""``) to disable disk caching.
            cache_ttl_hours (float): Freshness window for cached responses.
            timeout (float): Per-request timeout in seconds.
            requests_per_second (float | None): Network-request throttle, or
                ``None`` to disable it.
            session (requests.Session | None): Optional pre-built HTTP session,
                used as-is; when ``None`` a new one with the package User-Agent
                is created.
        """
        # Fall back to the environment when no key is passed; validation is
        # deferred to the first network call so metadata-free use never trips.
        self.api_key = api_key or os.environ.get(_ENV_KEY)
        _register_secret(self.api_key)

        # DEFAULT_CACHE_DIR means ~/.cache/eia_api (resolved now, not at import);
        # ``None`` or any other falsy value (e.g. "") disables disk caching.
        self.cache_dir = _resolve_cache_dir(cache_dir)
        # Create the directory up front so later writes never race on it.
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)

        # Store the TTL in seconds because that is what ``time.time()`` returns.
        self.cache_ttl_seconds = cache_ttl_hours * 3600.0
        self.timeout = timeout
        if session is None:
            session = requests.Session()
            # Assign rather than ``setdefault``: a fresh session already carries
            # requests' own ``python-requests/x.y`` User-Agent, so ``setdefault``
            # would never apply ours. A caller's session is not modified.
            session.headers["User-Agent"] = _USER_AGENT
        self.session = session

        # Client-side throttle between network requests (never for cache hits);
        # the first request never waits.
        self._throttler = Throttle(requests_per_second, _log)

        # In-memory memoisation of route metadata so repeated lookups (e.g. the
        # automatic data-column discovery in ``get_data``) are free per process.
        self._meta_cache: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # HTTP + cache
    # ------------------------------------------------------------------ #
    @staticmethod
    def _normalize_route(route: str) -> str:
        """
        Canonicalise a user-supplied route path.

        Trims surrounding slashes and whitespace and drops a trailing ``/data``
        segment, so ``"/electricity/retail-sales/data/"`` and
        ``"electricity/retail-sales"`` address the same dataset node. The
        ``/data`` and ``/facet`` suffixes are appended by the client, never by
        the caller.

        Args:
            route (str): The route path as supplied by the caller.

        Returns:
            str: The cleaned route with no leading/trailing slash and no trailing
            ``/data`` segment.
        """
        return _norm_route(route)

    def _cache_path(
        self, endpoint: str, params: Sequence[tuple[str, str]]
    ) -> Path | None:
        """
        Return the on-disk cache path for a request, or ``None`` when disabled.

        The filename is a SHA-256 digest of the endpoint plus the request
        parameters with the API key stripped out, so the secret never influences
        (or leaks into) the cache key and two callers with different keys share
        the same cached payload.

        Args:
            endpoint (str): The route path being requested (below the base URL).
            params (Sequence[tuple[str, str]]): The request parameters as
                (name, value) pairs, including the ``api_key`` pair.

        Returns:
            Path | None: The cache file path, or ``None`` if caching is disabled.
        """
        return _cache_path(self.cache_dir, endpoint, params)

    def _throttle(self) -> None:
        """
        Sleep just enough to respect the configured requests-per-second cap.

        Called immediately before each network request (never for cache hits).
        Sleeps for the shortfall between the configured minimum interval and the
        time elapsed since the previous network request, then records the new
        request time. A no-op when throttling is disabled.
        """
        self._throttler.wait()

    def _request(
        self, endpoint: str, params: Sequence[tuple[str, str]]
    ) -> dict[str, Any]:
        """
        Perform one GET against the API and return its ``response`` object.

        Serves a fresh cached payload when one exists within the TTL; otherwise
        calls the API, persists the payload and returns it. EIA reports problems
        as a top-level ``error`` field (e.g. a missing or invalid key) rather
        than always via the HTTP status, so that is checked explicitly and turned
        into a clear exception.

        Args:
            endpoint (str): The route path to request, below the base URL and
                already including any ``/data`` or ``/facet/<id>`` suffix.
            params (Sequence[tuple[str, str]]): Request parameters as
                (name, value) pairs. The ``api_key`` pair must be present.

        Returns:
            dict[str, Any]: The decoded ``response`` object from the payload.

        Raises:
            RuntimeError: If the API returns an ``error`` field.
        """
        cache = _disk_cache(self.cache_dir, self.cache_ttl_seconds)
        # Reuse the cached copy while it is still within its freshness window.
        hit, cached = cache.get(endpoint, params)
        if hit:
            cached_response: dict[str, Any] = cached["response"]
            return cached_response

        # Only real network requests are throttled; cache hits returned above.
        self._throttle()
        _log.debug("GET %s %s", endpoint, _loggable(params))
        started = time.monotonic()
        resp = self.session.get(
            _BASE_URL + endpoint, params=list(params), timeout=self.timeout
        )
        _log.debug(
            "HTTP %s for %s in %.2fs",
            resp.status_code,
            endpoint,
            time.monotonic() - started,
        )
        payload = resp.json()
        # EIA signals a bad key, unknown route, etc. with a top-level "error"
        # (often alongside a 200/403), so surface that before raise_for_status.
        if isinstance(payload, dict) and payload.get("error"):
            msg = f"EIA API error: {payload['error']} (code {payload.get('code')})"
            raise RuntimeError(msg)
        resp.raise_for_status()

        cache.put(endpoint, params, payload)
        response: dict[str, Any] = payload["response"]
        return response

    def _params(self, extra: Iterable[tuple[str, str]] = ()) -> list[tuple[str, str]]:
        """
        Build the base parameter list, injecting the (validated) API key.

        Args:
            extra (Iterable[tuple[str, str]]): Additional (name, value) pairs to
                append after the ``api_key`` pair.

        Returns:
            list[tuple[str, str]]: The parameter list starting with the API key.

        Raises:
            ValueError: If no API key was configured.
        """
        if not self.api_key:
            msg = (
                "No EIA API key configured. Pass api_key=... or set the "
                f"{_ENV_KEY} environment variable. Register for a free key at "
                "https://www.eia.gov/opendata/."
            )
            raise ValueError(msg)
        return [("api_key", self.api_key), *extra]

    # ------------------------------------------------------------------ #
    # Metadata / browsing
    # ------------------------------------------------------------------ #
    def metadata(self, route: str = "") -> dict[str, Any]:
        """
        Return the raw metadata object for a route.

        Requesting a route *without* its ``/data`` endpoint returns metadata: for
        a parent route a list of child ``routes``; for a leaf dataset the
        available ``frequency`` list, ``facets`` list, ``data`` columns and
        ``startPeriod``/``endPeriod``. The result is memoised per process.

        Args:
            route (str): Route path, e.g. ``""`` for the top level or
                ``"electricity/retail-sales"`` for a dataset. Any trailing
                ``/data`` is ignored.

        Returns:
            dict[str, Any]: The decoded metadata ``response`` object.
        """
        route = self._normalize_route(route)
        if route in self._meta_cache:
            return self._meta_cache[route]
        meta = self._request(route, self._params())
        self._meta_cache[route] = meta
        return meta

    def browse(self, route: str = "") -> pd.DataFrame:
        """
        List the child routes beneath a route.

        Use this to walk the API tree from the top level down to a dataset. A
        leaf dataset has no child routes, so an empty frame there is the signal to
        switch to :meth:`data_columns`/:meth:`facets`/:meth:`get_data`.

        Args:
            route (str): Route path to list beneath (``""`` for the top level).

        Returns:
            pd.DataFrame: Columns ``[id, name, description]``, one row per child
            route (empty when the route is a leaf dataset).
        """
        return _routes_frame(self.metadata(route))

    def frequencies(self, route: str) -> pd.DataFrame:
        """
        List the frequencies (periodicities) a dataset supports.

        Args:
            route (str): Dataset route path.

        Returns:
            pd.DataFrame: One row per frequency; columns typically include
            ``[id, description, query, format]`` as provided by the API.
        """
        meta = self.metadata(route)
        return pd.DataFrame(meta.get("frequency", []))

    def facets(self, route: str) -> pd.DataFrame:
        """
        List the facets (filtering dimensions) a dataset exposes.

        Each facet is a dimension such as ``stateid`` or ``sectorid`` whose values
        can be passed to the ``facets`` argument of :meth:`get_data`. Use
        :meth:`facet_values` to enumerate the allowed values of one facet.

        Args:
            route (str): Dataset route path.

        Returns:
            pd.DataFrame: One row per facet; columns typically ``[id,
            description]``.
        """
        meta = self.metadata(route)
        return pd.DataFrame(meta.get("facets", []))

    def data_columns(self, route: str) -> pd.DataFrame:
        """
        List the value columns a dataset offers for the ``data`` argument.

        These are the measured quantities (e.g. ``price``, ``revenue``,
        ``sales``) that :meth:`get_data` requests via ``data[]``. Passing
        ``data=None`` to :meth:`get_data` selects every column listed here.

        Args:
            route (str): Dataset route path.

        Returns:
            pd.DataFrame: Columns ``[id, alias, units]`` (missing pieces blank),
            one row per available value column.
        """
        return _data_columns_frame(self.metadata(route))

    def facet_values(self, route: str, facet_id: str) -> pd.DataFrame:
        """
        List the allowed values of a single facet.

        Args:
            route (str): Dataset route path.
            facet_id (str): The facet whose values to enumerate, e.g.
                ``"stateid"`` (see :meth:`facets`).

        Returns:
            pd.DataFrame: One row per facet value; columns typically ``[id,
            name/alias]`` as provided by the API.
        """
        route = self._normalize_route(route)
        meta = self._request(f"{route}/facet/{facet_id}", self._params())
        return pd.DataFrame(meta.get("facets", []))

    # ------------------------------------------------------------------ #
    # Data
    # ------------------------------------------------------------------ #
    def _resolve_data(self, route: str, data: str | Iterable[str] | None) -> list[str]:
        """
        Resolve the requested value columns, defaulting to every column.

        A single column may be given as a plain string. ``None`` asks the API
        (via :meth:`data_columns`) for every value column the dataset exposes, so
        ``get_data(route)`` returns all measures without the caller naming them.

        Args:
            route (str): Dataset route path.
            data (str | Iterable[str] | None): One column, several columns, or
                ``None`` for all.

        Returns:
            list[str]: The value-column ids to request.
        """
        if data is None:
            return self.data_columns(route)["id"].tolist()
        if isinstance(data, str):
            return [data]
        return list(data)

    @staticmethod
    def _parse_period(period: pd.Series) -> pd.Series:
        """
        Parse EIA period labels of any frequency into period-start Timestamps.

        EIA period labels vary by frequency: a bare year (``"2020"``), a month
        (``"2020-03"``), a quarter (``"2020-Q1"``), a date (``"2020-03-15"``) or
        an hour (``"2020-03-15T05"``). Quarters are mapped to their first month;
        everything else is parsed with pandas' mixed-format parser. Labels that do
        not parse become ``NaT``.

        Args:
            period (pd.Series): The raw ``period`` column returned by the API.

        Returns:
            pd.Series: Period-start Timestamps, with unparseable labels as
            ``NaT``.
        """
        return _parse_period_series(period)

    @overload
    def get_data(
        self,
        route: str,
        data: str | Iterable[str] | None = ...,
        facets: Mapping[str, str | Iterable[str]] | None = ...,
        frequency: str | None = ...,
        start: str | None = ...,
        end: str | None = ...,
        sort: Sequence[tuple[str, str]] | None = ...,
        offset: int = ...,
        length: int = ...,
        max_rows: int | None = ...,
        *,
        tidy: bool = ...,
        as_polars: Literal[False] = ...,
    ) -> pd.DataFrame: ...

    @overload
    def get_data(
        self,
        route: str,
        data: str | Iterable[str] | None = ...,
        facets: Mapping[str, str | Iterable[str]] | None = ...,
        frequency: str | None = ...,
        start: str | None = ...,
        end: str | None = ...,
        sort: Sequence[tuple[str, str]] | None = ...,
        offset: int = ...,
        length: int = ...,
        max_rows: int | None = ...,
        *,
        tidy: bool = ...,
        as_polars: Literal[True],
    ) -> pl.DataFrame: ...

    @overload
    def get_data(
        self,
        route: str,
        data: str | Iterable[str] | None = ...,
        facets: Mapping[str, str | Iterable[str]] | None = ...,
        frequency: str | None = ...,
        start: str | None = ...,
        end: str | None = ...,
        sort: Sequence[tuple[str, str]] | None = ...,
        offset: int = ...,
        length: int = ...,
        max_rows: int | None = ...,
        *,
        tidy: bool = ...,
        as_polars: bool,
    ) -> pd.DataFrame | pl.DataFrame: ...

    def get_data(
        self,
        route: str,
        data: str | Iterable[str] | None = None,
        facets: Mapping[str, str | Iterable[str]] | None = None,
        frequency: str | None = None,
        start: str | None = None,
        end: str | None = None,
        sort: Sequence[tuple[str, str]] | None = None,
        offset: int = 0,
        length: int = _MAX_LENGTH,
        max_rows: int | None = None,
        *,
        tidy: bool = False,
        as_polars: bool = False,
    ) -> pd.DataFrame | pl.DataFrame:
        """
        Fetch a dataset's rows into a tidy DataFrame, paginating as needed.

        Requests the ``/data`` endpoint of ``route`` and follows the API's
        pagination until the whole result (or ``max_rows``) is retrieved, since a
        single response is capped at 5000 rows. The returned frame is tidy — one
        row per observation — with the requested value columns coerced to numbers
        and a parsed ``date`` column added ahead of the raw ``period``.

        Args:
            route (str): Dataset route path, e.g. ``"electricity/retail-sales"``.
                Any trailing ``/data`` is ignored.
            data (str | Iterable[str] | None): Value column(s) to return (see
                :meth:`data_columns`). A single column may be a plain string.
                ``None`` (default) requests every value column the dataset has.
            facets (Mapping[str, str | Iterable[str]] | None): Facet filters as a
                mapping of facet id to one value or several, e.g.
                ``{"stateid": "CO", "sectorid": ["RES", "COM"]}``. ``None`` applies
                no filter (see :meth:`facets`, :meth:`facet_values`).
            frequency (str | None): Periodicity to request (e.g. ``"monthly"``,
                ``"annual"``); ``None`` uses the dataset default (see
                :meth:`frequencies`).
            start (str | None): Inclusive lower period bound in the dataset's
                period format (e.g. ``"2015"``, ``"2015-01"``, ``"2015-01-01"``).
            end (str | None): Inclusive upper period bound, same formats.
            sort (Sequence[tuple[str, str]] | None): Sort keys as
                ``(column, direction)`` pairs with direction ``"asc"`` or
                ``"desc"``. Defaults to ``[("period", "asc")]`` for stable,
                reproducible pagination.
            offset (int): Row offset to begin at (rarely needed; pagination is
                automatic).
            length (int): Page size per request, capped at 5000.
            max_rows (int | None): Stop after retrieving this many rows. ``None``
                (default) fetches the entire result.
            tidy (bool): Return a standard long shape instead of the default
                one: columns ``date`` (period start), the facet
                ids as dimensions (snake_case, with a facet's label column as
                ``<facet>_name`` where the API sends one), ``variable`` (the
                measure id), ``value`` (float64) and ``unit`` (from the
                ``<measure>-units`` column; ``<NA>`` when absent). One row per
                measure and observation, sorted by the dimensions then ``date``.
            as_polars (bool): Return a ``polars.DataFrame`` (works with
                ``tidy``). Needs the optional ``polars`` extra.

        Returns:
            pd.DataFrame | pl.DataFrame: By default the result as returned by
            the API: columns ``date`` then ``period``, the facet columns, and
            each requested value column with its paired ``<column>-units``
            column (exact set as returned by the API). Empty when the query
            matches nothing. ``tidy`` and ``as_polars`` change the shape and
            the library as described above.

        Raises:
            ValueError: If ``length`` is not positive.
        """
        if length <= 0:
            msg = f"length must be a positive integer, got {length}."
            raise ValueError(msg)

        route = self._normalize_route(route)
        columns = self._resolve_data(route, data)
        # Default to period-ascending sorting: without an explicit sort the API's
        # row order is not guaranteed stable across pages, which can duplicate or
        # drop rows while paginating.
        sort = sort or [("period", "asc")]
        page = min(length, _MAX_LENGTH)

        rows = self._paginate(
            route,
            columns=columns,
            facets=facets,
            frequency=frequency,
            start=start,
            end=end,
            sort=sort,
            offset=offset,
            page=page,
            max_rows=max_rows,
        )
        return _finish(
            self._to_frame(rows, columns), columns, tidy=tidy, as_polars=as_polars
        )

    def _paginate(
        self,
        route: str,
        *,
        columns: list[str],
        facets: Mapping[str, str | Iterable[str]] | None,
        frequency: str | None,
        start: str | None,
        end: str | None,
        sort: Sequence[tuple[str, str]],
        offset: int,
        page: int,
        max_rows: int | None,
    ) -> list[dict[str, Any]]:
        """
        Fetch successive pages of a data query and accumulate the rows.

        Loops the ``/data`` endpoint, advancing ``offset`` by the page size until
        a short page arrives, the reported ``total`` is reached, or ``max_rows``
        is collected. See :meth:`get_data` for the argument meanings.

        Args:
            route (str): Normalised dataset route path.
            columns (list[str]): Resolved value-column ids to request.
            facets (Mapping[str, str | Iterable[str]] | None): Facet filters.
            frequency (str | None): Requested periodicity.
            start (str | None): Inclusive lower period bound.
            end (str | None): Inclusive upper period bound.
            sort (Sequence[tuple[str, str]]): Sort keys.
            offset (int): Starting row offset.
            page (int): Page size (already capped at 5000).
            max_rows (int | None): Optional cap on total rows collected.

        Returns:
            list[dict[str, Any]]: The accumulated raw row dictionaries.
        """
        rows: list[dict[str, Any]] = []
        pages = 0
        while True:
            extra = self._data_params(
                columns=columns,
                facets=facets,
                frequency=frequency,
                start=start,
                end=end,
                sort=sort,
                offset=offset,
                length=page,
            )
            resp = self._request(f"{route}/data", self._params(extra))
            batch = resp.get("data", []) or []
            rows.extend(batch)
            pages += 1
            total = resp.get("total")
            _log.debug(
                "%s: page %d, %d rows (%d of %s so far)",
                route,
                pages,
                len(batch),
                len(rows),
                total if total is not None else "?",
            )

            # Stop once we have enough for the caller's cap.
            if max_rows is not None and len(rows) >= max_rows:
                rows = rows[:max_rows]
                break
            # A short page means the server had nothing more to give.
            if len(batch) < page:
                break
            # Otherwise advance; also stop if we have reached the reported total.
            offset += page
            if total is not None and offset >= int(total):
                break
        _log.info("Fetched %d rows from %s in %d page(s)", len(rows), route, pages)
        return rows

    @staticmethod
    def _data_params(
        *,
        columns: list[str],
        facets: Mapping[str, str | Iterable[str]] | None,
        frequency: str | None,
        start: str | None,
        end: str | None,
        sort: Sequence[tuple[str, str]],
        offset: int,
        length: int,
    ) -> list[tuple[str, str]]:
        """
        Encode a data query into the API's bracketed query parameters.

        The EIA API expects repeated bracketed keys (``data[]``,
        ``facets[<id>][]``, ``sort[<i>][column]``), which are represented here as
        a list of (name, value) pairs so ``requests`` sends each occurrence.

        Args:
            columns (list[str]): Value-column ids for ``data[]``.
            facets (Mapping[str, str | Iterable[str]] | None): Facet filters.
            frequency (str | None): Requested periodicity.
            start (str | None): Inclusive lower period bound.
            end (str | None): Inclusive upper period bound.
            sort (Sequence[tuple[str, str]]): Sort keys.
            offset (int): Row offset.
            length (int): Page size.

        Returns:
            list[tuple[str, str]]: The encoded query parameters (without the key).
        """
        return _encode_data_params(
            columns=columns,
            facets=facets,
            frequency=frequency,
            start=start,
            end=end,
            sort=sort,
            offset=offset,
            length=length,
        )

    @staticmethod
    def _to_frame(rows: list[dict[str, Any]], columns: list[str]) -> pd.DataFrame:
        """
        Assemble the tidy DataFrame from accumulated rows.

        Coerces the requested value columns to numbers (the API returns them as
        strings) and inserts a parsed ``date`` column ahead of the raw
        ``period``. An empty result yields an empty frame.

        Args:
            rows (list[dict[str, Any]]): Accumulated raw row dictionaries.
            columns (list[str]): The requested value-column ids to coerce.

        Returns:
            pd.DataFrame: The tidy result described in :meth:`get_data`.
        """
        return _rows_to_frame(rows, columns)

    # ------------------------------------------------------------------ #
    # Export
    # ------------------------------------------------------------------ #
    def to_csv(self, path: str | Path, **kwargs: Any) -> Path:
        """
        Fetch data and write it to a CSV file in one step.

        A thin convenience wrapper around :meth:`get_data` that forwards its
        keyword arguments and persists the tidy result.

        Args:
            path (str | Path): Destination CSV path.
            **kwargs (Any): Forwarded verbatim to :meth:`get_data` (``route``,
                ``data``, ``facets``, ``frequency``, ``start``, ``end``, ``tidy``, ...).

        Returns:
            Path: The path that was written.
        """
        frame = self.get_data(**kwargs)
        path = write_frame(frame, path, "csv")
        _log.info("Wrote %d rows to %s", len(frame), path)
        return path

    def to_excel(self, path: str | Path, **kwargs: Any) -> Path:
        """
        Fetch data and write it to an Excel (.xlsx) workbook in one step.

        The Excel counterpart of :meth:`to_csv`, using the bundled ``openpyxl``
        engine. The parsed ``date`` column is written as plain calendar days so
        Excel does not show a spurious midnight time component.

        Args:
            path (str | Path): Destination ``.xlsx`` path.
            **kwargs (Any): Forwarded verbatim to :meth:`get_data` (including
                ``tidy=True``).

        Returns:
            Path: The path that was written.
        """
        frame = self.get_data(**kwargs).copy()
        path = Path(path)
        # Excel renders a datetime cell with a time component; convert the parsed
        # dates to plain ``date`` objects so Excel shows just the day.
        if "date" in frame.columns:
            frame["date"] = pd.to_datetime(frame["date"]).dt.date
        write_frame(frame, path, "excel")
        _log.info("Wrote %d rows to %s", len(frame), path)
        return path

    def to_parquet(self, path: str | Path, **kwargs: Any) -> Path:
        """
        Fetch data and write it to a Parquet file in one step.

        The Parquet counterpart of :meth:`to_csv`, using the bundled ``pyarrow``
        engine. Handy for dropping extracts straight into a data pipeline.

        Args:
            path (str | Path): Destination ``.parquet`` path.
            **kwargs (Any): Forwarded verbatim to :meth:`get_data` (including
                ``tidy=True``).

        Returns:
            Path: The path that was written.
        """
        frame = self.get_data(**kwargs)
        path = write_frame(frame, path, "parquet")
        _log.info("Wrote %d rows to %s", len(frame), path)
        return path

    def to_json(self, path: str | Path, **kwargs: Any) -> Path:
        """
        Fetch data and write it to a JSON file in one step.

        Writes a list of record objects (one per row), with dates in ISO format.

        Args:
            path (str | Path): Destination ``.json`` path.
            **kwargs (Any): Forwarded verbatim to :meth:`get_data` (including
                ``tidy=True``).

        Returns:
            Path: The path that was written.
        """
        frame = self.get_data(**kwargs)
        path = write_frame(frame, path, "json")
        _log.info("Wrote %d rows to %s", len(frame), path)
        return path

    # ------------------------------------------------------------------ #
    # Incremental update
    # ------------------------------------------------------------------ #
    @classmethod
    def _key_columns(cls, frame: pd.DataFrame, value_cols: Iterable[str]) -> list[str]:
        """
        Return the columns that identify a row (period plus facet columns).

        A row's identity is everything that is not a measured value: the parsed
        ``date`` is derived from ``period`` and so is excluded, as are the
        requested value columns and their ``<column>-units`` companions.

        Args:
            frame (pd.DataFrame): A fetched (or stored) long frame.
            value_cols (Iterable[str]): The requested value-column ids.

        Returns:
            list[str]: The identifying (key) columns present in ``frame``.
        """
        values = set(value_cols)
        return [
            c
            for c in frame.columns
            if c != "date" and c not in values and not c.endswith(cls._UNITS_SUFFIX)
        ]

    @classmethod
    def _row_key(
        cls, frame: pd.DataFrame, value_cols: list[str], *, tidy: bool
    ) -> tuple[list[str], list[str]]:
        """
        Return a frame's key columns and the value columns compared for revisions.

        In the default shape the key is ``period`` plus the facet columns and the
        compared values are the requested measures. In the tidy shape there is no
        ``period``, so ``date`` joins the key (dimensions plus ``variable`` plus
        ``date``) and the only compared value is ``value``.

        Args:
            frame (pd.DataFrame): A fetched frame in either shape.
            value_cols (list[str]): The requested value-column ids.
            tidy (bool): Whether ``frame`` is in the tidy shape.

        Returns:
            tuple[list[str], list[str]]: The key columns and the value columns.
        """
        if tidy:
            keys = [c for c in frame.columns if c not in _TIDY_VALUE_COLUMNS]
            return keys, ["value"]
        return cls._key_columns(frame, value_cols), value_cols

    def new_observations(
        self,
        existing: pd.DataFrame | str | Path,
        route: str,
        data: str | Iterable[str] | None = None,
        *,
        include_revisions: bool = True,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """
        Fetch only the observations missing from an already-extracted set.

        Given what has already been pulled (an in-memory long DataFrame or a path
        to a long CSV), this fetches the latest data and returns only the rows not
        already held: newer periods for tracked facet combinations and every row
        of combinations never extracted. With ``include_revisions`` (the default)
        it also returns values EIA has since revised.

        This is the file-free building block behind :meth:`update_csv`; reach for
        it when the delta should be returned in memory to append to a custom store
        (a database, a different file format, and so on). Pass ``tidy=True`` when
        ``existing`` is in the tidy shape.

        Args:
            existing (pd.DataFrame | str | Path): The prior extract, as a long
                DataFrame or a path to a long CSV.
            route (str): Dataset route path.
            data (str | Iterable[str] | None): Value column(s) to fetch (see
                :meth:`get_data`).
            include_revisions (bool): Also include values EIA has revised, not
                just entirely new observations.
            **kwargs (Any): Further filters forwarded to :meth:`get_data`
                (``facets``, ``frequency``, ``start``, ``end``, ``tidy``, ...).

        Returns:
            pd.DataFrame: A long DataFrame of just the missing (and optionally
            revised) rows; empty when ``existing`` is already up to date.
        """
        value_cols = self._resolve_data(route, data)
        fresh: pd.DataFrame = self.get_data(
            route, data=value_cols, as_polars=False, **kwargs
        )
        key_cols, compare_cols = self._row_key(
            fresh, value_cols, tidy=bool(kwargs.get("tidy"))
        )
        existing_frame = read_existing(
            existing, fresh, compare_cols, text_cols=["period"]
        )
        new = select_new(
            fresh,
            existing_frame,
            key_cols,
            compare_cols,
            include_revisions=include_revisions,
        )
        _log.info(
            "%s: %d new/revised rows versus the existing extract", route, len(new)
        )
        return new

    def update_csv(
        self,
        path: str | Path,
        route: str,
        data: str | Iterable[str] | None = None,
        **kwargs: Any,
    ) -> pd.DataFrame:
        """
        Incrementally update a long-format CSV with new and revised rows.

        On the first run (the file does not yet exist) this writes the full
        extract. On later runs it reads what was already saved, fetches the latest
        data, and upserts on the row key (period plus facet columns; in the tidy
        shape the dimensions, ``variable`` and ``date``) — appending new
        observations and overwriting any values EIA has since revised, while
        keeping stored history the fresh pull no longer covers. Re-running it
        keeps recently published periods correct rather than frozen at their
        first, preliminary value. Use the same ``tidy`` setting on every run.

        Args:
            path (str | Path): Long-format CSV to create or update in place.
            route (str): Dataset route path.
            data (str | Iterable[str] | None): Value column(s) to fetch (see
                :meth:`get_data`).
            **kwargs (Any): Further filters forwarded to :meth:`get_data`
                (``facets``, ``frequency``, ``start``, ``end``, ``tidy``, ...).

        Returns:
            pd.DataFrame: The rows that were newly added or revised; empty when
            the file was already up to date.
        """
        path = Path(path)
        tidy = bool(kwargs.get("tidy"))
        value_cols = self._resolve_data(route, data)
        fresh: pd.DataFrame = self.get_data(
            route, data=value_cols, as_polars=False, **kwargs
        )

        # First run: nothing on disk yet, so write the whole extract and return
        # it (every row is "new").
        if not path.exists():
            fresh.to_csv(path, index=False)
            _log.info("Created %s with %d rows", path, len(fresh))
            return fresh

        key_cols, compare_cols = self._row_key(fresh, value_cols, tidy=tidy)
        existing = read_existing(path, fresh, compare_cols, text_cols=["period"])
        changed = select_new(fresh, existing, key_cols, compare_cols)
        if changed.empty:
            # Already up to date; leave the file untouched.
            _log.info("%s is already up to date", path)
            return changed

        # Upsert: the fresh row wins wherever a (string) key collides, so revised
        # values overwrite stale ones while history absent from ``fresh`` is
        # kept. The default shape sorts by its key (period first); the tidy shape
        # by its dimensions, then date.
        sort_cols = (
            [*(c for c in key_cols if c != "date"), "date"] if tidy else key_cols
        )
        combined = upsert(existing, fresh, key_cols, sort_cols)
        combined.to_csv(path, index=False)
        _log.info(
            "Upserted %d new/revised rows into %s (%d rows total)",
            len(changed),
            path,
            len(combined),
        )
        return changed


class AsyncEIA:
    """
    Async counterpart of :class:`EIA`, backed by ``httpx.AsyncClient``.

    Mirrors the read surface of :class:`EIA` — route browsing, metadata
    inspection and :meth:`get_data` (with the same automatic pagination, facet
    encoding, number coercion and parsed ``date`` column) — but every network
    method is a coroutine, so many datasets can be fetched concurrently with
    ``asyncio.gather``. On-disk response caching and client-side throttling behave
    exactly as in the sync client, and the two share their cache directory and
    file format.

    Use it as an async context manager so the underlying HTTP client is closed
    cleanly (or call :meth:`aclose` yourself)::

        async with AsyncEIA() as eia:
            monthly, retail = await asyncio.gather(
                eia.get_data("natural-gas/pri/fut", frequency="monthly"),
                eia.get_data("electricity/retail-sales", data="price"),
            )

    It is also reachable as :attr:`EIA.AsyncAPI` for parity with the sync client.

    Args:
        api_key (str | None): EIA API key; falls back to ``EIA_API_KEY``.
        cache_dir (str | Path | _DefaultCacheDir | None): Directory used to
            cache raw API responses. The default, :data:`~eia_api.DEFAULT_CACHE_DIR`,
            means ``~/.cache/eia_api`` (resolved when the client is built).
            ``None`` (or ``""``) disables the on-disk cache.
        cache_ttl_hours (float): Freshness window for cached responses.
        timeout (float): Per-request timeout in seconds.
        requests_per_second (float | None): Network-request throttle, or ``None``
            to disable it.
        client (httpx.AsyncClient | None): Optional pre-built async client. When
            omitted one is created lazily and closed by :meth:`aclose`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        cache_dir: str | Path | _DefaultCacheDir | None = DEFAULT_CACHE_DIR,
        cache_ttl_hours: float = 24.0,
        timeout: float = 60.0,
        requests_per_second: float | None = 9.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """
        Initialise the async client; see the class docstring for the arguments.

        Args:
            api_key (str | None): API key, or ``None`` to read ``EIA_API_KEY``.
            cache_dir (str | Path | _DefaultCacheDir | None): Cache directory;
                :data:`~eia_api.DEFAULT_CACHE_DIR` for ``~/.cache/eia_api``, ``None``
                (or ``""``) to disable disk caching.
            cache_ttl_hours (float): Freshness window for cached responses.
            timeout (float): Per-request timeout in seconds.
            requests_per_second (float | None): Network-request throttle, or
                ``None`` to disable it.
            client (httpx.AsyncClient | None): Optional pre-built async client.
        """
        self.api_key = api_key or os.environ.get(_ENV_KEY)
        _register_secret(self.api_key)
        self.cache_dir = _resolve_cache_dir(cache_dir)
        if self.cache_dir:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.cache_ttl_seconds = cache_ttl_hours * 3600.0
        self.timeout = timeout
        self._throttler = AsyncThrottle(requests_per_second, _log)
        # A caller-supplied client is never closed by us; a lazily-created one is.
        self._client = client
        self._owns_client = client is None
        self._meta_cache: dict[str, dict[str, Any]] = {}

    async def __aenter__(self) -> AsyncEIA:  # ruff: ignore[non-self-return-type] - concrete class is fine
        """
        Enter the async context.

        Returns:
            AsyncEIA: This client, unchanged.
        """
        return self

    async def __aexit__(self, *_exc: object) -> None:
        """Close the owned HTTP client when leaving the context."""
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client if this instance created it."""
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    def _params(self, extra: Iterable[tuple[str, str]] = ()) -> list[tuple[str, str]]:
        """
        Build the base parameter list, injecting the (validated) API key.

        Args:
            extra (Iterable[tuple[str, str]]): Additional (name, value) pairs to
                append after the ``api_key`` pair.

        Returns:
            list[tuple[str, str]]: The parameter list starting with the API key.

        Raises:
            ValueError: If no API key was configured.
        """
        if not self.api_key:
            msg = (
                "No EIA API key configured. Pass api_key=... or set the "
                f"{_ENV_KEY} environment variable. Register for a free key at "
                "https://www.eia.gov/opendata/."
            )
            raise ValueError(msg)
        return [("api_key", self.api_key), *extra]

    async def _throttle(self) -> None:
        """
        Async throttle: ``await asyncio.sleep`` to honour requests_per_second.

        Mirrors :meth:`EIA._throttle` but yields to the event loop instead of
        blocking it, so concurrent tasks share the rate budget cooperatively.
        """
        await self._throttler.await_turn()

    async def _request(
        self, endpoint: str, params: Sequence[tuple[str, str]]
    ) -> dict[str, Any]:
        """
        Perform one async GET against the API and return its ``response`` object.

        The sync/async twin of :meth:`EIA._request`: it serves a fresh cached
        payload when one exists within the TTL; otherwise throttles, calls the
        API over httpx, surfaces the API's top-level ``error`` field as a clear
        exception, persists the payload and returns it.

        Args:
            endpoint (str): The route path to request, below the base URL and
                already including any ``/data`` or ``/facet/<id>`` suffix.
            params (Sequence[tuple[str, str]]): Request parameters as
                (name, value) pairs. The ``api_key`` pair must be present.

        Returns:
            dict[str, Any]: The decoded ``response`` object from the payload.

        Raises:
            RuntimeError: If the API returns an ``error`` field.
        """
        cache = _disk_cache(self.cache_dir, self.cache_ttl_seconds)
        hit, cached = cache.get(endpoint, params)
        if hit:
            cached_response: dict[str, Any] = cached["response"]
            return cached_response

        # Only real network requests are throttled; cache hits returned above.
        await self._throttle()
        if self._client is None:
            self._client = httpx.AsyncClient(headers={"User-Agent": _USER_AGENT})
            self._owns_client = True
        _log.debug("GET %s %s", endpoint, _loggable(params))
        started = time.monotonic()
        resp = await self._client.get(
            _BASE_URL + endpoint, params=list(params), timeout=self.timeout
        )
        _log.debug(
            "HTTP %s for %s in %.2fs",
            resp.status_code,
            endpoint,
            time.monotonic() - started,
        )
        payload = resp.json()
        if isinstance(payload, dict) and payload.get("error"):
            msg = f"EIA API error: {payload['error']} (code {payload.get('code')})"
            raise RuntimeError(msg)
        resp.raise_for_status()

        cache.put(endpoint, params, payload)
        response: dict[str, Any] = payload["response"]
        return response

    async def metadata(self, route: str = "") -> dict[str, Any]:
        """
        Return the raw metadata object for a route (async :meth:`EIA.metadata`).

        Args:
            route (str): Route path, ``""`` for the top level. Any trailing
                ``/data`` is ignored.

        Returns:
            dict[str, Any]: The decoded metadata ``response`` object.
        """
        route = _norm_route(route)
        if route in self._meta_cache:
            return self._meta_cache[route]
        meta = await self._request(route, self._params())
        self._meta_cache[route] = meta
        return meta

    async def browse(self, route: str = "") -> pd.DataFrame:
        """
        List the child routes beneath a route (async :meth:`EIA.browse`).

        Args:
            route (str): Route path to list beneath (``""`` for the top level).

        Returns:
            pd.DataFrame: Columns ``[id, name, description]`` (empty at a leaf).
        """
        return _routes_frame(await self.metadata(route))

    async def frequencies(self, route: str) -> pd.DataFrame:
        """
        List a dataset's frequencies (async :meth:`EIA.frequencies`).

        Args:
            route (str): Dataset route path.

        Returns:
            pd.DataFrame: One row per supported frequency.
        """
        meta = await self.metadata(route)
        return pd.DataFrame(meta.get("frequency", []))

    async def facets(self, route: str) -> pd.DataFrame:
        """
        List a dataset's facets (async :meth:`EIA.facets`).

        Args:
            route (str): Dataset route path.

        Returns:
            pd.DataFrame: One row per facet (filtering dimension).
        """
        meta = await self.metadata(route)
        return pd.DataFrame(meta.get("facets", []))

    async def data_columns(self, route: str) -> pd.DataFrame:
        """
        List a dataset's value columns (async :meth:`EIA.data_columns`).

        Args:
            route (str): Dataset route path.

        Returns:
            pd.DataFrame: Columns ``[id, alias, units]``, one row per column.
        """
        return _data_columns_frame(await self.metadata(route))

    async def facet_values(self, route: str, facet_id: str) -> pd.DataFrame:
        """
        List a facet's allowed values (async :meth:`EIA.facet_values`).

        Args:
            route (str): Dataset route path.
            facet_id (str): The facet whose values to enumerate.

        Returns:
            pd.DataFrame: One row per facet value.
        """
        route = _norm_route(route)
        meta = await self._request(f"{route}/facet/{facet_id}", self._params())
        return pd.DataFrame(meta.get("facets", []))

    async def _resolve_data(
        self, route: str, data: str | Iterable[str] | None
    ) -> list[str]:
        """
        Resolve requested value columns, defaulting to all (async twin).

        Args:
            route (str): Dataset route path.
            data (str | Iterable[str] | None): One column, several, or ``None``.

        Returns:
            list[str]: The value-column ids to request.
        """
        if data is None:
            frame = await self.data_columns(route)
            return frame["id"].tolist()
        if isinstance(data, str):
            return [data]
        return list(data)

    @overload
    async def get_data(
        self,
        route: str,
        data: str | Iterable[str] | None = ...,
        facets: Mapping[str, str | Iterable[str]] | None = ...,
        frequency: str | None = ...,
        start: str | None = ...,
        end: str | None = ...,
        sort: Sequence[tuple[str, str]] | None = ...,
        offset: int = ...,
        length: int = ...,
        max_rows: int | None = ...,
        *,
        tidy: bool = ...,
        as_polars: Literal[False] = ...,
    ) -> pd.DataFrame: ...

    @overload
    async def get_data(
        self,
        route: str,
        data: str | Iterable[str] | None = ...,
        facets: Mapping[str, str | Iterable[str]] | None = ...,
        frequency: str | None = ...,
        start: str | None = ...,
        end: str | None = ...,
        sort: Sequence[tuple[str, str]] | None = ...,
        offset: int = ...,
        length: int = ...,
        max_rows: int | None = ...,
        *,
        tidy: bool = ...,
        as_polars: Literal[True],
    ) -> pl.DataFrame: ...

    @overload
    async def get_data(
        self,
        route: str,
        data: str | Iterable[str] | None = ...,
        facets: Mapping[str, str | Iterable[str]] | None = ...,
        frequency: str | None = ...,
        start: str | None = ...,
        end: str | None = ...,
        sort: Sequence[tuple[str, str]] | None = ...,
        offset: int = ...,
        length: int = ...,
        max_rows: int | None = ...,
        *,
        tidy: bool = ...,
        as_polars: bool,
    ) -> pd.DataFrame | pl.DataFrame: ...

    async def get_data(
        self,
        route: str,
        data: str | Iterable[str] | None = None,
        facets: Mapping[str, str | Iterable[str]] | None = None,
        frequency: str | None = None,
        start: str | None = None,
        end: str | None = None,
        sort: Sequence[tuple[str, str]] | None = None,
        offset: int = 0,
        length: int = _MAX_LENGTH,
        max_rows: int | None = None,
        *,
        tidy: bool = False,
        as_polars: bool = False,
    ) -> pd.DataFrame | pl.DataFrame:
        """
        Fetch a dataset's rows into a tidy DataFrame (async :meth:`EIA.get_data`).

        Behaves exactly like the sync method — same arguments, pagination, facet
        encoding, number coercion and parsed ``date`` column — but awaits each
        page, so multiple ``get_data`` calls can run concurrently.

        Args:
            route (str): Dataset route path; any trailing ``/data`` is ignored.
            data (str | Iterable[str] | None): Value column(s); ``None`` for all.
            facets (Mapping[str, str | Iterable[str]] | None): Facet filters.
            frequency (str | None): Periodicity; ``None`` uses the default.
            start (str | None): Inclusive lower period bound.
            end (str | None): Inclusive upper period bound.
            sort (Sequence[tuple[str, str]] | None): Sort keys; defaults to
                ``[("period", "asc")]`` for stable pagination.
            offset (int): Starting row offset (pagination is automatic).
            length (int): Page size per request, capped at 5000.
            max_rows (int | None): Stop after this many rows; ``None`` for all.
            tidy (bool): Return the tidy long shape (see :meth:`EIA.get_data`).
            as_polars (bool): Return a ``polars.DataFrame``; needs the optional
                ``polars`` extra.

        Returns:
            pd.DataFrame | pl.DataFrame: The result, identical in shape to the
            sync client for the same arguments.

        Raises:
            ValueError: If ``length`` is not positive.
        """
        if length <= 0:
            msg = f"length must be a positive integer, got {length}."
            raise ValueError(msg)

        route = _norm_route(route)
        columns = await self._resolve_data(route, data)
        sort = sort or [("period", "asc")]
        page = min(length, _MAX_LENGTH)

        rows: list[dict[str, Any]] = []
        pages = 0
        while True:
            extra = _encode_data_params(
                columns=columns,
                facets=facets,
                frequency=frequency,
                start=start,
                end=end,
                sort=sort,
                offset=offset,
                length=page,
            )
            resp = await self._request(f"{route}/data", self._params(extra))
            batch = resp.get("data", []) or []
            rows.extend(batch)
            pages += 1
            total = resp.get("total")
            _log.debug(
                "%s: page %d, %d rows (%d of %s so far)",
                route,
                pages,
                len(batch),
                len(rows),
                total if total is not None else "?",
            )
            if max_rows is not None and len(rows) >= max_rows:
                rows = rows[:max_rows]
                break
            if len(batch) < page:
                break
            offset += page
            if total is not None and offset >= int(total):
                break
        _log.info("Fetched %d rows from %s in %d page(s)", len(rows), route, pages)
        return _finish(
            _rows_to_frame(rows, columns), columns, tidy=tidy, as_polars=as_polars
        )


# The async client is also reachable as ``EIA.AsyncAPI`` for parity with the
# sync entry point (mirrors fedfred's ``FredAPI.AsyncAPI``).
EIA.AsyncAPI = AsyncEIA


# ---------------------------------------------------------------------- #
# CLI
# ---------------------------------------------------------------------- #
def _configure_logging(level: str = "WARNING", log_file: str | None = None) -> None:
    """
    Attach the CLI's console (and optional file) handlers to the package logger.

    Only the command line calls this; the library itself never configures
    logging. The console handler writes colored records to stderr, so stdout
    keeps only the command's output; the file handler, when requested, appends
    every record down to DEBUG as plain text. Calling it again replaces the
    handlers it added earlier rather than stacking duplicates.

    Args:
        level (str): Console threshold, one of ``DEBUG``, ``INFO``, ``WARNING``
            or ``ERROR`` (case-insensitive).
        log_file (str | None): Path of a log file to append to, or ``None`` for
            console logging only.
    """
    configure_logging(_log, level, log_file, flag=_CLI_HANDLER_FLAG)


# Fire parses number-like arguments (``--start 2024`` arrives as an int); the
# CLI passes period bounds through as text.
_as_text = as_text


class _Cli:
    """
    Command-line facade over :class:`EIA`.

    `python-fire <https://github.com/google/python-fire>`_ turns each method here
    into a subcommand and each parameter into a flag, so the CLI mirrors the
    library without any hand-written argument parsing. The methods deliberately
    return display-ready text (a formatted table or a status line) rather than
    raw DataFrames, because Fire would otherwise introspect a returned DataFrame
    and print its attributes instead of the data.

    The constructor's parameters become global flags, accepted with any
    command: ``--no-cache``, ``--log-level`` and ``--log-file``.
    """

    def __init__(
        self,
        log_level: str = "WARNING",
        log_file: str | None = None,
        no_cache: bool = False,
    ) -> None:
        """
        Set up CLI logging and a default :class:`EIA` client.

        Tests replace the ``_eia`` attribute with a network-mocked client.

        Args:
            log_level (str): Console log threshold: ``DEBUG``, ``INFO``,
                ``WARNING`` (default, quiet) or ``ERROR``. Log lines go to
                stderr, so command output on stdout stays clean.
            log_file (str | None): Append a full DEBUG log with timestamps to
                this file. A bare filename is written into ``outputs/``.
            no_cache (bool): Disable the on-disk response cache (``--no-cache``);
                by default responses are cached in the default directory.
        """
        _configure_logging(log_level, self._resolve_out(log_file) if log_file else None)
        self._eia = EIA(cache_dir=None) if no_cache else EIA()

    def browse(self, route: str = "") -> str:
        """
        List the child routes beneath a route as a printable table.

        Args:
            route (str): Route path to list beneath (``""`` for the top level).

        Returns:
            str: The child routes rendered as text.
        """
        return self._eia.browse(route).to_string(index=False)

    def frequencies(self, route: str) -> str:
        """
        Print the frequencies a dataset supports.

        Args:
            route (str): Dataset route path.

        Returns:
            str: The frequencies rendered as text.
        """
        return self._eia.frequencies(route).to_string(index=False)

    def facets(self, route: str) -> str:
        """
        Print the facets (filtering dimensions) a dataset exposes.

        Args:
            route (str): Dataset route path.

        Returns:
            str: The facets rendered as text.
        """
        return self._eia.facets(route).to_string(index=False)

    def data_columns(self, route: str) -> str:
        """
        Print the value columns a dataset offers for ``--data``.

        Args:
            route (str): Dataset route path.

        Returns:
            str: The value columns rendered as text.
        """
        return self._eia.data_columns(route).to_string(index=False)

    def facet_values(self, route: str, facet_id: str) -> str:
        """
        Print the allowed values of a single facet.

        Args:
            route (str): Dataset route path.
            facet_id (str): The facet whose values to enumerate.

        Returns:
            str: The facet values rendered as text.
        """
        return self._eia.facet_values(route, facet_id).to_string(index=False)

    @staticmethod
    def _resolve_out(path: str) -> str:
        """
        Route a bare filename into the ``outputs/`` folder.

        Args:
            path (str): The user-supplied destination path.

        Returns:
            str: ``outputs/<name>`` for a bare name (the folder is created),
            otherwise ``path`` unchanged.
        """
        return resolve_out(path)

    def get_data(
        self,
        route: str,
        data: str | list[str] | None = None,
        facets: dict[str, str | list[str]] | None = None,
        frequency: str | None = None,
        start: str | None = None,
        end: str | None = None,
        offset: int = 0,
        length: int = _MAX_LENGTH,
        max_rows: int | None = None,
        out: str | None = None,
        *,
        tidy: bool = False,
    ) -> str:
        """
        Fetch data and either print it or write it to ``out``.

        Args:
            route (str): Dataset route path.
            data (str | list[str] | None): Value column(s); omit for all.
            facets (dict[str, str | list[str]] | None): Facet filters, e.g.
                ``'{"stateid": "CO"}'`` on the command line.
            frequency (str | None): Requested periodicity.
            start (str | None): Inclusive lower period bound.
            end (str | None): Inclusive upper period bound.
            offset (int): Starting row offset.
            length (int): Page size per request (capped at 5000).
            max_rows (int | None): Stop after this many rows.
            out (str | None): If given, write to this path instead of printing.
                The extension selects the format: ``.xlsx``/``.xls`` Excel,
                ``.parquet`` Parquet, ``.json`` JSON, anything else CSV.
            tidy (bool): Write/print the tidy long shape (``date``, dimensions,
                ``variable``, ``value``, ``unit``) instead of the default one.

        Returns:
            str: The rendered table, or a status line when ``out`` is used.
        """
        if out is None:
            frame = self._eia.get_data(
                route,
                data=data,
                facets=facets,
                frequency=frequency,
                start=_as_text(start),
                end=_as_text(end),
                offset=offset,
                length=length,
                max_rows=max_rows,
                tidy=tidy,
            )
            return frame.to_string(index=False)
        out = self._resolve_out(out)
        low = out.lower()
        # Route by extension so the on-disk format matches the chosen suffix.
        if low.endswith((".xlsx", ".xls")):
            writer = self._eia.to_excel
        elif low.endswith(".parquet"):
            writer = self._eia.to_parquet
        elif low.endswith(".json"):
            writer = self._eia.to_json
        else:
            writer = self._eia.to_csv
        writer(
            out,
            route=route,
            data=data,
            facets=facets,
            frequency=frequency,
            start=_as_text(start),
            end=_as_text(end),
            offset=offset,
            length=length,
            max_rows=max_rows,
            tidy=tidy,
        )
        return f"Wrote {out}"

    def to_csv(
        self,
        path: str,
        route: str,
        data: str | list[str] | None = None,
        facets: dict[str, str | list[str]] | None = None,
        frequency: str | None = None,
        start: str | None = None,
        end: str | None = None,
        max_rows: int | None = None,
        *,
        tidy: bool = False,
    ) -> str:
        """
        Write data to a CSV file and report where it went.

        Args:
            path (str): Destination CSV path.
            route (str): Dataset route path.
            data (str | list[str] | None): Value column(s); omit for all.
            facets (dict[str, str | list[str]] | None): Facet filters.
            frequency (str | None): Requested periodicity.
            start (str | None): Inclusive lower period bound.
            end (str | None): Inclusive upper period bound.
            max_rows (int | None): Stop after this many rows.
            tidy (bool): Write/print the tidy long shape (``date``, dimensions,
                ``variable``, ``value``, ``unit``) instead of the default one.

        Returns:
            str: A status line naming the file written.
        """
        path = self._resolve_out(path)
        self._eia.to_csv(
            path,
            route=route,
            data=data,
            facets=facets,
            frequency=frequency,
            start=_as_text(start),
            end=_as_text(end),
            max_rows=max_rows,
            tidy=tidy,
        )
        return f"Wrote {path}"

    def to_excel(
        self,
        path: str,
        route: str,
        data: str | list[str] | None = None,
        facets: dict[str, str | list[str]] | None = None,
        frequency: str | None = None,
        start: str | None = None,
        end: str | None = None,
        max_rows: int | None = None,
        *,
        tidy: bool = False,
    ) -> str:
        """
        Write data to an Excel (.xlsx) workbook and report where it went.

        Args:
            path (str): Destination ``.xlsx`` path.
            route (str): Dataset route path.
            data (str | list[str] | None): Value column(s); omit for all.
            facets (dict[str, str | list[str]] | None): Facet filters.
            frequency (str | None): Requested periodicity.
            start (str | None): Inclusive lower period bound.
            end (str | None): Inclusive upper period bound.
            max_rows (int | None): Stop after this many rows.
            tidy (bool): Write/print the tidy long shape (``date``, dimensions,
                ``variable``, ``value``, ``unit``) instead of the default one.

        Returns:
            str: A status line naming the file written.
        """
        path = self._resolve_out(path)
        self._eia.to_excel(
            path,
            route=route,
            data=data,
            facets=facets,
            frequency=frequency,
            start=_as_text(start),
            end=_as_text(end),
            max_rows=max_rows,
            tidy=tidy,
        )
        return f"Wrote {path}"

    def to_parquet(
        self,
        path: str,
        route: str,
        data: str | list[str] | None = None,
        facets: dict[str, str | list[str]] | None = None,
        frequency: str | None = None,
        start: str | None = None,
        end: str | None = None,
        max_rows: int | None = None,
        *,
        tidy: bool = False,
    ) -> str:
        """
        Write data to a Parquet file and report where it went.

        Args:
            path (str): Destination ``.parquet`` path.
            route (str): Dataset route path.
            data (str | list[str] | None): Value column(s); omit for all.
            facets (dict[str, str | list[str]] | None): Facet filters.
            frequency (str | None): Requested periodicity.
            start (str | None): Inclusive lower period bound.
            end (str | None): Inclusive upper period bound.
            max_rows (int | None): Stop after this many rows.
            tidy (bool): Write/print the tidy long shape (``date``, dimensions,
                ``variable``, ``value``, ``unit``) instead of the default one.

        Returns:
            str: A status line naming the file written.
        """
        path = self._resolve_out(path)
        self._eia.to_parquet(
            path,
            route=route,
            data=data,
            facets=facets,
            frequency=frequency,
            start=_as_text(start),
            end=_as_text(end),
            max_rows=max_rows,
            tidy=tidy,
        )
        return f"Wrote {path}"

    def to_json(
        self,
        path: str,
        route: str,
        data: str | list[str] | None = None,
        facets: dict[str, str | list[str]] | None = None,
        frequency: str | None = None,
        start: str | None = None,
        end: str | None = None,
        max_rows: int | None = None,
        *,
        tidy: bool = False,
    ) -> str:
        """
        Write data to a JSON file and report where it went.

        Args:
            path (str): Destination ``.json`` path.
            route (str): Dataset route path.
            data (str | list[str] | None): Value column(s); omit for all.
            facets (dict[str, str | list[str]] | None): Facet filters.
            frequency (str | None): Requested periodicity.
            start (str | None): Inclusive lower period bound.
            end (str | None): Inclusive upper period bound.
            max_rows (int | None): Stop after this many rows.
            tidy (bool): Write/print the tidy long shape (``date``, dimensions,
                ``variable``, ``value``, ``unit``) instead of the default one.

        Returns:
            str: A status line naming the file written.
        """
        path = self._resolve_out(path)
        self._eia.to_json(
            path,
            route=route,
            data=data,
            facets=facets,
            frequency=frequency,
            start=_as_text(start),
            end=_as_text(end),
            max_rows=max_rows,
            tidy=tidy,
        )
        return f"Wrote {path}"

    def update_csv(
        self,
        path: str,
        route: str,
        data: str | list[str] | None = None,
        facets: dict[str, str | list[str]] | None = None,
        frequency: str | None = None,
        start: str | None = None,
        end: str | None = None,
        *,
        tidy: bool = False,
    ) -> str:
        """
        Incrementally update a long-format CSV and report the row count.

        Args:
            path (str): Long-format CSV to create or update.
            route (str): Dataset route path.
            data (str | list[str] | None): Value column(s); omit for all.
            facets (dict[str, str | list[str]] | None): Facet filters.
            frequency (str | None): Requested periodicity.
            start (str | None): Inclusive lower period bound.
            end (str | None): Inclusive upper period bound.
            tidy (bool): Write/print the tidy long shape (``date``, dimensions,
                ``variable``, ``value``, ``unit``) instead of the default one.

        Returns:
            str: A status line with the number of rows added or revised.
        """
        path = self._resolve_out(path)
        changed = self._eia.update_csv(
            path,
            route=route,
            data=data,
            facets=facets,
            frequency=frequency,
            start=_as_text(start),
            end=_as_text(end),
            tidy=tidy,
        )
        return f"Wrote {len(changed):,} new/revised rows -> {path}"


# Parses ``--facets``, keeping leading-zero codes such as ``{generatorid: 01}``
# as text (Fire would otherwise give up on the literal or turn ``00`` into 0).
_facets_literal = literal_keeping_codes


#: CLI parameters that carry codes, and the parse function that keeps them.
_CODE_PARSERS = {"facets": _facets_literal}


def _keep_codes_as_text() -> None:
    """
    Make the CLI keep the leading zeros of facet codes.

    Registers the parse functions in :data:`_CODE_PARSERS` with python-fire on
    every :class:`_Cli` command that takes such a parameter. Calling it again is
    harmless. :func:`main` calls it; tests that drive Fire directly call it too.
    """
    set_parse_fns(_Cli, _CODE_PARSERS)


def _bind_no_cache_flag(argv: list[str]) -> list[str]:
    """
    Pin a bare ``--no-cache`` to ``True`` wherever it appears on the command line.

    Fire lets a bare ``--flag`` take the next word as its value, so in
    ``eia-api --no-cache browse`` the subcommand would be swallowed. Arguments
    after a standalone ``--`` are left untouched.

    Args:
        argv (list[str]): The command-line arguments, without the program name.

    Returns:
        list[str]: A new argument list with ``--no-cache`` bound explicitly.
    """
    return bind_bool_flags(argv, ["no_cache"])


def main() -> None:
    """
    Run the Fire-generated command-line interface.

    Every method of :class:`_Cli` becomes a subcommand and its parameters become
    flags, generated automatically by Fire — there is no hand-written argument
    parsing. For example::

        eia-api browse electricity
        eia-api facets electricity/retail-sales
        eia-api get-data electricity/retail-sales --data price --frequency monthly
        eia-api to-csv price.csv --route electricity/retail-sales --data price

    An expected error (missing API key, unknown route, HTTP failure) prints a
    concise message and exits with code 1 (``SystemExit``); Fire also exits for
    CLI usage errors.
    """
    _keep_codes_as_text()
    # Expected, user-facing errors (a missing key, an unknown route, an HTTP
    # failure) print a concise message; the traceback goes to the log (shown
    # with --log-level DEBUG and always written to --log-file).
    run(
        _Cli,
        name="eia-api",
        logger=_log,
        expected=(ValueError, RuntimeError, requests.RequestException),
        argv=_bind_no_cache_flag(sys.argv[1:]),
    )


if __name__ == "__main__":
    main()
