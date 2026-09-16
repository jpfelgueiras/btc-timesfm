# Horizon-specific feature ablation

Each enabled family—derivatives, microstructure, cross-asset, and multiresolution—uses the same leakage-safe, paired walk-forward baseline. A row trains a fold only after its target has matured; candidate and baseline are evaluated at identical origins.

Every horizon records `evidence`: effect size, deterministic bootstrap confidence interval, paired sample count, and three chronological fold stability. Its decision is one of `recommend_review`, `do_not_promote_harmful`, or `do_not_promote_inconclusive`. Harmful or inconclusive evidence cannot select a family, and no report changes production forecasts: selection remains recommendation-only.

Each report carries an `experiment_manifest` with the family, horizons, fixed bootstrap policy, fold policy, leakage flag, recommendation-only mode, and a SHA-256 policy digest. The aggregate selection report hashes each component report and includes every enabled family when its artifact is supplied.
