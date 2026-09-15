# Recency-aware online adaptation evaluation

Issue #130 evaluates whether the production ensemble should adapt faster to
structural market change without overreacting to short-term noise. This page
documents the **leakage-free** offline study of bounded online-adaptation
techniques: recency weighting, rolling-window choice, and decay schedules.

The study is **recommendation-only**. It produces significance and
championship evidence through the existing promotion policy but never changes
production defaults.

## What is evaluated

`src/btc_timesfm/research/recency_adaptation.py` replays frozen per-model
forecasts chronologically through purged walk-forward folds. At every
validation origin every policy may consume **only** target outcomes whose
candle is already visible at that origin — the same contract enforced by
`build_purged_walk_forward_folds` + `assert_no_fold_leakage`
(`forecasting/cross_validation.py`).

Each candidate policy produces per-horizon ensemble weights anchored to the
production adaptive weights and is scored against:

- the **current production adaptive ensemble** (`adaptive_model_weights`)
- **persistence** (predict the current price)

| Technique | Description |
| --- | --- |
| `exponential_decay` | weight outcomes by `2 ** (-age / half_life)` |
| `linear_decay` | linearly down-weight outcomes older than `half_life` |
| `rolling_window` | uniform weights over the last `history_limit` outcomes |
| `uniform` | uniform over all permitted outcomes (production style) |

Default catalog (`default_policies()`): `rolling_24`, `rolling_48`,
`recency_exp_6`, `recency_exp_12`, `recency_linear_12`.

## Leakage safety

- forecasts are frozen; policies are replayed on identical origins
- fold construction purges any training origin whose target overlaps the
  validation boundary (`purge_hours >= max_target_hours`)
- every policy reads matured outcomes only: `actual_by_timestamp` is filtered
  to targets `<= current_origin` before weighting
- drift state is evaluated from matured outcomes only (issue #33 semantics),
  so a future candle can never influence an earlier weight decision
- the report records `leakage_safety.origin_time_only_actuals` and
  `assert_fold_leakage` as explicit checks

## Drift vs normal segmentation

Each validation origin is labelled `drift` or `normal` using the production
drift detector (`ops/drift_detection.evaluate_drift`) over matured outcomes and
observed completed-candle features. Overall, per-horizon, and per-policy
metrics are reported separately for the two segments
(`by_segment`, `significance.by_segment`) so a policy that helps during market
regimes but hurts otherwise is visible rather than averaged away.

The drift state also applies the production confidence schedule
(`adaptive_confidence_for_severity`): warning halves learned-weight progress,
severe falls back to the static regime prior.

## Guardrails

Adaptation is bounded so extreme parameter changes cannot come from small
samples:

- **small-sample guardrail** — with fewer than `min_effective_samples` matured
  outcomes the policy returns the production weights exactly
  (`mode: static_prior`); otherwise the blend scales from `0` to `max_blend`
  between `min_effective_samples` and `full_effective_samples` Kish-effective
  samples
- **extreme-change guardrail** — per-model absolute weight change vs production
  is capped at `max_weight_shift` per origin (`_cap_weight_shift`)
- guardrail application counts per policy appear in `guardrails.by_policy`

## Responsiveness vs stability

`responsiveness_stability` reports the trade-off explicitly:

- **responsiveness** `= 1 / (1 + effective_recency_memory)` — how short the
  effective memory horizon is
- **stability** `= 1 / (1 + mean L1 weight turnover)` — how little weights jump
  between consecutive origins

Both are reported per policy and per horizon, so a durable policy can be chosen
at the desired point on the reactivity/stability frontier.

## Significance and promotion

Every candidate is compared against production and persistence with the
existing `paired_bootstrap_comparison` (`forecasting/statistical_significance`)
at the standard confidence, per horizon, per objective (mean across horizons),
and separately per drift/normal segment. The best candidate is routed through
the existing `evaluate_promotion` and champion-challenger report
(`ops/promotion_policy.py`, `research/champion_challenger.py`):

```python
decision = evaluate_promotion(optimizer_report, health=health)
champion = challenger_report.build_report(optimizer_report, decision)
```

The verdict is **report-only**: `review_contract.production_changes_automatic`
is `false` and `review_contract.requires_human_review` is `true`.

## Reproducibility and registry

Every run produces a reproducibility manifest consistent with
`forecasting/experiment_manifest` (#122) and registers first-write-wins in the
experiment registry:

```python
from btc_timesfm.research.recency_adaptation import (
    evaluate_recency_adaptation,
    register_evaluation,
)

report = evaluate_recency_adaptation(samples, actual_by_timestamp)
registration = register_evaluation(report, db_path=path)
```

The report includes `experiment_manifest` (run id, configuration id, data id,
fold definitions, drift config, policy catalog) and `metrics_for_registry`
(optimizer-shaped metrics of the best challenger). Registering the same
`run_id` twice is idempotent.

## CLI

```bash
python -m btc_timesfm.research.recency_adaptation \
  --samples-json frozen_samples.json \
  --actuals-json actual_by_timestamp.json \
  --output recency_adaptation_report.json \
  --markdown recency_adaptation_summary.md \
  --db .state/experiment_registry.sqlite --folds 3
```

## Outputs

`evaluate_recency_adaptation(...)` returns a single JSON report with:

- `leakage_safety` — purge/embargo configuration and fold definitions
- `drift_segmentation` — config and drift/normal origin counts
- `by_horizon` — production vs persistence vs policies, MAE deltas
- `by_segment` — drift and normal period metrics
- `responsiveness_stability` — memory/turnover per policy and horizon
- `guardrails` — guardrail configuration and applied counts
- `significance` — paired-bootstrap comparisons vs production/persistence
- `promotion` — promotion decision + champion-challenger evidence (report-only)
- `metrics_for_registry` / `experiment_manifest` — for the #122 registry