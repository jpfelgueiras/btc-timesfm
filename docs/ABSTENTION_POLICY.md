# Forecast abstention policy

Issue #126 makes the production decision explicit before public formatting. The
machine-readable `abstention_policy` section in `forecast.json` has one of four
deterministic states:

- `healthy` — data health, drift, calibrated probabilities, rolling skill,
  no-edge thresholds, and model agreement permit directional claims.
- `low_confidence_no_measurable_edge` — the forecast remains available, but
  public directional claims are suppressed because evidence or measured edge is
  insufficient.
- `degraded_inputs` — model disagreement is below 50%, so public directional
  claims are suppressed.
- `forecast_withheld` — degraded market-data health or severe production drift
  forces withholding.

`reasons` lists every active rule and `inputs` records the evaluated source
signals. Evaluation is stateless: once inputs recover, the same healthy inputs
always produce `healthy` without a manual reset.

The X formatter renders `FORECAST WITHHELD` and per-horizon `WITHHELD` rows for
a withheld forecast, never directional labels or directional values.
