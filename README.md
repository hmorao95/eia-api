# eia-api

[![PyPI](https://img.shields.io/pypi/v/eia-api.svg)](https://pypi.org/project/eia-api/)
[![CI](https://github.com/hmorao95/eia-api/actions/workflows/ci.yml/badge.svg)](https://github.com/hmorao95/eia-api/actions/workflows/ci.yml)
[![Docs](https://github.com/hmorao95/eia-api/actions/workflows/docs.yml/badge.svg)](https://hmorao95.github.io/eia-api/)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/downloads/)
[![uv](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/uv/main/assets/badge/v0.json)](https://github.com/astral-sh/uv)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![Checked with mypy](https://img.shields.io/badge/mypy-checked-2a6db2.svg)](https://mypy-lang.org/)
[![pre-commit](https://img.shields.io/badge/pre--commit-enabled-brightgreen?logo=pre-commit)](https://github.com/pre-commit/pre-commit)
[![Keep a Changelog](https://img.shields.io/badge/changelog-Keep%20a%20Changelog-orange.svg)](CHANGELOG.md)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A small, dependency-light Python wrapper around the U.S. Energy Information
Administration (EIA) [API v2](https://www.eia.gov/opendata/).

The EIA publishes electricity, petroleum, natural gas, coal, nuclear and
renewables statistics behind a single versioned REST API. It is organised as a
*tree of routes* rather than a flat list of series: parent routes list child
routes, and each dataset exposes a `/data` endpoint plus metadata describing its
value columns, facets (filtering dimensions) and frequencies. Every response is
capped at 5000 rows. This wrapper handles that for you:

- Walks the route tree so you can discover datasets, their facets and their
  value columns from Python or the command line.
- Fetches a dataset into a tidy `pandas` DataFrame, **paginating automatically**
  past the 5000-row limit and encoding facet filters for you.
- Coerces value columns to numbers and adds a parsed `date` column alongside the
  API's native `period` label (year, month, quarter, date or hour).
- Exports straight to CSV/Excel/Parquet/JSON, and keeps a long CSV current with
  incremental upserts (append new periods, overwrite revisions).
- Caches raw JSON responses on disk (24 h TTL) so repeat calls are fast.

You need a free EIA API key — register at <https://www.eia.gov/opendata/>. The
wrapper reads it from the `EIA_API_KEY` environment variable (or a constructor
argument); it is never written to disk or embedded in cache filenames.

## Quickstart

New to Python tooling? These steps take you from nothing to a spreadsheet of
energy data. You need [git](https://git-scm.com/downloads) and
[uv](https://docs.astral.sh/uv/) (a fast Python package manager) installed.

Install uv if you do not have it:

```bash
# macOS / Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Then:

```bash
# 1. Get the code
git clone https://github.com/hmorao95/eia-api.git
cd eia-api

# 2. Install it (uv creates a .venv and pulls dependencies; no manual Python setup)
uv sync

# 3. Tell the wrapper your free API key (get one at https://www.eia.gov/opendata/)
export EIA_API_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx   # PowerShell: $env:EIA_API_KEY="..."

# 4. Explore the catalog: top-level routes, then drill in
uv run eia-api browse
uv run eia-api browse electricity
uv run eia-api facets electricity/retail-sales

# 5. Save a series to a CSV you can open in Excel
uv run eia-api to-csv price.csv --route electricity/retail-sales --data price \
    --frequency monthly --facets '{"stateid": "CO", "sectorid": "RES"}'
```

A bare output filename is written into the repo's `outputs/` folder
automatically (so `price.csv` becomes `outputs/price.csv`). That folder's
contents are gitignored, so extracts stay out of version control. Pass a path
with a directory (e.g. `data/price.csv` or an absolute path) to write elsewhere.

See [Reference](#reference) for the full list of commands and options.

To install the library into your own Python environment instead, use pip:

```bash
pip install eia-api
```

Requires Python 3.10 or newer.

> Note: the `eia-api` command lives inside the project's `.venv`, which is why
> each command starts with `uv run`. If you skip `uv run` and get "command not
> found", that is the reason. To get a global command, see
> [Command line](#command-line) below.

## Usage from Python

`get_data()` returns a [pandas](https://pandas.pydata.org/) DataFrame, the
standard table type for data work in Python.

```python
from eia_api import EIA

eia = EIA()  # reads EIA_API_KEY from the environment

# Discover: walk the route tree, then inspect a dataset
eia.browse()  # electricity, petroleum, natural-gas, ...
eia.browse("electricity")  # child routes under electricity
eia.data_columns(
    "electricity/retail-sales"
)  # value columns: price, revenue, sales, ...
eia.facets("electricity/retail-sales")  # filtering dimensions: stateid, sectorid
eia.facet_values("electricity/retail-sales", "stateid")  # CO, CA, TX, ...

# Fetch: tidy long DataFrame, paginated automatically
eia.get_data(
    "electricity/retail-sales",
    data="price",
    facets={"stateid": "CO", "sectorid": ["RES", "COM"]},
    frequency="monthly",
    start="2015-01",
    end="2024-12",
)

# One-liner to CSV or Excel
eia.to_csv(
    "retail_price.csv",
    route="electricity/retail-sales",
    data="price",
    facets={"stateid": "CO"},
    frequency="monthly",
)
eia.to_excel("retail_price.xlsx", route="electricity/retail-sales", data="price")

# Incremental update: only append periods newer than what's already saved
# (and overwrite any values EIA has since revised).
eia.update_csv(
    "retail_price.csv",
    route="electricity/retail-sales",
    data="price",
    facets={"stateid": "CO"},
    frequency="monthly",
)
```

What the arguments mean:

- `route` is the dataset path, e.g. `"electricity/retail-sales"` (browse to find
  it). A trailing `/data` is optional — the wrapper adds it.
- `data` names the value column(s) to return (see `data_columns()`). Pass a
  single name as a string, several as a list. **Omit it to request every value
  column** the dataset exposes.
- `facets` filters by dimension: a mapping of facet id to one value or a list of
  values (see `facets()` / `facet_values()`). Omit for no filter.
- `frequency` picks the periodicity (see `frequencies()`), e.g. `"monthly"`,
  `"annual"`, `"daily"`.
- `start` and `end` are inclusive period bounds in the dataset's own format
  (`"2015"`, `"2015-01"`, `"2015-01-01"`).
- `max_rows` caps how many rows are fetched; by default the whole result is
  paginated in.

The tidy frame has a parsed `date` column first, then the API's `period`, the
facet columns, and each requested value column with its `<column>-units`
companion.

### Async

For concurrent fetches, use `AsyncEIA` (also reachable as `EIA.AsyncAPI`), the
`httpx`-backed counterpart to `EIA`. It mirrors the read surface — `browse`,
`metadata`, `frequencies`, `facets`, `facet_values`, `data_columns` and
`get_data` — as coroutines, with the same pagination, caching and throttling.

```python
import asyncio
from eia_api import AsyncEIA


async def main():
    async with AsyncEIA() as eia:  # closes the HTTP client on exit
        gas, power = await asyncio.gather(
            eia.get_data("natural-gas/pri/fut", frequency="monthly"),
            eia.get_data("electricity/retail-sales", data="price", frequency="monthly"),
        )
    return gas, power


gas, power = asyncio.run(main())
```

## Command line

The CLI is generated from the library with
[python-fire](https://github.com/google/python-fire): each method becomes a
subcommand and each parameter becomes a flag, so there is no separate set of
options to learn. Run `eia-api --help` to see them all.

Inside a uv project the console script lives in `.venv`, so call it with `uv run`
(or activate the venv first). `python -m eia_api` works too:

```bash
uv run eia-api browse electricity
```

To make the command available everywhere (outside this project, on your PATH),
install it as a uv tool once:

```bash
uv tool install .
eia-api browse electricity
```

The examples below use the bare command; prefix them with `uv run` if you have
not installed the tool globally.

```bash
# Walk the tree
eia-api browse                      # top-level routes
eia-api browse electricity          # child routes under a route

# Inspect a dataset
eia-api frequencies electricity/retail-sales
eia-api facets electricity/retail-sales
eia-api data-columns electricity/retail-sales
eia-api facet-values electricity/retail-sales stateid

# Print a table to the terminal
eia-api get-data electricity/retail-sales --data price --frequency monthly

# Filter by facets (JSON mapping; on Windows cmd use double quotes outside,
# single quotes inside: --facets "{'stateid': 'CO'}")
eia-api get-data electricity/retail-sales --data price \
    --facets '{"stateid": "CO", "sectorid": "RES"}' --start 2015-01

# Extract to a file (format chosen by extension for get-data --out)
eia-api get-data electricity/retail-sales --data price --out price.csv
eia-api to-csv price.csv --route electricity/retail-sales --data price
eia-api to-excel price.xlsx --route electricity/retail-sales --data price

# Incremental: add new and revised rows to an existing long CSV
eia-api update-csv price.csv --route electricity/retail-sales --data price \
    --facets '{"stateid": "CO"}' --frequency monthly
```

## Reference

Run `eia-api --help`, or `eia-api <command> --help` for a single command, to see
this from the CLI.

### CLI commands

| Command | Positional | Options |
|---|---|---|
| `browse` | `[ROUTE]` | (none) |
| `frequencies` | `ROUTE` | (none) |
| `facets` | `ROUTE` | (none) |
| `data-columns` | `ROUTE` | (none) |
| `facet-values` | `ROUTE FACET_ID` | (none) |
| `get-data` | `ROUTE` | `--data --facets --frequency --start --end --offset --length --max-rows --out` |
| `to-csv` | `PATH` | `--route --data --facets --frequency --start --end --max-rows` |
| `to-excel` | `PATH` | `--route --data --facets --frequency --start --end --max-rows` |
| `to-parquet` | `PATH` | `--route --data --facets --frequency --start --end --max-rows` |
| `to-json` | `PATH` | `--route --data --facets --frequency --start --end --max-rows` |
| `update-csv` | `PATH` | `--route --data --facets --frequency --start --end` |

For `get-data`, `--out` picks the format by extension (`.xlsx`/`.xls` Excel,
`.parquet` Parquet, `.json` JSON, otherwise CSV); without `--out` it prints a
table. A bare output filename (no directory) is written into the `outputs/`
folder; pass a path with a directory to write elsewhere.

### Options

| Option | Values | Meaning |
|---|---|---|
| `--route` | e.g. `electricity/retail-sales` | Dataset path (browse to find it). A trailing `/data` is optional. |
| `--data` | name or list of names | Value column(s) to return. Omit for all (see `data-columns`). |
| `--facets` | JSON mapping | Facet filters, e.g. `'{"stateid": "CO", "sectorid": ["RES", "COM"]}'`. |
| `--frequency` | e.g. `monthly`, `annual`, `daily` | Periodicity (see `frequencies`). |
| `--start`, `--end` | e.g. `2015`, `2015-01`, `2015-01-01` | Inclusive period bounds. |
| `--length` | int (≤ 5000) | Page size per request. Pagination is automatic. |
| `--max-rows` | int | Stop after this many rows. |

### Python API

```python
EIA(
    api_key=None,
    cache_dir=None,
    cache_ttl_hours=24.0,
    timeout=60.0,
    requests_per_second=9.0,
    session=None,
)
```

`requests_per_second` throttles network requests (cache hits are never
throttled) so bulk pagination stays polite to the API; pass `None` to disable.

`AsyncEIA(...)` (also `EIA.AsyncAPI`) takes the same arguments (plus an optional
`client=` httpx client) and exposes the read methods as coroutines — see
[Async](#async).

| Method | Returns |
|---|---|
| `browse(route="")` | DataFrame of child routes `[id, name, description]` |
| `metadata(route="")` | Raw metadata dict for a route |
| `frequencies(route)` | DataFrame of supported frequencies |
| `facets(route)` | DataFrame of filtering dimensions |
| `data_columns(route)` | DataFrame of value columns `[id, alias, units]` |
| `facet_values(route, facet_id)` | DataFrame of a facet's allowed values |
| `get_data(route, data=None, facets=None, frequency=None, start=None, end=None, sort=None, offset=0, length=5000, max_rows=None)` | Tidy long DataFrame |
| `to_csv(path, **kwargs)` | Writes CSV; returns the `Path` |
| `to_excel(path, **kwargs)` | Writes `.xlsx`; returns the `Path` |
| `to_parquet(path, **kwargs)` | Writes `.parquet`; returns the `Path` |
| `to_json(path, **kwargs)` | Writes JSON records; returns the `Path` |
| `update_csv(path, route, data=None, **kwargs)` | Upserts the CSV; returns the added/revised rows |
| `new_observations(existing, route, data=None, include_revisions=True, **kwargs)` | Rows missing from `existing` (DataFrame or CSV path) |

The `to_*` writers forward `**kwargs` to `get_data`. The tidy output columns are
`date`, `period`, the facet columns, and each requested value column plus its
`<column>-units` companion.

## Incremental updates

`update_csv()` (CLI: `update-csv`) keeps a long-format CSV current without
rewriting the whole file each run:

- First run (file missing) writes the full extract.
- Later runs read what's already saved and upsert on the row key (the `period`
  plus the facet columns): they append new observations and overwrite any values
  EIA has since revised.
- If nothing changed, the file is left untouched and an empty frame is returned.

```python
new_rows = eia.update_csv(
    "retail_price.csv",
    route="electricity/retail-sales",
    data="price",
    facets={"stateid": "CO"},
    frequency="monthly",
)
print(f"added {len(new_rows)} observations")
```

### Just the delta, in memory

If you keep your data somewhere other than a CSV, `new_observations()` is the
file-free building block. Give it what you already have (a long DataFrame or a
long-CSV path) and it returns only the rows you are missing.

```python
have = my_store.load()  # any long DataFrame from a prior pull
missing = eia.new_observations(have, route="electricity/retail-sales", data="price")
my_store.append(missing)

# Strictly-new rows only (ignore EIA back-revisions):
missing = eia.new_observations(
    have, route="electricity/retail-sales", data="price", include_revisions=False
)
```

## Development

Managed with [uv](https://docs.astral.sh/uv/). The package lives under a `src/`
layout (`src/eia_api/`) and tests under `tests/`.

```bash
uv sync                     # create .venv and install deps (dev + typing groups)
uv run ruff check .         # lint
uv run ruff format .        # format
uv run mypy                 # strict type checking
uv run pytest               # tests + coverage (network-mocked, no API calls)
```

Full quality gate (mirrors CI):

```bash
uv run ruff check . && uv run ruff format --check .
uv run codespell src tests README.md CHANGELOG.md
uv run deptry src
uv run interrogate -c pyproject.toml src tests   # docstring coverage >= 95%
uv run mypy
uv run pytest
```

[pre-commit](https://pre-commit.com/) runs ruff, codespell, and mypy on every
commit:

```bash
uv run pre-commit install       # one-time, enables the git hook
uv run pre-commit run --all-files
```

CI (`.github/workflows/ci.yml`) runs the same gate on every push and pull
request.

### Build and publish

```bash
uv build
```

Publishing a GitHub release triggers `.github/workflows/publish.yml`, which runs
`uv build` and `uv publish` to PyPI using Trusted Publishing, so no API token is
stored in the repo.

## Changelog

See [CHANGELOG.md](CHANGELOG.md) (Keep a Changelog format) or the
[GitHub releases](https://github.com/hmorao95/eia-api/releases) for per-version
notes.

## Citation

If you use this wrapper in your work, please cite it (see
[CITATION.cff](CITATION.cff); GitHub renders a "Cite this repository" button):

> Morão, H. (2026). *eia-api* (v0.1.0) [Software].
> https://pypi.org/project/eia-api/

Please also credit the underlying data source, the U.S. Energy Information
Administration (EIA).

## Data source & license

Data © the U.S. Energy Information Administration (EIA), a U.S. government agency
whose data is generally in the public domain. Source and API terms:
<https://www.eia.gov/opendata/>.

This wrapper code is released under the MIT License. It is an independent project
and is not affiliated with or endorsed by the EIA.
