# Incremental feature value gate (#346)

**Status: blocked; inventory only.** This bounded gate does not fetch data,
produce forecasts, or make feature-value claims. It uses the existing feature
registry (`feature_registry.py`) for source and source-schema lineage. The
registry does not declare units. Supplied observations count as point-in-time
available only when both per-feature capture time and vintage are present,
timezone-aware, parseable, and no later than the exact origin timestamp.
Unproven observations count as missing.

The pure audit entry point is
`btc_timesfm.research.incremental_feature_value.audit_feature_rows`. Rows have
`origin_at`, a `features` mapping, and per-feature `feature_capture_times` and
`feature_vintages` mappings. Its output records all-origin denominators,
per-feature missingness, and common-available origin indices for this fixed
candidate set:

* volume transform: registered `volume_zscore_7d`;
* OHLC shape/volatility: registered range and volatility summaries;
* calendar: registered UTC hour and weekday encodings;
* external ETH/funding/open-interest: registered fields, included in inventory
  only when supplied with explicit point-in-time proof.

This is not a feature menu and does not compute transforms. Each feature's
source/schema comes from the existing registry; units remain explicitly
unknown until that contract is extended with evidence. The external group is
blocked as an experiment without a historical as-of corpus. The report always
blocks a performance claim and the TimesFM covariate API because the pinned
runtime contract has not established support. If a future experiment requires
modeling, a separately named regularized direct residual challenger needs a
reviewed corpus, walk-forward folds, and exact matched origins. It cannot
silently enter production.

Missing optional input must preserve baseline output exactly; the small helper
`baseline_or_candidate` encodes that fallback contract. A real leave-family
in/out comparison remains blocked until immutable source rows establish units,
capture/publication times, vintage/revision policy, and coverage across the
common cohort. Existing live freshness metadata or post-inference external
signals do not prove historical availability. No value conclusion is drawn.
