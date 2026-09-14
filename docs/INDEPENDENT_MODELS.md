# Independent model families

Two additional forecasting model families are evaluated for ensemble diversity. Both reuse the engineered, past-only feature set and forecast schema of `ridge_features` (`diversified_model`), so they run through the exact same walk-forward origins and are scored identically. They are pure NumPy (no sklearn/scipy/statsmodels), GitHub-Actions-friendly, and bounded so each forecast completes in well under a second.

## Candidates

- `gbdt_features` — gradient-boosted shallow regression trees fit directly per 2h/4h/8h/16h horizon. Defaults keep compute small: 40 estimators, max tree depth 2, min 8 samples per leaf, 0.05 learning rate. Split search is vectorized over cumulative sums, and extrapolation is bounded by the same volatility-scaled return clip as `ridge_features`.
- `elasticnet_features` — elastic-net (L1 + L2) linear regression fit by deterministic coordinate descent on the standardized engineered features, an alternative regularizer to the ridge path. Defaults: `alpha=0.6`, `l1_ratio=0.6`, up to 200 iterations.

Each candidate returns `{price_usd, predicted_log_return, training_samples}` per horizon, matching `ridge_feature_forecast`, and is deterministic for a given market window.

## Evaluation

`src/btc_timesfm/research/independent_models_evaluation.py` runs all candidates plus `ridge_features` and the statistical baselines through identical expanding-window origins over hourly closes, then reports:

- standalone skill (MAE%, direction accuracy) by horizon, vs `persistence` and vs `ridge_features`
- regime-segmented skill
- pairwise residual-error correlation across candidates and existing models
- measured runtime (mean and total seconds per forecast)

Reports are written as JSON + markdown (`--json`/`--markdown`).

## Production gate

The families are **research-only by default**. They are never added to production or the ensemble by this code path; the report itself records that no candidate is promoted from in-sample gains, and any future promotion requires the existing statistical-evidence and promotion-policy gates before `BTC_ENABLE_INDEPENDENT_MODELS=true` is set.

Other configuration:

- `BTC_GBDT_ESTIMATORS=40`, `BTC_GBDT_MAX_DEPTH=2`, `BTC_GBDT_MIN_SAMPLES_LEAF=8`, `BTC_GBDT_LEARNING_RATE=0.05`
- `BTC_ELASTICNET_ALPHA=0.6`, `BTC_ELASTICNET_L1_RATIO=0.6`, `BTC_ELASTICNET_ITERATIONS=200`
- `BTC_RIDGE_MIN_TRAIN_SAMPLES` (shared history floor)