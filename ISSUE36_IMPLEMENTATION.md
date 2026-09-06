# Issue #36 implementation notes

Cross-asset and macro signals are intentionally passive research context in production until walk-forward evidence justifies promotion.

Production captures timestamp-safe ETH/USD crypto-beta features from completed Kraken hourly candles and conservative daily VIX/US 10-year observations from FRED. Daily observations are treated as available only from 00:00 UTC on the following calendar day to prevent same-day look-ahead.

Every snapshot records source provenance, freshness, missing features and provider errors. Provider failures degrade the snapshot to partial/unavailable without interrupting the BTC forecast.

The scheduled ablation compares the existing baseline with the same model augmented by cross-asset features using matured targets only. Results are reported separately for 2h, 4h, 8h and 16h horizons and by regime, with deterministic paired-bootstrap uncertainty. No signal is promoted from in-sample evidence alone.
