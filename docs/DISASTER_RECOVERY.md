# Forecast-history disaster recovery

The weekly **Forecast history disaster-recovery drill** restores the newest versioned `forecast_history.backup-*.sqlite.gz` Release asset into Actions scratch storage. It verifies the restored schema and SQLite integrity, runs the read-only history audit, checks row counts, and requires a forecast origin no older than 30 days. The drill never uploads, modifies, or deletes a production history asset.

The workflow shares the `forecast-history-release` concurrency group with the backup lifecycle. This prevents a drill from downloading a generation while production is publishing it. Its JSON report is retained as a 90-day Actions artifact and is included in the performance dashboard when available.

## Failure response

A failed scheduled drill opens an issue linking to this runbook. Preserve the artifact before investigation.

1. Inspect `disaster_recovery_report.json` for the restore, audit, schema, row-count, or recency failure.
2. Download the selected versioned backup Release asset and repeat locally:

   ```bash
   PYTHONPATH=src python -m btc_timesfm.history.disaster_recovery \
     --archive forecast_history.backup-<generation>.sqlite.gz \
     --report disaster_recovery_report.json
   ```

3. If the newest generation is corrupt, test the prior versioned asset. Restore only a verified archive into a new scratch database; do not overwrite `.state/forecast_history.sqlite` in place.
4. If no versioned backup passes, investigate the current and previous Release assets, then rebuild history only through the documented production bootstrap path.

## Credentials and paths

GitHub Actions uses the repository-scoped `GITHUB_TOKEN` to read Release assets and create a failure issue. No recovery secret is required. Production history remains the `forecast-history-v1` Release asset `forecast_history.sqlite.gz`; retained recovery generations are `forecast_history.backup-*.sqlite.gz`. The drill uses only temporary `.drill/` workspace files.
