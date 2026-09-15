# Multiple-testing safeguards

Automated research evaluates many configurations, model variants, and feature hypotheses against the same production baseline. The more variants a search family tries, the more likely a pure-noise candidate is to clear an otherwise reasonable single-comparison threshold. Issue #123 adds a machine-readable multiple-testing policy that maps the family size to a stricter per-comparison evidence threshold, so that promotion never benefits from simply trying more variants.

## Policy

`MultipleTestingPolicy` is a frozen dataclass with three fields:

| Field | Default | Purpose |
| --- | ---: | --- |
| `method` | `"holm"` | Adjustment procedure (`bonferroni`, `sidak`, `holm`, or `by` for Benjamini-Yekutieli) |
| `alpha` | `0.05` | Family-wise error rate (or FDR target for `by`) |
| `max_comparisons` | `1000` | Safety cap on the family size used for adjustment |

The policy is serialised to JSON via `policy_to_dict` and reconstructed with `policy_from_dict`. A stable `policy_id` is derived from the canonical contents.

## Methods

| Method | Type | Behaviour |
| --- | --- | --- |
| `bonferroni` | FWER | Divides alpha equally across comparisons; conservative but simple |
| `sidak` | FWER | Slightly more powerful than Bonferroni; uses `(1-alpha)^(1/m)` |
| `holm` | FWER | Step-down procedure; rejects as many as possible in order |
| `by` | FDR | Benjamini-Yekutieli; controls false-discovery rate under arbitrary dependence |

## Family of comparisons

The "family" is the set of challengers replayed against the production baseline in one optimizer run. `family_comparison_count` records the raw and capped family size, whether the cap was applied, and which research family the comparisons belong to.

## Adjusted evidence

`adjusted_bootstrap_comparison` runs the deterministic paired bootstrap at `confidence = 1 - per_comparison_alpha` for the comparison's rank and family size. The returned `conclusion` is the adjusted one and is what promotion logic consumes.

`build_multiple_testing_assessment` builds the full family-aware assessment for an optimizer report: it computes raw p-values for every challenger, ranks the selected candidate, and produces the adjusted conclusion.

## Integration with promotion

`promotion_policy.py` evaluates the multiple-testing-adjusted conclusion as a new review requirement: `multiple_testing_adjusted_significance`. When `require_multiple_testing_adjustment` is true (default), the candidate must pass the adjusted threshold before reaching `review`. The adjustment details are included in `promotion_decision.json` under `evidence.multiple_testing_adjustment`.

## Integration with champion/challenger reports

`champion_challenger.py` includes a `multiple_testing` section in the report with the adjustment method, alpha, family size, adjusted conclusion, and whether the candidate survives. The summary markdown renders a "Multiple testing" section.

## False-positive control

The synthetic tests in `test_multiple_testing.py` demonstrate that under pure noise the adjusted false-positive rate stays near the nominal alpha, regardless of family size. A large family of noisy candidates can never promote a candidate solely because many variants were tried.
