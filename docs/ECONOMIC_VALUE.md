# Economic-value evaluation

The economic-value evaluation answers whether statistically significant forecast
improvements remain meaningful after simple market-friction assumptions.  It is
a research-only sanity check with no execution, brokerage, or trading
functionality.

```bash
PYTHONPATH=src python -m btc_timesfm.research.economic_value \
  --db .state/forecast_history.sqlite \
  --json economic_value_report.json \
  --markdown economic_value_report.md
```

## Assumptions

All friction assumptions are explicit and configurable via CLI arguments or
the `assumptions` dict passed to `build_report`:

| Parameter | Default | Description |
| --- | ---: | --- |
| `round_trip_fee_pct` | 0.1 | Round-trip trading fee (% of notional) |
| `slippage_pct` | 0.05 | Estimated slippage (% of notional) |
| `decision_threshold_pct` | 0.15 | Minimum predicted change to trigger a signal |
| `min_meaningful_move_pct` | 0.25 | Threshold for "practically negligible" flagging |
| `max_daily_turnover` | 6 | Maximum daily turnover assumed |

## Report structure

The JSON artifact includes:

- **Overall**: aggregate turnover rate, gross effect, friction-adjusted effect,
  statistical significance, and practical-negligibility flag.
- **By horizon**: per-horizon (2 h, 4 h, 8 h, 16 h) turnover, gross effect,
  friction-adjusted effect, significance, and instability flags.
- **Negligible horizons**: list of horizons where improvements are statistically
  significant but practically negligible.

## Key concepts

- **Turnover**: fraction of evaluation samples with an active (non-flat) signal.
- **Gross effect**: mean return from following the signal direction before friction.
- **Friction-adjusted effect**: gross effect minus round-trip fee and slippage.
- **Practically negligible**: statistically significant improvement whose
  friction-adjusted effect falls below the `min_meaningful_move_pct` threshold.

## Leakage guard

All evaluations are strictly out-of-sample.  Only matured durable-history rows
with `actual_target_price_usd` are consumed.  No future data is leaked at any
point.
