# Developer guide

This guide adds practical orientation for contributors. The repository's
[contribution process](https://github.com/jpfelgueiras/btc-timesfm/blob/main/CONTRIBUTING.md) and [agent/repository rules](https://github.com/jpfelgueiras/btc-timesfm/blob/main/AGENTS.md)
are canonical for setup requirements, validation commands, and general safeguards.
Supported Python versions are 3.11, 3.12, and 3.13; dependencies are pinned in
`uv.lock` and managed with the supported `uv` version (0.12.x).

## Clean setup and checks

From the repository root, create an environment with Python 3.11, install the
locked test dependencies, and activate it:

```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install uv==0.12.19
uv sync --locked --no-default-groups --group test
```

Set `PYTHONPATH=src` for direct package commands from a checkout. Run one module
with `PYTHONPATH=src python -m unittest tests.forecasting.test_forecast_engine`;
run the full suite and repository checks with the commands in
[CONTRIBUTING.md](https://github.com/jpfelgueiras/btc-timesfm/blob/main/CONTRIBUTING.md). In particular, CI runs unittest tests,
coverage (75% across configured critical modules), Ruff lint and format checks,
workflow and action-pin linters, and mypy on its selected critical modules.
Security dependency auditing is defined in the security workflow with
`pip-audit`. For the exact current mypy file list and CI invocation, consult
`.github/workflows/tests.yml`; those selected files are the CI type-check scope,
not a promise that every module is checked.

For dashboard browser tests, install Chromium once in the environment:

```bash
python -m playwright install --with-deps chromium
```

Then run the targeted test or full unittest suite. Tests under `tests/web/` that
exercise a browser need Playwright and Chromium; static-site and contract tests
can often run without a browser. Integration tests may need additional local
setup or network access. The dedicated `tests/forecasting/test_real_timesfm/`
contract exercises the actual model and may require the optional model extra,
model artifact download, and network access. Most forecasting tests use local
stubs/fixtures and do not need model weights or network. Install the real model
extra only when needed (`uv sync --locked --no-default-groups --group test
--extra model`); do not treat a stub test as validation of actual model loading.

To build the documentation as CI does, include its locked dependency group and
build strictly:

```bash
uv sync --locked --no-default-groups --group docs
mkdocs build --strict
```

## Repository map

- `src/btc_timesfm/data/`: provider adapters, validation, and derived features.
- `src/btc_timesfm/forecasting/`: production forecast construction, policies,
  calibration, and feature/model support.
- `src/btc_timesfm/research/`: evaluation, backtests, experiments, and
  recommendation-only research tools.
- `src/btc_timesfm/history/`: durable SQLite forecast/outcome history and its
  audit, backup, and migration tools.
- `src/btc_timesfm/api/`: forecast-facing API contracts and service logic.
- `src/btc_timesfm/web/`: generation and validation of the static dashboard.
- `src/btc_timesfm/ops/`, `cli/`, and `x/`: operations safeguards, command-line
  entry points, and X publication integration.
- `tests/`: tests grouped by domain; `scripts/`: repository lint/check scripts;
  `.github/workflows/`: CI, security, and Pages build definitions.

The dashboard is a generated static site. This repository does not bundle a
general-purpose API server or require a container or Node-based build.

## Where to test

Use the test package that matches the behavior being changed:

| Domain | Tests | Typical coverage |
| --- | --- | --- |
| Data/provider | `tests/data/` | Parsing, validation, time alignment, gaps, missingness, optional-source health and quarantine |
| Forecasting | `tests/forecasting/` | Features, forecast engine, model behavior, calibration, policies and cross-validation |
| Research | `tests/research/` | Backtests, experiments, metrics and recommendation/evidence gates |
| History | `tests/history/` | SQLite persistence, migrations, backup, audit and recovery |
| API | `tests/api/` | Payload fields, identity and contract validation |
| Static web | `tests/web/` | Generated output, schema/contract, accessibility and browser behavior |
| Integration/operations/CLI | `tests/integration/`, `tests/ops/`, `tests/cli/` | Workflow boundaries, safeguards and entry points |

For static dashboard changes, pair focused `tests/web/` checks (for example
`test_static_site.py` and `test_site_contract.py`) with the relevant accessibility,
browser, or UI regression test. Keep generated output and the downloadable JSON
contract aligned; the site contract validator and static-site tests protect this
boundary.

## Safe changes to data providers and optional features

When adding a provider or optional input, follow existing adapters and validation
patterns in `src/btc_timesfm/data/`:

1. Normalize event/capture times to timezone-aware UTC and document whether a
   timestamp denotes event time, publication time, or observation time. Convert
   source units explicitly (including price/quantity units); never infer units
   from magnitude silently.
2. Validate shape, finiteness, ordering, freshness, and completeness before the
   values can affect a forecast. Define missing/stale behavior explicitly; an
   optional source should degrade safely rather than invalidate otherwise sound
   inputs or silently fabricate values.
3. Preserve provenance/lineage and feature-set versioning so derived values can
   be audited and replayed. Keep retained raw inputs where the existing domain
   supports reconstruction.
4. Treat revisions, stale data, incomplete responses, and unavailable sources
   as observable conditions. Preserve quarantine, fallback, and no-publication
   behavior where applicable; do not let a quarantined optional source leak
   into derived features.
5. Add focused tests for valid input, malformed or missing input, stale/revised
   data, units and UTC boundaries, and the resulting lineage/quarantine behavior.

Follow feature registries and explicit feature mappings in the forecasting
modules. A new optional feature must not become implicitly enabled merely
because its implementation exists.

## Model and evaluation changes

Keep model choices explicit in the production composition and mappings. There is
no plugin registry: adding a model means wiring it deliberately through the
relevant forecast path, defining its input/output and failure behavior, and
testing that mapping. Preserve deterministic stub-based tests for normal unit
coverage; use the real-TimesFM contract test for model integration when its
optional dependencies and weights are available. Research challengers and
optimizers are recommendation-only until explicit reviewed configuration makes
a change; they must not mutate production configuration implicitly.

Backtests must remain strict walk-forward simulations. At each forecast origin,
use only information available then; fit preprocessing, feature selection,
calibration, and weights within the training portion of each fold. Nested folds
are required when selecting/tuning among alternatives so the reported outer
evaluation is not also used for selection. Score future outcomes only after the
forecast has been produced.

## Forecasting time and leakage safeguards

- Treat a candle as usable only once its close-time has completed. Represent
  close/origin times in timezone-aware UTC; a candle's open time is not evidence
  that its final close or derived high/low/volume was already known.
- Compute features as-of the forecast origin. Every source observation and
  derived feature must have been available by that origin; respect publication
  and capture delays, not merely the timestamp the source assigns to an event.
- Keep target/outcome data out of feature construction and model fitting. Mature
  and score outcomes only when the exact timezone-aware target timestamp is
  reached; do not join on date, nearest row, or rounded timestamp.
- In rolling or nested evaluation, isolate each fold's training data, including
  imputation, scaling, selection, hyperparameters, and calibration. Do not use
  a future fold to choose a past forecast's behavior.
- Add regression tests with boundary timestamps and deliberately future-dated
  inputs to demonstrate that look-ahead values are excluded.

See [AGENTS.md](https://github.com/jpfelgueiras/btc-timesfm/blob/main/AGENTS.md) for the durable-history, unsupported-schema,
optional-source, and production-safeguard rules.

## API and static dashboard contracts

Treat fields consumed by the static dashboard as a versioned schema contract.
When changing a forecast/API field, update its producer, validation, fixtures,
and consumers together. Keep required fields and types explicit; preserve
timezone-aware timestamps, stable forecast identity, units, and null/missing
semantics. Reject malformed or unsupported schema versions rather than silently
guessing, and retain backward compatibility only where the current contract
intends it. Update `tests/api/` contract tests and the `tests/web/` static site
and site-contract tests for user-visible payload changes. Static tests should
assert both populated and empty/failure states as appropriate; browser tests
cover behavior that cannot be verified from generated HTML alone.

## Documentation changes

Keep setup and contribution policy in [CONTRIBUTING.md](https://github.com/jpfelgueiras/btc-timesfm/blob/main/CONTRIBUTING.md),
repository-wide coding/safety constraints in [AGENTS.md](https://github.com/jpfelgueiras/btc-timesfm/blob/main/AGENTS.md), and
domain-specific implementation guidance here. Update the source docs and links
when behavior or a contract changes, then run `mkdocs build --strict` to catch
broken references and warnings.
