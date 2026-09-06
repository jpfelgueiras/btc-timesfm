# How the forecast works

This page explains the BTC forecast from zero knowledge to the full implementation.

If you only want the short version:

1. The system downloads recent hourly BTC/USD candles from Kraken.
2. It turns price history into hourly log returns.
3. TimesFM predicts future return paths from several lookback windows.
4. Simple baseline models predict the same horizons.
5. The system detects the current market regime.
6. It blends the models using past out-of-sample performance.
7. It converts the blended return forecast back into prices.
8. It builds uncertainty intervals and saves everything to `forecast.json` and history.

## 1) Beginner explanation

Think of the forecast like this:

- we look at the recent BTC price history;
- we ask several models what they think happens next;
- we compare those answers with a few simple reference guesses;
- we decide which models deserve more trust right now;
- we produce four future prices: **2h, 4h, 8h, and 16h** ahead.

The forecast is not one single number from one model. It is an ensemble, meaning a weighted mix of multiple forecasts.

The system also produces uncertainty bands. Those bands are not a promise. They are a calibrated estimate of how wide the forecast should be based on recent evidence.

## 2) The full flow

```text
Kraken candles
  -> hourly closes, highs, lows, opens, volumes
  -> log returns
  -> feature extraction + regime detection
  -> TimesFM forecasts from multiple context windows
  -> baseline forecasts
  -> adaptive weights
  -> ensemble price forecast
  -> uncertainty interval calibration
  -> forecast confidence diagnostics
  -> forecast.json + history storage
```

## 3) Step-by-step breakdown

### 3.1 Fetch market data

The engine downloads hourly OHLC data from Kraken for the `XBTUSD` pair.

Important details:

- only completed candles are used;
- at least 64 completed candles are required;
- the system keeps enough candles for the largest context window;
- the largest context is 512 hours, so the fetch keeps at least 513 closes.

The forecast uses:

- `opens`
- `highs`
- `lows`
- `closes`
- `volumes`

### 3.2 Convert prices into log returns

The model does not directly forecast price levels. It forecasts hourly **log returns**:

```text
log(close[t] / close[t-1])
```

Why this matters:

- returns are usually more stable than raw BTC price levels;
- they make different price regimes easier to compare;
- a sequence of returns can be turned back into prices later.

To recover a future price, the system accumulates predicted returns and applies them to the current price:

```text
future_price = current_price * exp(cumulative_return)
```

### 3.3 Build market features

The engine computes descriptive features from the recent candles:

- 6h, 24h, and 7d realized volatility;
- average high-low range over 24h;
- volume z-score over 7d;
- RSI(14);
- 6h, 24h, and 7d momentum;
- hour-of-day and day-of-week cycles.

These features do not directly set the forecast price. They help decide what type of market is active.

### 3.4 Detect the regime

The market is classified into one of three regimes:

- `range`
- `trending`
- `high_volatility`

The rules are simple:

- if 24h volatility is much larger than 7d volatility, the market is treated as `high_volatility`;
- if momentum is strong or RSI is extreme, the market is treated as `trending`;
- otherwise it is `range`.

This matters because different models work better in different regimes.

### 3.5 Run TimesFM in multiple context windows

TimesFM is the main learned model.

Instead of giving it one history window, the system uses up to three:

- 168h = 7 days
- 336h = 14 days
- 512h = about 21 days

Each context window is forecast separately.

Why multiple windows?

- short windows react faster;
- long windows capture broader structure;
- no single window is always best.

For each context, TimesFM predicts:

- a point forecast path;
- quantiles for uncertainty (Q10, Q50, Q90).

### 3.6 Create baseline forecasts

The system also makes three simple forecasts:

- `persistence` — future price equals the current price;
- `drift_7d` — the average 7-day return continues forward;
- `ar1` — a simple autoregressive return model.

These baselines matter because a sophisticated model should be compared with very simple alternatives.

If TimesFM cannot beat persistence over time, the extra complexity is not helping.

### 3.7 Combine TimesFM and baselines

At this point the system has several candidate forecasts for each horizon.

The ensemble does not average them equally. It assigns weights.

The starting weights come from a hand-tuned prior based on regime:

- in `high_volatility`, TimesFM gets the largest share;
- in `trending`, drift gets more weight;
- in `range`, persistence gets more weight.

TimesFM context windows also get different prior shares:

- 168h: 25%
- 336h: 35%
- 512h: 40%

### 3.8 Adapt weights using historical performance

The system looks at matured historical forecasts and asks:

- which model had lower MAE?
- which model got direction right more often?
- which model had less bias?
- which model’s intervals were better calibrated?

This produces adaptive weights per horizon.

Important safety rules:

- if there is not enough history, use the static prior;
- if regime-specific history is sparse, fall back to all-regime history;
- weights stay inside hard bounds so one lucky run cannot dominate;
- if complex models stop beating persistence, persistence gets a fallback boost.

### 3.9 Produce the ensemble price forecast

For each horizon, the ensemble combines model return forecasts into one return forecast.

That blended return is converted back to a price.

The forecasted horizons are:

- `2h`
- `4h`
- `8h`
- `16h`

### 3.10 Build uncertainty intervals

Each horizon gets a Q10 / Q50 / Q90 interval.

The interval starts from:

- TimesFM quantiles;
- disagreement between models;
- a small minimum width.

Then it is adjusted using recent empirical coverage.

If past intervals were too narrow, the current interval is widened.
If they were too wide, it is tightened.

### 3.11 Compute forecast confidence

The separate confidence layer is conservative.

It is not a probability that the forecast is correct.
It is an evidence-quality score.

It only activates when there is enough matured history for:

- ensemble performance;
- interval calibration;
- persistence comparison;
- current interval availability.

The confidence score uses:

- edge vs persistence;
- calibration quality;
- sample depth;
- interval informativeness;
- drift severity.

Severe drift suppresses the confidence claim entirely.

### 3.12 Save outputs and history

The final result is written to `forecast.json` and stored in the forecast history.

That history is used later for:

- adaptive weighting;
- performance summaries;
- calibration;
- confidence diagnostics;
- backtesting.

## 4) What each output field means

### `predictions`

The final ensemble forecast for each horizon.

Each horizon includes:

- `price_usd`
- `change_pct`
- `q10_usd`
- `q50_usd`
- `q90_usd`
- `model_agreement`
- `weighting_mode`
- `weighting_samples`
- `interval_calibration_multiplier`
- `calibration_samples`
- `empirical_q10_q90_coverage`

### `model_predictions`

Every raw model forecast before ensembling.

This includes:

- `timesfm_168h`
- `timesfm_336h`
- `timesfm_512h`
- `persistence`
- `drift_7d`
- `ar1`

### `model_weights`

The final horizon-specific weights used for the ensemble.

### `market_features`

The inputs used for regime detection and analysis.

### `regime`

The current market state: `range`, `trending`, or `high_volatility`.

### `weighting_diagnostics`

Why the chosen weights were selected, including sample counts, blend factor, and fallback behavior.

## 5) Deep dive: the exact math

### 5.1 Adaptive weighting score

For each model, the system scores historical performance using:

- MAE %
- direction accuracy
- signed bias
- comparison to persistence

That score is converted into a positive weight and then blended with the regime prior.

The blend is gradual:

- little history -> mostly prior
- more history -> more learned weighting
- still capped so it cannot overreact

### 5.2 Interval calibration multiplier

For each horizon, the system checks recent historical coverage:

```text
did actual price land between Q10 and Q90?
```

If coverage is below target, the interval widens.
If coverage is above target, the interval can narrow.

This is why the system can adapt its uncertainty to reality instead of assuming the raw quantiles are always perfectly calibrated.

### 5.3 Confidence score

The confidence score is a weighted mix of four factors:

- relative edge vs persistence
- empirical interval calibration
- evidence sample depth
- interval width compared with history

Then it is reduced by drift.

That makes the confidence output conservative by design.

## 6) End-to-end summary

So, in one sentence:

> the forecast turns recent hourly BTC data into return-based multi-context model outputs, adjusts them with regime-aware learned weights, calibrates uncertainty from past outcomes, and publishes four future price horizons with diagnostics.

## 7) Where to look in the code

- `src/btc_timesfm/forecasting/forecast_engine.py` — the main forecast pipeline
- `src/btc_timesfm/forecasting/forecast_confidence.py` — evidence-quality confidence bands
- `src/btc_timesfm/cli/btc_forecast.py` — command-line entrypoint and history scoring
