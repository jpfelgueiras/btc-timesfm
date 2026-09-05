# Forecast-history retention, backup and recovery

The durable SQLite forecast history is a critical research asset. Production keeps a canonical database plus bounded, verified rollback generations in the private GitHub Release tagged `forecast-history-v1`.

## Release assets

The release contains these stable assets:

- `forecast_history.sqlite.gz` — current canonical database
- `forecast_history.csv.gz` — current analysis export
- `forecast_history.previous.sqlite.gz` — compatibility/quick-rollback alias for the database that was canonical before the latest successful publish

It also contains bounded versioned generations named:

```text
forecast_history.backup-YYYYMMDDTHHMMSSZ-RUN_ID.sqlite.gz
```

A versioned backup is created from the previously restored, verified canonical database before the current canonical asset is replaced.

## Retention policy

Defaults are intentionally conservative and can be changed in `.github/workflows/forecast.yml`:

- retain at most **7 versioned generations**
- reject any compressed backup generation larger than **50 MiB**
- retain at most **250 MiB** across versioned backup generations
- always retain the newest verified versioned generation, even if it alone crosses the total-byte target
- delete oldest generations only after a new generation has been uploaded and independently re-downloaded and verified

The canonical database and CSV are fixed-name assets that are replaced in place, so they do not grow the Release asset count over time. `forecast_history.previous.sqlite.gz` is also a fixed-name alias.

## Production publish sequence

For an existing history Release, the forecast workflow follows this order:

1. download and verify the canonical database
2. preserve that known-good database as `forecast_history.previous.sqlite`
3. run the forecast and update the new canonical database
4. validate both the new canonical database and the previous database
5. gzip the previous database as a versioned generation
6. upload the versioned generation without clobbering an existing generation
7. re-download the exact uploaded generation and verify its SQLite integrity, foreign keys and schema version
8. replace the canonical, CSV and `previous` alias assets
9. compute a retention plan and delete only versioned generations outside the count/byte limits

If any step before canonical replacement fails, the existing canonical Release remains untouched. Old backups are never pruned before the new generation has been verified.

The very first production run has no previous canonical database, so it creates only the canonical assets. Versioned backups start with the next successful publish.

## Local backup verification

```bash
python history_backup.py verify \
  --archive forecast_history.backup-20260905T220000Z-123.sqlite.gz
```

Verification decompresses to a temporary location and validates the contained SQLite database. It does not modify the working history database.

## Restore procedure

Download a selected generation from the private Release:

```bash
gh release download forecast-history-v1 \
  --pattern 'forecast_history.backup-20260905T220000Z-123.sqlite.gz' \
  --output forecast_history.backup.sqlite.gz
```

Restore atomically:

```bash
python history_backup.py restore \
  --archive forecast_history.backup.sqlite.gz \
  --output .state/forecast_history.sqlite
```

Then verify the restored database with the normal history tooling:

```bash
python history_store.py --db .state/forecast_history.sqlite verify
```

`restore` verifies the archive before replacement, restores through a temporary file, validates the restored SQLite database again, and only then atomically replaces the requested destination. A corrupt or incompatible backup cannot silently overwrite an existing destination.

## Manual recovery of the Release canonical asset

Do not overwrite the Release first. Restore and verify locally, keep a local copy of the current Release asset, then upload the verified restored database only as an explicit operator recovery action. Production's normal workflow will create the next versioned generation before future canonical replacements.

## Retention-plan inspection

The workflow obtains the Release asset inventory and feeds it to:

```bash
python history_backup.py retention-plan \
  --assets-json release_assets.json \
  --keep 7 \
  --max-total-bytes 262144000
```

The JSON result has `keep` and `delete` arrays. Only assets matching the versioned backup naming convention are eligible for deletion. Canonical, CSV, and `previous` assets are ignored by the pruning logic.

## Failure behavior

Backup creation fails closed when the source database is invalid or the compressed generation exceeds its configured cap. Backup verification fails on unreadable gzip data, SQLite integrity failures, foreign-key violations, or unsupported schema versions. In all of these cases, production must not remove old generations or replace a known-good canonical database as part of the failed backup sequence.
