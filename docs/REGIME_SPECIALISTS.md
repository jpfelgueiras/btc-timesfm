# Regime-specialized forecasting experts

`research/regime_specialists.py` tests a mixture-of-experts design where each market
regime delegates to its own weighting configuration, and compares it to the current
regime-conditioned weighting used in production.

## Expert catalog

`EXPERT_DEFINITIONS` maps each validated regime to a distinct, deterministic weighting
configuration. Every expert is a reproducible override applied on top of the existing
`correlation_aware_model_weights` policy:

| Knob | Meaning |
| --- | --- |
| `history_limit` | usable window of matured outcomes passed to the weighting policy |
| `confidence` | adaptive-blend confidence (shrink toward the static prior) |
| `max_weight` | post-processing per-model weight cap before renormalization |
| `direction_reward` | boost models with better realized direction accuracy |
| `persistence_fallback_boost` | extra persistence weight when complex models fail to beat it |

- **range** – conservative blend, tighter cap for diversification, small direction
  reward, persistence anchor.
- **trending** – longest window, maximum blend, strongest direction reward so
  directional models dominate clean trends.
- **high_volatility** – short window (volatility expands/decays fast), low blend,
  tight cap, strong persistence fallback.
- **fallback** – conservative equal-ish static allocation (half equal weights, half the
  neutral `range` prior) used when the regime is unknown or history is too sparse.

`expert_catalog()` returns a deep copy so callers cannot mutate the module-level
definitions.

## Evaluation

`evaluate_regime_specialists(samples, actual_by_timestamp, ...)` replays frozen
per-origin forecasts chronologically (the same schema as `regime_backtest.py`). At each
origin:

1. the validated regime detector classifies `market_features`; incomplete feature rows
   are treated as an unknown regime;
2. the baseline computes the current regime-conditioned correlation-aware weights;
3. if the regime is unknown or any horizon reports `insufficient_history`, the candidate
   uses the fallback expert, otherwise the matching regime specialist is used;
4. both policies only receive actuals with `target <= origin_timestamp` (leakage-safe);
5. both ensemble prices are scored against the matured outcome for every horizon.

The report includes per-horizon and per-(regime, horizon) MAE%/direction accuracy for
baseline and candidate, paired-bootstrap comparisons (`paired_bootstrap_comparison`),
expert transition/churn, expert utilization counts, fallback usage broken down by reason
(`unknown_regime` vs `sparse_history`), sample sizes, and promotion candidates.

Promotion to production requires a statistically defensible out-of-sample improvement:
a segment is only a `promotion_candidate` when the bootstrap confidence interval is above
zero for MAE and the segment is not low-sample. Low-sample segments are marked
`inconclusive`.

## CLI

```text
python -m btc_timesfm.research.regime_specialists \
  --samples-json samples.json --actuals-json actuals.json \
  --output regime_specialists_report.json --markdown regime_specialists_report.md
```

JSON is the machine-readable truth; the markdown file is a concise human summary.