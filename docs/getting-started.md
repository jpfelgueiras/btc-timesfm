# Getting started

## Requirements

Use Python 3.11, 3.12, or 3.13 and [uv](https://docs.astral.sh/uv/). The project
pins its supported Python range (`>=3.11,<3.14`) and dependency versions in
`pyproject.toml` and `uv.lock`. Install uv using its
[official instructions](https://docs.astral.sh/uv/getting-started/installation/).
No Node.js, Docker, or GPU is required. SQLite history is stored locally under
`.state/`; that directory is git-ignored.

## Install and run tests

From the repository root, this installs the locked application and test
dependencies without the optional TimesFM model or other dependency groups:

```sh
uv sync --locked --no-default-groups --group test
PYTHONPATH=src uv run python -m unittest discover -s tests -p 'test_*.py'
```

The test-only setup does not download or install TimesFM/Torch and cannot run a
real model forecast. For development checks, the repository also uses `ruff
check .` and `ruff format --check .` after installing the test group.

## Generate a local forecast

Install the optional CPU model dependencies, then run the same validated
forecast entry point used by the scheduled workflow:

```sh
uv sync --locked --no-default-groups --extra model
PYTHONPATH=src uv run python -m btc_timesfm.ops.validated_entrypoints forecast
```

The first run needs internet access to fetch market data and the pinned
TimesFM 3.0.2 checkpoint from Hugging Face; package installation also needs
access to the configured Python package index. The model runs on CPU, requires
no GPU, and its download can be large. Forecasting writes `forecast.json`,
`tweet.txt`, signal/report files in the repository root, and rolling/durable
SQLite state under `.state/` (including `forecast_history.sqlite` and
`shadow_deployment.sqlite`). Keep `.state/` as local runtime state; it is not
source-controlled. Further detail is in [Forecasting](forecasting.md) and
[Data](data.md).

Forecast generation does not require X cookies. Posting to X is a separate,
optional operation that requires X credentials; see [X posting](X_POSTING.md).

## Build and preview documentation

Install the locked documentation dependencies and build the docs locally:

```sh
uv sync --locked --no-default-groups --group docs
uv run mkdocs build --strict
uv run mkdocs serve
```

The strict build writes the documentation site to `build/`; `mkdocs serve`
serves a live docs preview, usually at <http://127.0.0.1:8000/>. The separate
static forecast dashboard is published at the
[forecast dashboard](https://jpfelgueiras.github.io/btc-timesfm/forecasts/).
Dashboard generation needs a local history database and is not part of the
documentation build. Once one exists, it can be generated with:

```sh
PYTHONPATH=src uv run python -m btc_timesfm.web.static_site \
  --db .state/forecast_history.sqlite --output-dir build/forecasts
```

The dashboard generator reads the database you provide; it does not download
production history or start an API service. See [dashboard operations](PERFORMANCE_DASHBOARD.md)
for more information.
