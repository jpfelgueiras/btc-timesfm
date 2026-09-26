# Forecast-history disaster recovery

The weekly **Forecast history disaster-recovery drill** downloads the independent S3-compatible archive and manifest into Actions scratch storage. It verifies the archive SHA-256 and size, SQLite integrity/schema, and the manifest's row counts/latest origin before running the history audit and 30-day recency check. Only a successful full drill records the last successful restore receipt in S3. The drill never uploads, modifies, or deletes a production history asset.

The 30-minute **Independent forecast-history backup monitor** also downloads and verifies the archive/manifest pair. It reports backup age, bytes, checksum, schema, row counts, and last successful restore; a copy older than two hours fails visibly and opens a deduplicated incident, providing margin before the three-hour RPO. The weekly drill and monitor share the `btc-timesfm-forecast` concurrency group with production publication. Reports are retained as 90-day Actions artifacts.

## Failure response

A failed scheduled drill opens an issue linking to this runbook. Preserve the artifact before investigation.

1. Inspect the workflow report/artifact for the manifest, checksum, restore, audit, schema, row-count, or recency failure. Missing S3 configuration is an explicit failure; configure the repository variable and secrets described below before treating independent recovery as available.
2. For a normal S3 object/manifest failure while GitHub remains available, inspect the open backup incident, verify bucket access/versioning and lifecycle policy with the bucket administrator, then run the scratch procedure below. Never repair the canonical database by uploading an unverified object.
3. If the GitHub repository or Release is unavailable or compromised, recover directly from the separately administered S3 bucket using independently provisioned AWS credentials. Do not rely on a Release asset for this scenario. Download and validate into scratch first:

   ```bash
   PYTHONPATH=src python -m btc_timesfm.history.independent_backup restore \
     --uri s3://<private-bucket>/btc-timesfm/forecast-history/latest.sqlite.gz \
     --output /tmp/forecast-history-independent.sqlite.gz
   PYTHONPATH=src python -m btc_timesfm.history.disaster_recovery \
     --archive /tmp/forecast-history-independent.sqlite.gz \
     --report /tmp/disaster_recovery_report.json \
     --scratch-dir /tmp
   ```

   The independent restore command verifies manifest/archive agreement before atomically placing the compressed archive at the requested scratch path. The drill then restores only into a temporary database and compares the scratch contents with the manifest. Review `status`, `row_counts`, and `recent_content.latest_origin_at` before any operator recovery action.
4. If independent storage is unavailable but GitHub remains trusted, test the newest versioned Release backup, then prior generations, using `history_backup verify` and `disaster_recovery` as appropriate. Restore only a verified archive into new scratch storage; never overwrite `.state/forecast_history.sqlite` in place.
5. After repository/bucket access is recovered, restore production only as an explicit operator action: retain the existing database, verify the chosen scratch restore again, and follow the canonical recovery procedure in [HISTORY_BACKUP.md](HISTORY_BACKUP.md). If no backup passes, rebuild only through the documented production bootstrap path.

## Credentials and paths

GitHub Actions uses the repository-scoped `GITHUB_TOKEN` to create deduplicated failure issues and repository secrets `HISTORY_BACKUP_AWS_ACCESS_KEY_ID` and `HISTORY_BACKUP_AWS_SECRET_ACCESS_KEY` (plus variables `HISTORY_BACKUP_AWS_REGION` and `HISTORY_INDEPENDENT_BACKUP_URI`) to access the private independent bucket. This repository currently has no destination variables/secrets configured, so workflows fail visibly and do not replace canonical history until an operator provisions them. See [HISTORY_BACKUP.md](HISTORY_BACKUP.md) for least-privilege setup and retention. Production history remains the `forecast-history-v1` Release asset `forecast_history.sqlite.gz`; independent archive, manifest, and restore receipt are stored in the configured bucket.

## Manual independent-copy drill

With independently provisioned AWS CLI credentials configured locally, download and validate the configured archive/manifest pair, then run the normal scratch drill:

```bash
PYTHONPATH=src python -m btc_timesfm.history.independent_backup restore \
  --uri s3://<private-bucket>/btc-timesfm/forecast-history/latest.sqlite.gz \
  --output /tmp/forecast-history-independent.sqlite.gz
PYTHONPATH=src python -m btc_timesfm.history.disaster_recovery \
  --archive /tmp/forecast-history-independent.sqlite.gz \
  --report /tmp/forecast-history-drill.json \
  --scratch-dir /tmp
```

Confirm `status` is `passed`, and review `row_counts` and `recent_content.latest_origin_at`. The scheduled workflow records a restore receipt only after this drill succeeds. These commands use scratch paths only; do not point them at `.state/forecast_history.sqlite`.
