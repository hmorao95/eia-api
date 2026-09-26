"""Sphinx configuration for the eia-api documentation site."""

from __future__ import annotations

from importlib import metadata

project = "eia-api"
author = "Hugo Morão"
copyright = "2026, Hugo Morão"  # noqa: A001 - Sphinx requires this global name

try:
    release = metadata.version("eia-api")
except metadata.PackageNotFoundError:  # pragma: no cover - not installed
    release = "0.0.0"
version = release

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",  # Google-style docstrings
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "myst_parser",  # Markdown pages
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

# Autodoc: render type hints in the description, document members in source order.
autodoc_typehints = "description"
autodoc_member_order = "bysource"
autoclass_content = "both"
napoleon_google_docstring = True
napoleon_numpy_docstring = False

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
    "pandas": ("https://pandas.pydata.org/docs/", None),
}

html_theme = "pydata_sphinx_theme"
html_title = "eia-api"
html_theme_options = {
    "github_url": "https://github.com/hmorao95/eia-api",
    "icon_links": [
        {
            "name": "PyPI",
            "url": "https://pypi.org/project/eia-api/",
            "icon": "fa-brands fa-python",
        },
    ],
    "navigation_with_keys": True,
}
