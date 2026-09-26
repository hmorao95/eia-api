# Usage

## Discover, then fetch

The API is a tree of routes. Walk it with {py:meth}`~eia_api.EIA.browse`, inspect
a dataset with {py:meth}`~eia_api.EIA.facets`,
{py:meth}`~eia_api.EIA.facet_values`, {py:meth}`~eia_api.EIA.frequencies` and
{py:meth}`~eia_api.EIA.data_columns`, then pull rows with
{py:meth}`~eia_api.EIA.get_data`.

```python
from eia_api import EIA

eia = EIA()

eia.browse()                                   # top-level routes
eia.browse("electricity")                      # child routes
eia.data_columns("electricity/retail-sales")   # value columns: price, revenue, ...
eia.facets("electricity/retail-sales")         # stateid, sectorid, ...
eia.facet_values("electricity/retail-sales", "stateid")

eia.get_data(
    "electricity/retail-sales",
    data="price",                              # omit for every value column
    facets={"stateid": "CO", "sectorid": ["RES", "COM"]},
    frequency="monthly",
    start="2015-01",
    end="2024-12",
)
```

The tidy frame has a parsed `date` column first, then the API's `period`, the
facet columns, and each requested value column with its `<column>-units`
companion. Pagination past the 5000-row cap is automatic.

## Export and incremental updates

```python
eia.to_csv("retail_price.csv", route="electricity/retail-sales", data="price")
eia.to_excel("retail_price.xlsx", route="electricity/retail-sales", data="price")

# Append only newer periods (and overwrite revised values):
eia.update_csv("retail_price.csv", route="electricity/retail-sales", data="price",
               frequency="monthly")
```

## Rate limiting

Every network request passes through one throttle. By default the client issues
at most **9 requests/second** (cache hits are never throttled); pass
`requests_per_second=None` to disable, or a smaller number to be gentler.

```python
eia = EIA(requests_per_second=5)
```

## Async

For concurrent fetches, use {py:class}`~eia_api.AsyncEIA` (also reachable as
`EIA.AsyncAPI`), the [httpx](https://www.python-httpx.org/)-backed counterpart to
{py:class}`~eia_api.EIA`. It mirrors the read surface as coroutines, with the
same pagination, caching and throttling, and works as an async context manager.

```python
import asyncio
from eia_api import AsyncEIA

async def main():
    async with AsyncEIA() as eia:
        gas, power = await asyncio.gather(
            eia.get_data("natural-gas/pri/fut", frequency="monthly"),
            eia.get_data("electricity/retail-sales", data="price", frequency="monthly"),
        )
    return gas, power

gas, power = asyncio.run(main())
```

## Command line

The CLI is generated from the library with python-fire — every method is a
subcommand and every parameter a flag.

```bash
eia-api browse
eia-api facets electricity/retail-sales
eia-api to-csv price.csv --route electricity/retail-sales --data price --frequency monthly
eia-api --help
```
