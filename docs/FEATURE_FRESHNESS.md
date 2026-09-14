# Feature freshness and decay analysis

Issue #120 measures how predictive value changes as each external or engineered
signal ages, and turns that into horizon-specific stale thresholds for the
feature-selection pipeline.

## What it does

1. **Deterministic feature age** — `feature_ages(features_dict, origin_at)`
   computes each feature's age in hours as the time between its recorded
   observation/capture time and the forecast origin. It is a pure function, so
   the same features and origin always produce the same ages.
2. **Value-vs-age curves** — matured predictions are bucketed by feature-age
   band per horizon and each band reports sample count, MAE%, direction
   accuracy, and the neutral/persistence baseline MAE% (predicting no change).
   Bands with too few samples are marked inconclusive.
3. **Evidence-based stale thresholds** — the threshold for a feature/horizon is
   the first age band (youngest to oldest) where the feature stops improving
   over the neutral baseline beyond a tolerance. Staleness is treated as
   cumulative. When evidence is insufficient (or never saw the feature improve),
   the deterministic provider default is kept.
4. **Deterministic missing/stale policy** — `stale_signal_policy` maps
   `(value, age, threshold)` to `fresh`, `stale_downgrade`, or `stale_exclude`.
5. **Recommendations** — a JSON-serializable recommendation per feature and
   horizon (`threshold_hours`, `default_threshold_hours`, `evidence_samples`,
   `action`) feeds the feature-selection pipeline.

## How feature freshness is recorded

`market_features_json` stores feature values together with capture metadata in
reserved keys (they are never treated as features themselves):

```json
{
  "derivatives_funding_rate_pct": 0.01,
  "volatility_24h_pct": 1.2,
  "feature_observation_times": {
    "derivatives_funding_rate_pct": "2026-01-10T08:00:00+00:00"
  },
  "captured_at": "2026-01-10T08:00:00+00:00"
}
```

- A per-feature map under `feature_observation_times` (alias `feature_timestamps`)
  takes precedence over the global `captured_at`/`observation_at` fallback.
- Timestamps may be ISO strings or epoch seconds (epochs above 1e12 are treated
  as milliseconds).
- Features without any recorded observation time are omitted from
  `feature_ages`, so a caller can tell freshness was never recorded.

Production signal modules already expose exactly this provenance: derivatives
(`funding_age_hours`, `stats_age_hours`), microstructure (`captured_at` /
`capture_lag_hours`), and cross-asset (`eth_age_hours`, VIX/US10Y `age_days`).

## Value-vs-age evaluation

Rows use the same shape as `ForecastHistoryStore.export_rows()` (one row per
origin/model/horizon). Only matured rows participate, and **any row whose
`target_at` falls after the evaluation `now` is excluded**, so curves cannot
leak a future outcome. When `model_name` is not specified, the ensemble is used
if present, otherwise all matured rows.

The report's neutral/baseline MAE% is persistence (predicting zero change),
computed from `actual_change_pct`. A band "improves" when its candidate MAE% is
below the baseline; a band is "degraded" when candidate MAE% exceeds the
baseline by more than the degradation tolerance (default 5% relative).

## Running

```bash
PYTHONPATH=src python -m btc_timesfm.research.feature_freshness_analysis \
  --db .state/forecast_history.sqlite \
  --json feature_freshness_report.json \
  --markdown feature_freshness_report.md
```

Options: `--now` (ISO cutoff, default current time), `--horizons` (default
`2,4,8,16`), `--min-samples` (default 20), `--tolerance` (default 0.05),
`--model-name`.

## Default thresholds

Deterministic defaults are used only when the curves provide insufficient
evidence, and mirror provider staleness rules:

| Feature prefix | Default threshold (hours) |
| --- | --- |
| `derivatives_funding_*` | 12.0 |
| `derivatives_*` | 2.5 |
| `microstructure_*` | 1.25 |
| `cross_eth_*`, `cross_btc_eth_*` | 2.0 |
| `macro_*` | 168.0 (7 days) |
| other market features | 1.0 |

## Missing/stale policy (deterministic)

| Case | Classification |
| --- | --- |
| value present, `age <= threshold` | `fresh` |
| value present, `age > threshold` | `stale_downgrade` |
| value present, age beyond `exclude_age_hours` (when supplied) | `stale_exclude` |
| value missing (`None`) | `stale_exclude` |
| value present, age unrecorded | `stale_downgrade` (conservative) |

The report's `recommendations` array decides whether a feature's aged region
should be down-weighted (`downgrade`) or excluded (`exclude`) at each horizon;
the runtime classification above is the deterministic faithful implementation
of that policy.