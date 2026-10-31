# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com),
and this project adheres to [Semantic Versioning](https://semver.org).

## [Unreleased]

### Added

- Opt-in tidy output: `get_data(..., tidy=True)` (sync and async, the `to_*`
  writers, `update_csv`/`new_observations`, and `--tidy` on the CLI's data
  commands) returns exactly `date`, the facet ids as dimensions (plus
  `<facet>_name` labels where the API sends them), `variable` (the measure id),
  `value` (float64) and `unit` (from the `<measure>-units` column, `<NA>` when
  absent), sorted by the dimensions then `date`. The default output is
  unchanged.
- Opt-in polars output: `get_data(..., as_polars=True)` (sync and async) returns
  a `polars.DataFrame`, with `@overload`ed return types. polars is an optional
  extra: `pip install "eia-api[polars]"`.

- Logging: the library now logs to the `eia_api` logger (silent by default via a
  `NullHandler`). Requests, cache hits and expiries, throttle sleeps and each
  page are logged at DEBUG; a summary per fetch, export and incremental update
  at INFO. Both clients log the same lines.
- CLI flags `--log-level` (colored console output on stderr, via `colorlog`) and
  `--log-file` (appends a full DEBUG log with timestamps), accepted with any
  command. On a failed command the traceback goes to the log.
- The API key is masked as `***` in log records from `urllib3` and `httpx`
  (which log full request URLs) and from the package logger.

### Fixed

- CLI `--facets` codes keep their leading zeros. python-fire reads the mapping as
  a Python literal, so a bare code such as `'{generatorid: 01}'` made it give up
  and pass the whole text on as a string (the command then failed), and
  `'{generatorid: 00}'` became the int `0`. Bare leading-zero codes are now
  read as text; quoted values and other numbers parse as before.
- `update_csv`/`new_observations` compare row keys as text, so numeric-looking
  facet ids (`"01"`, `"3"`) survive the CSV round-trip instead of being treated
  as new rows, and a stored value that is still missing (NaN) no longer counts
  as new on every run.
- CLI period bounds given as bare numbers (`--start 2024`, `--end 202406`) are
  passed to the client as text. python-fire had turned them into ints, which
  broke the `str | None` contract and gave the query a different cache entry
  from the equivalent library call; the query builder now also stringifies the
  bounds.
- `--log-file` no longer gets ANSI color codes in tracebacks on Python 3.13+:
  the file handler re-renders tracebacks instead of reusing the console's
  colored copy.
- The API key is now also masked in `urllib3.util.retry` records, which log the
  full request URL when a connection error is retried or given up on.

- The README, the installation docs and `CITATION.cff` no longer point to PyPI,
  where the package is not published: the PyPI badge and identifier are removed,
  installation uses the GitHub repository, and the citation links to it.

## [0.2.0] - 2026-10-19

### Added

- Documentation site (Sphinx + PyData theme, published to GitHub Pages): install
  and usage guides, an autodoc API reference for `EIA`, `AsyncEIA` and `Route`, a
  glossary, and the changelog. Built and deployed by `.github/workflows/docs.yml`
  (a `docs` dependency group provides Sphinx, the theme and MyST).
- Async client: `AsyncEIA` (also reachable as `EIA.AsyncAPI`), an `httpx`-backed
  counterpart to `EIA` with coroutine versions of `browse`, `metadata`,
  `frequencies`, `facets`, `facet_values`, `data_columns` and `get_data` (same
  pagination, facet encoding, number coercion and parsed `date` column). Works as
  an async context manager and shares the on-disk cache and throttle with the
  sync client, so datasets can be fetched concurrently with `asyncio.gather`.
- Client-side rate limiting: `EIA(requests_per_second=...)` throttles network
  requests (cache hits are never throttled), sleeping just enough between calls
  to stay under the cap. Defaults to a gentle 9/s so bulk pagination stays polite
  to the API; pass `None` to disable.

### Fixed

- `data_columns()` (and therefore `get_data(..., data=None)`) no longer crashes
  on datasets whose metadata maps a column to an empty list instead of an
  `{alias, units}` dict — e.g. the petroleum (`petroleum/pri/*`) and natural-gas
  (`natural-gas/pri/*`) price routes. Such columns are now listed with blank
  alias/units, so a bare `get_data("petroleum/pri/spt")` works.
- `EIA` now actually sends its browser-like User-Agent. The constructor used
  `session.headers.setdefault("User-Agent", ...)`, but a fresh
  `requests.Session` already carries `python-requests/x.y`, so the default was
  never applied and every request went out as `python-requests`. A session the
  client creates now has the header set outright; a caller-supplied `session=` is
  left untouched. `AsyncEIA` was not affected.

## [0.1.0] - 2026-09-13

### Added

- `EIA` client for the U.S. Energy Information Administration (EIA) API v2, with
  the API key read from the constructor or the `EIA_API_KEY` environment
  variable.
- Route-tree browsing: `browse()` (child routes), `metadata()` (raw metadata),
  `frequencies()`, `facets()`, `data_columns()` and `facet_values()`.
- `get_data()` — tidy long DataFrames with automatic pagination past the
  5000-row limit, facet filtering, frequency and inclusive `start`/`end` period
  bounds, numeric coercion of value columns and a parsed `date` column added
  ahead of the API's native `period`.
- Export helpers `to_csv()`, `to_excel()`, `to_parquet()` and `to_json()`, plus
  the matching CLI commands (bare filenames land in the `outputs/` folder).
- Incremental updates: `update_csv()` upserts on the row key (period plus facet
  columns), appending new observations and overwriting values EIA later revises;
  `new_observations()` is the file-free building block returning just the delta.
- On-disk caching of raw JSON responses (24 h TTL, keyed without the API key)
  plus in-process metadata memoisation.
- Command-line interface (`eia-api` / `python -m eia_api`) generated from the
  library with [python-fire](https://github.com/google/python-fire).
- `src/` package layout, `py.typed` marker, and a network-mocked pytest suite.
- Tooling: uv project config, strict ruff lint/format, mypy `--strict`, deptry,
  interrogate (docstring coverage), codespell, pre-commit hooks, and GitHub
  Actions CI and publish workflows.

[Unreleased]: https://github.com/hmorao95/eia-api/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/hmorao95/eia-api/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/hmorao95/eia-api/releases/tag/v0.1.0
