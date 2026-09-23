# Roadmap v5 full-policy shadow status

**Decision: blocked; no challenger is eligible for adoption.**

The already-captured D2 ledger is short, mixed-version diagnostic history and
is not an untouched shadow-confirmation window. No winning, eligible full
candidate has been frozen from the upstream experiments. The checkout does not
contain a complete candidate checkpoint/state, candidate-specific raw and
calibrated intervals, feature/calibration lineage, or prospective failure and
partial-maturity ledger sufficient to represent the full policy. No D3 period
has met the minimum 30 calendar days and 200 exact paired matured outcomes for
each target horizon. Therefore this issue cannot support a promote/keep/reject
result beyond **blocked/insufficient evidence**.

The existing shadow implementation is not equivalent to a full candidate
pipeline (notably, it reweights component forecasts and previously borrowed
champion intervals). This PR does not relabel that path as full-policy evidence
or change public output. It fixes paired evaluation so each per-horizon CI uses
only the same `(origin, exact target candle, actual close)` across candidate
and champion; mismatched or partially matured targets are excluded from that
horizon's pair set and counts are reported separately. Promotion now has an
explicit per-horizon paired-sample floor (default 200) and requires positive
paired edge against both champion and persistence, in addition to the 30-day
window. These are guardrails, not evidence that any challenger meets them.

## Required completion before shadow acceptance

1. Complete the upstream evaluation and freeze one candidate with immutable
   model checkpoint, code/configuration, feature, calibration, and data IDs.
   Optional blocked/negative experiments do not imply a production winner.
2. Implement no-op parity first across the complete forecast contract and
   verify public champion output is byte-for-byte unchanged under challenger
   failures, partial maturation and reruns.
3. Run prospective D3 for at least 30 days and until the predeclared effective
   power is met, including >=200 exact paired matured observations at each
   horizon. Use a fixed end/alpha-spending rule; do not repeatedly peek until a
   result passes.
4. Persist raw/final candidate intervals, attempt failures, missing features,
   exact venue/data-vintage joins and origin-target policy identities. Apply
   point-vs-distribution acceptance gates separately, report protected
   segments, and exercise exact rollback to the recorded champion.
5. Keep adoption human-reviewed and blocked unless eligibility, model-use
   authorization, lineage, all horizon/segment guards, and D3 evidence pass.

No automatic promotion, production reweighting, trading, or public claim is
introduced by this status update.
