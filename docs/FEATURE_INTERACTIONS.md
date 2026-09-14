# Feature interaction analysis

Issue #119 measures whether pre-registered feature *combinations* add predictive
value beyond the individual feature groups already covered by the family
ablations. The interaction catalog is fixed, explicit, and versioned, so the
analysis can never degrade into an exhaustive combinatorial search over feature
pairs.

## Pre-registered interaction catalog

Every hypothesis names the two feature groups it combines, documents its economic
rationale, and specifies a bounded rule. The catalog lives in
`btc_timesfm.research.feature_interaction_analysis` and is hashed with SHA-256;
bumping `INTERACTION_SCHEMA_VERSION` changes the identifier reported in every
run and every experiment manifest.

| Interaction | Component A | Component B | Rule |
| --- | --- | --- | --- |
| `funding_x_trend` | lasting funding rate | trend (6h/24h/7d momentum) | product of standardized composites |
| `open_interest_x_volatility` | OI level + 1h/24h change | 24h/7d volatility | product of standardized composites |
| `order_book_imbalance_x_momentum` | persisted book imbalance (10/25 bps, microprice) | 6h/24h momentum | product of standardized composites |
| `eth_relative_strength_x_btc_regime` | ETH-vs-BTC relative strength | BTC regime label | boolean regime gate (`trending`) |

A *product* rule interacts two sides that are z-standardized using only the
training rows of the current walk-forward fold, so scale differences between
features (e.g. funding rate vs. open interest USD) cannot dominate the term. A
*regime gate* is a boolean filter combo: the ETH-relative-strength composite is
active only when the pre-registered regime condition holds, otherwise it
contributes zero.

## Leakage-safe evaluation

Interactions reuse the exact leakage-safe walk-forward methodology of the family
ablations (`derivatives_ablation.py`, `microstructure_ablation.py`,
`cross_asset_ablation.py`):

1. each interaction is scored against the same baseline feature set with the
   **identical folds**;
2. a training row is eligible only when its target has already matured by the
   simulated origin (`origin + horizon_hours` rule);
3. interaction features are computed only from completed timestamps — the
   standardization statistics come from the training fold, never from future
   rows;
4. folds that explicitly include future-maturing targets are rejected by the
   evaluator.

The report schema matches the family ablation reports (`baseline_features`,
`feature_names`, `by_horizon` with `baseline_mae_pp` / `candidate_mae_pp` /
`significance`, and `overall` metrics), so sections can be aggregated or diffed
with the same tooling.

## Incremental delta and uncertainty

For every interaction/horizon the report contains:

- `incremental_delta_pp`: baseline MAE minus candidate MAE in percentage points
  (negative means the interaction helped);
- `incremental_relative_improvement`: the same delta expressed relative to the
  baseline MAE;
- `significance`: the deterministic paired bootstrap comparison
  (`paired_bootstrap_comparison`) including the mean improvement confidence
  interval, so the uncertainty around the delta is explicit.

## Promotion policy

Promotion requires **stable out-of-sample evidence**, not a single lucky fold:

- the horizon-level bootstrap conclusion is `candidate_better`;
- there are enough walk-forward samples (`promotion_min_samples`, default 32);
- at least `min_stable_folds` folds contributed and a majority of them improved
  (`improvement_fraction`);
- an interaction is promoted only when **two or more** horizons qualify and no
  horizon shows a statistically worse result (`rejected`).

Low-sample or missing-data situations are marked `insufficient_evidence` and are
never promoted. A promoted research interaction still never changes production
forecasts on its own — promotion to production remains a separate decision.

## Running

```bash
PYTHONPATH=src python -m btc_timesfm.research.feature_interaction_analysis \
  --days 30 --samples 96 --min-train 24 --folds 4
```

The CLI writes `feature_interaction_analysis_report.json` and
`feature_interaction_analysis_summary.md`. The pure evaluator
(`evaluate_interaction(features_frame, target, origin_indices, folds)` and
`build_interaction_analysis_report(...)`) is synthetic-data friendly, so unit
tests exercise the catalog, fold leakage safety, delta/uncertainty reporting,
and the promotion gate without any external provider.