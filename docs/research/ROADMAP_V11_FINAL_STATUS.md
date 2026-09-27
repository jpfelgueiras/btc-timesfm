# Roadmap v11 — evidence-led forecast accuracy closeout

_Status dated: 2026-09-27_

All 16 roadmap work items are closed. The closeout is **inconclusive for
forecast accuracy**: the required historical point-in-time corpus remains
unavailable, recent prospective/dashboard evidence is not an admissible
canonical historical evaluation, and no candidate was selected. This outcome
supports no positive or negative accuracy claim and authorizes no production
model, input, interval, or weighting change.

## Workstream outcomes

| Workstream | Issues | Outcome and evidence |
|---|---|---|
| Evidence continuity and admissible data | [#407](https://github.com/jpfelgueiras/btc-timesfm/issues/407), [#408](https://github.com/jpfelgueiras/btc-timesfm/issues/408), [#409](https://github.com/jpfelgueiras/btc-timesfm/issues/409), [#410](https://github.com/jpfelgueiras/btc-timesfm/issues/410) | The independent backup and prospective ledger recovery/continuity items are closed. The canonical corpus gate remains blocked: [the audit](ISSUE_397_PIT_BTCUSD_CORPUS.md) documents 0/32,136 target hours and 0/4,320 warm-up hours, with no admissible versioned BTC/USD archive acquired. The frozen scoring contract is recorded in #410. |
| Production diagnosis | [#411](https://github.com/jpfelgueiras/btc-timesfm/issues/411) | Production and registered baselines were captured, and the frozen replay-readiness audit was run. The audit is blocked: its last verified snapshot had 48 timestamp-eligible rows, all legacy/unversioned and excluded, with zero confirmatory origins or matured pairs. Metrics are null; this is not a scored replay. See [the frozen cohort record](FROZEN_SHADOW_COHORT.md) and its issue report. |
| Conditional candidate evaluation and selection | [#412](https://github.com/jpfelgueiras/btc-timesfm/issues/412), [#413](https://github.com/jpfelgueiras/btc-timesfm/issues/413), [#414](https://github.com/jpfelgueiras/btc-timesfm/issues/414), [#415](https://github.com/jpfelgueiras/btc-timesfm/issues/415), [#416](https://github.com/jpfelgueiras/btc-timesfm/issues/416), [#417](https://github.com/jpfelgueiras/btc-timesfm/issues/417) | All six dispositions are blocked/not attempted because #411 did not provide a powered diagnosis. No candidates were scored, no winner was selected, and D3 was not launched. See [#412](ISSUE_412_TIMESFM_CONTEXT_BLOCKED.md), [#413](ISSUE_413_ENSEMBLE_WEIGHTING_BLOCKED.md), [#414](ISSUE_414_INTERVAL_EVIDENCE_BLOCKED.md), [#415](ISSUE_415_OPTIONAL_SIGNAL_FAMILIES_BLOCKED.md), [#416](ISSUE_416_BLOCKED_FAMILY_CLOSEOUT.md), and [#417](ISSUE_417_D3_BLOCKED_NO_CANDIDATE.md). |
| Mature-cohort and dashboard evidence integrity | [#419](https://github.com/jpfelgueiras/btc-timesfm/issues/419), [#420](https://github.com/jpfelgueiras/btc-timesfm/issues/420), [#421](https://github.com/jpfelgueiras/btc-timesfm/issues/421), [#422](https://github.com/jpfelgueiras/btc-timesfm/issues/422), [#423](https://github.com/jpfelgueiras/btc-timesfm/issues/423) | Frozen mature-cohort accounting and dashboard coverage, segment, rolling-window, and volatility-attribution reporting were completed. The audit noted 200 exact matured outcomes out of 208 expected pairs, with 8 right-edge pairs naturally pending; this is not a candidate skill result. |

The issue-level discussions, closed states, and linked artifacts are the
detailed outcome record for each work item. The supplemental audit in [the #418
issue discussion](https://github.com/jpfelgueiras/btc-timesfm/issues/418#issuecomment-5854099426)
records a separate short-history dashboard diagnostic: all 30-day, 90-day, and
all-history windows contained the same approximately 22-day sample, and the
minimum effective-block gate was not met. Its paired deltas and interval are
not a canonical skill estimate and do not override the blocked #411 replay.

## Frozen research evidence

The checked-in machine-readable records provide immutable versions and digests
for the blocked research decisions:

| Evidence | Report/version | SHA-256 or protocol digest | Result |
|---|---|---|---|
| [#397 canonical corpus audit](ISSUE_397_CANONICAL_BENCHMARK_AUDIT.json) | audit artifact | `e2bf0c671a5a4327c6852d02614a7d2e403ed12f8fe7c4362336c5e87d44a0c7` | 0/32,136 target and 0/4,320 warm-up hours |
| [#409 corpus comparison](ISSUE_409_PIT_CORPUS_COMPARISON.json) | artifact v1 | `f1991ec340479c9cb2e617f36892123c70428a9522d5620203aff3c540e1c266` | Five channels reviewed; none admitted; no provider contacted or data acquired |
| [#410 frozen scoring protocol](ISSUE_410_SCORING_PROTOCOL.json) | `v11-prospective-1.0.1`; file digest `1b8c6705d53882a66c0ab1277cf2a63c845d00c2c7d0cc978c0c75d8121efd18`; protocol digest `4da936d505df9a15cbd073ee85ee0e4ed2db483e6058f6b81ce163f4724bb4f0` | Digests identify the frozen pre-score contract | Protocol gate passed; it does not assert scoring occurred |
| [#411 replay readiness](ISSUE_411_BASELINE_CAPTURE_BLOCKED.json) | schema v1; file digest `7561c14b141b69bd1c05f0926434a258e8baef410c2fd62613e2e1237288bc71` | Snapshot database `b102f14db535264ecc2474a3b338c929abea4c39616f233a5701611c9300f57f`; cohort `462287eb433a27c16e4f25360d9ee40e5dda2fabdced13e5390859a055f7bbb8`; audit code `79a95fc3debd4134ab68cfc55795007a340f15a6eff2ef7212a875a9846c28fc` | 48 eligible timestamps, all legacy-excluded; zero confirmatory origins/pairs; metrics null |
| [#417 D3 disposition](ISSUE_417_D3_BLOCKED_NO_CANDIDATE.json) | artifact v1; file digest `6c7356a79d1295c6332ba40bc1cace15f478016ccbef4868b6d48f81b986c8dd` | Candidate/champion and metrics null | D3 not launched |

The artifacts' issue-specific records preserve the commands, schemas, and
reopening gates. No licensed corpus, private runtime database, or forecast-score
data is included in the repository.

## Backup incident follow-up

The later backup incident [#440](https://github.com/jpfelgueiras/btc-timesfm/issues/440)
was a recurrence of the already tracked #407 failure mode. The independent
monitor initially reported a verified but stale archive. Follow-up verification
and a scratch-only disaster-recovery drill passed:

| Check | Run and result |
|---|---|
| Independent backup monitor | [Run 36346607970](https://github.com/jpfelgueiras/btc-timesfm/actions/runs/36346607970), passed at 2026-09-27 20:04 UTC. Report v1: schema 6, 147 origins, 4,092 predictions, 3,966 matured predictions, SHA-256 `df4a001580ed480f6d2e0e685b14c4b4fedae1e449e89ca77e9a4c5822cc3eeb`, backup age 6,317 seconds (below the 2-hour gate). The independent shadow backup also passed at schema 7. |
| Disaster-recovery drill | [Run 36347546910](https://github.com/jpfelgueiras/btc-timesfm/actions/runs/36347546910), passed at 2026-09-27 20:19 UTC. Report v1 verified SQLite integrity and schema 6 in scratch storage, matched manifest counts/hash, and recorded restore receipt at 20:19:38 UTC. The shadow database also restored and verified at schema 7. |

The drill used the repository workflow's scratch-only recovery path; it did not
replace production history. Report artifacts are retained on the linked Actions
runs. The failed run report is retained at
[run 36335817092](https://github.com/jpfelgueiras/btc-timesfm/actions/runs/36335817092).

## Reproduction and validation

The canonical corpus audit command and required immutable input conventions are
documented in [the corpus acquisition audit](ISSUE_397_PIT_BTCUSD_CORPUS.md).
The operational backup checks are reproducible through the manual dispatches
of `independent-history-backup-monitor.yml` and
`disaster-recovery-drill.yml`; the linked runs above record their exact commit,
commands, reports, and check results. No licensed market data, private runtime
database, backup archive, or generated workflow report is committed here.

## Final disposition

The roadmap work is complete, but the evidence chain does not meet the frozen
requirements for an accuracy-improvement claim. The canonical source remains
blocked; available prospective evidence is limited and does not meet the
historical skill gate; and no candidate passed selection and confirmation.
Retain the production policy. Reopen evaluation only after its stated corpus,
sample-size, provenance, maturity, and inference gates are satisfied.
