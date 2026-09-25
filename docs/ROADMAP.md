# BTC TimesFM — Project State and Roadmap

_Last reconciled: 2026-09-25_

This document distinguishes shipped capability from required operational setup, known risks, and recommendation-only research. GitHub issues are authoritative for live status and acceptance criteria; this page is the orientation and prioritization guide. The former master roadmap issue [#45](https://github.com/jpfelgueiras/btc-timesfm/issues/45) is closed.

## Current state

The project is a scheduled BTC/USD forecasting and publishing pipeline, not a hosted forecasting API product. Production forecasts use hourly log returns, validated spot OHLCV, three TimesFM context lengths and baseline models, with adaptive weights and matured exact-target outcomes persisted in SQLite. The workflow runs on GitHub Actions, stores durable history in the `forecast-history-v1` Release, builds performance/reporting outputs, and can publish scheduled forecasts to X.

The implementation also includes purged walk-forward evaluation, statistical comparisons, drift monitoring, interval calibration, external crypto/cross-asset inputs, feature ablation, champion/challenger shadow evaluation, and promotion/rollback recommendations. These capabilities do not establish predictive value by their presence; only prospective and out-of-sample evidence can do that.

### Shipped functionality

- Kraken BTC/USD production candles with validated Bitstamp fallback and fail-closed disagreement checks.
- TimesFM 168h/336h/512h contexts, 2h/4h/8h/16h forecasts, simple baselines, regime-aware/adaptive ensemble weighting and calibrated intervals.
- Durable, versioned SQLite forecast history; exact-timestamp, write-once outcome maturation; auditing, recovery and reproducibility manifests.
- Scheduled production in GitHub Actions, durable publication idempotency/session preflight, health gates, circuit breakers, observability and performance reporting.
- Leakage-safe backtesting and research workflows, with statistical evidence and low-sample/inconclusive states.
- Authenticated read-only `/v1` API service using the canonical history database; see [API contract](FORECAST_API_CONTRACT.md) and [service/deployment notes](FORECAST_API_SERVICE.md).
- CI quality workflow, coverage/lint/format/type checks, workflow/action-pin checks, and security scanning.

### Operational configuration required

- API users must deploy the WSGI app behind a production WSGI server and TLS termination, supply bearer keys and trusted client addresses, and provision canonical history/health files. The repository does not deploy or scale the API service.
- X publishing requires a working `X_COOKIES_JSON` repository secret. GitHub Actions schedules are best-effort; the durable publication guard controls cadence.
- Production history persistence and publication require the workflow's configured `GITHUB_TOKEN` permissions and the `forecast-history-v1` Release.
- Merge blocking depends on repository branch protection/ruleset settings and required status-check selection; workflow YAML alone cannot enforce those settings.

## Production architecture and boundaries

```text
Kraken hourly candles ──┐
                        ├── validation/provider checks ── features, regime, manifest
Bitstamp fallback ──────┘                                  │
                                                           ▼
                                      TimesFM contexts + baseline models
                                                           │
                                                           ▼
                                                adaptive ensemble
                                          ┌────────────────┴───────────────┐
                                          ▼                                ▼
                                 forecast + SQLite history       observability/reports
                                          │                                │
                                Release-backed history                    dashboard
                                          │
                                          ├── guarded scheduled X publication
                                          └── optional read-only WSGI API
```

The API currently has no repository-managed deployment, autoscaling, shared rate-limit backend, or aggregate metrics backend. Rate limits and service metrics are process-local; read-audit JSONL is local and currently has no documented rotation/retention bound. The current service guide therefore describes a WSGI integration point, not a completed multi-worker production deployment. See open [#323](https://github.com/jpfelgueiras/btc-timesfm/issues/323).

Historical API reads currently need bounded SQL pagination and index-backed access as history grows; see open [#322](https://github.com/jpfelgueiras/btc-timesfm/issues/322). This is a current query scalability gap, not an API contract limitation.

The GitHub Actions CI workflow runs core gates with `continue-on-error` so it can collect all results, then fails its final aggregation step when any gate fails; compatibility status jobs mirror that result. Repository ruleset/branch-protection configuration and checks required for merging remain external operational controls (open [#325](https://github.com/jpfelgueiras/btc-timesfm/issues/325)).

Dependencies have version bounds and selected exact pins, but the requirements files are not a complete resolver lock and the supported Python matrix is not yet represented by a reproducible tested dependency lock (open [#324](https://github.com/jpfelgueiras/btc-timesfm/issues/324)).

## Current actionable queue

These are the current production-readiness work items, in suggested order. They can be pursued independently unless noted; none depends on model or signal research.

| Order | Issue | Work and dependency | Why it matters |
|---|---|---|---|
| 1 | [#322](https://github.com/jpfelgueiras/btc-timesfm/issues/322) — bounded, indexed API queries | Push keyset pagination/limits into SQLite, verify deterministic cursor behavior and query plans. No issue dependency. | Prevents API query memory and latency from growing with all historical rows. |
| 2 | [#323](https://github.com/jpfelgueiras/btc-timesfm/issues/323) — API deployment and scaling | Document/enforce deployment topology; shared or explicitly single-worker rate limiting; bounded audit retention/failure behavior; aggregate metrics and readiness/liveness. Can start in parallel with #322; deployment assumptions should inform its operational tests. | Makes API guarantees meaningful in a real deployment. |
| 3 | [#324](https://github.com/jpfelgueiras/btc-timesfm/issues/324) — reproducible dependencies | Declare supported Python versions and add lock/constraints for production and test installs; update CI and refresh process. Independent of #322/#323. | Reduces resolver drift and improves reproducibility of deployments and CI. |
| 4 | [#325](https://github.com/jpfelgueiras/btc-timesfm/issues/325) — required merge gates | Verify ruleset/branch protection and stable required check names; add drift detection/runbook and stage broader critical-module type checking. Independent, but enforcement must match the finalized workflow statuses. | Prevents unverified changes when external merge protection is missing or stale. |

Issue #326 is documentation-only and does not change production behavior. Keep the four issues above as the live actionable queue; update this table when issue state or implementation changes.

## Completed roadmap history

These historical phases are complete and must not be mistaken for open work. Status was reconciled against GitHub on 2026-09-25.

- **Foundation/data:** #17–25, including validation, fallback, durable history, manifests, observability, reporting, recovery and CI quality gates.
- **Model evaluation:** #26–33, including baselines, purged walk-forward CV, statistical testing, regime detection, correlation-aware weighting, conformal intervals, diversified-model evaluation and drift detection.
- **Market signals:** #34–37, including derivatives, microstructure, cross-asset/macro signals, and automated feature ablation/selection.
- **Production reliability:** #38–40 and #44, including X publication safeguards, pipeline health/circuit breakers, dependency/security scanning, and evidence-grounded confidence explanations.
- **Research automation:** #41–43, including promotion guardrails, champion/challenger evaluation reports, and safe optimizer-generated parameter PRs.

Specific closures relevant to the previous roadmap's stale statements: [#36](https://github.com/jpfelgueiras/btc-timesfm/issues/36) cross-asset/macro signals, [#37](https://github.com/jpfelgueiras/btc-timesfm/issues/37) feature ablation/selection, [#43](https://github.com/jpfelgueiras/btc-timesfm/issues/43) optimizer-generated parameter PRs, and [#45](https://github.com/jpfelgueiras/btc-timesfm/issues/45) master roadmap are closed. None belongs in the current queue.

## Recommendation-only research and known risks

Weekly optimization, feature ablations, champion/challenger shadow results, promotion policy, and rollback safeguards generate evidence or recommendations. They do not silently alter production forecasts/configuration, open an automatic deployment, or merge changes. Any promotion remains subject to human review and the normal CI/merge process.

Forecast quality remains experimental: matured samples are finite and may not represent future regimes; intervals and learned weights can degrade under regime shifts. External features are optional and can be stale, revised, quarantined or unavailable; the validated spot forecast must remain usable without them. Three TimesFM contexts are not three independent model families. X posting uses unofficial Twikit behavior and can fail when sessions or upstream interfaces change. GitHub Actions schedules are best-effort. TimesFM model licensing must be checked for the intended use.

## Prioritization principles

1. Preserve no-look-ahead behavior and exact, timezone-aware outcome matching.
2. Keep persistence and simple baselines as explicit competitors; require sample-aware out-of-sample evidence for model/feature claims.
3. Fail closed on unsafe production inputs or pipeline state; optional data and notification systems must not undermine core forecasting safeguards.
4. Keep optimization and challenger evaluation recommendation-only until changes pass human review and CI.
5. Bound GitHub Actions runtime/cost and production API resource use.

## Roadmap freshness

Reconcile this page with issue state and implementation **at least quarterly**, and whenever a roadmap issue closes, a production workflow/API contract changes, or a new production-readiness gap is opened. During each review, verify the live queue and dependency order, closed issue references, deployment/configuration requirements, known risks, and the stated freshness date. The GitHub open-issue list and issue bodies remain authoritative for live status and acceptance criteria.
