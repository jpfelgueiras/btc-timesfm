# Issue #416: blocked family registry and closeout

**Disposition: accepted blocked closeout.** This registry accounts for the bounded evaluation family spanning #411–#415 under the frozen #410 protocol. It records a blocked/not-attempted disposition rather than selecting or ranking candidates.

The registry records five bounded families and zero actual candidate attempts and scores. Issue #411 reports zero confirmatory origins and zero eligible pairs; its 48 timestamp-eligible legacy records are all excluded. The reports for #412–#415 likewise report no candidate scoring. Metrics are null throughout and there is no winner. The untouched final holdout and prospective D3 were not used, and production was not changed.

The machine-readable [JSON registry](ISSUE_416_BLOCKED_FAMILY_CLOSEOUT.json) is the canonical accounting record. It includes SHA-256 digests for evidence artifacts where available; hashes are checked against the checked-in files by the tests. It does not fabricate candidates, folds, or power.

## Reopening

Do not access candidate scores until all reopening rules in the JSON are met. In particular, first resolve #411 and establish a supported diagnosis and evidence gates, then freeze and hash one complete preregistration manifest enumerating the entire family, every candidate and comparison, outcomes, strata, planned attempts, exclusions/failures, cutoffs, and stopping rule. Freeze exact-key/vintage provenance, nested earlier purged walk-forward folds, training-only selection, dependence-aware inference, and family-wise multiplicity as part of that manifest. Keep the chronological holdout and separate prospective D3 untouched.

If a study remains underpowered, blocked, or inconclusive, preserve that disposition; do not force selection. No winner may be declared when attempts are zero or accounting/gates are incomplete.

This closeout does not complete model selection or resolve the open #418 accuracy objective.
