# Roadmap v5 cumulative interval experiment status

**Decision: blocked/inconclusive. Keep the existing interval policy unchanged;
do not claim OOS calibration or a sharpness improvement.**

## What is and is not measurable

The engine builds point and marginal interval paths from hourly return
forecasts, then the ensemble and conformal multiplier transform the
published-horizon interval. Historical snapshots store the published q10/q50/q90
values and calibration multiplier, but do not preserve the required immutable
raw per-horizon scale/residuals and complete raw → calibrated → final lineage.
Consequently, the existing ledger cannot fairly reconstruct a raw cumulative
residual policy or compare it with the final/coherence-adjusted interval as if
it had been issued then.

The frozen D1 BTC/USD OHLCV corpus (2023-01-01 to 2026-08-31 plus 180-day
warm-up), its hash manifest, and per-origin raw predictions are not present in
this checkout. D2 is diagnostic, mixed-version production history; it is not
an untouched selection/test set and lacks raw intervals. The audit in issue
#257 reports paired D2 nominal-80% q10–q90 coverage of 87.37%, 87.37%, 92.55%,
and 96.74% at 2h, 4h, 8h, and 16h respectively, with 16h mean width about 7%
of origin price. Those descriptive numbers motivate measurement but cannot
validate a candidate or an interval policy. D3 has not accumulated the
pre-registered 30-day minimum.

## Experiment required to unblock

1. Persist immutable raw per-horizon point and scale/quantiles, calibration
   inputs/output, final reconciled interval and policy/configuration IDs for
   each origin. Never infer raw values from already-calibrated widths.
2. Derive cumulative target residuals at exact target timestamps. Compare direct
   per-horizon residual intervals against the existing method and a
   volatility-normalized residual candidate. Do not sum hourly marginal
   quantiles as cumulative quantiles absent a validated joint-path model.
3. Calibrate only from earlier matured raw residuals. Predeclare disjoint
   calibration and evaluation windows; score each candidate once on later
   origins. Preserve all missing/failed origins and report coverage, width,
   WIS/pinball, horizon/segment support and dependence-aware paired intervals.
4. Apply the stated adoption gate only on a properly powered outer set: >=3%
   WIS improvement, coverage in 75–85% at powered horizons and no protected
   segment regression beyond predeclared tolerance. Otherwise report
   reject/inconclusive. D3 is frozen before prospective confirmation.

No calibration path or published forecast is changed by this blocked status
report. In particular, calibration-fit coverage is not being labeled held-out
coverage, and the incomplete D2 ledger is not promoted to D1 evidence.
