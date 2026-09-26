# Installation

Requires Python 3.10 or newer.

```bash
pip install eia-api
```

Or, in a [uv](https://docs.astral.sh/uv/) project:

```bash
uv add eia-api
```

## API key

Live use needs a free EIA API key. Register at
<https://www.eia.gov/opendata/register.php>, then make it available to the
client — either in the environment:

```bash
export EIA_API_KEY=your_key_here          # PowerShell: $env:EIA_API_KEY="your_key_here"
```

or by passing it explicitly:

```python
from eia_api import EIA

eia = EIA(api_key="your_key_here")
```

The test suite does not need a key.
