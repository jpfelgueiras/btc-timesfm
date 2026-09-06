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
    "- **Timestamp-safe crypto derivatives context** covering funding, open interest and liquidations, with durable feature persistence and leakage-safe ablation.\n",
    "- **Timestamp-safe crypto derivatives context** covering funding, open interest and liquidations, with durable feature persistence and leakage-safe ablation.\n"
    "- **Prospectively captured order-book microstructure context** with Kraken primary, Bitstamp fallback, bounded capture lag, durable feature persistence, and leakage-safe horizon-specific ablation.\n",
)
swap(
    "- [#34 Crypto derivatives signals](https://github.com/jpfelgueiras/btc-timesfm/issues/34)\n",
    "- [#34 Crypto derivatives signals](https://github.com/jpfelgueiras/btc-timesfm/issues/34)\n"
    "- [#35 Order-book and market microstructure features](https://github.com/jpfelgueiras/btc-timesfm/issues/35)\n",
)
swap(
    "Production now captures timestamp-safe funding, open-interest and liquidation context alongside validated spot OHLCV. Order-book microstructure and cross-asset/macro information are still not integrated, and derivatives signals remain passive until their walk-forward ablation demonstrates defensible out-of-sample value.",
    "Production now captures timestamp-safe funding, open-interest, liquidation and prospectively observed order-book microstructure context alongside validated spot OHLCV. Cross-asset/macro information is still not integrated, and external signals remain passive until their walk-forward ablations demonstrate defensible out-of-sample value.",
)
swap(
    "**Completed roadmap items:** #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28, #29, #30, #31, #32, #33, #34, #38, #39, #40, #41, #42, and #44.",
    "**Completed roadmap items:** #17, #18, #19, #20, #21, #22, #23, #24, #25, #26, #27, #28, #29, #30, #31, #32, #33, #34, #35, #38, #39, #40, #41, #42, and #44.",
)
swap(
    "**Highest-value currently unblocked work:** #35 (order-book/microstructure features) and #36 (cross-asset/macro signals) to unlock the P1 #37 feature-ablation pipeline. #43 (safe optimizer-generated PRs) is also fully unblocked and can proceed in parallel.",
    "**Highest-value currently unblocked work:** #36 (cross-asset/macro signals), which is the final dependency needed to unlock the P1 #37 feature-ablation pipeline. #43 (safe optimizer-generated PRs) is already fully unblocked and can proceed in parallel.",
)
swap(
    "| [#35](https://github.com/jpfelgueiras/btc-timesfm/issues/35) | Order-book and microstructure features | #17, #18 | P2 |",
    "| [#35](https://github.com/jpfelgueiras/btc-timesfm/issues/35) | ✅ Order-book and microstructure features | #17, #18 | P2 |",
)
swap(
    "   │          ├──► #35 order-book signals ─┼──► #37 feature ablation",
    "   │          ├──► #35 ✅ order-book signals ─┼──► #37 feature ablation",
)
swap(
    "The core modeling/evaluation path through **#29, #30, #31, #32, #33**, the first external signal family **#34**, champion/challenger review **#42**, and evidence-grounded public confidence **#44** is complete. The remaining roadmap is concentrated in richer market inputs, automated feature selection, and safe research automation.",
    "The core modeling/evaluation path through **#29, #30, #31, #32, #33**, external signal families **#34 and #35**, champion/challenger review **#42**, and evidence-grounded public confidence **#44** is complete. The remaining roadmap is concentrated in cross-asset inputs, automated feature selection, and safe research automation.",
)
swap(
    "1. **#35 — Order-book and market microstructure features (P2)**  \n   Add timestamp-safe liquidity, spread, depth and imbalance context, with independent 2h/4h/8h/16h evaluation.\n\n2. **#36 — Cross-asset and macro signals (P2)**  \n   Add a small reproducible set of related-market features with strict completed-hour alignment and graceful outages.\n\n3. **#37 — Automated feature ablation and selection (P1)**  \n   Once #35 and #36 are complete, evaluate all external feature families together and promote only stable out-of-sample contributors.\n\n4. **#43 — Safe optimizer-generated parameter PRs (P2)**  \n   This is fully unblocked by #21, #41 and #42 and can proceed in parallel, while remaining review-only and never auto-merging.",
    "1. **#36 — Cross-asset and macro signals (P2)**  \n   Add a small reproducible set of related-market features with strict completed-hour alignment and graceful outages.\n\n2. **#37 — Automated feature ablation and selection (P1)**  \n   Once #36 is complete, evaluate derivatives, microstructure and cross-asset feature families together and promote only stable out-of-sample contributors.\n\n3. **#43 — Safe optimizer-generated parameter PRs (P2)**  \n   This is fully unblocked by #21, #41 and #42 and can proceed in parallel, while remaining review-only and never auto-merging.",
)
swap(
    "Completed: **#34**. Remaining: **#35, #36, #37**.",
    "Completed: **#34, #35**. Remaining: **#36, #37**.",
)

path.write_text(text, encoding="utf-8")
