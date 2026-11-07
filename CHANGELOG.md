# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com),
and this project adheres to [Semantic Versioning](https://semver.org).

## [Unreleased]

### Fixed

- A `--facets` value the CLI cannot parse (such as `{stateid: [CO, TX}`, a
  missing bracket) no longer ends in a Python traceback. `get_data` now
  raises a `ValueError` when `facets` is not a mapping, and the CLI prints
  it as a one-line error and exits with code 1.
- A bare `--tidy` no longer swallows the next argument. In
  `eia-api update-csv --tidy out.csv electricity/retail-sales ...` the path
  was taken as the value of `--tidy`, so the command failed or wrote the
  wrong shape. Every boolean flag (`--tidy`, `--no-cache`) is now pinned to
  `True` when given bare, wherever it appears.
- A single facet value that is a number, such as `facets={"plantid": 3}` or
  the CLI's `--facets "{stateid: 8}"` (which Fire parses as an int), no
  longer raises `TypeError`. It is sent as one value, like a string.
- `to_excel` no longer cuts hourly timestamps to days. It wrote every `date`
  as a calendar day, so all 24 hours of a day showed the same date. Dates
  are now written as plain days only when every timestamp is at midnight;
  hourly data keeps its time.

## [0.4.2] - 2026-11-04

### Fixed

- `local-hourly` data no longer crashes `get_data`. Its period labels carry
  a UTC offset (`2024-11-03T01-05`), which the parser that builds the `date`
  column could not handle. These hours are now converted to UTC, so `date`
  stays a plain (timezone-free) UTC timestamp like `hourly` data, and the
  hour repeated when daylight saving time ends gets its own row instead of
  colliding with the first. The `period` column keeps the local label.
- `get_data` no longer repeats or drops rows between pages. It sorted on
  `period` only, so the rows of one period (one per state, sector, ...)
  could come back in a different order on each page. The sort now adds
  `period` and then each facet of the dataset, ascending, after any keys
  you pass, so the order is fixed. This costs one metadata request when
  `data` is given (it is cached). The request parameters change, so pages
  cached by an earlier version are fetched again once.
- `update_csv` and `new_observations` no longer add a second row when EIA
  renames a facet label (such as `stateDescription`, or `stateid_name` in
  the tidy shape). The label was part of the row key, so the renamed row
  looked new. Labels are now compared like values: the stored row is
  updated and reported as revised. Rows a past rename had already doubled
  in a stored CSV are merged into one on the next update, keeping the
  newest label.
- wrapkit is now pinned at v0.2.6 (was v0.2.1), which brings these fixes:
  - Codes such as `"NA"` (Namibia) stay text when the stored CSV is read back;
    they used to turn blank, so those rows were re-added as duplicates.
  - Stored numbers read back exactly, so on pandas before 3.1 unchanged values
    no longer count as revisions and rewrite the file on every run.
  - A zero-byte stored CSV counts as no prior extract.
  - Comparing a column that goes between missing and a value no longer crashes
    the update.
  - A numeric key matches whether it reads `110` or `110.0`, so a revised row
    replaces the stored one instead of being added next to it.
  - A cache entry left half-written by a crash is fetched again instead of
    raising.

### Security

- The API key no longer reaches the disk cache. EIA echoes every request,
  key included, under `request.params`, and the payload was cached as
  received. The key is now dropped before caching. Cache files written by
  earlier versions still hold it: delete `~/.cache/eia_api` (or your
  `cache_dir`) once after upgrading.
- HTTP errors no longer show the API key. The messages of `requests` and
  `httpx` errors quote the request URL, so the CLI's `Error:` line (and any
  traceback) printed the key. They are re-raised with the key masked, without
  the original chained. An `httpx.HTTPStatusError` from `AsyncEIA` now comes
  out as a plain `httpx.HTTPError`.

## [0.4.1] - 2026-11-03

### Changed

- `.gitignore` now also ignores LaTeX build files (`*.aux`, `*.log`,
  `*.synctex.gz`, `*.fdb_latexmk` and the like) and Jupyter checkpoints
  (`.ipynb_checkpoints/`). No change to the package.

## [0.4.0] - 2026-11-03

### Changed

- wrapkit pin moved from v0.1.0 to [v0.2.1](https://github.com/hmorao95/wrapkit/releases/tag/v0.2.1):
  log lines mask registered secrets longest first, so a secret that
  contains another is never masked in pieces. No other change here.
- `pre-commit` is no longer a dev dependency: run the hooks with
  `uvx pre-commit` (for example `uvx pre-commit install`). This drops
  `virtualenv` and `filelock` from `uv.lock`; filelock has no patched release
  for Python 3.10.
- The manual Publish workflow's version check (tag = pyproject = CITATION) now runs
  when the workflow is started from a release tag; it was tied to release events,
  which no longer trigger the workflow.
- The shared plumbing (logging and key redaction, the disk cache and
  `DEFAULT_CACHE_DIR`, the request throttle, the tidy-shape and polars helpers,
  incremental updates, file writers and the CLI's Fire helpers) now comes from
  [wrapkit](https://github.com/hmorao95/wrapkit) v0.1.0, installed from GitHub.
  The public API, outputs and cache files are unchanged. `fire` and `colorlog`
  are now installed through wrapkit.
- The async client's throttle now spaces concurrent requests too (they queue
  for their turn); before, tasks started together could all go at once.

## [0.3.0] - 2026-11-01

### Added

- CLI flag `--no-cache`, accepted before or after any command, to skip the
  on-disk response cache. By default the CLI still caches in
  `~/.cache/eia_api`.
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

### Changed

- **Behaviour change:** `cache_dir=None` now disables the on-disk cache (it
  used to mean the default `~/.cache/eia_api` directory); omit the argument or
  pass the new `DEFAULT_CACHE_DIR` for the default. `""` still disables the
  cache, and calling `EIA()` / `AsyncEIA()` with no `cache_dir` caches in
  `~/.cache/eia_api` as before. `DEFAULT_CACHE_DIR` is exported from the
  package; the home directory is looked up when a client is built, not at
  import.
- CI fails when test coverage drops below 92%: `fail_under = 92` in
  `[tool.coverage.report]`, which `uv run pytest` enforces (coverage was
  94.68% when the threshold was set).

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

[Unreleased]: https://github.com/hmorao95/eia-api/compare/v0.4.2...HEAD
[0.4.2]: https://github.com/hmorao95/eia-api/compare/v0.4.1...v0.4.2
[0.4.1]: https://github.com/hmorao95/eia-api/compare/v0.4.0...v0.4.1
[0.4.0]: https://github.com/hmorao95/eia-api/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/hmorao95/eia-api/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/hmorao95/eia-api/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/hmorao95/eia-api/releases/tag/v0.1.0
