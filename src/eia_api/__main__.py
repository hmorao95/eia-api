"""
Module entry point enabling ``python -m eia_api``.

Running the package as a module simply delegates to :func:`eia_api.core.main`,
which launches the Fire-generated command-line interface. This mirrors the
``eia-api`` console script installed via the project's entry points.
"""

from __future__ import annotations

from eia_api.core import main

if __name__ == "__main__":
    main()
