# BTC Forecast Ensemble

A BTC/USD forecasting experiment built around **TimesFM 3**, Kraken hourly candles, multiple baselines, regime detection, calibrated uncertainty and walk-forward backtesting.

## What changed

The production forecast no longer feeds raw BTC prices into one TimesFM context. It now:

- forecasts **hourly log returns** and reconstructs future prices
- runs TimesFM with **168h, 336h and 512h** context windows
- compares/ensembles TimesFM with **persistence, 7-day drift and AR(1)** baselines
- changes ensemble weights for **range, trending and high-volatility** regimes
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

Each forecast uses three views of the market:

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

Those features classify the market as `range`, `trending` or `high_volatility`. The regime changes the relative weights of TimesFM, persistence, drift and AR(1), and the features are saved for later analysis.

## Confidence and uncertainty

For each horizon the output includes the ensemble price, change from current price, Q10/Q50/Q90 interval, model agreement, interval calibration multiplier and empirical Q10-Q90 coverage once enough history exists.

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

Posting uses Twikit with the `X_COOKIES_JSON` repository secret. Example:

```text
BTC/USD ensemble forecast
Now $100,000 | trending
2h $100,200 (+0.20%) | 4h $100,450 (+0.45%)
8h $100,800 (+0.80%) | 16h $101,100 (+1.10%)
Prev err: 2h 0.31% | 4h 0.42% | 8h 0.75% | 16h 1.10%
Experimental - not financial advice.
```

Twikit is an unofficial X client and can break when X changes its internal frontend/API behavior.

## Backtesting

`backtest.py` performs walk-forward historical forecasts and compares the ensemble against every underlying model.

The manual **BTC Forecast Backtest** GitHub workflow defaults to 90 days of history and 60 walk-forward samples.

Historical backtesting uses Binance BTCUSDT hourly candles because Kraken's public OHLC endpoint does not expose enough older hourly candles for a multi-month walk-forward test. Production forecasting still uses Kraken BTC/USD.

Run locally:

```bash
python backtest.py --days 90 --samples 60
```

The result is written to `backtest_report.json` with MAE %, mean signed error, direction accuracy, ensemble interval coverage and ensemble performance by regime.

The most important comparison is whether the ensemble and the individual TimesFM contexts beat `persistence` consistently.

## Production output

`forecast.json` contains the latest BTC/USD close, market features, regime, model weights, per-model forecasts, ensemble 2h/4h/8h/16h forecasts, model agreement, calibrated uncertainty, latest matured reliability and rolling performance summary.

Forecast history lives in `.state/previous_forecast.json` and is persisted with the GitHub Actions cache rather than committed to the repository.

## Run locally

Python 3.11:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
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
