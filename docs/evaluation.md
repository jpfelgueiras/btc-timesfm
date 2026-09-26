# Evaluation: backtests, metrics, and evidence

Historical forecast scores describe how predictions matched past outcomes; they
do not guarantee future performance. This page explains what the repository's
backtest and optimizer measure, what their data does and does not represent, and
the current evidence limits. See [statistical evidence](STATISTICAL_EVIDENCE.md),
[cross-validation](CROSS_VALIDATION.md), [benchmarks](BENCHMARKS.md), and the
[research status index](research/index.md) for specialist details.

## Running `backtest.py`

Run from the checkout with `PYTHONPATH=src`:

```bash
PYTHONPATH=src python -m btc_timesfm.research.backtest
```

The command writes `backtest_report.json` in the current directory. Its options
are:

| Option | Default | Meaning |
|---|---:|---|
| `--days` | `90` | Recent history requested from Binance when no offline dataset is supplied. |
| `--samples` | `60` | Maximum number of evenly spaced forecast origins selected. |
| `--offline-dataset PATH` | — | `.npz` arrays (`timestamps`, `opens`, `highs`, `lows`, `closes`, `volumes`) instead of downloading history. |
| `--cv-folds` | `3` | Number of chronological validation folds. |
| `--cv-mode` | `expanding` | `expanding` or `rolling` training history. |
| `--cv-min-train-samples` | `12` | Minimum initial training origins. |
| `--cv-purge-hours` | `16` | Required gap so the longest target matures before validation. |
| `--cv-embargo-hours` | `0` | Additional gap after the purge. |
| `--cv-rolling-train-samples` | `24` | Maximum training origins when mode is `rolling`. |

The remote history is hourly **Binance BTCUSDT**, a BTC/USDT research proxy, not
the production Kraken BTC/USD market. Results from it must not be presented as
canonical BTC/USD evidence or exact production-market replay. Origins are
selected across the available candle range (up to 60 by default), leaving the
longest 16-hour target in range. A run can fail when its history cannot support
the requested origins/folds. An offline file changes the source, but does not by
itself certify venue, vintage, revisions, or production parity.

## Origin, target, and folds

Each forecast is generated from the market context available at its origin. The
scored targets are the close at exactly `origin + 2h`, `+4h`, `+8h`, and `+16h`;
they are not nearest-time matches. The origin's close is the current price. The
simulated adaptive policy may use an earlier forecast outcome only once that
outcome's exact target candle is visible at the current origin. This is the
backtest's maturity/no-look-ahead rule.

The report's walk-forward evaluation has chronological folds. Training origins
are eligible only if their longest target has matured before the validation
boundary, followed by the configured embargo. The default purge is 16 hours,
equal to the maximum target. Rolling mode additionally caps training history.
These folds evaluate the replay policy; they are not a nested selection and
untouched final test of an experiment catalog. For a design that selects within
inner folds and evaluates on separate outer folds, see
[nested walk-forward methodology](CROSS_VALIDATION.md) and the linked research
gates below.

## Metrics emitted by the backtest

For origin close `P`, predicted target close `P_hat`, and exact target close
`P_actual`, the backtest's per-observation price error is
`e = P_hat - P_actual`:

- **`mae_pct`** is mean absolute percentage price error:
  `mean(abs(e) / P_actual * 100)`. Lower is better. This report does not emit
  USD MAE or USD RMSE.
- **`mean_signed_error_pct`** is mean signed percentage error:
  `mean(e / P_actual * 100)`. Positive means overprediction; negative means
  underprediction. This is a price-bias measure, not a log-return loss.
- **`direction_accuracy`** is the fraction where the sign of
  `P_hat - P` matches the sign of `P_actual - P`; near-zero changes use the
  implementation's `1e-9` epsilon and are classified as zero. It is not
  directional probability calibration.
- **`q10_q90_coverage`** is the fraction of adaptive-ensemble targets satisfying
  `q10_usd <= P_actual <= q90_usd`. The interval is nominally the q10-to-q90
  predictive band. Coverage alone says nothing about interval sharpness and is
  not a probability of profit.
- **Fold dispersion** reports mean, population standard deviation, minimum and
  maximum of fold-level MAE/direction values. It describes variation across the
  configured folds; it is not a confidence interval.

Metrics are reported by 2h, 4h, 8h, and 16h horizon; the summary also reports
regime slices and the adaptive ensemble's differences in `mae_pct` from
persistence and the best-scoring benchmark. The best benchmark is picked by
observed MAE within that summary, so that label is descriptive rather than an
independent selection result.

The optimizer's paired comparisons use the origin-level mean MAE over these
four horizons, direction accuracy, per-horizon MAE, and persistence MAE. For an
error metric its improvement is `baseline - candidate`; for direction it is
`candidate - baseline`. The default uncertainty procedure is a deterministic
5,000-iteration, 95% moving-block paired bootstrap (24-origin minimum block in
the optimizer), with at least 32 paired origins and eight effective blocks
required for a directional conclusion. Origins stay paired across candidates;
the moving blocks account for serial dependence better than treating every
origin as independent. A confidence interval wholly above zero favors the
candidate under this orientation; otherwise the result is inconclusive or favors
the baseline. See [the procedure and interpretation](STATISTICAL_EVIDENCE.md).
At the optimizer's default 48 origins, a 24-origin block gives only two
effective blocks, below the eight-block floor, so the default-sized run cannot
reach a directional statistical conclusion regardless of its point estimate.

These reports do **not** compute USD MAE/RMSE, log-return MAE/RMSE, or return
bias/loss metrics. Those are distinct possible measures: for log return
`r = log(P_actual/P)` and prediction `r_hat`, return MAE/RMSE would be
`mean(abs(r_hat-r))` / `sqrt(mean((r_hat-r)^2))`, and return bias would be
`mean(r_hat-r)`. Do not attribute them to the CLI output. Likewise, quantile
coverage here is a point summary, not a paired interval-skill test.

## Baselines and production membership

The backtest evaluates its adaptive forecast and standalone model predictions
alongside six evaluation benchmarks on the same origins and horizons:

| Baseline | Definition |
|---|---|
| `persistence` | Random walk: every horizon equals the latest close. Primary reference. |
| `drift_7d` | Capped seven-day mean log-return drift. |
| `drift_24h` | Capped 24-hour mean log-return drift. |
| `seasonal_naive_24h` | Repeats the close at the matching hour of the previous day (`t+h-24`), which is already observed for horizons through 16h. |
| `ar1` | Capped first-order autoregressive return baseline. |
| `ema_return_24h` | Projects the exponentially weighted mean log return with a 24-hour span, capped using recent volatility. |

These are the **evaluation baseline suite**, not a declaration that each is a
production ensemble member. Production membership and forecast-policy parity
are separate configuration questions. Some simple baselines, including
persistence, drift, and AR(1), also appear as members in the research optimizer's
candidate weighting catalog; this overlap does not make seasonal-naive,
24-hour drift, or EMA a production member. See [the baseline definitions and
leakage rules](BENCHMARKS.md).

## Optimizer and parity gates

The research optimizer defaults to `--days 120 --samples 48` and accepts the
same `--offline-dataset PATH` option. It reuses frozen model forecasts to replay
a bounded candidate policy catalog; its comparisons are paired by origin and
dependence-aware. It is **recommendation-only**: it emits reports and does not
change production parameters or forecasts. See [optimizer statistical
evidence](STATISTICAL_EVIDENCE.md) and [promotion policy](PROMOTION_POLICY.md).

The optimizer/backtest reports include a forecast-policy-parity audit. A green
mechanics check is not sufficient to claim historical production parity: #342
requires eligible empirical records and all configured parity gates, in
addition to matching market source, inputs, model variant, and policy behavior.
The research backtest is explicitly labeled with research ridge enabled and
production parity blocked. Until the full #342 parity gates pass on eligible
canonical records, treat these as research replays, not production-equivalent
forecasts.

## Current evidence and limits

The canonical same-venue BTC/USD benchmark gate remains blocked: **0 of 32,136
required target bars and 0 of 4,320 required warm-up bars** are available in the
current checkout. Consequently there is no eligible canonical replay or
historical skill proof. The Binance BTCUSDT proxy cannot fill this gap. The short,
mixed-version D2 production ledger is descriptive only; it is not an untouched
D1 evaluation set and cannot establish skill, policy parity, or a promotion
case. There is no completed prospective D3 confirmation. These are evidence
availability limits, not evidence that a candidate is better or worse.

The roadmap-v8 closeout documents this status and the #339–#350 gates in
[research status](research/ROADMAP_V8_FINAL_STATUS.md); see also the
[research index](research/index.md), [benchmark gate](BENCHMARKS.md), and
[statistical evidence rules](STATISTICAL_EVIDENCE.md). No metric values or
candidate winners are asserted here because the canonical empirical evaluation
has not run.
