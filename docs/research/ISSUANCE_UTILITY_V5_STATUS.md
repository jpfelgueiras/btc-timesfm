# Roadmap v5 issuance-time economic utility status

**Decision: blocked/inconclusive; no net-utility, trading, or retargeting claim.**

The current economic evaluator uses the candle origin price to score a matured
forecast. In audited live runs, forecast generation can occur after that close
(for example, an origin at 19:00 was generated at 19:20:58), so the origin
close cannot be assumed to be an executable fill. The durable history export
stores generation and target timestamps but does not retain a timestamped
decision-time quote series, actual spreads, or execution fills. The frozen D1
OHLCV dataset is not in this checkout and D3 decision-time capture is not
mature. Current D2 results are diagnostics only.

The existing `retargeting.py` functions are now documented explicitly as
research-only no-op placeholders; they have no effect on predictions and are
not activated. This issue adds a research helper that only chooses the first
timestamped price at or after generation and before target, returning no
opportunity when no price is available. It also defines a fixed first-issued,
non-overlapping position baseline. Unit tests cover publication delay, missing
prices, target bounds, timezone-awareness and overlap rejection. These helpers
do not supply real source prices or evidence of returns.

## Required evaluation before decision

1. Collect immutable timestamped post-publication decision quotes/trades and
   data vintages; apply no fill before generation and no unavailable-price
   fill. Include spread, slippage, fee and, where relevant, funding ranges.
2. Freeze the overlap/netting/capital rule, no-trade and simple persistence /
   momentum controls, plus base and stress-cost assumptions in inner validation.
3. Evaluate the frozen probability/abstention policy on paired opportunities
   without test-period threshold tuning. Report all-origin and active-call
   utility, turnover, exposure, drawdown/tail loss, break-even costs and
   latency sensitivity with dependence-aware uncertainty.
4. Keep statistical price skill separate from net utility. Report the
   retargeting placeholders as inactive until a separate predeclared
   distributional/economic experiment justifies changing canonical forecasts.

The D1/D3 economic experiment is therefore blocked; no existing forecast or
trading configuration changes in this branch.
