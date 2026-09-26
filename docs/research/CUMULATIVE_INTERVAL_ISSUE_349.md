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

The report gate fails closed unless the canonical audit includes a ready
manifest SHA-256, exact target coverage, and prospective-period readiness; each
origin/horizon/target key has exactly one raw cumulative, pre-coherence, and
final stage with identical source and candidate identities; each such key has
a unique exact-target matured outcome; and the preregistered support is met.
The support default is at least 200 exact pairs per required horizon in both
the evaluation set and prospective period. `dataset_ready=True` by itself is
not evidence. The gate remains blocked because the required empirical evidence
is not available in this checkout:

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

Weighted interval score uses the standard median-plus-central-80% expression
`(0.5 * MAE + 0.1 * IS80) / 1.5`, equivalent to the sum of q10/q50/q90 pinball
losses divided by 1.5 under the implemented pinball definition.
