# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com),
and this project adheres to [Semantic Versioning](https://semver.org).

## [Unreleased]

### Added

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

[Unreleased]: https://github.com/hmorao95/eia-api/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/hmorao95/eia-api/releases/tag/v0.1.0
