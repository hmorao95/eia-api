# eia-api

A small, typed Python wrapper around the U.S. Energy Information Administration
(EIA) [API v2](https://www.eia.gov/opendata/). It authenticates with an API key,
walks the API's route tree, inspects a dataset's facets, frequencies and
columns, and pulls its rows into tidy [pandas](https://pandas.pydata.org/)
DataFrames — paginating past the 5000-row limit — with CSV/Excel/Parquet/JSON
export, incremental updates, client-side rate limiting, a synchronous and an
asynchronous client, and a [python-fire](https://github.com/google/python-fire)
command-line interface.

```python
from eia_api import EIA

eia = EIA()  # reads EIA_API_KEY from the environment
eia.browse()  # electricity, petroleum, natural-gas, ...
eia.get_data(
    "electricity/retail-sales",
    data="price",
    facets={"stateid": "CO", "sectorid": "RES"},
    frequency="monthly",
    start="2015-01",
)
```

```{toctree}
:maxdepth: 2
:caption: Guide

installation
usage
```

```{toctree}
:maxdepth: 2
:caption: Reference

api
glossary
changelog
```
