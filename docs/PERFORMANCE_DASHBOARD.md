# Forecast performance dashboard

`performance_dashboard.py` generates an operator-friendly view of production forecast quality directly from the durable SQLite history database. No CSV preprocessing or manual joins are required.

## Metrics

The dashboard reports, separately for every 2h, 4h, 8h and 16h horizon:

- mean absolute percentage error (MAE)
- rolling forecast skill scores against `persistence` and each additional persisted baseline model
- mean signed error percentage (bias)
- direction accuracy
- Q10-Q90 interval coverage when a model emitted interval bounds
- matured sample count and interval sample count

The ensemble, every persisted individual model, and `persistence` are shown side by side. Persistence is always rendered as a required baseline even when no persisted samples are available, in which case the report flags `no_samples`.

Skill is normalized as `1 - candidate_mae / baseline_mae` over paired origins in the selected window, horizon and regime. Positive values mean the candidate beat the baseline; negative values mean it underperformed. The JSON report includes skill against persistence plus any additional persisted baseline/model, while the Markdown and HTML tables surface skill and edge state against persistence directly.

## Segmentation

Every metric is regenerated for:

- all available history
- rolling 7-day, 30-day and 90-day windows by default
- each persisted market regime inside every window and horizon

The rolling windows can be overridden with `--rolling-days`, for example `--rolling-days 14,60,180`.

## Confidence warnings

Segments with fewer than 20 matured observations are marked as low confidence by default. The threshold is configurable with `--low-sample-threshold`. Missing segments are marked `no_samples`.

Skill scores use the same threshold on paired candidate-vs-baseline samples and report `low_paired_sample_count`, `no_paired_samples`, or `zero_baseline_error` when the normalized score would be unreliable. Edge state is `positive`, `neutral`, `negative`, or `inconclusive`; the neutral band defaults to ±2 percentage points and can be changed with `--skill-neutral-threshold`.

These warnings are descriptive; they do not alter forecasts or adaptive weights.

## Generate locally

```bash
PYTHONPATH=src python -m btc_timesfm.research.performance_dashboard \
  --db .state/forecast_history.sqlite \
  --json performance_dashboard.json \
  --markdown performance_dashboard.md \
  --html performance_dashboard.html
```

The command verifies the durable database before reading it. A failed database verification stops dashboard generation rather than publishing misleading metrics.

Outputs:

- `performance_dashboard.json`: machine-readable metrics and warnings
- `performance_dashboard.md`: GitHub/terminal-friendly report
- `performance_dashboard.html`: standalone static dashboard with expandable regime tables

## Automation

`.github/workflows/performance-dashboard.yml` runs daily and on manual dispatch. It downloads the canonical `forecast_history.sqlite.gz` asset from the private `forecast-history-v1` GitHub Release, verifies/decompresses it, regenerates all three dashboard formats, appends the Markdown report to the Actions job summary, and uploads the dashboard files as a 90-day workflow artifact.

The workflow requires only the repository `GITHUB_TOKEN` with read access. It does not post to X and does not modify forecast history.

## Interpretation

MAE answers how far predictions were from the realized price on average. Skill scores answer whether that error was lower than a baseline on the same paired forecast origins. Signed bias shows systematic over- or under-prediction. Direction accuracy measures whether the model correctly predicted the sign of the move from the origin close. Q10-Q90 coverage measures calibration only for observations where interval bounds were stored.

Always compare the ensemble with persistence before treating a lower MAE as meaningful. Low-sample segments should be considered preliminary until the warning clears.
