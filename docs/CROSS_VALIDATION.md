# Purged walk-forward cross-validation

Issue #27 adds a deterministic cross-validation layer to the research backtest. The existing chronological backtest remains available in `summary`; the new `cross_validation` section replays adaptive weighting inside independent walk-forward folds so model and parameter changes are judged across multiple time windows rather than one aggregate window.

## Leakage protection

Each fold is chronological. Training origins always precede validation origins, and a training origin is eligible only when its longest scored target has matured before the validation boundary. The default purge is therefore 16 hours, matching the longest 16-hour forecast horizon. The code rejects a purge shorter than the longest target instead of silently allowing overlapping labels.

An optional embargo adds an extra time gap after the training label has matured and before validation begins. At each validation origin, adaptive weighting receives only actual prices whose timestamps are no later than that origin. Earlier validation predictions may join the history as the fold advances, but their outcomes are ignored until the corresponding target candle is observable.

## Fold modes

`expanding` mode keeps all eligible history before each validation block. `rolling` mode keeps only the most recent eligible training origins, bounded by `--cv-rolling-train-samples`.

The defaults are:

- 3 folds
- expanding history
- 12 minimum training origins
- 16-hour purge
- 0-hour embargo
- 24 training origins when rolling mode is selected

Example:

```bash
python backtest.py \
  --days 120 \
  --samples 72 \
  --cv-folds 4 \
  --cv-mode rolling \
  --cv-min-train-samples 24 \
  --cv-purge-hours 16 \
  --cv-embargo-hours 2 \
  --cv-rolling-train-samples 32
```

## Report structure

`backtest_report.json` now contains `cross_validation.configuration`, the exact fold definitions, fold-by-fold metrics, aggregate validation metrics, and dispersion across folds. MAE and direction accuracy dispersion include mean, standard deviation, minimum, and maximum values.

The exact training and validation origin indices plus their UTC boundaries are also stored under `experiment_manifest.configuration.run_parameters.cross_validation.fold_definitions`. This makes a research run reproducible and allows later significance tests to compare identical folds.

## Safety invariants

Tests enforce deterministic folds, chronological training, purge/embargo label separation, rolling-window bounds, and rejection of an unsafe purge shorter than the longest forecast horizon. The backtest also calls the leakage assertion before evaluating every fold, so invalid fold definitions fail closed.
