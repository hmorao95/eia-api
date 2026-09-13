# CLAUDE.md

This project's agent instructions live in [AGENTS.md](AGENTS.md). Read and follow
them.

@AGENTS.md

Key reminders for this repo:

- Run the full quality gate before committing (see AGENTS.md): version check,
  ruff, ruff-format, codespell, deptry, interrogate, mypy, pytest.
- Branch, open a PR, squash-merge when CI is green.
- Commit as the repository owner; never add a Co-Authored-By or AI-authorship
  trailer to commits, tags or PRs.
- The API key is a secret: never log it, write it to a file, or put it in a cache
  filename.
- Releases bump the version in `pyproject.toml`, `CITATION.cff`, the README
  citation and `uv.lock` together; a GitHub release auto-publishes to PyPI.
