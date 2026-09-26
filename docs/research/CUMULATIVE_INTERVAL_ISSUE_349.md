# Roadmap v8 cumulative-horizon interval calibration — issue #349

**Decision: blocked. No calibration, coverage-skill, or sharpness claims are supported.**
The production interval path is unchanged.

The reusable evaluation helpers in `research/cumulative_interval_calibration.py`
require explicitly identified cumulative-horizon predictions and distinguish
raw, pre-coherence, and final output stages. They select calibration residuals
only from verified raw cumulative rows whose exact, timezone-aware target time
is no later than the forecast origin. Future outcomes, unverified lineage, and
other stages are excluded. A row produced at an origin cannot calibrate itself
before its target has matured. Interval scores consume supplied cumulative
q10/q50/q90 values; independently accumulated TimesFM per-step marginals are
not interpreted as cumulative quantiles.

The report gate remains blocked because the required empirical evidence is not
available in this checkout:

- No immutable raw paired per-origin TimesFM outputs, cumulative residuals,
  calibration inputs/outputs, or complete raw → pre-coherence → final lineage.
- No frozen #339 canonical BTC/USD evaluation corpus and per-origin raw
  predictions. Mixed-version history cannot replace this untouched corpus.
- The #343 target pipeline/evidence gate is not ready; there are no validated
  exact-target-time matured labels for the requested comparison.
- Thus there is no valid calibration/evaluation split, nor sufficient data to
  compare the existing ensemble width policy with direct empirical cumulative
  residual intervals by horizon.

Once those gates are satisfied, compare the existing path with direct empirical
cumulative-residual intervals using only prior matured residuals, preserving
stage identity and missing origins. Report q10/q50/q90 pinball loss, 80%
coverage, interval and weighted interval scores, normalized width, conditional
calibration, and point MAE non-regression by horizon. Evaluate before and after
coherence separately. These helpers do not run inference, fit a calibrator, or
change published forecasts. Until the raw lineage and corpus exist, this is a
software gate only and makes no empirical performance claim.
