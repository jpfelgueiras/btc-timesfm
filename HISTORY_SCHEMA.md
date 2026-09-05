# Forecast-history schema compatibility

The durable forecast-history database is a versioned SQLite asset. The application treats `PRAGMA user_version` as the authoritative schema version and mirrors that value in `metadata.schema_version` for human-readable diagnostics.

## Migration policy

- Schema changes are added as ordered migrations in `history_migrations.py`.
- Migration numbers are contiguous and never reused.
- Opening the database automatically upgrades every supported older version to the current schema.
- Migrations are idempotent: reopening an already-current database does not reapply changes.
- Applied migrations are recorded in `schema_migrations` from schema version 2 onward.
- A database with a schema version newer than the running code is rejected without modification. Forward compatibility is therefore fail-safe rather than best-effort.
- Downgrades are not performed in place. To run older code, restore the previous release asset or another backup created by that older release.

## Recovery guarantees

Before changing any existing database, the migration runner creates a byte-for-byte backup named:

`<database>.pre-migration-v<source-version>.bak`

Migrations execute in a SQLite transaction. If a migration or post-migration validation fails, the transaction is rolled back and the original database bytes are restored from that backup before the error is re-raised. A failed clean-database migration removes the partially created database.

The production workflow also keeps `forecast_history.previous.sqlite.gz`, copied from the restored release asset before migration. The durable release is only replaced after the migrated database passes integrity, foreign-key, schema-version, metadata-version and migration-audit validation.

## Release compatibility

The `forecast-history-v1` GitHub Release tag identifies the durable datastore product, not the internal SQLite schema version. Its `forecast_history.sqlite.gz` asset may therefore contain an older supported schema. Production restores that asset, migrates it automatically, validates it, runs the forecast, and only then publishes the upgraded asset.

Current code supports every schema version represented by the migration registry in `history_migrations.py`. Removing an old migration is a breaking storage change and should only happen together with an explicit data-retention decision and release migration plan.

## Diagnostics

Use:

```bash
python history_store.py --db .state/forecast_history.sqlite verify
python history_store.py --db .state/forecast_history.sqlite stats
```

Both commands expose the current schema version. Migration-specific diagnostics, including the ordered audit trail, are available through `history_migrations.schema_diagnostics()` and are validated by `history_migrations.validate_database()`.
