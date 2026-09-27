# Issue #400 — bounded candidate and prospective confirmation

## Current outcome: blocked

The reproducible machine-readable outcome is
[`ISSUE_400_CANDIDATE_CONFIRMATION_BLOCKED.json`](ISSUE_400_CANDIDATE_CONFIRMATION_BLOCKED.json).
No candidates were preregistered or scored because production-parity diagnosis
is unavailable: #397 has no admissible corpus, #398 has no accumulated mature
evidence, and #399 remains blocked. There are no results, no winner, and no
skill inference. Blocked means prerequisites prevent evaluation; inconclusive
means completed evaluation did not resolve the hypothesis; negative means the
completed evaluation failed its preregistered acceptance criteria. They are
not interchangeable.

Regenerate the report from the repository root:

```bash
PYTHONPATH=src python -m btc_timesfm.research.candidate_confirmation \
  --parity-report docs/research/ISSUE_399_PRODUCTION_PARITY_BLOCKED.json \
  --output docs/research/ISSUE_400_CANDIDATE_CONFIRMATION_BLOCKED.json
```

An alternate #399 report can be supplied with `--parity-report PATH`. The
report is parsed from its exact UTF-8 bytes, and the output records the input
path and SHA-256 of those bytes. Read/decoding errors, arbitrary ready status
strings, unsupported schema/issue versions, failed gates, blockers, or
contradictory skill/metric fields fail closed. A valid #399 readiness report
must use schema version 1 and issue 399, have `status: ready_for_replay`, both
corpus and eligible mature production-policy gates passed, no blockers,
`canonical_skill_claim: false`, and `metrics: null`. Even then, this command
remains **blocked** until a candidate-family preregistration is frozen and
hashed; this readiness tool accepts no candidate registry or completed
evaluation evidence. Inconclusive, negative, and human-review dispositions are
reserved for validation of a separate completed evidence package.

## Protocol to freeze before any future scoring

The machine-readable protocol is embedded in the output and bound by its
canonical-JSON `protocol_sha256`. No candidate is currently registered. Later,
create a preregistration manifest containing the directional hypotheses, the
bounded candidate family and each candidate/configuration identity, outcomes,
supported horizons, fold/key schedule, minimum effects, fixed stopping/cutoff,
all thresholds, multiplicity family and adjustment, and the complete planned
attempt ledger. Canonicalize as UTF-8 JSON with sorted keys and compact
separators, compute SHA-256 over those exact canonical bytes, and preserve both
the immutable manifest and digest before any scoring. Append every tried,
failed, blocked, or excluded attempt to complete accounting; do not silently
drop failures.

Use exactly 2h, 4h, 8h, and 16h horizons and exact shared `(origin, target,
horizon)` keys across candidates, frozen champion, persistence, and strongest
naive baseline in nested earlier purged walk-forward folds. Apply the existing
final-selection contract: at least 3% adjusted primary improvement, positive
adjusted confidence interval lower bound, positive skill against persistence
and strongest naive, per-horizon/segment powered regression no greater than
5%, directional-loss increase no greater than 2 percentage points, and a
disjoint final holdout. Use paired dependence-aware moving-block uncertainty
and a declared whole-family multiplicity adjustment spanning all candidates,
comparisons, horizons, and reported strata, including failures.

Selection eligibility is not a winner. Only after the frozen selection passes
may one configuration enter a disjoint prospective D3 shadow: freeze its
identity, interval, cutoff, and stopping rule in advance; collect at least 30
calendar days and 200 exact mature pairs for each of the four supported
horizons. Retain every attempt and failure and apply the preregistered
per-horizon regression, directional-loss, adjusted uncertainty, and baseline
skill safeguards. This follows the 30-day/200-pair shadow deployment contract.
Human review is required; no automatic promotion or production forecast/config
change is authorized.

Relevant contracts: [final selection](FINAL_SELECTION_ISSUE_350.md), [production
parity readiness](ISSUE_399_PRODUCTION_PARITY_REPLAY.md), and the
[multiple-testing policy](../MULTIPLE_TESTING.md).
