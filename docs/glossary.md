# Glossary

```{glossary}
route
  A path through the EIA API's tree of datasets, e.g.
  `electricity/retail-sales`. Parent routes list child routes; a leaf route is a
  dataset with a `/data` endpoint. See {py:meth}`~eia_api.EIA.browse`.

data column
  A measured quantity a dataset exposes for the `data` argument (e.g. `price`,
  `revenue`, `sales`). See {py:meth}`~eia_api.EIA.data_columns`. Passing
  `data=None` requests every column.

facet
  A filtering dimension of a dataset, such as `stateid` or `sectorid`. Its
  allowed values come from {py:meth}`~eia_api.EIA.facet_values`; filters are
  passed to {py:meth}`~eia_api.EIA.get_data` as `facets={...}`.

frequency
  A dataset's periodicity — `annual`, `quarterly`, `monthly`, `daily`, `hourly`
  — chosen with the `frequency` argument. See
  {py:meth}`~eia_api.EIA.frequencies`.

period
  The API's native time label for a row, whose format follows the frequency
  (`2020`, `2020-03`, `2020-Q1`, `2020-03-15`). The client adds a parsed `date`
  column alongside it.

pagination
  The API caps a single response at 5000 rows; {py:meth}`~eia_api.EIA.get_data`
  fetches successive pages with `offset`/`length` until the whole result (or
  `max_rows`) is retrieved.
```
