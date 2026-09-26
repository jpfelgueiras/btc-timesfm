# Roadmap v8 final champion/challenger selection — issue #350

**Current status: blocked; no selection or accuracy conclusion is available.**

Run the structural gate with:

```bash
PYTHONPATH=src python -m btc_timesfm.research.final_selection
PYTHONPATH=src python -m btc_timesfm.research.final_selection --contract frozen_contract.json
```

The first command reports the missing-evidence blockers in this checkout. The
validator never creates missing corpus records, scores models, chooses a winner,
or changes production. A `ready_for_evaluation` result means only that the
contract's evidence structure passed validation; candidate metrics and all
acceptance criteria still need a separate, auditable evaluation. Winner remains
null and recommendations remain human-only.

The frozen contract must enumerate every attempted candidate, including failed
and blocked attempts, and bind each to exact source, vintage, data, model, and
policy identities. Eligible candidates must retain raw and final forecasts,
failures, and the same exact origin/target/horizon pair keys across the complete
four-horizon family (2h, 4h, 8h, 16h). Selection uses nested earlier folds,
labels available by the fixed cutoff, a disjoint final holdout, dependence-aware
moving-block inference, and whole-family multiple-test correction over all
attempts. Comparisons include the frozen champion, persistence, and strongest
naive baseline.

Prospective D3 begins only after freeze, follows the fixed cutoff/stopping rule,
is disjoint from selection and final holdout, spans at least 30 calendar days,
and has at least 200 exact mature pairs at every horizon. The evaluation gate
must retain raw/final/failure outcomes and exact source/vintage joins. Any later
human recommendation must apply the predeclared effect, adjusted confidence,
baseline, powered-regression, and directional-loss criteria; no post-hoc
thresholds or repeated peeking are permitted. No automatic promotion or
production changes are allowed.

The checked-in canonical benchmark audit currently reports no eligible
immutable BTC/USD corpus. Consequently, absent external frozen artifacts, the
expected report is `blocked`; this is not a negative forecast-accuracy result.
