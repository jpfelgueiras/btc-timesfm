# BTC Forecast Ensemble

A BTC/USD forecasting experiment built around **TimesFM 3**, Kraken hourly candles, multiple baselines, regime detection, adaptive ensemble weighting, calibrated uncertainty, durable production history and walk-forward backtesting.

## What changed

The production forecast no longer feeds raw BTC prices into one TimesFM context. It now:

- forecasts **hourly log returns** and reconstructs future prices
- runs TimesFM with **168h, 336h and 512h** context windows
- compares/ensembles TimesFM with **persistence, 7-day drift and AR(1)** baselines
- starts from regime-specific priors for **range, trending and high-volatility** markets
- adapts model weights separately for **2h, 4h, 8h and 16h** from matured out-of-sample performance
- derives market context from volatility, volume, high-low range, RSI, momentum and time-of-day/day-of-week
- reports **model agreement** as an additional confidence signal
- calibrates the Q10-Q90 interval from recent empirical coverage once enough samples exist
- evaluates 2h, 4h, 8h and 16h forecasts against the exact matching Kraken close
- tracks MAE, signed bias, direction accuracy and interval coverage
- stores per-model predictions so TimesFM can be compared directly with the simple baselines
- persists every logical production forecast in a durable SQLite history database
- includes walk-forward backtesting plus a scheduled weekly recommendation-only optimizer

A second foundation model is intentionally not part of the scheduled ensemble yet: loading another large checkpoint every run would substantially increase GitHub Actions runtime. The architecture now makes it straightforward to add one later if backtests show it is worth the extra compute.

## Forecast horizons

Every production run forecasts +2h, +4h, +8h and +16h. The main `predictions` block is the ensemble result. `model_predictions` contains each underlying model/context separately.

## Why forecast returns instead of raw price?

The model receives hourly log returns:

```text
log(close[t] / close[t-1])
```

The predicted return path is accumulated and converted back into a BTC price. Returns are generally more stable than the raw BTC price level and make forecasts across different market-price regimes more comparable.

## Multi-context TimesFM

Each forecast uses three views of the market when enough completed candles are available:

```text
168 hours   = 7 days
336 hours   = 14 days
512 hours   = ~21 days
```

The contexts are forecast independently and then combined. This reduces dependence on one arbitrary context length.

## Baselines

The ensemble also contains:

- `persistence`: future BTC = current BTC
- `drift_7d`: extrapolates the mean 7-day log return, with volatility clipping
- `ar1`: a simple autoregressive forecast of hourly log returns

These are deliberately simple. If TimesFM cannot consistently beat persistence in the backtest, the more complex forecast is not adding much useful signal.

## Market features and regime detection

Each run records 6h/24h/7d realized volatility, average high-low range, volume z-score, RSI(14), 6h/24h/7d momentum and cyclic hour/day features.

Those features classify the market as `range`, `trending` or `high_volatility`. The regime defines the initial prior weights and is also used to select comparable historical forecasts for adaptive weighting.

## Adaptive ensemble weighting

For each horizon independently, the ensemble scores matured historical predictions from every underlying model and adjusts weights from measured out-of-sample performance.

The adaptive score uses:

- recent MAE % as the main signal
- direction accuracy as a smaller reward
- signed bias as a penalty
- Q10-Q90 coverage as a small calibration penalty when a model exposes quantiles
- performance relative to the persistence baseline

Production weighting consumes the **matured outcomes stored in the durable SQLite history**, so observations remain usable after their Kraken candles have fallen outside the recent OHLC API window. During walk-forward backtests there is no durable outcome shortcut: a previous forecast can only be scored once its target candle is already visible at the current simulated origin.

The current market regime is preferred when there are enough matching samples. If regime-specific history is sparse, the system falls back to recent observations from all regimes. With fewer than 6 matured samples per current model it uses the static regime prior.

The rolling sample limit defaults to **200 relevant matured observations per model/horizon/regime** and applies *after* regime filtering, rather than simply slicing the latest 200 forecast origins. It can be changed without code changes:

```bash
export BTC_ADAPTIVE_HISTORY_LIMIT=300
```

Adaptation is deliberately gradual. The learned distribution is blended with the static prior and reaches at most 80% of the final decision after enough observations. Every active model is constrained to a 3% minimum and 55% maximum weight, preventing one short lucky streak from taking over the ensemble.

If the non-persistence models are collectively failing to beat persistence, the weighting logic explicitly boosts the persistence baseline before the final bounded normalization.

`forecast.json` exposes the result per horizon:

```text
model_weights.2h
model_weights.4h
model_weights.8h
model_weights.16h
weighting_diagnostics.<horizon>
```

The diagnostics include adaptive/static mode, selected history source, configured history limit, per-model sample count, MAE, direction accuracy, signed bias, Q10-Q90 coverage and interval sample count, durable-vs-current-candle outcome counts, raw adaptive score, learned weight, final weight, persistence edge, blend factor and whether the persistence fallback was activated.

This makes the weighting reproducible and observable while preserving strict no-look-ahead behavior. Sparse samples shrink toward the existing prior; sufficient samples can move each horizon and regime to a different learned model mix.

## Durable production history

`history_store.py` implements the long-term source of truth for production forecasts. The canonical database is:

```text
.state/forecast_history.sqlite
```

The database is **not committed to the source tree**. GitHub Actions compresses it and stores it in the dedicated GitHub Release tagged:

```text
forecast-history-v1
```

That Release contains:

```text
forecast_history.sqlite.gz           canonical current database
forecast_history.csv.gz              denormalized analysis export
forecast_history.previous.sqlite.gz  previous known-good database generation
```

The workflow uses only the repository `GITHUB_TOKEN`; no additional storage secret is required. The forecast job has `contents: write` solely so it can update these Release assets.

### History schema

SQLite schema version **1** uses two main tables:

`forecast_origins` stores one row per completed source candle used as a forecast origin:

```text
origin_at
generated_at
source_name
pair
source_price_usd
regime
market_features_json
first_seen_at
last_seen_at
```

`forecast_predictions` stores one row per origin, model and horizon:

```text
origin_at
model_name
horizon_hours
target_at
predicted_price_usd
predicted_change_pct
q10_usd / q50_usd / q90_usd
model_agreement
ensemble_weight
actual_target_price_usd
absolute_error_usd
absolute_error_pct
signed_error_pct
actual_change_pct
direction_correct
within_q10_q90
matured_at
```

The logical primary key is:

```text
(origin_at, model_name, horizon_hours)
```

The ensemble itself is stored as model name `ensemble`, alongside `timesfm_168h`, `timesfm_336h`, `timesfm_512h`, `persistence`, `drift_7d` and `ar1` when those models are available.

Schema versioning is recorded both in `PRAGMA user_version` and the `metadata` table. A database created by a newer unsupported schema is rejected rather than silently modified.

### Append safety and manual reruns

Forecast rows are **first-write-wins**. A manual rerun for the same source candle does not create a duplicate and does not rewrite the original prediction. It only updates the origin's `last_seen_at` timestamp and can fill outcomes that were previously missing.

This is intentional: the historical dataset must preserve the forecast that was actually first observed, rather than allowing a later rerun to rewrite research history.

The scheduled workflow also uses one concurrency group, so only one forecast job can update the Release-backed database at a time.

### Outcome maturation

Every production run compares all pending database rows with the completed Kraken candles currently available. A row matures only when the **exact target candle timestamp** exists.

For a matured row the store persists:

- actual target price
- absolute USD and percentage error
- signed error
- actual percentage move
- direction correctness
- Q10-Q90 interval coverage when quantiles exist
- maturation timestamp

Outcomes are also write-once. Once an exact target candle has been persisted, a later run cannot overwrite it.

Because the longest live horizon is 16h and Kraken exposes much more than 16 hours of recent OHLC data, normal scheduled operation has ample time to mature every prediction. If the workflow is disabled for an unusually long period, pending rows remain explicitly pending instead of being guessed from another exchange.

### Query and export

After obtaining the SQLite asset, analysis does not need to scrape Actions runs or artifacts.

Verify and inspect the database:

```bash
python history_store.py --db forecast_history.sqlite verify
python history_store.py --db forecast_history.sqlite stats
python history_store.py --db forecast_history.sqlite summary
```

Export to CSV or JSON Lines:

```bash
python history_store.py --db forecast_history.sqlite export \
  --format csv --output forecast_history.csv

python history_store.py --db forecast_history.sqlite export \
  --format jsonl --output forecast_history.jsonl
```

Download the current database with GitHub CLI:

```bash
gh release download forecast-history-v1 \
  --pattern forecast_history.sqlite.gz
gunzip forecast_history.sqlite.gz
```

The CSV export is also attached directly to the same Release for quick notebook/spreadsheet analysis.

### Retention and recovery

The `forecast-history-v1` Release is intended to be retained indefinitely. At the current forecast cadence the normalized SQLite database remains small; the project can rotate to a new history release/version if it eventually becomes operationally large.

Before each mutation, the workflow keeps the successfully restored database as `forecast_history.previous.sqlite.gz`. The new database is verified with SQLite integrity and foreign-key checks before it is uploaded. If the current asset is damaged, the previous asset is therefore a one-generation recovery point.

The workflow will only overwrite an existing Release when that same run successfully restored it first. This prevents a transient download/authentication failure from replacing the real dataset with a newly initialized empty database.

The small `.state/previous_forecast.json` Actions cache remains in place for fast scheduler decisions. It is not the long-term source of truth, but it also provides a bootstrap source for recent forecasts if the durable database is being created for the first time.

## Confidence and uncertainty

For each horizon the output includes the ensemble price, change from current price, Q10/Q50/Q90 interval, model agreement, weighting mode/sample count, interval calibration multiplier and empirical Q10-Q90 coverage once enough history exists.

The TimesFM quantile paths provide the starting uncertainty band. The band is widened when the models disagree and, after at least 10 matured samples, adjusted toward approximately 80% empirical Q10-Q90 coverage.

## Reliability metrics

Matured predictions are matched to the exact Kraken hourly close at their target timestamp.

For each horizon the project tracks absolute USD error, MAE %, signed error/bias, predicted vs actual change, direction accuracy, Q10-Q90 coverage and per-model performance. `forecast.json` includes an aggregate `performance_summary` backed by the durable database once matured records exist.

## Schedule

GitHub Actions wakes up hourly at minute `37` UTC:

```yaml
schedule:
  - cron: "37 * * * *"
```

A lightweight guard restores the rolling scheduler state first. The expensive forecast only runs after at least two completed candle-hours have elapsed since the previous saved forecast. This gives GitHub another opportunity every hour if a scheduled event is delayed or dropped while still producing roughly one forecast every two hours.

When a forecast is due, the durable Release database is restored before model execution, updated and verified after the forecast, and published back before any X post is sent.

Scheduled forecasts post to X automatically. Manual runs only post when `post_to_x=true`.

## X posting

Posting uses Twikit with the `X_COOKIES_JSON` repository secret. The generated post is reformatted by `format_tweet.py` into the compact emoji-rich X layout before posting.

Twikit is an unofficial X client and can break when X changes its internal frontend/API behavior.

## Backtesting

`backtest.py` performs walk-forward historical forecasts and compares the **adaptive ensemble**, the equivalent **static-prior ensemble**, persistence, each TimesFM context and the other simple baselines.

The manual **BTC Forecast Backtest** GitHub workflow defaults to 90 days of history and 60 walk-forward samples.

Historical backtesting uses Binance BTCUSDT hourly candles because Kraken's public OHLC endpoint does not expose enough older hourly candles for a multi-month walk-forward test. Production forecasting still uses Kraken BTC/USD.

Run locally:

```bash
python backtest.py --days 90 --samples 60
```

The backtest feeds only prior forecast snapshots into each new forecast, so adaptive weights can learn during the walk-forward run without seeing future targets. It uses the same adaptive scoring policy and configured history limit as production.

The result is written to `backtest_report.json` with MAE %, mean signed error, direction accuracy, ensemble interval coverage and adaptive ensemble performance by regime.

The most important comparisons are whether the adaptive ensemble beats the static ensemble and whether either consistently beats `persistence`.

## Weekly walk-forward optimization

`optimizer.py` is a bounded research optimizer that runs automatically every Sunday at **04:17 UTC** through the **Weekly Forecast Optimizer** workflow. It is deliberately recommendation-only: it cannot change production parameters, commit configuration changes or open a promotion PR.

The default weekly experiment downloads 120 days of Binance BTCUSDT hourly history and evaluates 48 walk-forward origins. TimesFM forecasts are generated **once per origin**. The optimizer then replays a small deterministic catalog of candidate weighting configurations over those frozen per-model forecasts, avoiding a separate TimesFM inference pass for every candidate and keeping Actions runtime predictable.

The bounded catalog currently explores conservative variations of:

- TimesFM context combinations, including long-context-only variants
- adaptive minimum/full sample thresholds
- rolling history length
- MAE sensitivity
- direction-accuracy reward
- adaptation blend strength
- model weight floors/caps
- persistence fallback strength
- interval-coverage penalty

At each simulated origin the weighting policy only receives candle outcomes whose timestamp is **less than or equal to that origin**. Future target prices are used only after the forecast has been produced to score that origin. This keeps the candidate replay strictly walk-forward and prevents look-ahead leakage.

Every candidate is compared against both the **currently deployed production configuration** and **persistence**. Reports include metrics by horizon, metrics by detected regime, chronological fold stability, selected parameters and relative improvement.

A candidate is only marked `candidate_worth_review` when all current promotion guardrails pass:

- at least 32 walk-forward origins
- at least 3% mean relative MAE improvement over production
- no horizon degrades by more than 5% relative MAE
- mean direction accuracy does not fall by more than 2 percentage points
- at least 2 of 3 chronological folds improve
- the worst fold does not degrade by more than 2%

Otherwise the recommendation is `keep_current`, even when a candidate has the numerically lowest aggregate MAE. This intentionally penalizes improvements that are concentrated in one horizon or one unusually favorable time segment.

The workflow writes:

```text
optimizer_report.json   complete machine-readable inputs, candidates and metrics
optimizer_summary.md    concise human-readable Actions summary
```

Both files are uploaded as a GitHub Actions artifact for 90 days. Because the report records the tested period, candidate catalog, selected parameters and guardrails, a run can be reproduced from the same code revision and CLI inputs.

Run the same optimizer manually:

```bash
python optimizer.py --days 120 --samples 48
```

The promotion policy is intentionally manual for now. A `candidate_worth_review` result is evidence to inspect and rerun with a larger sample, not authorization to deploy it. Automatic parameter PR creation should only be considered after the weekly optimizer has shown stable recommendations over multiple independent runs.

## Tests

Run the complete unit-test suite:

```bash
pip install -r requirements-test.txt
python -m unittest discover -s tests -p 'test_*.py' -v
```

The adaptive tests cover sparse-history fallback, current-regime/all-regime selection, durable outcomes beyond the recent candle window, configurable rolling limits, interval-coverage scoring, persistence fallback and strict weight floors/caps. The history-store tests cover schema verification, idempotent manual reruns, first-write-wins predictions, exact-target maturation, write-once outcomes, rolling-cache migration, adaptive-history reconstruction and CSV/JSONL export. Optimizer tests cover bounded/reproducible candidate generation, temporary parameter isolation, chronological folds, strict origin-time outcome visibility and promotion guardrails.

## Production output

`forecast.json` contains the latest BTC/USD close, market features, regime, per-horizon adaptive model weights, weighting diagnostics, per-model forecasts, ensemble 2h/4h/8h/16h forecasts, model agreement, calibrated uncertainty, latest matured reliability, durable performance summary and `history_store` statistics.

The durable history lives in the `forecast-history-v1` GitHub Release. `.state/previous_forecast.json` remains only as a small rolling scheduler cache.

## Run locally

Python 3.11:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -p 'test_*.py' -v
python btc_forecast.py
```

To post the generated `tweet.txt` locally:

```bash
export X_COOKIES_JSON="$(cat x_cookies.json)"
python post_to_x.py
```

## Important

This is an experiment, not a trading signal or financial advice. Backtesting is required before interpreting direction accuracy or forecast errors as useful predictive skill.

TimesFM 3 pretrained weights have their own license terms; verify the model license before production or commercial use.

## Interval calibration

Prediction intervals use finite-sample conformal calibration once enough matured history exists, with the previous empirical multiplier as the sparse-history fallback. See `CONFORMAL_CALIBRATION.md` for configuration and diagnostics.
