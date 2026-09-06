#!/usr/bin/env python3
from pathlib import Path
import re

path = Path("ROADMAP.md")
text = path.read_text(encoding="utf-8")

replacements = {
    "- **Machine-readable optimizer promotion policy** with statistical, horizon, regime, persistence, and production-health guardrails; decisions remain review-only.\n": (
        "- **Machine-readable optimizer promotion policy** with statistical, horizon, regime, persistence, and production-health guardrails; decisions remain review-only.\n"
        "- **Validated regime detection** with controlled transition churn and out-of-sample comparison against the legacy heuristic.\n"
        "- **Correlation-aware ensemble weighting** that penalizes redundant matured residual patterns while preserving sparse-history safeguards.\n"
        "- **Conformal-style interval calibration** using matured historical nonconformity scores with safe sparse-history fallback.\n"
        "- **Optional diversified non-TimesFM model** evaluated walk-forward before any production participation.\n"
        "- **Champion-vs-challenger evaluation reports** with identical-origin pairing, statistical evidence, and policy recommendations.\n"
        "- **Timestamp-safe crypto derivatives context** covering funding, open interest and liquidations, with durable feature persistence and leakage-safe ablation.\n"
    ),
    "- [#41 Optimizer promotion policy and safety guardrails](https://github.com/jpfelgueiras/btc-timesfm/issues/41)\n": (
        "- [#41 Optimizer promotion policy and safety guardrails](https://github.com/jpfelgueiras/btc-timesfm/issues/41)\n"
        "- [#29 Improved market regime detection](https://github.com/jpfelgueiras/btc-timesfm/issues/29)\n"
        "- [#30 Correlation-aware ensemble weighting](https://github.com/jpfelgueiras/btc-timesfm/issues/30)\n"
        "- [#31 Conformal interval calibration](https://github.com/jpfelgueiras/btc-timesfm/issues/31)\n"
        "- [#32 Diversified non-TimesFM forecasting model](https://github.com/jpfelgueiras/btc-timesfm/issues/32)\n"
        "- [#34 Crypto derivatives signals](https://github.com/jpfelgueiras/btc-timesfm/issues/34)\n"
        "- [#42 Champion-vs-challenger evaluation reports](https://github.com/jpfelgueiras/btc-timesfm/issues/42)\n"
    ),
    "Production has validation and automatic Kraken → Bitstamp failover, but both inputs are still spot-market OHLCV sources. Funding, open interest, liquidations, order-book microstructure, and cross-asset information are not yet integrated.": (
        "Production now captures timestamp-safe funding, open-interest and liquidation context alongside validated spot OHLCV. Order-book microstructure and cross-asset/macro information are still not integrated, and derivatives signals remain passive until their walk-forward ablation demonstrates defensible out-of-sample value."
    ),
    "Three TimesFM contexts provide diversity in lookback length, but they are still the same underlying model family. Their errors can remain highly correlated.": (
        "Three TimesFM contexts still share one model family, but production research now measures residual correlation and can penalize redundant model influence. A materially different optional model is available for validation before production participation."
    ),
    "The current regime classifier is heuristic. Since regime labels influence adaptive-history selection and priors, a weak classifier can make the ensemble adapt to the wrong historical conditions.": (
        "Regime detection now uses a validated, reproducible detector with measured transition churn and an explicit legacy-heuristic benchmark. Regime labels still remain an important model-risk surface and should continue to be monitored as market structure changes."
    ),
    "Current intervals use empirical calibration, but conformal calibration has not yet been implemented. Coverage should therefore still be treated as experimental, especially during regime shifts.": (
        "Intervals now use conformal-style calibration from matured historical errors with sparse-history fallback. Coverage remains experimental during regime shifts and low-sample conditions, so observed coverage and width should continue to be monitored."
    ),
    "**Completed roadmap items:** #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28, #33, #38, #39, #40, and #41.": (
        "**Completed roadmap items:** #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28, #29, #30, #31, #32, #33, #34, #38, #39, #40, #41, and #42."
    ),
    "**Highest-value currently unblocked work:** #31 (conformal interval calibration), #30 (correlation-aware weighting), #29 (improved regimes), and #32 (diversified model). #42 (champion-vs-challenger reporting) is now also unblocked by completion of #41.": (
        "**Highest-value currently unblocked work:** #44 (statistically grounded confidence explanations), #43 (safe optimizer-generated PRs), #35 (order-book/microstructure features), and #36 (cross-asset/macro signals). #37 remains blocked until #35 and #36 are complete."
    ),
}

for old, new in replacements.items():
    if old not in text:
        print(f"warning: roadmap phrase not found: {old[:90]!r}")
    else:
        text = text.replace(old, new, 1)

for issue in (29, 30, 31, 32, 34, 42):
    pattern = rf"(\| \[#{issue}\]\(https://github\.com/jpfelgueiras/btc-timesfm/issues/{issue}\) \| )(?!✅ )"
    text, count = re.subn(pattern, rf"\1✅ ", text, count=1)
    if count == 0:
        print(f"warning: table row for #{issue} not updated")

text = text.replace("✅ #28 → #30/#32/✅ #33", "✅ #28 → ✅ #30/✅ #32/✅ #33")
text = text.replace("✅ #17 → ✅ #18 → #34/#35/#36 → #37", "✅ #17 → ✅ #18 → ✅ #34/#35/#36 → #37")
text = text.replace("✅ #41 → #42 → #43", "✅ #41 → ✅ #42 → #43")

path.write_text(text, encoding="utf-8")
