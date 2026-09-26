# Roadmap v8 deep forecast accuracy optimization — final status

_Status dated: 2026-09-26_

Roadmap v8 completed its implementation and audit-gate chain, but did not
complete an empirical accuracy evaluation. The merged work makes the proposed
experiments reproducible and fail-closed; it is not evidence of improved
forecast skill. No candidate was selected or promoted, and production
configuration and forecasts were not changed.

| Issue | Merged solution | Empirical outcome |
|---|---|---|
| [#339](https://github.com/jpfelgueiras/btc-timesfm/issues/339) | Canonical benchmark data and eligibility audit for a frozen, same-venue BTC/USD corpus. | **Blocked:** 0 eligible target bars of 32,136 and 0 warm-up bars of 4,320. The required corpus is unavailable; no skill comparison can be run. |
| [#342](https://github.com/jpfelgueiras/btc-timesfm/issues/342) | Dependence-aware synthetic Monte Carlo inference and a fail-closed forecast-policy parity blocker. | **Mechanics only:** synthetic simulations exercise inference behavior, not BTC forecast skill. Policy parity cannot be confirmed without eligible empirical records. |
| [#343](https://github.com/jpfelgueiras/btc-timesfm/issues/343) | Bounded target/return-path comparison gate. | **Blocked:** no eligible corpus or independently supported target head; no target winner or accuracy result. |
| [#344](https://github.com/jpfelgueiras/btc-timesfm/issues/344) | Context, horizon, checkpoint, and matched-lookback comparison gate. | **Blocked:** no canonical corpus or per-origin predictions; no context or checkpoint comparison. |
| [#345](https://github.com/jpfelgueiras/btc-timesfm/issues/345) | Persistence-first policy and incremental TimesFM skill ablation gate. | **Blocked:** no D1 paired outer predictions or qualifying candidate. D2 is short, mixed-version, descriptive only. |
| [#346](https://github.com/jpfelgueiras/btc-timesfm/issues/346) | Point-in-time feature inventory and incremental-value evidence gate. | **Blocked:** inventory mechanics only; no as-of corpus or leave-family-out value comparison. |
| [#347](https://github.com/jpfelgueiras/btc-timesfm/issues/347) | Causal native multi-timeframe aggregation and comparison gate. | **Blocked:** no eligible fine-resolution corpus, supported native frequency contract, or aligned predictions. |
| [#348](https://github.com/jpfelgueiras/btc-timesfm/issues/348) | Research-only regime/recency policy catalog and fail-closed evaluation gate. | **Blocked/inconclusive:** no eligible corpus, nested evaluation, or prospective confirmation; no policy skill finding. |
| [#349](https://github.com/jpfelgueiras/btc-timesfm/issues/349) | Cumulative-horizon interval calibration lineage and evidence gate. | **Blocked:** raw paired predictions, complete interval lineage, and required exact-target outcomes are unavailable; no calibration, coverage-skill, or sharpness claim. |
| [#350](https://github.com/jpfelgueiras/btc-timesfm/issues/350) | Frozen champion/challenger contract and final-selection structural gate. | **Blocked:** no eligible immutable corpus or complete candidate evidence; no winner or accuracy conclusion. |

The common blocker is #339's missing eligible data. In particular, the
implementation gates and synthetic tests do not substitute for empirical BTC
results. The short, mixed-version D2 history remains descriptive and cannot
establish skill or policy parity. No candidate has a frozen evidence package
ready to begin prospective D3; D3 confirmation therefore remains unavailable,
not negative. Any later accuracy or promotion claim requires eligible matched
records, the relevant preregistered evaluations, and prospective D3 evidence.

This closeout records the outcome of the #339 → #342 → #343 → #344 →
(#345, #346, #347) → (#348, #349) → #350 implementation chain. It makes no
model promotion, production configuration change, or empirical skill claim.
