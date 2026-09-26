# Getting started

The project supports Python 3.11–3.13. Install [uv](https://docs.astral.sh/uv/),
then sync the locked runtime and test dependencies:

```sh
uv sync --locked --no-default-groups --group test
```

The optional model dependency set is installed with `uv sync --locked --extra model`.
Set `PYTHONPATH=src` when running package modules from a checkout. Run unit tests
with `PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'`.

## Local documentation and dashboard

Install the locked documentation generator and build both static sites:

```sh
uv sync --locked --no-default-groups --group docs
mkdocs build --strict
PYTHONPATH=src python -m btc_timesfm.web.static_site --db .state/forecast_history.sqlite --output-dir build/forecasts
```

For a live docs-only preview, run `mkdocs serve`. To preview the combined Pages
artifact, build both outputs as above and run
`python -m http.server 8000 --directory build`; then open
`http://localhost:8000/` or `http://localhost:8000/forecasts/`. The generated
documentation occupies `build/`; the forecast dashboard is generated at
`build/forecasts/`. Dashboard history is local runtime state and is not
downloaded by the docs builder.
