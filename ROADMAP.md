# BTC TimesFM — Project State and Roadmap

_Last updated: 2026-09-05_

This document is the operational roadmap for `btc-timesfm`: what is already implemented, what the system currently does in production, the main limitations, and the recommended order for the next improvements.

The detailed issue tracker remains the source of truth for implementation work. The master roadmap issue is [#45](https://github.com/jpfelgueiras/btc-timesfm/issues/45).

---

## Current state snapshot

The project has moved beyond a single TimesFM experiment into a small forecasting platform with production scheduling, validated/redundant market data, durable versioned history, reproducible experiment manifests, adaptive ensembles, backtesting, weekly optimization, X publishing, CI quality gates, and automated security scanning.

### Implemented foundations

- **TimesFM 3 forecasting of hourly BTC log returns** instead of raw price levels.
- **Three TimesFM context windows:** 168h, 336h and 512h.
- **Forecast horizons:** 2h, 4h, 8h and 16h.
- **Simple benchmark models:** persistence, 7-day drift and AR(1).
- **Market regime classification:** `range`, `trending`, `high_volatility`.
- **Adaptive performance-based ensemble weighting** per horizon, using matured out-of-sample forecast results.
- **Weight safeguards:** sparse-history fallback, static-prior shrinkage, persistence fallback, minimum/maximum model weights.
- **Prediction intervals** with empirical coverage adjustment.
- **Strict OHLCV validation and anomaly detection** before production forecasting and research evaluation.
- **Redundant production market data:** Kraken BTC/USD primary with validated Binance BTCUSDT fallback and cross-provider disagreement checks.
- **Durable SQLite forecast history** stored outside the repository in GitHub Release assets.
- **Versioned forecast-history schema migrations** with transactional rollback and compatibility checks.
- **Exact-target outcome maturation** for all supported horizons.
- **Versioned reproducibility manifests** for production forecasts and backtests, recording Git/model/configuration identity, deterministic seeds, exact data-window fingerprints and stable configuration/data IDs.
- **Walk-forward backtesting** with Binance BTCUSDT historical candles as the research proxy.
- **Weekly recommendation-only optimizer** that evaluates candidate ensemble parameters.
- **Scheduled GitHub Actions production workflow** with a guard that targets roughly one forecast every two completed candle hours.
- **Automatic X posting** for scheduled forecasts using Twikit and the `X_COOKIES_JSON` repository secret.
- **Emoji-rich tweet formatting** with compact 280-character fallbacks.
- **PR CI quality gates** for unit tests, coverage, Ruff lint/format and mypy checks.
- **Automated dependency/security scanning** with vulnerability and credential-leak checks.

Relevant completed/foundation issues:

- [#5 Persist long-term forecast history](https://github.com/jpfelgueiras/btc-timesfm/issues/5)
- [#6 Use adaptive performance-based ensemble weights](https://github.com/jpfelgueiras/btc-timesfm/issues/6)
- [#7 Automate weekly walk-forward optimization](https://github.com/jpfelgueiras/btc-timesfm/issues/7)
- [#9 Unit test](https://github.com/jpfelgueiras/btc-timesfm/issues/9)
- [#17 Market data validation and anomaly detection](https://github.com/jpfelgueiras/btc-timesfm/issues/17)
- [#18 Redundant market data source and automatic fallback](https://github.com/jpfelgueiras/btc-timesfm/issues/18)
- [#19 Forecast-history schema migrations](https://github.com/jpfelgueiras/btc-timesfm/issues/19)
- [#21 Reproducible experiment manifests](https://github.com/jpfelgueiras/btc-timesfm/issues/21)
- [#25 CI quality gates](https://github.com/jpfelgueiras/btc-timesfm/issues/25)
- [#40 Automated dependency and security scanning](https://github.com/jpfelgueiras/btc-timesfm/issues/40)

---

## Production architecture

```text
Kraken BTC/USD hourly candles ──┐
                               ├── validation / provider comparison
Binance BTCUSDT fallback ──────┘
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
                            persistence / drift / AR(1)
                                         │
                                         ▼
                               adaptive ensemble
                                         │
                              2h / 4h / 8h / 16h
                                         │
                    ┌────────────────────┴────────────────────┐
                    ▼                                         ▼
          durable SQLite history                        forecast.json
          + experiment manifest                              │
          GitHub Release assets                              ▼
                                                     formatted X post
                                                            │
                                                            ▼
                                                          Twikit
```

### Production scheduling

The GitHub Actions forecast workflow wakes up hourly at minute `37` UTC. A lightweight scheduler guard checks the latest completed forecast candle and only runs the expensive model when at least two completed hourly candles have elapsed.

This design is intentional: GitHub scheduled workflows are best-effort and can be delayed. Waking hourly gives the project another chance to run without producing a new expensive forecast every hour.

Scheduled forecasts post to X automatically. Manual runs only post when explicitly requested.

### Market-data safety

Kraken BTC/USD remains the production primary. Binance BTCUSDT is a validated fallback. Both providers are normalized into the same hourly `MarketData` representation and overlapping closes are compared when both datasets are available. Production fails closed when provider disagreement exceeds the configured tolerance instead of silently trusting a suspicious source.

### Durable history and reproducibility

The canonical production history is a SQLite database stored through the dedicated GitHub Release tag:

```text
forecast-history-v1
```

The production workflow stores the current compressed SQLite database, analysis exports and a previous known-good generation. The rolling `.state/previous_forecast.json` cache remains useful for fast scheduling but is not the historical source of truth.

Each production forecast and walk-forward backtest now includes a versioned `experiment_manifest`. The manifest records the Git revision, TimesFM model/package version, effective model/ensemble configuration, deterministic seed, source/provider identity, exact candle window and a SHA-256 OHLCV fingerprint. Stable `configuration_id` and `data_id` values make equivalent runs directly comparable while `run_id` identifies one execution.

### Current modeling strategy

The ensemble predicts hourly log returns and reconstructs future BTC prices. TimesFM is combined with deliberately simple baselines so the project can continuously answer an important question:

> Is the complex forecast actually beating persistence?

Weights adapt separately for 2h, 4h, 8h and 16h according to matured historical performance. Current weighting considers MAE, direction accuracy, bias, interval behavior and performance relative to persistence, while retaining strict floors/caps and sparse-history fallbacks.

### Research workflow

The project currently has two complementary research paths:

1. **Walk-forward backtesting** for comparing models and ensemble behavior historically.
2. **Weekly optimizer** for evaluating bounded parameter candidates against the deployed configuration and persistence.

Both now emit reproducibility metadata. The optimizer is intentionally recommendation-only. It does not silently deploy parameter changes.

---

## Current strengths

1. **Out-of-sample thinking is built into the architecture.** Production outcomes are matured only after the real target candle exists.
2. **Persistence is treated as a first-class benchmark.** Complexity is not automatically considered better.
3. **The ensemble can adapt without allowing one model to dominate suddenly.**
4. **Historical forecasts survive GitHub Actions cache eviction.**
5. **Production and research share the same major forecasting concepts.**
6. **Input data is validated and the primary provider is no longer a single point of failure.**
7. **Historical results can be tied to code, model/configuration and exact input-data fingerprints.**
8. **The project is cheap enough to operate on GitHub Actions.**
9. **Public posts expose both direction and uncertainty rather than only a point estimate.**
10. **PR CI covers tests, coverage, lint/format and core-module typing; dependency/security audits run automatically.**

---

## Current limitations and risks

These are the main reasons the current forecast should still be considered experimental.

### Data breadth and provider basis

Production now has validation and automatic Kraken → Binance failover, but both inputs are still spot-market price/volume sources and the fallback uses BTCUSDT as a USD proxy. Richer independent signals and longer-term provider-quality monitoring are still missing.

### Model concentration

Three TimesFM contexts provide diversity in lookback length, but they are still the same underlying model family. Their errors can be strongly correlated.

### Regime detection

The current regime classifier is heuristic. Since regime labels influence adaptive history selection and priors, a weak classifier can make the ensemble adapt to the wrong historical conditions.

### Statistical evidence

The current project reports performance metrics, but stronger leakage-safe cross-validation and explicit significance/confidence testing are still needed before small improvements should be trusted.

### Feature set

Production primarily learns from BTC OHLCV-derived information. Funding, open interest, liquidations, market microstructure and cross-asset signals are not yet integrated.

### Social publishing

Twikit is an unofficial X client. Session cookies can expire, and frontend changes can break posting. Duplicate-post protection and explicit session-health checks should be added.

### Operational controls

Structured observability, circuit breakers, history integrity repair tooling and formal backup/retention policy remain to be completed.

### Merge enforcement

Unit tests and quality checks run on every pull request, but enforcing them as mandatory merge gates depends on repository branch/ruleset capabilities and the GitHub plan used for this private repository.

---

# Roadmap

The roadmap is split into five phases. Dependencies are intentional: later work should not be started when it depends on an unfinished foundation unless the work can safely proceed in parallel.

**Completed roadmap items:** #17, #18, #19, #21, #25 and #40.

**Immediately unblocked/high-value work after #21:** #20 (history integrity), #22 (observability) and #26 (expanded benchmarks). #34/#35/#36 are also dependency-unblocked, but the recommended modeling path is to establish #26 → #27 → #28 before expanding the feature surface aggressively.

## Phase 1 — Foundation & Data

Goal: make inputs, historical data and CI trustworthy enough that later modeling work is based on reliable evidence.

| Issue | Improvement | Depends on | Priority |
|---|---|---|---|
| [#17](https://github.com/jpfelgueiras/btc-timesfm/issues/17) | ✅ Market data validation and anomaly detection | #5, #9 | P0 |
| [#18](https://github.com/jpfelgueiras/btc-timesfm/issues/18) | ✅ Redundant market data source and automatic fallback | #17 | P0 |
| [#19](https://github.com/jpfelgueiras/btc-timesfm/issues/19) | ✅ Version forecast-history schema and add migrations | #5, #9 | P0 |
| [#20](https://github.com/jpfelgueiras/btc-timesfm/issues/20) | Forecast-history integrity audit and repair tooling | #19 | P1 |
| [#21](https://github.com/jpfelgueiras/btc-timesfm/issues/21) | ✅ Reproducible experiment manifests | #5, #9, #19 | P1 |
| [#22](https://github.com/jpfelgueiras/btc-timesfm/issues/22) | Structured observability and pipeline metrics | #17, #21 | P1 |
| [#23](https://github.com/jpfelgueiras/btc-timesfm/issues/23) | Forecast performance dashboard/reporting | #5, #22 | P1 |
| [#24](https://github.com/jpfelgueiras/btc-timesfm/issues/24) | Retention, backup and recovery policy | #19, #20 | P1 |
| [#25](https://github.com/jpfelgueiras/btc-timesfm/issues/25) | ✅ CI quality gates: linting, typing and coverage | #9 | P0 |

### Phase 1 definition of done

- ✅ Invalid/stale candles cannot reach the forecasting model unnoticed.
- ✅ A healthy secondary provider can safely replace the primary during an outage.
- ✅ Historical database changes use tested migrations.
- Historical-data corruption can be detected before it influences adaptive weights.
- ✅ Every experiment/production run is reproducible from recorded metadata.
- Pipeline stages expose structured status and timing information.
- Forecast quality can be reviewed without manually inspecting raw JSON.
- ✅ CI catches style/type/test regressions on every PR.

---

## Phase 2 — Modeling & Evaluation

Goal: make model improvements statistically defensible and increase ensemble diversity.

| Issue | Improvement | Depends on | Priority |
|---|---|---|---|
| [#26](https://github.com/jpfelgueiras/btc-timesfm/issues/26) | Expanded benchmark suite | #5, #9, #21 | P0 |
| [#27](https://github.com/jpfelgueiras/btc-timesfm/issues/27) | Purged walk-forward cross-validation | #26 | P0 |
| [#28](https://github.com/jpfelgueiras/btc-timesfm/issues/28) | Statistical significance and uncertainty testing | #27 | P0 |
| [#29](https://github.com/jpfelgueiras/btc-timesfm/issues/29) | Improved market regime detection | #5, #27, #28 | P1 |
| [#30](https://github.com/jpfelgueiras/btc-timesfm/issues/30) | Correlation-aware ensemble weighting | #6, #26, #28 | P1 |
| [#31](https://github.com/jpfelgueiras/btc-timesfm/issues/31) | Conformal calibration for forecast intervals | #5, #27 | P1 |
| [#32](https://github.com/jpfelgueiras/btc-timesfm/issues/32) | Diversified non-TimesFM forecasting model | #26, #27, #28 | P1 |
| [#33](https://github.com/jpfelgueiras/btc-timesfm/issues/33) | Model and feature drift detection | #5, #26, #28 | P1 |

### Phase 2 definition of done

- Every new model is evaluated against the same benchmark suite.
- Research uses deterministic chronological folds with explicit leakage protection.
- Candidate improvements include confidence intervals/effect sizes, not only average MAE.
- Regimes are validated out of sample.
- Ensemble weighting accounts for correlated model errors.
- Prediction intervals have empirically defensible coverage.
- At least one genuinely different model family is evaluated.
- Production can identify when recent market/error behavior has drifted materially.

---

## Phase 3 — Market Signals

Goal: determine whether crypto-native and cross-market information adds real out-of-sample predictive value.

| Issue | Improvement | Depends on | Priority |
|---|---|---|---|
| [#34](https://github.com/jpfelgueiras/btc-timesfm/issues/34) | Funding, open interest and liquidation signals | #17, #18, #19 | P1 |
| [#35](https://github.com/jpfelgueiras/btc-timesfm/issues/35) | Order-book and microstructure features | #17, #18 | P2 |
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
| [#38](https://github.com/jpfelgueiras/btc-timesfm/issues/38) | X posting idempotency and session-health checks | #9, #22 | P0 |
| [#39](https://github.com/jpfelgueiras/btc-timesfm/issues/39) | Pipeline alerts, health checks and circuit breakers | #17, #18, #22, #33 | P0 |
| [#40](https://github.com/jpfelgueiras/btc-timesfm/issues/40) | ✅ Automated dependency and security scanning | #25 | P1 |
| [#44](https://github.com/jpfelgueiras/btc-timesfm/issues/44) | Statistically grounded confidence explanations in posts | #23, #30, #31, #33 | P1 |

### Phase 4 definition of done

- Re-running a forecast cannot publish the same X post twice.
- Expired X sessions are detected clearly and safely.
- Severe data/model health conditions stop publication instead of emitting misleading forecasts.
- Repeated failures and recovery states are visible.
- ✅ Dependency vulnerabilities are surfaced automatically.
- Public confidence language is based on measured evidence, sample size and calibration rather than raw model agreement alone.

---

## Phase 5 — Research Automation

Goal: automate repetitive model-selection work without allowing the research system to silently change production behavior.

| Issue | Improvement | Depends on | Priority |
|---|---|---|---|
| [#41](https://github.com/jpfelgueiras/btc-timesfm/issues/41) | Optimizer promotion policy and safety guardrails | #7, #28, #33 | P0 |
| [#42](https://github.com/jpfelgueiras/btc-timesfm/issues/42) | Champion-vs-challenger evaluation reports | #7, #26, #28, #41 | P1 |
| [#43](https://github.com/jpfelgueiras/btc-timesfm/issues/43) | Automatically open safe parameter-change PRs | #21, #41, #42 | P2 |

### Phase 5 definition of done

- A machine-readable promotion policy determines whether research evidence is sufficient.
- Production and challenger configurations are evaluated on identical samples/folds.
- Research reports explain why a candidate is accepted, rejected or inconclusive.
- The optimizer can open a reviewable configuration PR only after all safety criteria pass.
- Production changes still require normal PR review and CI; the optimizer never auto-merges its own changes.

---

# Dependency map

## Critical paths

```text
#9 ──► #25 ✅ ──► #40 ✅
```

```text
#5 ──► #19 ✅ ──► #20 ──► #24
                │
                └──► #21 ✅ ──► #22 ──► #23
```

```text
#5 + #9 + #21 ✅
          │
          ▼
         #26
          │
          ▼
         #27
          │
          ▼
         #28
          │
          ├──► #29   improved regimes
          ├──► #30   correlation-aware ensemble
          ├──► #32   diversified model
          └──► #33   drift detection
```

```text
#17 ✅ ──► #18 ✅
   │          │
   │          ├──► #34 derivatives signals ─┐
   │          ├──► #35 order-book signals ─┼──► #37 feature ablation
   │          └──► #36 cross-asset signals ─┘
   │
   └──► #22 ──► #38 / #39
```

```text
#7 + #28 + #33
       │
       ▼
      #41
       │
       ▼
      #42
       │
       ▼
      #43
```

---

# Recommended next execution order

The highest-value next work now splits into three independent foundation/evaluation tracks:

1. **#26 — Expanded benchmark suite (P0)**  
   #21 completes the reproducibility dependency, so the main modeling critical path can now start. Stronger baselines should be in place before more sophisticated model changes are trusted.

2. **#20 — Forecast-history integrity audit and repair tooling (P1)**  
   Complete the durability track started by #19 and unblock #24 backup/retention work.

3. **#22 — Structured observability and pipeline metrics (P1)**  
   Now unblocked by #17 + #21. This creates the run-level telemetry needed by #23 and the production-hardening work in #38/#39.

Then continue in dependency order:

4. **#27 — Purged walk-forward cross-validation** after #26.
5. **#24 — Retention, backup and recovery policy** after #20.
6. **#38 — X posting idempotency/session health** after #22; this is the next P0 production-reliability item that becomes available.
7. **#28 — Statistical significance testing** after #27.
8. **#23 — Forecast performance dashboard** after #22.
9. **#31 — Conformal interval calibration** after #27.
10. **#34/#35/#36 — richer market signals** can be explored in parallel, but should be promoted only through the stronger #26/#27/#28 evaluation path.

After #28, the project is ready to pursue **#29/#30/#32/#33** in parallel and then formalize optimizer promotion through **#41 → #42 → #43**.

---

# Suggested release stages

## Stage A — Trusted inputs and history

Target issues: **#17, #18, #19, #20, #21, #24, #25**  
Completed: **#17, #18, #19, #21, #25**. Remaining: **#20, #24**.

Outcome: production data, durable history and CI are trustworthy and recoverable.

## Stage B — Defensible model evaluation

Target issues: **#26, #27, #28, #31**

Outcome: improvements can be evaluated with leakage-safe folds, stronger baselines and statistical evidence.

## Stage C — Better ensemble intelligence

Target issues: **#29, #30, #32, #33**

Outcome: better regime awareness, less correlated ensemble behavior, a genuinely different model family and drift awareness.

## Stage D — Richer market information

Target issues: **#34, #35, #36, #37**

Outcome: crypto-native and cross-market features are added only where ablation proves value.

## Stage E — Production hardening

Target issues: **#22, #23, #38, #39, #40, #44**  
Completed: **#40**.

Outcome: production can run unattended with usable monitoring, safer X publishing and better public confidence communication.

## Stage F — Controlled research automation

Target issues: **#41, #42, #43**

Outcome: the research loop can recommend and prepare improvements automatically while preserving human review and CI gates.

---

# Metrics that should guide the roadmap

The project should avoid optimizing for a single headline number. Track at least:

- MAE % by horizon
- signed bias by horizon
- direction accuracy by horizon
- Q10-Q90 empirical coverage
- average prediction-interval width
- performance relative to persistence
- performance by market regime
- fold-to-fold stability
- model residual correlation
- sample count behind every performance claim
- production run success rate
- data fallback rate
- model inference duration
- X publication success/duplicate-prevention rate

A change should generally not be promoted simply because average MAE improves if it materially worsens another protected horizon, becomes unstable across folds, loses badly to persistence in an important regime, or relies on too few observations.

---

# Project principles

1. **No look-ahead leakage.** Research results are invalid if future information can enter features, weights or model-selection decisions.
2. **Persistence is always a benchmark.** A complex model must earn its place.
3. **Production history is immutable research evidence.** Manual reruns must never rewrite the original forecast that was observed.
4. **Reproducibility is part of the result.** A metric without code/configuration/data identity is not enough evidence for promotion.
5. **Prefer measured improvement over model novelty.** New models/features should be added because evaluation supports them, not because they are fashionable.
6. **Uncertainty matters.** Point forecasts without reliable uncertainty can create false confidence.
7. **Fail closed on bad data.** Skipping a tweet is better than publishing from stale or contradictory inputs.
8. **Automation must remain reviewable.** Research automation may recommend or open PRs, but should not silently deploy its own findings.
9. **Keep GitHub Actions cost/runtime bounded.** The project should remain practical to operate continuously.

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
