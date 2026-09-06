#!/usr/bin/env python3
from pathlib import Path

path = Path("ROADMAP.md")
text = path.read_text(encoding="utf-8")


def swap(old: str, new: str) -> None:
    global text
    count = text.count(old)
    if count != 1:
        raise RuntimeError(f"Expected exactly one roadmap match, found {count}: {old[:100]!r}")
    text = text.replace(old, new, 1)


swap(
    "- **Machine-readable optimizer promotion policy** with statistical, horizon, regime, persistence, and production-health guardrails; decisions remain review-only.\n",
    "- **Machine-readable optimizer promotion policy** with statistical, horizon, regime, persistence, and production-health guardrails; decisions remain review-only.\n"
    "- **Validated regime detection** with controlled transition churn and out-of-sample comparison against the legacy heuristic.\n"
    "- **Correlation-aware ensemble weighting** that penalizes redundant matured residual patterns while preserving sparse-history safeguards.\n"
    "- **Conformal-style interval calibration** using matured historical nonconformity scores with safe sparse-history fallback.\n"
    "- **Optional diversified non-TimesFM model** evaluated walk-forward before any production participation.\n"
    "- **Champion-vs-challenger evaluation reports** with identical-origin pairing, statistical evidence, and policy recommendations.\n"
    "- **Timestamp-safe crypto derivatives context** covering funding, open interest and liquidations, with durable feature persistence and leakage-safe ablation.\n"
    "- **Evidence-grounded forecast confidence explanations** based on matured performance, calibration, sample depth and drift rather than raw model agreement.\n",
)

swap(
    "- [#41 Optimizer promotion policy and safety guardrails](https://github.com/jpfelgueiras/btc-timesfm/issues/41)\n",
    "- [#41 Optimizer promotion policy and safety guardrails](https://github.com/jpfelgueiras/btc-timesfm/issues/41)\n"
    "- [#29 Improved market regime detection](https://github.com/jpfelgueiras/btc-timesfm/issues/29)\n"
    "- [#30 Correlation-aware ensemble weighting](https://github.com/jpfelgueiras/btc-timesfm/issues/30)\n"
    "- [#31 Conformal interval calibration](https://github.com/jpfelgueiras/btc-timesfm/issues/31)\n"
    "- [#32 Diversified non-TimesFM forecasting model](https://github.com/jpfelgueiras/btc-timesfm/issues/32)\n"
    "- [#34 Crypto derivatives signals](https://github.com/jpfelgueiras/btc-timesfm/issues/34)\n"
    "- [#42 Champion-vs-challenger evaluation reports](https://github.com/jpfelgueiras/btc-timesfm/issues/42)\n"
    "- [#44 Statistically grounded confidence explanations](https://github.com/jpfelgueiras/btc-timesfm/issues/44)\n",
)

swap(
    "Production has validation and automatic Kraken → Bitstamp failover, but both inputs are still spot-market OHLCV sources. Funding, open interest, liquidations, order-book microstructure, and cross-asset information are not yet integrated.",
    "Production now captures timestamp-safe funding, open-interest and liquidation context alongside validated spot OHLCV. Order-book microstructure and cross-asset/macro information are still not integrated, and derivatives signals remain passive until their walk-forward ablation demonstrates defensible out-of-sample value.",
)
swap(
    "Three TimesFM contexts provide diversity in lookback length, but they are still the same underlying model family. Their errors can remain highly correlated.",
    "Three TimesFM contexts still share one model family, but production research now measures residual correlation and can penalize redundant model influence. A materially different optional model is available for validation before production participation.",
)
swap(
    "The current regime classifier is heuristic. Since regime labels influence adaptive-history selection and priors, a weak classifier can make the ensemble adapt to the wrong historical conditions.",
    "Regime detection now uses a validated, reproducible detector with measured transition churn and an explicit legacy-heuristic benchmark. Regime labels still remain an important model-risk surface and should continue to be monitored as market structure changes.",
)
swap(
    "Current intervals use empirical calibration, but conformal calibration has not yet been implemented. Coverage should therefore still be treated as experimental, especially during regime shifts.",
    "Intervals now use conformal-style calibration from matured historical errors with sparse-history fallback. Coverage remains experimental during regime shifts and low-sample conditions, so observed coverage and width should continue to be monitored.",
)

swap(
    "**Completed roadmap items:** #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28, #33, #38, #39, #40, and #41.",
    "**Completed roadmap items:** #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28, #29, #30, #31, #32, #33, #34, #38, #39, #40, #41, #42, and #44.",
)
swap(
    "**Highest-value currently unblocked work:** #31 (conformal interval calibration), #30 (correlation-aware weighting), #29 (improved regimes), and #32 (diversified model). #42 (champion-vs-challenger reporting) is now also unblocked by completion of #41.",
    "**Highest-value currently unblocked work:** #35 (order-book/microstructure features) and #36 (cross-asset/macro signals) to unlock the P1 #37 feature-ablation pipeline. #43 (safe optimizer-generated PRs) is also fully unblocked and can proceed in parallel.",
)

for issue, title in [
    (29, "Improved market regime detection"),
    (30, "Correlation-aware ensemble weighting"),
    (31, "Conformal calibration for forecast intervals"),
    (32, "Diversified non-TimesFM forecasting model"),
]:
    swap(
        f"| [#{issue}](https://github.com/jpfelgueiras/btc-timesfm/issues/{issue}) | {title} |",
        f"| [#{issue}](https://github.com/jpfelgueiras/btc-timesfm/issues/{issue}) | ✅ {title} |",
    )

swap(
    "| [#34](https://github.com/jpfelgueiras/btc-timesfm/issues/34) | Funding, open interest and liquidation signals |",
    "| [#34](https://github.com/jpfelgueiras/btc-timesfm/issues/34) | ✅ Funding, open interest and liquidation signals |",
)
swap(
    "| [#44](https://github.com/jpfelgueiras/btc-timesfm/issues/44) | Statistically grounded confidence explanations in posts |",
    "| [#44](https://github.com/jpfelgueiras/btc-timesfm/issues/44) | ✅ Statistically grounded confidence explanations in posts |",
)
swap(
    "| [#42](https://github.com/jpfelgueiras/btc-timesfm/issues/42) | Champion-vs-challenger evaluation reports |",
    "| [#42](https://github.com/jpfelgueiras/btc-timesfm/issues/42) | ✅ Champion-vs-challenger evaluation reports |",
)

for sentence in [
    "- Regimes are validated out of sample.",
    "- Ensemble weighting accounts for correlated model errors.",
    "- Prediction intervals have empirically defensible conformal coverage.",
    "- At least one genuinely different model family is evaluated.",
    "- Public confidence language is based on measured evidence, sample size, drift state, and calibration rather than raw model agreement alone.",
    "- Production and challenger configurations are evaluated on identical samples/folds.",
    "- Research reports explain why a candidate is accepted, rejected, or inconclusive.",
]:
    swap(sentence, sentence.replace("- ", "- ✅ ", 1))

swap("├──► #29   improved regimes", "├──► #29 ✅ improved regimes")
swap("├──► #30   correlation-aware ensemble", "├──► #30 ✅ correlation-aware ensemble")
swap("├──► #32   diversified model", "├──► #32 ✅ diversified model")
swap("├──► #34 derivatives signals ─┐", "├──► #34 ✅ derivatives signals ─┐")
swap("      #42\n       │", "      #42 ✅\n       │")

old_recommended = """The evaluation critical path **#26 → #27 → #28** is complete, #33 supplies production drift awareness, and the P0 production/research-safety work **#38, #39, and #41** is complete. The next work should improve uncertainty calibration, ensemble diversity, and reviewable research automation.\n\nThe highest-value sequence is:\n\n1. **#31 — Conformal calibration for forecast intervals (P1)**  \n   Improve uncertainty quality directly using the completed leakage-safe evaluation foundation.\n\n2. **#30 — Correlation-aware ensemble weighting (P1)**  \n   Prevent highly correlated TimesFM contexts from receiving misleadingly independent weight.\n\n3. **#29 — Improved market regime detection (P1)**  \n   Replace the heuristic classifier with an out-of-sample validated approach.\n\n4. **#32 — Diversified non-TimesFM forecasting model (P1)**  \n   Evaluate a genuinely different model family through the completed benchmark/CV/significance pipeline.\n\n5. **#42 — Champion-vs-challenger evaluation reports (P1)**  \n   Build the human-review layer now that #41 defines the machine-readable promotion contract; completion of #42 will unblock #43.\n\n6. **#34/#35/#36 — richer market signals** can proceed in parallel where useful, but promotion should continue to use the completed #26/#27/#28 evaluation path. After all three signal families exist, #37 can automate ablation/selection.\n\nOnce #30 and #31 are complete, #44 can replace raw model-agreement language in public posts with evidence-grounded confidence explanations.\n"""
new_recommended = """The core modeling/evaluation path through **#29, #30, #31, #32, #33**, the first external signal family **#34**, champion/challenger review **#42**, and evidence-grounded public confidence **#44** is complete. The remaining roadmap is concentrated in richer market inputs, automated feature selection, and safe research automation.\n\nThe highest-value sequence is:\n\n1. **#35 — Order-book and market microstructure features (P2)**  \n   Add timestamp-safe liquidity, spread, depth and imbalance context, with independent 2h/4h/8h/16h evaluation.\n\n2. **#36 — Cross-asset and macro signals (P2)**  \n   Add a small reproducible set of related-market features with strict completed-hour alignment and graceful outages.\n\n3. **#37 — Automated feature ablation and selection (P1)**  \n   Once #35 and #36 are complete, evaluate all external feature families together and promote only stable out-of-sample contributors.\n\n4. **#43 — Safe optimizer-generated parameter PRs (P2)**  \n   This is fully unblocked by #21, #41 and #42 and can proceed in parallel, while remaining review-only and never auto-merging.\n"""
swap(old_recommended, new_recommended)

swap(
    "Completed: **#26, #27, #28**. Remaining: **#31**.",
    "Completed: **#26, #27, #28, #31**. Remaining: **none**.",
)
swap(
    "Completed: **#33**. Remaining: **#29, #30, #32**.",
    "Completed: **#29, #30, #32, #33**. Remaining: **none**.",
)
swap(
    "Target issues: **#34, #35, #36, #37**\n\nOutcome:",
    "Target issues: **#34, #35, #36, #37**  \nCompleted: **#34**. Remaining: **#35, #36, #37**.\n\nOutcome:",
)
swap(
    "Completed: **#22, #23, #38, #39, #40**. Remaining: **#44**.",
    "Completed: **#22, #23, #38, #39, #40, #44**. Remaining: **none**.",
)
swap(
    "Completed: **#41**. Remaining: **#42, #43**.",
    "Completed: **#41, #42**. Remaining: **#43**.",
)

path.write_text(text, encoding="utf-8")
