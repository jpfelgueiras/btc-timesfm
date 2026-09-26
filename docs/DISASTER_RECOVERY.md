# Forecast-history disaster recovery

The weekly **Forecast history disaster-recovery drill** downloads the independent S3-compatible copy into Actions scratch storage. It verifies its SHA-256, schema and SQLite integrity, runs the history audit, records row counts/latest origin, and requires a forecast origin no older than 30 days. The drill never uploads, modifies, or deletes a production history asset. Its successful run timestamp is the last successful independent restore; failed restore/validation opens an incident issue.

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

GitHub Actions uses the repository-scoped `GITHUB_TOKEN` to create a failure issue and repository secrets `HISTORY_BACKUP_AWS_ACCESS_KEY_ID` and `HISTORY_BACKUP_AWS_SECRET_ACCESS_KEY` (plus variables `HISTORY_BACKUP_AWS_REGION` and `HISTORY_INDEPENDENT_BACKUP_URI`) to access the private independent bucket. See [HISTORY_BACKUP.md](HISTORY_BACKUP.md) for least-privilege setup and retention. Production history remains the `forecast-history-v1` Release asset `forecast_history.sqlite.gz`; the weekly drill independently restores the configured S3 object into temporary `.drill/` storage.

## Manual independent-copy drill

With AWS CLI credentials configured locally, download and validate the configured object, then run the normal scratch drill:

```bash
PYTHONPATH=src python -m btc_timesfm.history.independent_backup restore \
  --uri s3://<private-bucket>/btc-timesfm/forecast-history/latest.sqlite.gz \
  --output /tmp/forecast-history-independent.sqlite.gz
PYTHONPATH=src python -m btc_timesfm.history.disaster_recovery \
  --archive /tmp/forecast-history-independent.sqlite.gz \
  --report /tmp/forecast-history-drill.json \
  --scratch-dir /tmp
```

Confirm `status` is `passed`, and record `row_counts` and `recent_content.latest_origin_at`. This procedure uses scratch paths only; do not point it at `.state/forecast_history.sqlite`.
