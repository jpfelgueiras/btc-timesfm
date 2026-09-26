# Benchmark inference audit (issue #342)

## Current safeguards and known gaps

| Path | Pairing / selection | Inference and limits |
| --- | --- | --- |
| `research/backtest.py` | Purged chronological folds (16h target maturity); adaptive weights replayed from permitted history. Benchmark winners are selected on the same reported scores. | Descriptive summaries only; not an untouched outer estimate of a selected winner. |
| `research/optimizer.py` | Frozen origin forecasts; each candidate sees matured outcomes only; horizons are averaged within origin; paired by origin. Catalog currently has 19 configurations. | Moving-block bootstrap with a fixed 16-origin block; at the default 48 origins this is 3 effective blocks, below the eight-block evidence floor and therefore inconclusive. Selection and evidence use the same origins; fold summaries do not nest candidate selection. |
| `research/multiple_testing.py` / ops promotion | Counts the challenger family and applies Holm by default; pairing checks exact origin lists when building pairwise evidence. | Uses paired bootstrap on reported arrays. Promotion currently falls back to unadjusted evidence if adjusted evidence cannot be built; report shape does not require prediction completeness or demonstrate that every attempted/failed variant is registered. |
| Ablation and challenger research scripts | Implementations vary; many use paired bootstrap helpers, while others report metrics only. | Shared helper now defaults to moving-block inference. Existing experiment-specific resampling, selection, and trial accounting still require path-by-path validation before evidence is promotable. |

## Interpretation and protocol

Loss differences must remain paired at exact forecast origins; missing predictions
must not be silently dropped differently by candidate and baseline. The four
horizons are dependent outcomes at a common origin and should be clustered within
origin (as the optimizer's aggregate does), not counted as four independent
observations. Forecast labels at 2/4/8/16 hours overlap for dense hourly origins.
Purging at least the longest outcome maturity protects fold boundaries but does
not make adjacent validation losses independent.

The shared paired comparison uses moving-block bootstrap by default (stationary
bootstrap is also available). The block-length proxy is the maximum of 16 origins
and the cube-root rule, capped by the sample count. The 16-origin floor reflects
the longest 16-hour overlapping labels at dense hourly origins; it is a guardrail,
not an estimated dependence length. Explicit lengths must come from training-only
dependence or label-overlap diagnostics, then be frozen before evaluation. Report
sensitivity at longer block sizes and report the effective-block-count proxy
(``n / block_length``), not an independent-sample estimate. Results below 8
effective blocks are inconclusive even when nominal
sample count is large. This floor is a guardrail, not a guarantee of adequate
power; block bootstrap intervals are unstable with few blocks. HAC/Diebold-Mariano
inference is not currently implemented, so no HAC result is claimed.

Selection must be nested: select configurations and tune any policy only within
inner folds of each outer training window, then score the frozen selection once
on the outer holdout. Mutating outer labels must not alter inner choices. The
existing nested splitter enables this protocol, but the optimizer does not yet
use it, so current optimizer comparisons are exploratory and not an unbiased
post-selection estimate. Register the planned candidate budget, all attempted
variants (including failed runs), and exact paired origins before interpreting
family-adjusted evidence.

## Monte Carlo and corpus status

The unit suite runs a fixed-seed, bounded Monte Carlo (240 replications, 399
bootstrap draws, 512 origins per replicate) on 16-wide moving-average loss
differences. It checks empirical two-sided null rejection rate, 95% interval
coverage at a known effect, and one-sided power against tolerances fixed in the
test. The simulated block size is fixed at 16 before generation/evaluation and is
not selected from simulated outer outcomes. This validates only the synthetic
protocol configuration; it does not establish calibration under all dependence
structures. The
canonical benchmark corpus is unavailable/blocked in this checkout, so no BTC
accuracy, historical-drift, coverage, or power claim is made. A benchmark run
without complete, mature point-in-time outcomes should be reported as blocked or
inconclusive rather than filled with invented values.

## Policy parity

Production defaults to the production policy; `policy_for("backtest")` and
`policy_for("optimizer")` enable research ridge while production defaults it off.
The static parity audit is intentionally fail-closed: it reports backtest and
optimizer as ``blocked_policy_drift``, and shadow as ``blocked_unattested`` because
its persisted shadow decision policy does not attest forecast construction
configuration. The audit also identifies backtest's direct weighting override
and optimizer replay's direct adaptive-weighting call, so declared policy IDs do
not establish execution identity. Backtest and optimizer JSON reports include
this audit and explicitly label their research-ridge variant. Historical optimizer, replay,
shadow, and production policy versions have drifted (#260/#275). Historical
manifests are not sufficient to establish exact parity here, so no historical
skill claim is made. Promotion must remain blocked pending explicit parity
evidence and nested outer-holdout evaluation.
