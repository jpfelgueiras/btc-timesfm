# Reading the forecast dashboard

The public BTC/USD forecast dashboard is a **static snapshot** built from the
durable forecast history stored in SQLite. Its contents reflect the data
available when that page was generated; opening or refreshing the page does not
request a new forecast. The dashboard has three main tabs: **Overview**,
**Forecast History**, and **Model Metrics**.

## Overview: the latest recorded forecast

The Overview shows the latest recorded ensemble forecast, if one is present.
The source price is the BTC price observed at the forecast origin. Each card
shows a forecast for **2, 4, 8, or 16 hours** after that origin:

- **Predicted BTC price** is the point estimate for the target time.
- **Up/Down and percentage change** show that point estimate's change relative
  to the source price. They describe the point estimate, not a certainty about
  the eventual move.
- **Target time** is the timestamp for which the forecast was made.
- The **80% interval** in a history record, or **q10–q90 prediction interval**
  on the Overview, gives the lower q10 and upper q90 price estimates. **q50**
  is the median (50th percentile) estimate; it may differ slightly from the
  displayed point estimate. The interval is a
  model uncertainty range, not a guaranteed range and not a probability of
  profit.
- **P(up)** and **P(down)**, when shown, are calibrated directional estimates
  for a move above or below the origin price. They are distinct from the
  q10–q90 price interval. Calibration can be based on sparse evidence, so do
  not read a displayed probability as certainty.
- The separate **Ensemble edge vs persistence** metric compares the ensemble
  against a simple persistence baseline; an edge/no-edge diagnostic does not
  make the forecast direction certain. **Model agreement** (where described in
  forecast details) indicates how similarly models estimate a forecast, not a
  probability that it will be right.

The origin timestamp tells you when the forecast was issued, and **Page
generated** tells you when this static snapshot was built. The freshness badge
reports elapsed time since the forecast origin (more than four hours is labeled
**Forecast stale**). This measures forecast age; it does not independently
verify that source market data was fresh.

For example only (illustrative, not a recorded forecast): if the source price
were $60,000 and the 4h point estimate were $60,600, the displayed change would
be +1%. An interval of $59,000–$62,000 would communicate a broad range of model
estimates, not that a trade has an 80% chance to make money.

## Forecast History: inspect individual records

Use **Forecast History** to look through the recorded forecasts. The explorer
can filter by horizon and age of origin (7, 30, or 90 days, or all history),
search the visible origin, horizon, regime, or status text, and sort by newest
origin, horizon, or lowest error. The table gives origin, horizon, forecast,
actual, error, and status. Open a row's origin or its detail entry to see the
source price, predicted price and change, q10–q90 interval (labeled “80%
interval”), target and regime, plus actual outcome and error when available.

An outcome is **Pending** until its target time has passed and an actual target
price is available. Once matured, the actual price and error describe the
realized outcome for that specific forecast. Individual wins and losses do not
by themselves establish future performance.

## Model Metrics: historical results

Metrics summarize forecasts whose target outcomes have matured. Select among
the available evaluation windows (**7 days, 30 days, 90 days, All time**) and
read each result together with its horizon and **Sample count (n)**. Different
horizons and windows refer to different sets of forecasts.

- **MAE %** is mean absolute percentage price error: the average absolute
  difference between forecast and actual target prices, expressed as a
  percentage of the actual price. Lower is better; it measures price distance,
  not direction accuracy.
- **Direction accuracy** is the fraction of matured forecasts whose predicted
  move direction matched the realized move.
- **q10–q90 coverage** is the share of matured actual target prices that fell
  between the forecast's q10 and q90 estimates. It is an observed historical
  coverage rate, not a probability of profit.
- **Quantile fans & performance trends** plot matured forecast points and
  aggregate metrics over time. A point represents a forecast issued at its
  labeled origin for a particular horizon, not a continuous price path.
- **Ensemble edge vs persistence** compares historical ensemble and persistence
  results on paired samples. It is a retrospective comparison, not a promise of
  advantage.

`n` is essential context: a metric based on few examples can move substantially
when another outcome arrives. **Low sample** marks limited evidence, and
**Insufficient history** means no eligible matured samples are available for
that cell. A blank metric means it could not be computed from the available
records. If no forecast history is included, the Overview explains that there
are no records or no latest forecast; an empty history or chart can also mean
there are no matching or matured records yet.

## What the dashboard cannot tell you

The dashboard is **not a current BTC quote**, a live feed, or an API/backend
health monitor. A recent forecast badge only says that the recorded origin is
recent relative to page generation. The dashboard cannot establish that prices
or services are currently available.

Forecasts and historical metrics are experimental summaries, not guarantees,
financial advice, or trading signals. Future conditions can differ from the
historical evaluation period. Neither a point estimate, directional
probability, model agreement, confidence label, nor prediction interval ensures
an outcome.

## Technical details

- [Forecast lifecycle](FORECAST_WORKS.md) explains how forecasts and outcomes
  are produced and evaluated.
- [Forecast confidence](FORECAST_CONFIDENCE.md) describes the evidence-quality
  band; it is not a probability that a forecast will be correct.
- [Direction probabilities](DIRECTION_PROBABILITY.md) documents directional
  probability calibration and its evidence requirements.
- [Public dashboard and metrics](PERFORMANCE_DASHBOARD.md) describes dashboard
  generation and evaluation methodology.
