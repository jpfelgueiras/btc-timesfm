# Champion-vs-challenger evaluation

The weekly research pipeline compares the deployed `production` configuration (the champion) with the best bounded optimizer alternative (the challenger) using the same frozen walk-forward forecast origins.

`champion_challenger.py` consumes `optimizer_report.json` and `promotion_decision.json` after the optimizer and formal promotion policy have run. It does not reselect data, change parameters, or write to production configuration.

## Pairing and reproducibility

Every candidate in an optimizer run must contain the exact same ordered `paired_metrics.origins` list. Report generation fails if any candidate differs. The report stores the origin count, first/last origin, and SHA-256 hash of the complete origin sequence.

Champion and challenger manifests contain a stable `configuration_id` derived from the candidate name and complete parameter mapping. The report also records SHA-256 hashes of the optimizer and promotion-policy inputs, producing a stable `comparison_id` for equivalent evidence.

## Metrics

The report carries forward metrics from the shared walk-forward evaluation path rather than recalculating point-forecast performance on a different sample:

- MAE by horizon
- signed bias by horizon
- direction accuracy by horizon
- empirical interval coverage and average interval width
- persistence MAE by horizon
- regime-by-horizon metrics
- fold-level stability
- paired-bootstrap statistical evidence against production and persistence

Before the promotion policy is evaluated, `champion_challenger_intervals.py` attaches a causal rolling residual-band diagnostic to each candidate. For every scored origin the calibration threshold is derived only from older absolute errors, never the current or future outcome. The default requires 10 prior origins and uses at most the previous 48 observations. Sparse history is reported as unavailable rather than filled with future information. If a candidate already exposes native interval coverage, the native metric takes precedence. This makes the report useful now while allowing the native conformal metrics from #31 to replace the residual diagnostic automatically once available.

## Recommendation

The final recommendation comes from `promotion_policy.py`. The champion-vs-challenger report includes the policy ID, decision, evidence, checks, production-health snapshot, and reasons. A `review` result means the candidate cleared the configured evidence and safety thresholds; it does not change production automatically.

The workflow remains review-only. Issue #43 may later open a deterministic configuration PR from an approved recommendation, but production changes still require normal review and CI.

## Retention

Each weekly optimizer run uploads the optimizer, promotion-policy, and champion-vs-challenger artifacts with a run-specific artifact name. Champion-vs-challenger reports are retained for 90 days for longitudinal comparison, matching the repository's existing optimizer artifact retention window.
