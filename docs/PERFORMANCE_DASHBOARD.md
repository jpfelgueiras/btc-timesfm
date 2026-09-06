# Forecast performance dashboard

`performance_dashboard.py` generates an operator-friendly view of production forecast quality directly from the durable SQLite history database. No CSV preprocessing or manual joins are required.

## Metrics

The dashboard reports, separately for every 2h, 4h, 8h and 16h horizon:

- mean absolute percentage error (MAE)
- mean signed error percentage (bias)
- direction accuracy
- Q10-Q90 interval coverage when a model emitted interval bounds
- matured sample count and interval sample count

The ensemble, every persisted individual model, and `persistence` are shown side by side. Persistence is always rendered as a required baseline even when no persisted samples are available, in which case the report flags `no_samples`.

## Segmentation

Every metric is regenerated for:

- all available history
- rolling 7-day, 30-day and 90-day windows by default
- each persisted market regime inside every window and horizon

The rolling windows can be overridden with `--rolling-days`, for example `--rolling-days 14,60,180`.

## Confidence warnings

Segments with fewer than 20 matured observations are marked as low confidence by default. The threshold is configurable with `--low-sample-threshold`. Missing segments are marked `no_samples`.

These warnings are descriptive; they do not alter forecasts or adaptive weights.

## Generate locally

```bash
python performance_dashboard.py \
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

MAE answers how far predictions were from the realized price on average. Signed bias shows systematic over- or under-prediction. Direction accuracy measures whether the model correctly predicted the sign of the move from the origin close. Q10-Q90 coverage measures calibration only for observations where interval bounds were stored.

Always compare the ensemble with persistence before treating a lower MAE as meaningful. Low-sample segments should be considered preliminary until the warning clears.
