"""
Public package interface for :mod:`eia_api`.

This package wraps the U.S. Energy Information Administration (EIA) API v2. The
implementation lives in :mod:`eia_api.core`; the names most callers need are
re-exported here so that a simple ``from eia_api import EIA`` is enough to get
started.

Exports:
    EIA: The main client for browsing routes and downloading data.
    Route: Immutable metadata (id, name, description) for one node of the API
        route tree.
    main: Entry point for the ``python-fire`` command-line interface.
"""

from __future__ import annotations

from eia_api.core import EIA, Route, main

__all__ = ["EIA", "Route", "main"]
