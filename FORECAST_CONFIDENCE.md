# Forecast confidence / evidence bands

Issue #44 replaces vague agreement-style confidence messaging with a conservative evidence-quality band derived from matured out-of-sample production history.

The band is **not** a probability that the forecast will be correct. It summarizes how much historical support exists for the current four-horizon forecast.

## Eligibility

A horizon can publish a confidence band only when all of the following are available:

- at least 20 matured ensemble performance samples;
- at least 20 interval-calibration samples;
- ensemble and persistence MAE for a historical edge comparison;
- calibrated historical interval coverage;
- a valid current Q10-Q90 interval.

If any horizon lacks the required evidence, the overall public confidence claim is suppressed. Severe production drift also suppresses confidence entirely.

## Score inputs

Each horizon receives a 0-100 evidence-quality score from four bounded factors:

- **35% edge vs persistence** — relative MAE improvement of the ensemble over persistence;
- **30% interval calibration** — closeness of empirical calibrated coverage to the configured target;
- **20% sample depth** — evidence grows to full credit at 40 matured samples;
- **15% interval informativeness** — unusually wide current intervals are penalized relative to calibrated historical width.

The current production drift factor multiplies the score. Warning drift reduces the score and prevents a `high` band. Severe drift suppresses the claim.

A horizon that has not beaten persistence, or whose calibrated coverage misses target by more than 15 percentage points, is capped in the `low` band.

## Bands

- **high**: score >= 70, positive persistence edge, coverage within 5 percentage points of target, at least 40 evidence samples and no drift warning;
- **moderate**: score >= 45;
- **low**: score < 45.

Because one X post describes all 2h/4h/8h/16h forecasts, the overall score and label are bounded by the weakest eligible horizon. This intentionally avoids averaging away a weak horizon.

## Public wording

When all horizons are eligible the X formatter emits a compact line similar to:

```text
📊 Conf MOD • min edge +4% vs persist • n≥28
```

When evidence is insufficient or confidence is suppressed by severe drift, no confidence claim is emitted. The normal experimental/NFA disclaimer remains present.

Full supporting diagnostics are stored under `forecast_confidence` in `forecast.json`, including per-horizon samples, MAEs, persistence edge, calibrated coverage, interval widths, drift factor, component factors and explanation strings.
