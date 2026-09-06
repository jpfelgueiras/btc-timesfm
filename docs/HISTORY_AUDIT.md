# Forecast history integrity audit

`history_audit.py` validates the durable SQLite forecast-history database without changing it by default.

## What it checks

The audit reports:

- SQLite `integrity_check` and foreign-key violations
- schema/tables/required columns
- duplicate logical origins and prediction keys
- prediction rows whose origin no longer exists
- underlying model rows without a matching ensemble row
- invalid required timestamps, prices and market-feature JSON
- target timestamps that do not equal `origin + horizon`
- inconsistent `predicted_change_pct`
- matured rows with missing or inconsistent derived outcome metrics
- targets older than the maturity grace period that still have no actual outcome

Warnings for missing matured outcomes do not fail the automated workflow by default. Structural/data-integrity errors do.

## Dry-run audit

```bash
PYTHONPATH=src python -m btc_timesfm.history.history_audit \
  --db .state/forecast_history.sqlite \
  --report .state/history_audit.json
```

The command prints the same JSON report to stdout. Use `--fail-on warning` to make warnings fail the command, or `--fail-on never` for reporting-only use.

## Safe repair mode

Repair mode only changes deterministic derived fields. It never deletes duplicates, orphan rows or otherwise ambiguous historical records.

```bash
PYTHONPATH=src python -m btc_timesfm.history.history_audit \
  --db .state/forecast_history.sqlite \
  --repair \
  --report .state/history_audit.json
```

Before the first write, the tool creates a byte-for-byte backup next to the database:

```text
forecast_history.sqlite.pre-repair-YYYYMMDDTHHMMSSZ.bak
```

Safe repairs include:

- recomputing `target_at` from immutable `origin_at + horizon_hours`
- recomputing `predicted_change_pct`
- recomputing derived outcome metrics when the actual target price is already stored

Repairs are idempotent: running repair again on the same healthy database applies zero actions.

## Repair missing matured outcomes

The audit deliberately does not invent or approximate missing actual prices. Provide an explicit exact-target price map when repairing those rows:

```json
{
  "2026-09-05T12:00:00+00:00": 111234.56,
  "1788609600": 111500.00
}
```

Then run:

```bash
PYTHONPATH=src python -m btc_timesfm.history.history_audit \
  --db .state/forecast_history.sqlite \
  --actuals exact_actuals.json \
  --repair \
  --report .state/history_audit.json
```

A list form is also accepted:

```json
[
  {
    "target_at": "2026-09-05T12:00:00+00:00",
    "actual_target_price_usd": 111234.56
  }
]
```

Only exact target timestamps are used.

## Machine-readable report

The JSON report contains:

- `healthy`
- severity counts in `summary`
- actionable entries in `issues`
- proposed and applied repair actions
- the repair backup path, when a write occurs
- SQLite/schema diagnostics

Repair is blocked when structural or required-field errors make automatic mutation unsafe.

## Automated audit

`.github/workflows/history-audit.yml` restores the machine-managed `forecast-history-v1` release asset and runs a dry-run audit every day at **05:17 UTC**. It also supports manual dispatch.

The workflow fails on integrity errors, adds the full JSON report to the job summary and uploads it as a 30-day artifact. Missing matured outcomes remain visible as warnings without blocking the workflow.
