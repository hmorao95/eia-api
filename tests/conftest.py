"""
Shared pytest fixtures for the whole suite.

The clients cache in ``~/.cache/eia_api`` by default, and that directory is
resolved from :meth:`pathlib.Path.home` each time a client is built. The
autouse :func:`home` fixture points ``Path.home`` at a fresh temporary
directory for every test, so no test (including ones that build a client, or
the CLI, with default arguments) can create or modify anything under the real
home directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """
    Redirect ``Path.home()`` to an empty per-test directory.

    Autouse, so isolation never depends on a test remembering to patch the
    home directory. A test that needs the fake home (to check where the default
    cache went) can request this fixture by name; tests that patch
    ``Path.home`` themselves simply override it for their own duration.

    Args:
        tmp_path_factory (pytest.TempPathFactory): Source of the fresh directory;
            kept separate from ``tmp_path`` so a test's own files never land
            in the fake home.
        monkeypatch (pytest.MonkeyPatch): Undoes the patch after the test.

    Returns:
        Path: The fake home directory (empty at the start of the test).
    """
    fake = tmp_path_factory.mktemp("home")
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake))
    return fake
