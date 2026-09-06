# BTC TimesFM — Project State and Roadmap

_Last updated: 2026-09-06_

This document is the operational roadmap for `btc-timesfm`: what is already implemented, what the system currently does in production, the main limitations, and the recommended order for the next improvements.

The detailed issue tracker remains the source of truth for implementation work. The master roadmap issue is [#45](https://github.com/jpfelgueiras/btc-timesfm/issues/45).

---

## Current state snapshot

The project has moved beyond a TimesFM experiment into a small forecasting platform with production scheduling, validated redundant market data, durable versioned history, reproducible experiment manifests, adaptive ensembles, leakage-safe evaluation, statistical comparison, automated performance reporting, observability, weekly optimization, X publishing, CI quality gates, and automated security scanning.

### Implemented foundations

- **TimesFM 3 forecasting of hourly BTC log returns** instead of raw price levels.
- **Three TimesFM context windows:** 168h, 336h and 512h.
- **Forecast horizons:** 2h, 4h, 8h and 16h.
- **Adaptive performance-based ensemble weighting** per horizon using matured out-of-sample forecast results.
- **Weight safeguards:** sparse-history fallback, static-prior shrinkage, persistence fallback, and minimum/maximum model weights.
- **Prediction intervals** with empirical coverage adjustment.
- **Expanded benchmark suite:** persistence, 7-day drift, 24-hour drift, 24-hour seasonal naive, AR(1), and EMA-return baselines.
- **Market regime classification:** `range`, `trending`, `high_volatility`.
- **Strict OHLCV validation and anomaly detection** before production forecasting and research evaluation.
- **Redundant production market data:** Kraken BTC/USD primary with validated Bitstamp BTC/USD fallback and cross-provider disagreement checks.
- **Durable SQLite forecast history** stored outside the repository in GitHub Release assets.
- **Versioned forecast-history schema migrations** with transactional rollback and compatibility checks.
- **Forecast-history integrity auditing and conservative repair tooling** with machine-readable reports, backups, and structural-corruption safeguards.
- **Bounded forecast-history backup and recovery policy** with verified versioned generations, retention limits, and tested restoration.
- **Exact-target outcome maturation** for all supported horizons.
- **Versioned reproducibility manifests** for production forecasts and backtests, recording Git/model/configuration identity, deterministic seeds, exact data-window fingerprints, and stable configuration/data IDs.
- **Purged walk-forward cross-validation** with deterministic expanding/rolling folds, purge/embargo protection, fold-level metrics, and manifest persistence.
- **Paired statistical comparison** with bootstrap confidence intervals, effect sizes, sample counts, and explicit inconclusive outcomes.
- **Leakage-safe production drift detection** across matured forecast errors and observed market features, with warning/severe states that reduce adaptive-weight confidence.
- **Weekly recommendation-only optimizer** that evaluates bounded candidate ensemble parameters and consumes statistical evidence.
- **Structured production observability** with JSON snapshots, JSONL events, timings, counters, run identifiers, and GitHub Actions summaries.
- **Daily forecast performance dashboard** generated from durable history in JSON, Markdown, and standalone HTML.
- **Scheduled GitHub Actions production workflow** with a guard targeting roughly one forecast every two completed candle hours.
- **Automatic X posting** for scheduled forecasts using Twikit and the `X_COOKIES_JSON` repository secret.
- **Emoji-rich tweet formatting** with compact 280-character fallbacks.
- **PR CI quality gates** for unit tests, coverage, Ruff lint/format, and mypy checks.
- **Automated dependency/security scanning** with vulnerability and credential-leak checks.
- **Durable X publication idempotency and session-health preflight** with two-phase reservations, duplicate suppression, and persisted post metadata.
- **Persistent pipeline health and circuit breakers** with fail-closed publication gates, consecutive-failure tracking, deterministic recovery, and optional notifications.
- **Machine-readable optimizer promotion policy** with statistical, horizon, regime, persistence, and production-health guardrails; decisions remain review-only.
- **Validated regime detection** with controlled transition churn and out-of-sample comparison against the legacy heuristic.
- **Correlation-aware ensemble weighting** that penalizes redundant matured residual patterns while preserving sparse-history safeguards.
- **Conformal-style interval calibration** using matured historical nonconformity scores with safe sparse-history fallback.
- **Optional diversified non-TimesFM model** evaluated walk-forward before any production participation.
- **Champion-vs-challenger evaluation reports** with identical-origin pairing, statistical evidence, and policy recommendations.
- **Timestamp-safe crypto derivatives context** covering funding, open interest and liquidations, with durable feature persistence and leakage-safe ablation.
- **Prospectively captured order-book microstructure context** with Kraken primary, Bitstamp fallback, bounded capture lag, durable feature persistence, and leakage-safe horizon-specific ablation.
- **Evidence-grounded forecast confidence explanations** based on matured performance, calibration, sample depth and drift rather than raw model agreement.

Relevant completed roadmap/foundation issues:

- [#5 Persist long-term forecast history](https://github.com/jpfelgueiras/btc-timesfm/issues/5)
- [#6 Use adaptive performance-based ensemble weights](https://github.com/jpfelgueiras/btc-timesfm/issues/6)
- [#7 Automate weekly walk-forward optimization](https://github.com/jpfelgueiras/btc-timesfm/issues/7)
- [#9 Unit test](https://github.com/jpfelgueiras/btc-timesfm/issues/9)
- [#17 Market data validation and anomaly detection](https://github.com/jpfelgueiras/btc-timesfm/issues/17)
- [#18 Redundant market data source and automatic fallback](https://github.com/jpfelgueiras/btc-timesfm/issues/18)
- [#19 Forecast-history schema migrations](https://github.com/jpfelgueiras/btc-timesfm/issues/19)
- [#20 Forecast-history integrity audit and repair tooling](https://github.com/jpfelgueiras/btc-timesfm/issues/20)
- [#21 Reproducible experiment manifests](https://github.com/jpfelgueiras/btc-timesfm/issues/21)
- [#22 Structured observability and pipeline metrics](https://github.com/jpfelgueiras/btc-timesfm/issues/22)
- [#23 Forecast performance dashboard/reporting](https://github.com/jpfelgueiras/btc-timesfm/issues/23)
- [#24 Forecast-history retention, backup and recovery policy](https://github.com/jpfelgueiras/btc-timesfm/issues/24)
- [#25 CI quality gates](https://github.com/jpfelgueiras/btc-timesfm/issues/25)
- [#26 Expanded benchmark suite](https://github.com/jpfelgueiras/btc-timesfm/issues/26)
- [#27 Purged walk-forward cross-validation](https://github.com/jpfelgueiras/btc-timesfm/issues/27)
- [#28 Statistical significance and uncertainty testing](https://github.com/jpfelgueiras/btc-timesfm/issues/28)
- [#33 Model and feature drift detection](https://github.com/jpfelgueiras/btc-timesfm/issues/33)
- [#38 X posting idempotency and session-health checks](https://github.com/jpfelgueiras/btc-timesfm/issues/38)
- [#39 Pipeline alerts, health checks and circuit breakers](https://github.com/jpfelgueiras/btc-timesfm/issues/39)
- [#40 Automated dependency and security scanning](https://github.com/jpfelgueiras/btc-timesfm/issues/40)
- [#41 Optimizer promotion policy and safety guardrails](https://github.com/jpfelgueiras/btc-timesfm/issues/41)
- [#29 Improved market regime detection](https://github.com/jpfelgueiras/btc-timesfm/issues/29)
- [#30 Correlation-aware ensemble weighting](https://github.com/jpfelgueiras/btc-timesfm/issues/30)
- [#31 Conformal interval calibration](https://github.com/jpfelgueiras/btc-timesfm/issues/31)
- [#32 Diversified non-TimesFM forecasting model](https://github.com/jpfelgueiras/btc-timesfm/issues/32)
- [#34 Crypto derivatives signals](https://github.com/jpfelgueiras/btc-timesfm/issues/34)
- [#35 Order-book and market microstructure features](https://github.com/jpfelgueiras/btc-timesfm/issues/35)
- [#42 Champion-vs-challenger evaluation reports](https://github.com/jpfelgueiras/btc-timesfm/issues/42)
- [#44 Statistically grounded confidence explanations](https://github.com/jpfelgueiras/btc-timesfm/issues/44)

---

## Production architecture

```text
Kraken BTC/USD hourly candles ──┐
                               ├── validation / provider comparison
Bitstamp BTC/USD fallback ─────┘
                                         │
                                         ▼
                                  schedule_guard.py
                                         │
                                         ▼
                              features / regime / manifest
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
              TimesFM 168h        TimesFM 336h        TimesFM 512h
                    │                    │                    │
                    └────────────────────┴────────────────────┘
                                         │
                            benchmark / baseline models
                                         │
                                         ▼
                               adaptive ensemble
                                         │
                              2h / 4h / 8h / 16h
                                         │
             ┌───────────────────────────┼───────────────────────────┐
             ▼                           ▼                           ▼
   durable SQLite history          forecast.json              observability
   + experiment manifest                │                   snapshot / JSONL
   GitHub Release assets                ▼                           │
         │                        formatted X post                  ▼
         └──────────► dashboard         │                    Actions summary
                                      ▼
                                    Twikit
```

### Production scheduling

The GitHub Actions forecast workflow wakes up hourly at minute `37` UTC. A lightweight scheduler guard checks the latest completed forecast candle and only runs the expensive model when at least two completed hourly candles have elapsed.

This design is intentional: GitHub scheduled workflows are best-effort and can be delayed. Waking hourly gives the project another chance to run without producing an expensive forecast every hour.

Scheduled forecasts post to X automatically. Manual runs only post when explicitly requested.

### Market-data safety

Kraken BTC/USD remains the production primary. Bitstamp BTC/USD is the validated fallback. Both providers are normalized into the same hourly `MarketData` representation and overlapping closes are compared when both datasets are available. Production fails closed when provider disagreement exceeds the configured tolerance instead of silently trusting a suspicious source.

Volume-only anomalies can be handled conservatively without weakening timestamp, price, or OHLC integrity checks; invalid price structure still fails closed.

### Durable history, integrity, and reproducibility

The canonical production history is a SQLite database stored through the dedicated GitHub Release tag:

```text
forecast-history-v1
```

The production workflow stores the compressed SQLite database, analysis exports, a previous known-good generation, and bounded verified versioned backup generations. Retention limits cap both generation count and total backup bytes. The rolling `.state/previous_forecast.json` cache remains useful for fast scheduling but is not the historical source of truth.

The history layer now includes versioned schema migrations plus integrity auditing. Audit tooling checks SQLite integrity, foreign keys, uniqueness, required fields, orphan model rows, missing matured outcomes, and inconsistent derived values. Repair mode is intentionally conservative: it creates a backup first, is idempotent, and blocks automatic mutation when corruption is structural or ambiguous.

Each production forecast and walk-forward backtest includes a versioned `experiment_manifest`. The manifest records the Git revision, TimesFM model/package version, effective model/ensemble configuration, deterministic seed, source/provider identity, exact candle window, and a SHA-256 OHLCV fingerprint. Stable `configuration_id` and `data_id` values make equivalent runs directly comparable while `run_id` identifies one execution.

### Current modeling strategy

The ensemble predicts hourly log returns and reconstructs future BTC prices. TimesFM is combined with deliberately simple and stronger statistical baselines so the project can continuously answer an important question:

> Is the complex forecast actually beating a robust simple benchmark?

Weights adapt separately for 2h, 4h, 8h, and 16h according to matured historical performance. Current weighting considers MAE, direction accuracy, bias, interval behavior, and performance relative to persistence while retaining strict floors/caps and sparse-history fallbacks. Production drift detection monitors matured model errors and observed feature distributions; warning drift reduces the learned-weight blend and severe drift falls back to the static regime prior.

### Research and evaluation workflow

The research path now has four layers:

1. **Expanded benchmarks** ensure every model change is compared against strong simple alternatives.
2. **Purged walk-forward cross-validation** evaluates chronological folds with explicit leakage protection and fold-by-fold dispersion.
3. **Paired statistical testing** quantifies whether apparent improvements are supported by confidence intervals/effect sizes or should be marked inconclusive.
4. **Weekly optimizer** evaluates bounded candidate configurations and remains recommendation-only.

The optimizer does not silently deploy parameter changes. Statistical evidence is now part of the promotion guardrails rather than an optional post-hoc metric.

### Observability and reporting

Production runs emit structured stage timings and counters for skips, failures, fallbacks, data-quality events, successful posts, and other pipeline states. Events are correlated with experiment/run identifiers.

A daily performance dashboard reads the durable history directly and reports MAE, signed bias, direction accuracy, interval coverage, and sample counts by horizon, model, market regime, and rolling window. Missing or low-sample segments are explicitly flagged.

---

## Current strengths

1. **Out-of-sample thinking is built into the architecture.** Production outcomes are matured only after the real target candle exists.
2. **Simple benchmarks are first-class competitors.** Complexity is not automatically considered better.
3. **Research has explicit leakage protection.** Purge/embargo rules and chronological folds are part of the evaluation contract.
4. **Model-comparison uncertainty is measured.** Small metric differences can be marked inconclusive instead of being treated as wins.
5. **The ensemble can adapt without allowing one model to dominate suddenly.**
6. **Historical forecasts survive GitHub Actions cache eviction and have schema/integrity controls.**
7. **Production and research runs are reproducible from recorded code/configuration/data fingerprints.**
8. **The primary market-data provider is no longer a single point of failure.**
9. **Pipeline failures and slow stages are diagnosable from structured observability data.**
10. **Forecast quality is reviewable through an automated dashboard instead of raw JSON only.**
11. **The project remains practical to operate on GitHub Actions.**
12. **PR CI covers tests, coverage, lint/format, typing, and dependency/security scanning.**

---

## Current limitations and risks

These are the main reasons the current forecast should still be considered experimental.

### Data breadth

Production now captures timestamp-safe funding, open-interest, liquidation and prospectively observed order-book microstructure context alongside validated spot OHLCV. Cross-asset/macro information is still not integrated, and external signals remain passive until their walk-forward ablations demonstrate defensible out-of-sample value.

### Model concentration

Three TimesFM contexts still share one model family, but production research now measures residual correlation and can penalize redundant model influence. A materially different optional model is available for validation before production participation.

### Regime detection

Regime detection now uses a validated, reproducible detector with measured transition churn and an explicit legacy-heuristic benchmark. Regime labels still remain an important model-risk surface and should continue to be monitored as market structure changes.

### Evidence quantity

Evaluation is now leakage-aware and statistically explicit, but evidence quality is still bounded by the amount of matured forecast history and the representativeness of available market regimes. Confidence intervals do not remove the need for larger samples.

### Prediction-interval calibration

Intervals now use conformal-style calibration from matured historical errors with sparse-history fallback. Coverage remains experimental during regime shifts and low-sample conditions, so observed coverage and width should continue to be monitored.

### Social publishing

Twikit is an unofficial X client, so session cookies can still expire and frontend changes can still break posting. The production workflow now preflights session health, durably reserves each publication by forecast origin/content, suppresses duplicates, persists successful post IDs, and fails closed on ambiguous attempts.

### Operational controls

Structured observability, history auditing, bounded backup/recovery, model/feature drift detection, persistent stage-health counters, publication gates, and circuit breakers are implemented. Optional webhook alerts expose actionable failures without making core safety controls depend on an external notifier.

### Merge enforcement

CI checks run on every pull request. Enforcing every check as a mandatory merge gate still depends on repository branch/ruleset configuration and the GitHub plan used for this private repository.

---

# Roadmap

The roadmap is split into five phases. Dependencies are intentional: later work should not be started when it depends on an unfinished foundation unless the work can safely proceed in parallel.

**Completed roadmap items:** #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28, #29, #30, #31, #32, #33, #34, #35, #38, #39, #40, #41, #42, and #44.

**Highest-value currently unblocked work:** #36 (cross-asset/macro signals), which is the final dependency needed to unlock the P1 #37 feature-ablation pipeline. #43 (safe optimizer-generated PRs) is already fully unblocked and can proceed in parallel.

## Phase 1 — Foundation & Data

Goal: make inputs, historical data, observability, and CI trustworthy enough that later modeling work is based on reliable evidence.

| Issue | Improvement | Depends on | Priority |
|---|---|---|---|
| [#17](https://github.com/jpfelgueiras/btc-timesfm/issues/17) | ✅ Market data validation and anomaly detection | #5, #9 | P0 |
| [#18](https://github.com/jpfelgueiras/btc-timesfm/issues/18) | ✅ Redundant market data source and automatic fallback | #17 | P0 |
| [#19](https://github.com/jpfelgueiras/btc-timesfm/issues/19) | ✅ Version forecast-history schema and add migrations | #5, #9 | P0 |
| [#20](https://github.com/jpfelgueiras/btc-timesfm/issues/20) | ✅ Forecast-history integrity audit and repair tooling | #19 | P1 |
| [#21](https://github.com/jpfelgueiras/btc-timesfm/issues/21) | ✅ Reproducible experiment manifests | #5, #9, #19 | P1 |
| [#22](https://github.com/jpfelgueiras/btc-timesfm/issues/22) | ✅ Structured observability and pipeline metrics | #17, #21 | P1 |
| [#23](https://github.com/jpfelgueiras/btc-timesfm/issues/23) | ✅ Forecast performance dashboard/reporting | #5, #22 | P1 |
| [#24](https://github.com/jpfelgueiras/btc-timesfm/issues/24) | ✅ Retention, backup and recovery policy | #19, #20 | P1 |
| [#25](https://github.com/jpfelgueiras/btc-timesfm/issues/25) | ✅ CI quality gates: linting, typing and coverage | #9 | P0 |

### Phase 1 definition of done

- ✅ Invalid/stale candles cannot reach the forecasting model unnoticed.
- ✅ A healthy secondary provider can safely replace the primary during an outage.
- ✅ Historical database changes use tested migrations.
- ✅ Historical-data corruption can be detected before it influences adaptive weights.
- ✅ Every experiment/production run is reproducible from recorded metadata.
- ✅ Pipeline stages expose structured status, counters, and timing information.
- ✅ Forecast quality can be reviewed without manually inspecting raw JSON.
- ✅ CI catches style/type/test regressions on every PR.
- ✅ Formal retention, restore testing, and recovery policy are documented and automated where practical.

---

## Phase 2 — Modeling & Evaluation

Goal: make model improvements statistically defensible and increase ensemble diversity.

| Issue | Improvement | Depends on | Priority |
|---|---|---|---|
| [#26](https://github.com/jpfelgueiras/btc-timesfm/issues/26) | ✅ Expanded benchmark suite | #5, #9, #21 | P0 |
| [#27](https://github.com/jpfelgueiras/btc-timesfm/issues/27) | ✅ Purged walk-forward cross-validation | #26 | P0 |
| [#28](https://github.com/jpfelgueiras/btc-timesfm/issues/28) | ✅ Statistical significance and uncertainty testing | #27 | P0 |
| [#29](https://github.com/jpfelgueiras/btc-timesfm/issues/29) | ✅ Improved market regime detection | #5, #27, #28 | P1 |
| [#30](https://github.com/jpfelgueiras/btc-timesfm/issues/30) | ✅ Correlation-aware ensemble weighting | #6, #26, #28 | P1 |
| [#31](https://github.com/jpfelgueiras/btc-timesfm/issues/31) | ✅ Conformal calibration for forecast intervals | #5, #27 | P1 |
| [#32](https://github.com/jpfelgueiras/btc-timesfm/issues/32) | ✅ Diversified non-TimesFM forecasting model | #26, #27, #28 | P1 |
| [#33](https://github.com/jpfelgueiras/btc-timesfm/issues/33) | ✅ Model and feature drift detection | #5, #26, #28 | P1 |

### Phase 2 definition of done

- ✅ Every new model can be evaluated against the same stronger benchmark suite.
- ✅ Research uses deterministic chronological folds with explicit leakage protection.
- ✅ Candidate comparisons include confidence intervals/effect sizes and can be marked inconclusive.
- ✅ Regimes are validated out of sample.
- ✅ Ensemble weighting accounts for correlated model errors.
- ✅ Prediction intervals have empirically defensible conformal coverage.
- ✅ At least one genuinely different model family is evaluated.
- ✅ Production can identify when recent market/error behavior has drifted materially.

---

## Phase 3 — Market Signals

Goal: determine whether crypto-native and cross-market information adds real out-of-sample predictive value.

| Issue | Improvement | Depends on | Priority |
|---|---|---|---|
| [#34](https://github.com/jpfelgueiras/btc-timesfm/issues/34) | ✅ Funding, open interest and liquidation signals | #17, #18, #19 | P1 |
| [#35](https://github.com/jpfelgueiras/btc-timesfm/issues/35) | ✅ Order-book and microstructure features | #17, #18 | P2 |
| [#36](https://github.com/jpfelgueiras/btc-timesfm/issues/36) | Cross-asset and macro signals | #17, #18, #19 | P2 |
| [#37](https://github.com/jpfelgueiras/btc-timesfm/issues/37) | Automated feature ablation and selection | #27, #34, #35, #36 | P1 |

### Phase 3 definition of done

- External features are timestamp-safe and cannot leak future information.
- Missing third-party data never breaks the core spot-price forecast.
- Every feature family is evaluated independently through walk-forward ablation.
- Features are promoted only when they provide stable out-of-sample value.
- Feature-set versions are reproducible from experiment manifests.

---

## Phase 4 — Production Reliability

Goal: make the scheduled forecast/publishing pipeline safe to leave running unattended.

| Issue | Improvement | Depends on | Priority |
|---|---|---|---|
| [#38](https://github.com/jpfelgueiras/btc-timesfm/issues/38) | ✅ X posting idempotency and session-health checks | #9, #22 | P0 |
| [#39](https://github.com/jpfelgueiras/btc-timesfm/issues/39) | ✅ Pipeline alerts, health checks and circuit breakers | #17, #18, #22, #33 | P0 |
| [#40](https://github.com/jpfelgueiras/btc-timesfm/issues/40) | ✅ Automated dependency and security scanning | #25 | P1 |
| [#44](https://github.com/jpfelgueiras/btc-timesfm/issues/44) | ✅ Statistically grounded confidence explanations in posts | #23, #30, #31, #33 | P1 |

### Phase 4 definition of done

- ✅ Re-running a forecast cannot publish the same X post twice.
- ✅ Expired X sessions are detected clearly and safely.
- ✅ Severe data/model health conditions stop publication instead of emitting misleading forecasts.
- ✅ Repeated failures and recovery states are visible.
- ✅ Dependency vulnerabilities are surfaced automatically.
- ✅ Public confidence language is based on measured evidence, sample size, drift state, and calibration rather than raw model agreement alone.

---

## Phase 5 — Research Automation

Goal: automate repetitive model-selection work without allowing the research system to silently change production behavior.

| Issue | Improvement | Depends on | Priority |
|---|---|---|---|
| [#41](https://github.com/jpfelgueiras/btc-timesfm/issues/41) | ✅ Optimizer promotion policy and safety guardrails | #7, #28, #33 | P0 |
| [#42](https://github.com/jpfelgueiras/btc-timesfm/issues/42) | ✅ Champion-vs-challenger evaluation reports | #7, #26, #28, #41 | P1 |
| [#43](https://github.com/jpfelgueiras/btc-timesfm/issues/43) | Automatically open safe parameter-change PRs | #21, #41, #42 | P2 |

### Phase 5 definition of done

- ✅ A machine-readable promotion policy determines whether research evidence is sufficient.
- ✅ Production and challenger configurations are evaluated on identical samples/folds.
- ✅ Research reports explain why a candidate is accepted, rejected, or inconclusive.
- The optimizer can open a reviewable configuration PR only after all safety criteria pass.
- Production changes still require normal PR review and CI; the optimizer never auto-merges its own changes.

---

# Dependency map

## Critical paths

```text
#9 ──► #25 ✅ ──► #40 ✅
```

```text
#5 ──► #19 ✅ ──► #20 ✅ ──► #24 ✅
                │
                └──► #21 ✅ ──► #22 ✅ ──► #23 ✅
```

```text
#5 + #9 + #21 ✅
          │
          ▼
         #26 ✅
          │
          ▼
         #27 ✅
          │
          ▼
         #28 ✅
          │
          ├──► #29 ✅ improved regimes
          ├──► #30 ✅ correlation-aware ensemble
          ├──► #32 ✅ diversified model
          └──► #33 ✅ drift detection
```

```text
#17 ✅ ──► #18 ✅
   │          │
   │          ├──► #34 ✅ derivatives signals ─┐
   │          ├──► #35 ✅ order-book signals ─┼──► #37 feature ablation
   │          └──► #36 cross-asset signals ─┘
   │
   └──► #22 ✅ ──► #38 ✅
                 └──► #39 ✅ (also needs #33 ✅)
```

```text
#7 + #28 ✅ + #33 ✅
       │
       ▼
      #41 ✅
       │
       ▼
      #42 ✅
       │
       ▼
      #43
```

---

# Recommended next execution order

The core modeling/evaluation path through **#29, #30, #31, #32, #33**, external signal families **#34 and #35**, champion/challenger review **#42**, and evidence-grounded public confidence **#44** is complete. The remaining roadmap is concentrated in cross-asset inputs, automated feature selection, and safe research automation.

The highest-value sequence is:

1. **#36 — Cross-asset and macro signals (P2)**  
   Add a small reproducible set of related-market features with strict completed-hour alignment and graceful outages.

2. **#37 — Automated feature ablation and selection (P1)**  
   Once #36 is complete, evaluate derivatives, microstructure and cross-asset feature families together and promote only stable out-of-sample contributors.

3. **#43 — Safe optimizer-generated parameter PRs (P2)**  
   This is fully unblocked by #21, #41 and #42 and can proceed in parallel, while remaining review-only and never auto-merging.

---

# Suggested release stages

## Stage A — Trusted inputs and history

Target issues: **#17, #18, #19, #20, #21, #24, #25**  
Completed: **#17, #18, #19, #20, #21, #24, #25**. Remaining: **none**.

Outcome: production data, durable history, integrity controls, reproducibility, and CI are trustworthy and recoverable.

## Stage B — Defensible model evaluation

Target issues: **#26, #27, #28, #31**  
Completed: **#26, #27, #28, #31**. Remaining: **none**.

Outcome: improvements are evaluated with leakage-safe folds, stronger baselines, statistical evidence, and calibrated uncertainty.

## Stage C — Better ensemble intelligence

Target issues: **#29, #30, #32, #33**  
Completed: **#29, #30, #32, #33**. Remaining: **none**.

Outcome: better regime awareness, less correlated ensemble behavior, a genuinely different model family, and drift awareness.

## Stage D — Richer market information

Target issues: **#34, #35, #36, #37**  
Completed: **#34, #35**. Remaining: **#36, #37**.

Outcome: crypto-native and cross-market features are added only where ablation proves value.

## Stage E — Production hardening

Target issues: **#22, #23, #38, #39, #40, #44**  
Completed: **#22, #23, #38, #39, #40, #44**. Remaining: **none**.

Outcome: production can run unattended with usable monitoring, safer X publishing, circuit breakers, and better public confidence communication.

## Stage F — Controlled research automation

Target issues: **#41, #42, #43**  
Completed: **#41, #42**. Remaining: **#43**.

Outcome: the research loop can recommend and prepare improvements automatically while preserving human review and CI gates.

---

# Metrics that should guide the roadmap

The project should avoid optimizing for a single headline number. Track at least:

- MAE % by horizon
- signed bias by horizon
- direction accuracy by horizon
- Q10-Q90 empirical coverage
- average prediction-interval width
- performance relative to persistence and the best simple benchmark
- performance by market regime
- fold-to-fold stability
- paired confidence intervals/effect sizes for model changes
- model residual correlation
- sample count behind every performance claim
- production run success rate
- data fallback rate
- model inference duration
- history-audit health
- drift state and drift-event frequency
- X publication success/duplicate-prevention rate

A change should generally not be promoted simply because average MAE improves if it materially worsens another protected horizon, becomes unstable across folds, loses badly to persistence in an important regime, has confidence intervals consistent with no improvement, or relies on too few observations.

---

# Project principles

1. **No look-ahead leakage.** Research results are invalid if future information can enter features, weights, or model-selection decisions.
2. **Persistence and strong simple baselines are always competitors.** A complex model must earn its place.
3. **Production history is immutable research evidence.** Manual reruns must never rewrite the original forecast that was observed.
4. **Reproducibility is part of the result.** A metric without code/configuration/data identity is not enough evidence for promotion.
5. **Statistical uncertainty is part of model comparison.** A small metric delta is not automatically a real improvement.
6. **Prefer measured improvement over model novelty.** New models/features should be added because evaluation supports them, not because they are fashionable.
7. **Uncertainty matters.** Point forecasts without reliable uncertainty can create false confidence.
8. **Fail closed on bad data or unhealthy pipeline state.** Skipping a post is better than publishing from stale, contradictory, or degraded inputs.
9. **Automation must remain reviewable.** Research automation may recommend or open PRs, but should not silently deploy its own findings.
10. **Keep GitHub Actions cost/runtime bounded.** The project should remain practical to operate continuously.

---

## Roadmap maintenance

Update this document when:

- a roadmap issue is completed or substantially redesigned;
- a dependency changes;
- a new production model or data source is introduced;
- forecast cadence or horizons change;
- the promotion policy changes;
- a new roadmap phase is added.

For the live work queue and detailed acceptance criteria, use [issue #45](https://github.com/jpfelgueiras/btc-timesfm/issues/45) and the linked implementation issues.
