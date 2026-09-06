# Validated market regime detection

The production regime classifier keeps the existing stable labels (`range`, `trending`, `high_volatility`) but replaces the original two-rule heuristic with an interpretable score-state detector built from the validated feature pipeline.

The detector combines:

- 6h/24h/7d realized-volatility ratios
- 6h/24h/7d momentum normalized by realized volatility
- 24h average candle range
- 7d volume z-score
- RSI displacement and multi-window momentum direction consistency

Material thresholds and deadbands are fixed in code rather than fit on the evaluation period. The legacy heuristic remains available as `heuristic_regime`, and a deterministic fixed-prototype state classifier is included as a clustering-style research alternative.

## Transition behavior

`transition_churn()` reports state-change counts/rates. `smooth_regime_sequence()` provides a two-observation confirmation mechanism for offline sequence diagnostics so single-sample state spikes can be quantified. The production detector itself remains stateless/reproducible for an individual forecast and uses wider deadbands to avoid marginal transitions.

## Out-of-sample validation

`regime_backtest.py` replays the legacy and validated detectors on identical frozen forecast origins. Each detector gets its own regime-labelled adaptive history, and at every origin the weighting code only receives actual target candles already observable at that time. The report includes:

- MAE and direction accuracy by 2h/4h/8h/16h horizon
- metrics segmented by detected regime
- label distributions and transition churn for heuristic, validated-score and fixed-prototype methods
- relative MAE change versus the legacy detector
- a conservative safety veto when any protected horizon regresses by more than 5% by default

This comparison is intended to be run before accepting material threshold changes. The same validated detector is activated by the validated production/backtest/optimizer entrypoints once this issue is merged.
