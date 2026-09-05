# BTC Forecast Ensemble

A BTC/USD forecasting experiment built around **TimesFM 3**, Kraken hourly candles, multiple baselines, regime detection, adaptive ensemble weighting, calibrated uncertainty and walk-forward backtesting.

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
- includes a separate walk-forward backtesting workflow

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

The ensemble no longer relies only on fixed hand-tuned weights. For each horizon independently, it scores matured historical predictions from every underlying model and adjusts the weights from measured out-of-sample performance.

The adaptive score uses:

- recent MAE % as the main signal
- direction accuracy as a smaller reward
- signed bias as a penalty
- performance relative to the persistence baseline

The current market regime is preferred when there are enough matching samples. If regime-specific history is sparse, the system can use all recent regimes. With fewer than 6 matured samples per current model it falls back to the original static regime prior.

Adaptation is deliberately gradual. The learned distribution is blended with the static prior and reaches at most 80% of the final decision after enough observations. Every active model is constrained to a 3% minimum and 55% maximum weight, preventing one short lucky streak from taking over the ensemble.

If the non-persistence models are collectively failing to beat persistence, the weighting logic explicitly boosts the persistence baseline before the final bounded normalization.

Only target prices already present in completed historical candles are used when computing weights. Future outcomes are never consulted, avoiding look-ahead leakage in production and walk-forward tests.

`forecast.json` exposes the result per horizon:

```text
model_weights.2h
model_weights.4h
model_weights.8h
model_weights.16h
weighting_diagnostics.<horizon>
```

The diagnostics include static prior weight, recent sample count, MAE, direction accuracy, signed bias, raw adaptive score, learned weight, final weight, persistence edge, blend factor and whether the persistence fallback was activated.

The current implementation learns from the rolling Actions forecast history. GitHub issue #5 tracks moving that learning history to a durable long-term dataset so adaptive weighting can eventually use substantially more observations.

## Confidence and uncertainty

For each horizon the output includes the ensemble price, change from current price, Q10/Q50/Q90 interval, model agreement, weighting mode/sample count, interval calibration multiplier and empirical Q10-Q90 coverage once enough history exists.

The TimesFM quantile paths provide the starting uncertainty band. The band is widened when the models disagree and, after at least 10 matured samples, adjusted toward approximately 80% empirical Q10-Q90 coverage.

## Reliability metrics

The rolling history stores up to 72 snapshots. Matured predictions are matched to the exact Kraken hourly close at their target timestamp.

For each horizon the project tracks absolute USD error, MAE %, signed error/bias, predicted vs actual change, direction accuracy, Q10-Q90 coverage and per-model performance. `forecast.json` also includes an aggregate `performance_summary` from the history currently available.

## Schedule

GitHub Actions wakes up hourly at minute `37` UTC:

```yaml
schedule:
  - cron: "37 * * * *"
```

A lightweight guard restores forecast state first. The expensive forecast only runs after at least two completed candle-hours have elapsed since the previous saved forecast. This gives GitHub another opportunity every hour if a scheduled event is delayed or dropped while still producing roughly one forecast every two hours.

Scheduled forecasts post to X automatically. Manual runs only post when `post_to_x=true`.

## X posting

Posting uses Twikit with the `X_COOKIES_JSON` repository secret. The generated post is reformatted by `format_tweet.py` into the compact emoji-rich X layout before posting.

Twikit is an unofficial X client and can break when X changes its internal frontend/API behavior.

## Backtesting

`backtest.py` performs walk-forward historical forecasts and now compares the **adaptive ensemble**, the equivalent **static-prior ensemble**, persistence, each TimesFM context and the other simple baselines.

The manual **BTC Forecast Backtest** GitHub workflow defaults to 90 days of history and 60 walk-forward samples.

Historical backtesting uses Binance BTCUSDT hourly candles because Kraken's public OHLC endpoint does not expose enough older hourly candles for a multi-month walk-forward test. Production forecasting still uses Kraken BTC/USD.

Run locally:

```bash
python backtest.py --days 90 --samples 60
```

The backtest feeds only prior forecast snapshots into each new forecast, so adaptive weights can learn during the walk-forward run without seeing future targets.

The result is written to `backtest_report.json` with MAE %, mean signed error, direction accuracy, ensemble interval coverage and adaptive ensemble performance by regime.

The most important comparisons are whether the adaptive ensemble beats the static ensemble and whether either consistently beats `persistence`.

## Tests

Adaptive-weight safeguards have unit coverage for sparse-history fallback, favoring empirically better models, normalization and per-model weight floors/caps:

```bash
python -m unittest test_adaptive_weights.py
```

## Production output

`forecast.json` contains the latest BTC/USD close, market features, regime, per-horizon adaptive model weights, weighting diagnostics, per-model forecasts, ensemble 2h/4h/8h/16h forecasts, model agreement, calibrated uncertainty, latest matured reliability and rolling performance summary.

Forecast history lives in `.state/previous_forecast.json` and is persisted with the GitHub Actions cache rather than committed to the repository.

## Run locally

Python 3.11:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest test_adaptive_weights.py
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
