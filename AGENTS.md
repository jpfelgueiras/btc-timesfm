# Repository guidance

## Project overview

This is a Python 3.11–3.13 BTC/USD forecasting project. Application code lives in
`src/btc_timesfm/`, tests in `tests/`, operational scripts in `scripts/`, and
project documentation in `docs/`. The production workflow forecasts hourly
log returns, combines TimesFM contexts with baseline models, and persists
forecasts and matured outcomes in SQLite.

## Development

- Support Python 3.11, 3.12, and 3.13 and follow the `src/` package layout.
- Install locked development and test dependencies with `uv sync --locked
  --no-default-groups --group test` (runtime, model, test, and
  security dependency sets are declared in `pyproject.toml` and pinned in `uv.lock`).
- Set `PYTHONPATH=src` when running package commands directly from a checkout.
- Follow the existing style: type-aware Python, Ruff configuration from
  `pyproject.toml` (100-character configured line length), and focused changes.
- Keep tests alongside the relevant domain under `tests/` and use `unittest`.

## Validation

Before submitting changes, run the relevant tests and checks from
`CONTRIBUTING.md`:

```bash
PYTHONPATH=src python -m unittest discover -s tests -p 'test_*.py'
ruff check .
ruff format --check .
python scripts/lint_workflows.py
python scripts/lint_action_pins.py
```

For a focused change, run the relevant test module first; run the full suite
when practical. Some integration and browser tests may need additional local
setup.

## Forecasting and data integrity

- Preserve strict walk-forward/no-look-ahead behavior. A simulated forecast
  may use only information available at its origin; score it against future
  outcomes only after producing the forecast.
- Use exact, timezone-aware target timestamps when matching forecast outcomes.
- Preserve durable-history semantics: production forecast predictions and
  matured outcomes are write-once, and unsupported newer database schemas must
  not be silently modified.
- Treat optional data sources as fallible; retain existing validation,
  freshness, quarantine, and fallback behavior when changing data pipelines.
- Research optimizers and challenger evaluations are recommendation-only;
  do not make them alter production forecasts or configuration implicitly.

## Security and generated state

- Never commit credentials, X cookies, private keys, local state, generated
  forecasts, or unrelated formatting changes.
- Keep secrets out of logs, errors, and generated artifacts.
- SQLite state belongs under `.state/` and is not source-controlled.

## Documentation and operations

Update the relevant `README.md` or `docs/` material when commands,
configuration, API contracts, workflow behavior, or forecasting behavior
change. Keep GitHub Actions workflows consistent with the repository's
workflow and action-pin lint scripts. Production publication, X posting, and
promotion safeguards are operationally sensitive; preserve their validation
and review-oriented behavior.

For contribution process details, see `CONTRIBUTING.md`.
