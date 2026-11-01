# Agent guide

Instructions for AI coding agents working in this repository. Human contributors
may find it useful too.

## What this is

`eia-api` is a small, typed Python wrapper around the U.S. Energy Information
Administration (EIA) API v2. It authenticates with an API key, walks the API's
route tree, inspects a dataset's facets/frequencies/columns, and pulls its rows
into tidy pandas DataFrames — paginating past the 5000-row limit — with
CSV/Excel/Parquet/JSON export, incremental updates and a python-fire CLI.

Layout:

- `src/eia_api/`: the package (`core.py` is the whole implementation;
  `__init__.py` re-exports; `__main__.py` enables `python -m`).
- `tests/`: network-mocked pytest suite (no API calls).
- `outputs/`: default destination for CLI extracts (contents gitignored).

## Setup

Managed with [uv](https://docs.astral.sh/uv/). One command:

```bash
uv sync
```

Run anything through `uv run` (the console script lives in `.venv`):

```bash
uv run eia-api browse
uv run python -m eia_api --help
```

Live use needs a free API key in the `EIA_API_KEY` environment variable
(register at https://www.eia.gov/opendata/). The test suite does not need one.

## Quality gate (must pass before every commit)

CI runs exactly these; run them locally first:

```bash
uv run ruff check .
uv run ruff format --check .
uv run codespell src tests README.md CHANGELOG.md
uv run deptry src
uv run interrogate -c pyproject.toml src tests         # docstring coverage >= 95%
uv run mypy                                             # strict
uv run pytest                                           # also checks pyproject == CITATION
```

`uv run pytest` also enforces test coverage >= 92% (`fail_under` in
`[tool.coverage.report]`).

Do not weaken the gate to make it pass. Fix the code, or if a rule is genuinely
wrong for a case, add a narrowly-scoped ignore with a comment explaining why.

## Conventions

- **Docstrings**: verbose Google style: a summary line, an explanatory
  paragraph, then typed `Args:` / `Returns:` / `Raises:`. Every public and most
  private functions are documented; `interrogate` and the ruff `D`/`DOC` rules
  enforce this. Comment non-obvious lines.
- **Typing**: `mypy --strict` must pass; annotate everything. `from __future__
  import annotations` is on.
- **Exceptions**: assign the message to a `msg` variable, then `raise` (ruff
  `EM`). User-facing messages are fine.
- **HTTP**: every request goes through the single `EIA._request` choke point,
  which handles caching and the API's top-level `error` field. Tests mock that
  method (or `session.get`); never hit the network in tests.
- **Secrets**: the API key is never logged, written to a file, or included in a
  cache filename (the cache key strips it out). Keep it that way.
- **Cache directory**: every client's `cache_dir` defaults to the
  `DEFAULT_CACHE_DIR` sentinel, which `_resolve_cache_dir` maps to
  `~/.cache/eia_api` when the client is built (never at import); `None` (or
  any falsy value) disables the disk cache. Internal code that wants no cache
  passes `cache_dir=None` (as the CLI's `--no-cache` does).
- **Line length**: formatter targets 88; `E501` allows up to 150.
- **Imports**: fire is imported lazily (inside wrapkit's CLI helpers) on purpose.
- **Shared plumbing**: logging and redaction, the disk cache, the throttle,
  tidy/polars helpers, incremental updates, writers and Fire helpers come from
  [wrapkit](https://github.com/hmorao95/wrapkit), pinned by tag in
  `pyproject.toml`. Fix shared bugs there, release a tag, then bump the pin.

## Tests

- Never hit the network. Feed a synthetic API by monkeypatching `EIA._request`,
  or stub `session.get` with a fake response. See the `client` fixture and
  `_fake_request` in `tests/test_eia_api.py`.
- CLI tests drive fire directly: `fire.Fire(cli, command=[...])`.
- Tests never touch the real home directory: the autouse `home` fixture in
  `tests/conftest.py` points `Path.home()` at a fresh temporary directory for
  every test (request `home` to inspect it).
- Add tests for every new behavior; keep coverage healthy.

## Dependencies

- Add runtime deps with `uv add <pkg>`; dev/typing deps go in the `dev`/`typing`
  groups in `pyproject.toml`.
- A dependency used only at runtime by pandas (e.g. `openpyxl`, `pyarrow`) must
  be added to `[tool.deptry.per_rule_ignores] DEP002`, since it is not imported
  directly.

## Git and releases

- Work on a branch (`feature/…`, `fix/…`, `chore/…`), open a PR, and squash-merge
  once CI is green. Reference issues with `Closes #N`.
- **Commit as the repo owner. Never add a Co-Authored-By / AI-authorship trailer
  to commits, tags or PRs.** Do not skip hooks or bypass signing.
- **Releasing** (SemVer): after merging to `main`,
  1. move the `[Unreleased]` changelog items under a new `## [X.Y.Z] - <date>`
     section and update the compare links at the bottom;
  2. bump the version in **`pyproject.toml`**, **`CITATION.cff`**, the README
     citation line, and **`uv.lock`** (`uv lock`);
  3. commit, push, wait for CI;
  4. `git tag -a vX.Y.Z` and `gh release create vX.Y.Z`.
- Publishing is manual: a published GitHub release no longer publishes to PyPI
  (the package is not on PyPI yet). Run the Publish workflow
  (`.github/workflows/publish.yml`: uv build + `uv publish` via PyPI Trusted
  Publishing, no token) by hand once a Trusted Publisher exists.
