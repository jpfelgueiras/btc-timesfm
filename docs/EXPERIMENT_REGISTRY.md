# Experiment registry and longitudinal leaderboard

Every automated research run can register a forecasting experiment in a durable
SQLite registry. The registry separates research candidates from the production
champion, keeps every decision auditable, bounds storage by deduplicating on
`run_id`, and renders JSON/markdown leaderboards that can be filtered by
horizon, regime, and decision status.

## Registry

`src/btc_timesfm/research/experiment_registry.py` provides
`ExperimentRegistry`, a SQLite-backed store following the durable-storage
conventions of `btc_timesfm.history.history_store`:

- schema versioning through `PRAGMA user_version`, tracked in `metadata`
- a `schema_migrations` audit table and byte-for-byte rollback backups on upgrade
- append-only records keyed by `run_id` (primary key)

The `experiments` table stores the manifest identity and leaderboard fields:

- `run_id` (primary key), `run_type`, `created_at`, `configuration_id`, `data_id`
- `hypothesis`, `feature_set_version`, `model_set` (JSON list)
- `evaluation_window` (start/end or folds JSON)
- `metrics` (JSON, keyed by horizon via `by_horizon` and by regime via
  `by_regime`), `statistical_evidence` (JSON)
- `decision` (`champion` | `candidate` | `inconclusive` | `rejected`)
- `parent_run_id`, `git_sha`, `report_path`

Registering the same `run_id` twice is idempotent: the first write wins and a
retried run reports `created=False` without growing the database. Every
`update_decision` call appends a row to `experiment_decisions`, so historical
decisions remain reproducible.

## Registering an experiment

Automated research runs can register directly from a reproducibility manifest
produced by `btc_timesfm.forecasting.experiment_manifest.build_experiment_manifest`:

```python
from btc_timesfm.research.experiment_registry import ExperimentRegistry

registry = ExperimentRegistry(".state/experiment_registry.sqlite")
record, created = registry.register_from_manifest(
    manifest,  # dict from build_experiment_manifest(...)
    metrics=optimizer_candidate,  # optimizer-style candidate metrics dict
    decision="candidate",
)
```

`metrics` should use the optimizer report shape so the leaderboard can filter by
horizon and regime:

```python
{
    "samples": 48,
    "objective_mae_pct": 1.1,
    "mean_direction_accuracy": 0.65,
    "by_horizon": {"2h": {"samples": 48, "mae_pct": 0.6, ...}, ...},
    "by_regime": {"range": {"2h": {...}, ...}, ...},
}
```

The `production` champion is registered with `decision="champion"`, research
alternatives with `decision="candidate"`. A candidate that passes the promotion
policy review is upgraded via `update_decision(run_id, "champion")`.

## Leaderboard

`build_leaderboard(db, horizon=None, regime=None, status=None)` returns a JSON
payload with three views:

- `champion`: the production champion — the most recent `champion` decision,
  flagged `is_champion: true` and `role: "champion"`
- `candidates`: research candidates sorted by the selected metric's `mae_pct`
  (ascending)
- `entries`: the full ordered list used for rendering and audit hashing

Filters:

- `horizon="2h"` scores every entry from `metrics["by_horizon"]["2h"]`
- `regime="range"` scores every entry from the mean of
  `metrics["by_regime"]["range"]` across horizons
- `status="candidate"` / `"champion"` restricts the decision

Each leaderboard carries a reproducible `audit.sha256` over
`schema_version + filters + entries`, so regenerating the leaderboard from the
same registry data yields the same hash. `render_json(...)` writes the raw
payload and `render_markdown(...)` renders a champion-vs-candidate table;
`generated_at` is excluded from the audit hash.

## CLI

```bash
# register an automated run from its manifest and optimizer metrics
python -m btc_timesfm.research.experiment_registry register \
  --manifest experiment_manifest.json --metrics optimizer_candidate.json \
  --decision candidate --db .state/experiment_registry.sqlite

# render a filtered leaderboard
python -m btc_timesfm.research.experiment_registry leaderboard \
  --horizon 2h --regime range --status candidate \
  --out experiment_leaderboard.json --md experiment_leaderboard.md

python -m btc_timesfm.research.experiment_registry stats
python -m btc_timesfm.research.experiment_registry verify
```

## Bounded storage and Actions compatibility

The registry is a single SQLite file (`.state/experiment_registry.sqlite` by
default). Storage is bounded because `run_id` is a primary key: duplicate and
retried runs never add rows, and `experiment_decisions` only grows by one row
per decision change per experiment. Like the production forecast history, the
registry database is intended to be kept as a compressed GitHub Release asset,
which keeps it Actions-compatible.