# Optimizer promotion policy

The weekly optimizer remains recommendation-only. `promotion_policy.py` adds a separate, versioned decision layer that determines whether the best challenger should be **kept out of production**, **rejected**, or is **eligible for human review**.

A `review` decision is not deployment authorization. Production parameters still change only through the normal code-review/CI process; the optimizer and promotion policy never edit production configuration or merge a change automatically.

## Decisions

- **`keep`** — there is no hard safety violation, but evidence is insufficient for promotion. Production stays unchanged.
- **`reject`** — at least one hard safety veto fired. The challenger should not be promoted from this evidence.
- **`review`** — every hard veto and every evidence requirement passed. The challenger may be reviewed by a human, but is not automatically promoted.

The machine-readable result is `promotion_decision.json`; the Actions summary is `promotion_summary.md`.

## Versioned policy

Policy version 1 uses these defaults:

| Guardrail | Default |
| --- | ---: |
| Minimum walk-forward origins | 32 |
| Minimum mean MAE improvement vs production | 3% |
| Maximum MAE regression on any protected horizon | 5% |
| Maximum direction-accuracy drop | 2 percentage points |
| Minimum improving folds | 2 |
| Maximum worst-fold MAE regression | 2% |
| Minimum samples before a regime segment is protected | 8 |
| Maximum MAE regression in an evaluated regime/horizon | 10% |
| Maximum MAE regression vs persistence on any horizon | 5% |

The policy also requires statistically supported MAE improvement versus production before a candidate reaches `review`, rejects evidence that persistence is significantly better, rejects severe production drift, and rejects an open production circuit.

A stable `policy_id` is derived from the canonical policy contents. Changing any threshold produces a different ID.

## Hard vetoes

Any of the following produces `reject`:

- a protected 2h/4h/8h/16h MAE regression beyond the allowed limit;
- a sufficiently sampled regime/horizon regression beyond the regime limit;
- material underperformance versus persistence on a protected horizon;
- direction accuracy falling beyond the allowed limit;
- unstable fold behavior;
- statistical evidence that persistence is better;
- severe model/feature drift;
- any production pipeline circuit currently open.

This makes a single material degradation capable of vetoing an otherwise attractive average result.

## Requirements for review

If no hard veto fires, the challenger reaches `review` only when all of these are true:

- sample count meets the minimum;
- mean MAE improvement is material;
- paired statistical evidence concludes the challenger is better than production;
- production drift state is `none`;
- the durable production-health snapshot is available.

A warning drift state, inconclusive statistical result, tiny sample, small improvement, or unavailable production-health snapshot produces `keep`, not `review`.

## Production health input

The optimizer workflow downloads the latest `.state/pipeline_health.json` from the machine-managed `forecast-history-v1` Release when available. The promotion decision records a sanitized snapshot containing:

- drift severity;
- open circuits;
- overall health;
- health-state version/timestamp.

The policy never needs X cookies or other secrets.

## Reproducibility

`promotion_decision.json` records:

- the complete policy and `policy_id`;
- SHA-256 of the exact `optimizer_report.json` input;
- challenger and production parameter snapshots;
- horizon/fold/regime/persistence comparison values;
- statistical conclusions;
- production-health snapshot;
- every hard-veto and review-requirement result;
- final decision and reasons.

Therefore a decision can be audited from the report contents without relying on an undocumented manual judgment.

## Relationship to future automation

Issue #41 deliberately stops at a reviewable decision. Future optimizer-generated parameter PRs must consume this decision and may only proceed when it is `review`; they must still go through standard branch/PR/CI controls and must never auto-merge their own recommendation.
