# Documentation truth audit and GitHub Pages information architecture

**Audit baseline:** `issue-356-docs-audit`, 2026-09-26. This is a source-to-doc map and planning record, not a rewrite of the listed guides. Verify mutable facts against the linked source files before publishing new user-facing pages.

## Findings and decisions

There are 54 pre-existing top-level Markdown guides in `docs/`, plus this audit, and 16 dated/versioned research artifacts in `docs/research/`. The root README is 457 lines and repeats substantial material from those guides. `.github/workflows/pages.yml` currently downloads forecast history, runs `btc_timesfm.web.static_site` into `site/`, validates the dashboard contract, and deploys that directory. The Python generator builds the forecast dashboard and its data payload; it does not render Markdown, provide a docs home, or provide documentation navigation. No docs framework or docs configuration is present. The Pages workflow deploys from trusted `main` on pushes, a schedule, and forecast workflow completion.

### Verified discrepancies to correct in the owning authoring issue

| Existing claim/location | Current source of truth | Required correction |
| --- | --- | --- |
| `docs/MARKET_DATA.md`: Binance BTCUSDT is the spot fallback | `src/btc_timesfm/data/market_data_sources.py`, `tests/data/test_market_data_sources.py` | Active spot fallback is Bitstamp BTC/USD; make provider ordering and fallback semantics follow code/tests. |
| `docs/HISTORY_SCHEMA.md`: history schema v4 | `src/btc_timesfm/history/history_migrations.py` (`CURRENT_SCHEMA_VERSION = 6`), migration/store tests | Current production history schema is v6. Distinguish this from unrelated API, static-site, and research artifact schema versions. |
| `docs/FORECAST_API_CONTRACT.md`: contract does not create an HTTP server | `src/btc_timesfm/api/forecast_service.py`, `tests/api/` | The contract module defines/validates the payload; a separate authenticated WSGI service is implemented. Explain contract vs serving runtime. |
| `docs/ROADMAP.md`: completed items #322–325 remain queued | GitHub issues #322–325 (closed), issue #326, current code | Replace stale queue with a maintained pointer to GitHub roadmap/issues, or archive the snapshot; do not present closed work as open. |
| `README.md`: schema v1 and GitHub Release as sole/durable storage; says no extra storage secret | `src/btc_timesfm/history/`, `.github/workflows/forecast.yml`, `.github/workflows/independent-history-backup-monitor.yml`, `docs/HISTORY_BACKUP.md` | README's schema description is stale; Release is not the only backup failure domain now that independent S3 backup exists and is configured through workflow secrets. State distinct operational roles without publishing secret names/values. |
| `docs/HISTORY_BACKUP.md`: GitHub Release is private | GitHub repository visibility (public), `.github/workflows/pages.yml`, `src/btc_timesfm/history/history_backup.py` | Correct visibility statement: repository Releases are public here; do not call these assets private. |
| `docs/FORECAST_WORKS.md` simplifies forecast behavior | `src/btc_timesfm/forecasting/`, `src/btc_timesfm/data/`, `tests/integration/test_vertical_forecast_path.py` | Reconcile the explanation with the actual active pipeline, current source/data validation, feature gates, ensemble and calibration. Link specialized detail instead of duplicating internals. |
| README links `CONFORMAL_CALIBRATION.md` at repository root | Actual file is `docs/CONFORMAL_CALIBRATION.md` | Fix relative link or point to the stable Pages URL. |

Other facts requiring careful wording: the production forecast workflow and the Pages workflow are distinct; static dashboard publication is not the authenticated WSGI API; forecast-history SQLite is runtime state, not a checked-in package asset; GitHub Release history and independent S3 backup have different roles; weekly/automated optimization and challenger research are recommendation/shadow-only and must not be implied to promote production automatically. A public dashboard does not imply that private API credentials or endpoints are published.

## Proposed information architecture and URL plan

Canonical project docs live under the existing project Pages origin `https://jpfelgueiras.github.io/btc-timesfm/`. The Docs landing page is the Pages root `/`, built from `docs/index.md`. Move the existing forecast dashboard to `/forecasts/`, with its `data.json`, scripts, styles, and other assets under that same route. Preserve the dashboard's function and output contract at this documented stable subpath; adapt its contract/accessibility checks for the nested base path. Link to `/forecasts/` from the Docs landing page and navigation. Do not replace the dashboard implementation with the documentation theme. Human-readable docs paths use lowercase kebab-case page names. Link within docs using relative links that work under the GitHub project Pages base path. Redirect old URLs only if an implementation introduces aliases and tests them.

Proposed nav (destination sections; v9 content is authored by the linked follow-up issues, not this audit):

| Docs path | Audience / destination scope | Authoring issue |
| --- | --- | --- |
| `/` (`docs/index.md`) | First-time users: project purpose, links to dashboard, setup, forecast explanation, and limitations | #358, #359 |
| `/getting-started/` | Python support, install/model dependencies, verified local quick start and commands | #358 |
| `/forecasts/` (Docs section) | Dashboard link plus output fields, uncertainty, forecast lifecycle, TimesFM contract and limitations; the interactive dashboard itself is `/forecasts/` on the site | #359, #360 |
| `/data/` | Market providers, validation, timestamps, preprocessing and active feature groups | #361 |
| `/evaluation/` | Baselines, walk-forward methods, metrics, evidence and research interpretation | #362 |
| `/api/` | Versioned payload contract, WSGI service, auth/runtime boundaries, examples | #363 |
| `/operations/` | Workflows, configuration/commands, persisted state/schema, backups, deployment and troubleshooting | #364, #366 |
| `/development/` | Contribution, tests, safe forecasting changes, CI and documentation maintenance | #365 |
| `/research/` | Explicitly non-production research archive/index; links to retained evidence with status/date | Maintained as a secondary index, not a default user-facing promise |

The exact URL rendering depends on the generator selected in implementation issue #357. The planned canonical paths above are the contract; generator-specific file extensions must not leak into navigation or make route changes silently. Each guide should have one canonical home and a short summary/redirect/link in legacy locations during migration. Keep repo-level contribution/security/community policies accessible from the root Docs home/navigation, with the repository files authoritative.

## Generator and Pages architecture decision

Decision: use a static Markdown documentation generator for the docs tree, selected and integrated in #357; MkDocs Material is the candidate named there, while the issue allows a simpler fit if the audit supports it. Retain the tested custom Python generator as the forecast-dashboard producer. Prefer an established generator with project-page base URL support, accessible responsive navigation, local preview, stable clean URLs, and no server-side runtime. The current Python generator remains authoritative for dashboard HTML/data and its output contract. Compose both outputs into one Pages artifact: generated Docs landing/navigation at `site/index.html`, and the dashboard HTML, `data.json`, scripts, styles and assets under `site/forecasts/`. Keep dashboard output contract validation (updated for `/forecasts/`), docs output/link validation, `.nojekyll`, deploy permissions, and dashboard regression/accessibility tests. Do not reimplement the dashboard in the documentation theme or publish raw repository Markdown as the IA. Generator dependency, lock/update policy, build command, link checking and rendered-output tests belong in #357/#367.

## Source ownership and terminology

When a conflict exists, follow this source precedence: executable implementation and tests for behavior; workflow YAML and `pyproject.toml`/`uv.lock` for operations and supported dependencies; API contract and contract tests for wire format; migration code/tests for persisted schema; docs explain those facts but do not redefine them. GitHub workflow schedules/permissions and repository visibility are checked in the live repository because they can change independently of source docs. Tests establish intended behavior, not necessarily successful external service availability.

| Subject | Primary owners (source) | Corroborating tests / wording boundary |
| --- | --- | --- |
| Runtime, installation, CLI | `pyproject.toml`, `uv.lock`, `src/btc_timesfm/cli/`, `src/btc_timesfm/config.py` | `tests/cli/`; model extra is optional, Python range is >=3.11,<3.14. |
| Forecast pipeline, TimesFM, ensembles, uncertainty | `src/btc_timesfm/forecasting/`, `src/btc_timesfm/forecast.py` | `tests/forecasting/`, `tests/integration/`; distinguish deployed path from optional/research experiments. |
| Market inputs/features | `src/btc_timesfm/data/`, forecasting feature registry | `tests/data/`, registry tests; name only active features as production inputs. |
| API | `src/btc_timesfm/api/`, API contract module | `tests/api/`; payload contract and WSGI server are distinct concepts. |
| History/schema/backup/recovery | `src/btc_timesfm/history/`, history workflows | `tests/history/`, workflow tests; SQLite schema v6 is distinct from other schema versions. |
| Dashboard and Pages | `src/btc_timesfm/web/static_site.py`, `contract_validation.py`, `.github/workflows/pages.yml` | `tests/web/`, browser/accessibility tests; static public dashboard is not API. |
| Operations/security/CI | `.github/workflows/`, `.github/actions/`, scripts, `SECURITY.md` | `tests/ops/`, workflow/action lint scripts; never document secret values. |
| Research/evaluation/promotion policy | `src/btc_timesfm/research/`, `src/btc_timesfm/ops/promotion_policy.py` | `tests/research/`, promotion tests; recommendation-only, shadow-only, or candidate status must be explicit. |

Use “production/active” only for behavior on the scheduled forecast/publication path. “Research” means evaluation or analysis that does not implicitly change production behavior. “Shadow” means evaluated without affecting public forecasts. “Recommendation-only” means human-reviewed output, not automatic promotion. “Forecast history” is persisted production predictions and matured outcomes; “dashboard” is the static Pages presentation; “API” means the separately implemented WSGI service/contract. Always qualify schema names by owner.

## Audit ledger: root and community documents

| File | Audience | Disposition / mismatch | Destination |
| --- | --- | --- | --- |
| `README.md` | Repository visitors | **Consolidate/update** to brief project overview, dashboard/docs links, trustworthy summary; remove repeated implementation guide and stale v1 schema/storage claims; repair conformal link. | Root landing page; detailed material to forecasts, data, evaluation, operations. |
| `CONTRIBUTING.md` | Contributors | **Preserve/update** commands against current locked dependencies and CI. Avoid competing with AGENTS. | Development / contributing. |
| `AGENTS.md` | Coding agents and maintainers | **Preserve** as repository-local agent instructions, not end-user product documentation. | Link from Development; keep root file canonical. |
| `SECURITY.md` | Vulnerability reporters and users | **Preserve** policy; link to canonical root file; do not duplicate reporting address/process. | Security link/footer and Operations. |
| `CODE_OF_CONDUCT.md` | Community | **Preserve** policy; link to canonical root file. | Community link/footer. |
| `docs/DOCUMENTATION_AUDIT.md` | Maintainers / v9 authors | **Preserve** as audit decision record; update only when scope/decision changes. | Development / documentation maintenance. |

## Audit ledger: existing `docs/*.md` guides

All are preserved in version control. “Consolidate” means make the named destination the canonical user-facing explanation and retain a short link or mark the source note historical; it does not authorize deleting evidence. Cross-check every assertion during the authoring issue.

| Existing file | Audience | Disposition and verified/likely audit focus | Proposed destination section |
| --- | --- | --- | --- |
| `ABSTENTION_POLICY.md` | Researchers | Consolidate; active policy vs evaluation scope. | Evaluation / uncertainty |
| `BENCHMARKS.md` | Contributors/researchers | Update; benchmark method, frozen data and gate status from code/tests. | Evaluation / benchmarks |
| `CHAMPION_CHALLENGER.md` | Researchers/maintainers | Consolidate; distinguish shadow evaluation from promotion. | Evaluation / research |
| `CI_WORKFLOWS.md` | Contributors | Update against actual workflow inventory. | Development / CI |
| `CONDITIONAL_CALIBRATION.md` | Researchers | Preserve as detail; mark active use only if production path confirms it. | Evaluation / uncertainty |
| `CONFORMAL_CALIBRATION.md` | Users/researchers | Preserve/link; correct README target and active-vs-research framing. | Forecasts / uncertainty; Evaluation |
| `CORRELATION_WEIGHTING.md` | Researchers | Consolidate; distinguish production weighting from experiment. | Evaluation / ensemble research |
| `CROSS_ASSET_SIGNALS.md` | Users/contributors | Update provider, freshness, quarantine and active feature status. | Data / features |
| `CROSS_VALIDATION.md` | Researchers | Preserve as method reference; verify terminology against implementation. | Evaluation / methodology |
| `DASHBOARD_UX_BASELINE.md` | Maintainers | Archive as implementation baseline; dashboard UX tests remain authority. | Research archive / UI history |
| `DERIVATIVES_SIGNALS.md` | Users/contributors | Update availability, source health, freshness and feature activation. | Data / features |
| `DIRECTION_PROBABILITY.md` | Users/researchers | Consolidate; explain published field and sample/neutral gates only as implemented. | Forecasts / output fields |
| `DISASTER_RECOVERY.md` | Operators | Update from recovery scripts and workflows; clarify recovery boundaries. | Operations / recovery |
| `DIVERSIFIED_MODEL.md` | Researchers | Keep as research record unless proven on scheduled production path. | Research archive / models |
| `DRIFT_DETECTION.md` | Operators/researchers | Consolidate; distinguish monitoring signal from forecast adaptation. | Operations / monitoring |
| `DYNAMIC_THRESHOLDS.md` | Users/researchers | Verify active output behavior against policy and tests. | Forecasts / interpretation |
| `ECONOMIC_VALUE.md` | Researchers | Keep as evaluation methodology, not profit claim. | Evaluation / metrics |
| `EDGE_ATTRIBUTION_REPORT.md` | Researchers | Preserve report workflow as research/evidence. | Evaluation / diagnostics |
| `EXPERIMENTS.md` | Researchers | Consolidate as catalog/index; status/date every experiment. | Evaluation / research |
| `EXPERIMENT_REGISTRY.md` | Contributors/researchers | Update against registry implementation and persisted artifact schema. | Evaluation / reproducibility |
| `FEATURE_ABLATION.md` | Researchers | Preserve methodology; no production claim from ablation alone. | Evaluation / features |
| `FEATURE_FRESHNESS.md` | Operators/contributors | Align with active source health/freshness gates. | Data / validation; Operations |
| `FEATURE_INTERACTIONS.md` | Researchers | Research-only unless explicitly promoted in runtime. | Research archive / features |
| `FEATURE_REGISTRY.md` | Contributors | Align names/versions with registry code and manifest. | Data / feature lineage |
| `FLAKY_TESTS.md` | Contributors | Update with current retry/quarantine CI behavior. | Development / testing |
| `FORECAST_API_CONTRACT.md` | API consumers/contributors | Correct contract-vs-server claim; align payload to contract tests. | API / contract |
| `FORECAST_API_SERVICE.md` | Operators/API consumers | Align with actual authenticated WSGI entry point, auth and deployment status. | API / service |
| `FORECAST_CONFIDENCE.md` | Users | Consolidate with published uncertainty fields and limitations. | Forecasts / uncertainty |
| `FORECAST_WORKS.md` | Users | Update and simplify; verify full production lifecycle rather than stale simplified pipeline. | Forecasts / lifecycle |
| `FRESHNESS_SLO.md` | Operators | Verify thresholds/reporting against `service_slo.json` and workflow/runtime. | Operations / reliability |
| `HISTORY_AUDIT.md` | Operators | Align command and audit schema with current implementation. | Operations / history |
| `HISTORY_BACKUP.md` | Operators | Correct public Release visibility; document Release plus independent S3 roles from workflow. | Operations / storage and backup |
| `HISTORY_SCHEMA.md` | Developers/operators | Correct production history schema to v6; migration ownership and table names from code/tests. | Operations / persisted data |
| `INDEPENDENT_MODELS.md` | Researchers | Research-only; do not imply another model runs in scheduled ensemble. | Research archive / models |
| `MARKET_DATA.md` | Users/contributors | Correct Binance claim to Bitstamp fallback; verify primary, fallback, candle/timestamp semantics in code. | Data / sources |
| `MICROSTRUCTURE_SIGNALS.md` | Users/contributors | Validate provider availability, freshness and active feature registration. | Data / features |
| `MULTIPLE_TESTING.md` | Researchers | Preserve method; state scope and selection safeguards accurately. | Evaluation / statistical evidence |
| `MULTI_RESOLUTION.md` | Researchers | Research status unless enabled on production forecast path. | Research archive / signals |
| `OBSERVABILITY.md` | Operators | Update from emitted metrics/events and workflows. | Operations / monitoring |
| `PERFORMANCE_DASHBOARD.md` | Researchers/operators | Clarify research performance report vs public forecast dashboard. | Evaluation / reporting |
| `PIPELINE_HEALTH.md` | Operators | Align health checks and fallback semantics to source. | Operations / reliability |
| `PROMOTION_POLICY.md` | Maintainers | Preserve safeguards; research recommendations do not auto-promote. | Operations / governance |
| `RECENCY_ADAPTATION.md` | Researchers | Mark research/adaptation boundary, verify production wiring. | Evaluation / research |
| `REGIME_DETECTION.md` | Users/contributors | Align active regime fields/features and fallback behavior with data code. | Data / features; Forecasts |
| `REGIME_SPECIALISTS.md` | Researchers | Research-only unless production wiring is evidenced. | Research archive / models |
| `ROADMAP.md` | Repository visitors | Archive snapshot or replace with canonical GitHub roadmap; closed #322–325 cannot remain queued. | Root roadmap link; archived history if retained |
| `SERVICE_SLO.md` | Operators | Reconcile with `service_slo.json`, runtime monitors and workflow cadence. | Operations / reliability |
| `SHADOW_DEPLOYMENT.md` | Maintainers/researchers | Clarify isolated shadow path and no public forecast impact. | Evaluation / research; Operations |
| `STACKED_ENSEMBLE.md` | Researchers | Research-only unless deployed path demonstrates otherwise. | Research archive / models |
| `STATISTICAL_EVIDENCE.md` | Researchers | Preserve inference guidance; avoid overstating significance. | Evaluation / evidence |
| `UNCERTAINTY_WEIGHTING.md` | Researchers | Verify whether production path consumes it; otherwise research-only. | Evaluation / uncertainty |
| `WORKFLOW_GUARDIAN.md` | Operators | Update from guardian workflow and code; describe supported workflow scope. | Operations / monitoring |
| `X_POSTING.md` | Operators/contributors | Verify optional automation, schedules and secrets only from workflow/code. | Operations / integrations |
| `benchmark-inference-audit.md` | Researchers | Preserve as dated audit evidence; not a user-facing benchmark guarantee. | Research archive / benchmark evidence |

## Audit ledger: `docs/research/*.md` artifacts

These 16 files are retained as research provenance, not promoted as active product documentation. Keep a dated index with explicit status and issue link where available; archive completed status snapshots as historical evidence, and update a snapshot only when its owning research issue requires it. No report by itself establishes production activation.

| Existing file | Audience / disposition | Destination |
| --- | --- | --- |
| `research/COHERENCE_ABLATION_V5_STATUS.md` | Historical v5 research status; retain, label snapshot. | Research archive / historical status |
| `research/CONTEXT_COMPARISON_ISSUE_344.md` | Issue #344 evidence; retain with outcome/status. | Research archive / TimesFM context |
| `research/CONTEXT_CONFIGURATION_V5.md` | Historical configuration note; retain. | Research archive / TimesFM context |
| `research/CUMULATIVE_INTERVAL_ISSUE_349.md` | Issue #349 evidence; retain with outcome/status. | Research archive / calibration |
| `research/CUMULATIVE_INTERVAL_V5_STATUS.md` | Historical status snapshot; retain. | Research archive / calibration |
| `research/EXTERNAL_SIGNALS_V5_STATUS.md` | Historical external-signal status; retain. | Research archive / signals |
| `research/FULL_POLICY_SHADOW_V5_STATUS.md` | Shadow experiment snapshot; retain; explicitly non-production. | Research archive / shadow |
| `research/INCREMENTAL_FEATURE_VALUE_ISSUE_346.md` | Issue #346 evidence; retain with outcome/status. | Research archive / feature evidence |
| `research/ISSUANCE_UTILITY_V5_STATUS.md` | Historical issuance research; retain. | Research archive / status snapshots |
| `research/ISSUED_DIRECTION_V5_STATUS.md` | Historical direction research; retain. | Research archive / status snapshots |
| `research/MULTITIMEFRAME_V5_STATUS.md` | Historical multi-timeframe status; retain. | Research archive / TimesFM contexts |
| `research/NATIVE_COVARIATES_V5.md` | Historical covariate research; retain. | Research archive / TimesFM inputs |
| `research/PERSISTENCE_FIRST_ISSUE_345.md` | Issue #345 evidence; retain with outcome/status. | Research archive / baselines |
| `research/PERSISTENCE_SHRINKAGE_V5_STATUS.md` | Historical shrinkage status; retain. | Research archive / status snapshots |
| `research/REGIME_RECENCY_V5_STATUS.md` | Historical regime/recency snapshot; retain. | Research archive / status snapshots |
| `research/TARGET_COMPARISON_V5_STATUS.md` | Historical target comparison snapshot; retain. | Research archive / status snapshots |

## Prior documentation and Pages work (avoid redoing)

Repository history and GitHub issue/PR search checked 2026-09-26. Earlier user-facing forecast explanation is #107 / merged PR #107; repo docs/tests organization is #105 / PR #105. Dashboard publication is #140 / PR #140; interactive explorer #128/#162; Pages dependency repair PR #209; dashboard UX/accessibility and output contracts include issues #166/#171/#295/#301/#302 and merged PRs #308/#315/#317. Root repository agent guidance is PR #321; community standards are PR #319. These establish existing dashboard behavior, workflow constraints, community policy, and prior explanations; preserve them. Historical forecasting/operational PRs are indexed in repo history and are not a reason to recreate their implementation. Current docs-specific v9 successors: #357 (portal architecture/build), #358–#366 (content areas), #367 (canonical source/docs quality), #368 (Pages docs workflow). #356 supplies the audit/source map and does not duplicate those deliverables. Roadmap state should point to GitHub rather than copying an issue queue.

## Validation and maintenance

Before merging subsequent authoring/build work, check every linked source and guide path; run the dedicated workflow/action-pin lint scripts when workflows change; validate rendered docs links, accessibility, base-path routing and unchanged dashboard output; and verify the API and schema wording against their contract/migration tests. A docs change should report which source changed and which docs were reviewed. Re-audit mutable provider, schedule, schema, API and storage statements when their source owner changes. Never infer successful live market-data delivery from unit tests alone.
