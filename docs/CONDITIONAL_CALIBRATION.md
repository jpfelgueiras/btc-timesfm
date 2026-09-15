# Regime- and volatility-conditional interval calibration

Marginally calibrated intervals restore ~80% coverage on average, but a market currently in a regime or volatility state where intervals under-cover keeps being under-covered. Issue #155 re-calibrates the conformal interval adjustment per *(regime, realized-volatility)* bucket and applies the bucket's multiplier to the current forecast.

## Bucket definition

A forecast snapshot is assigned to exactly one bucket from origin-time information only:

- **regime** — the label attached to the snapshot when the forecast was created (`range`, `trending`, `high_volatility`);
- **realized-volatility state** — the short-window realized volatility of the dollar move (`market_features.volatility_6h_pct` by default, with fallbacks to other origin-time vol features for older snapshots), cut into `low`/`medium`/`high` by `BTC_CONDITIONAL_VOL_CUTS` (default `0.75, 1.50`).

The bucket key is `regime:volatility_bucket`, e.g. `range:low`. Assignment never reads outcome, target, or prediction fields, so two snapshots that share the same origin-time state land in the same bucket no matter how their realized outcomes differ.

## Recalibration

For each 2h/4h/8h/16h horizon the per-bucket conformal multiplier reuses the same normalized nonconformity scores (`abs(actual - point) / historical_half_width`) and finite-sample conformal quantile as the marginal `conformal_calibration` module. A bucket is trusted only once it has at least `min_samples` (default 20) matured samples; sparse buckets shrink back toward the marginal multiplier:

```
weight = min(1, samples / min_samples)
multiplier = weight * conformal_multiplier + (1 - weight) * marginal_multiplier
```

Sparse and empty buckets always report their sample size, coverage and shrinkage (`mode=shrunken` / `mode=marginal_fallback`), so they never silently claim precision. Buckets with enough samples use the raw conformal multiplier and report whether the resulting coverage is within `BTC_CONDITIONAL_COVERAGE_TOLERANCE` (default 0.10) of the target.

## No-look-ahead guarantee

Bucket labels are derived only from snapshot fields known at forecast time. Calibration consumes only matured snapshots whose origin is at or before the forecast origin (`now`); an explicit origin cutoff excludes any snapshot that postdates the forecast. Reassignment is origin-time-only and verified by tests that would catch look-ahead leakage (e.g. buckets computed from outcomes, or future origins feeding calibration).

## Output

`forecast.json` exposes the section under `conditional_calibration`:

```text
conditional_calibration.version
conditional_calibration.horizons.<horizon>.selected_bucket
conditional_calibration.horizons.<horizon>.selected.{samples, mode, shrinkage, multiplier, coverage_after, verified}
conditional_calibration.horizons.<horizon>.buckets.<bucket>.{samples, multiplier, shrinkage, coverage_before, coverage_after, verified}
conditional_calibration.horizons.<horizon>.recalibrated_interval.{q10_usd, q50_usd, q90_usd, half_width_usd, multiplier}
conditional_calibration.horizons.<horizon>.coverage_violations
conditional_calibration.overall.{verified_horizons, sparse_horizons, coverage_violation_buckets}
conditional_calibration.leakage_guard
```

`recalibrated_interval` rescales the current forecast's q10–q90 interval by the selected bucket's multiplier, derived from the base interval the engine already emitted. The JSON is the contract between calibration and downstream consumers (site/notebooks), matching the way distributional-metrics and direction-probability fields were added.

## Persistence and future work

The module keeps no mutable state: every forecast re-estimates from matured snapshots that predate the current origin, so the same path supports the later rolling recalibration work (#159). The per-bucket diagnostic surface (coverage, widths, shrinkage) is where #115 (dynamic no-edge claims) and #131 (uncertainty-aware weighting) plug in.

Implementation: `src/btc_timesfm/forecasting/conditional_calibration.py`, wired into `src/btc_timesfm/cli/btc_forecast.py`; tests in `tests/forecasting/test_conditional_calibration.py`.