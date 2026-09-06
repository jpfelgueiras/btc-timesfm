from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if old not in text:
        raise SystemExit(f"expected text not found in {path}: {old[:120]!r}")
    file.write_text(text.replace(old, new, 1), encoding="utf-8")


replace(
    "history_backup.py",
    "from history_migrations import validate_database\n",
    "from history_migrations import migrate_database, validate_database\n",
)

replace(
    "history_backup.py",
    '''    verification = validate_database(source)\n    if not _verification_ok(verification):\n        raise RuntimeError(f"source database failed verification: {verification}")\n\n    output.parent.mkdir(parents=True, exist_ok=True)\n    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")\n    try:\n        with (\n            source.open("rb") as source_handle,\n            gzip.open(temporary, "wb", compresslevel=9) as target,\n        ):\n            shutil.copyfileobj(source_handle, target, length=1024 * 1024)\n        archive_bytes = temporary.stat().st_size\n        if archive_bytes > max_generation_bytes:\n            raise RuntimeError(\n                f"compressed backup is {archive_bytes} bytes, exceeding "\n                f"the {max_generation_bytes}-byte generation limit"\n            )\n        os.replace(temporary, output)\n    finally:\n        temporary.unlink(missing_ok=True)\n''',
    '''    output.parent.mkdir(parents=True, exist_ok=True)\n    temporary = output.with_name(f".{output.name}.{os.getpid()}.tmp")\n    try:\n        # A previous-known-good database can legitimately be one schema version\n        # behind immediately after a new migration ships. Prepare the backup from\n        # a temporary copy so it is upgraded to the current schema without\n        # mutating the rollback source itself.\n        with tempfile.TemporaryDirectory(prefix="forecast-history-backup-") as directory:\n            prepared = Path(directory) / "forecast_history.sqlite"\n            shutil.copy2(source, prepared)\n            migrate_database(prepared)\n            verification = validate_database(prepared)\n            if not _verification_ok(verification):\n                raise RuntimeError(f"source database failed verification: {verification}")\n\n            with (\n                prepared.open("rb") as source_handle,\n                gzip.open(temporary, "wb", compresslevel=9) as target,\n            ):\n                shutil.copyfileobj(source_handle, target, length=1024 * 1024)\n\n        archive_bytes = temporary.stat().st_size\n        if archive_bytes > max_generation_bytes:\n            raise RuntimeError(\n                f"compressed backup is {archive_bytes} bytes, exceeding "\n                f"the {max_generation_bytes}-byte generation limit"\n            )\n        os.replace(temporary, output)\n    finally:\n        temporary.unlink(missing_ok=True)\n''',
)

replace(
    "test_history_backup.py",
    "from history_migrations import validate_database\n",
    "from history_migrations import CURRENT_SCHEMA_VERSION, migrate_database, validate_database\n",
)

replace(
    "test_history_backup.py",
    '''    def test_restore_archive_recovers_valid_database(self) -> None:\n''',
    '''    def test_create_archive_upgrades_older_schema_without_modifying_source(self) -> None:\n        with tempfile.TemporaryDirectory() as directory:\n            root = Path(directory)\n            db = root / "legacy.sqlite"\n            migrate_database(db, target_version=3)\n            self.assertEqual(validate_database(db, expected_version=3)["schema_version"], 3)\n\n            archive = root / "legacy-backup.sqlite.gz"\n            created = create_archive(db, archive)\n            verified = verify_archive(archive)\n\n            # The rollback source remains byte-compatible with its original schema,\n            # while the archive is immediately restorable by the current code.\n            self.assertEqual(validate_database(db, expected_version=3)["schema_version"], 3)\n            self.assertEqual(\n                created["database_verification"]["schema_version"], CURRENT_SCHEMA_VERSION\n            )\n            self.assertEqual(\n                verified["database_verification"]["schema_version"], CURRENT_SCHEMA_VERSION\n            )\n\n    def test_restore_archive_recovers_valid_database(self) -> None:\n''',
)

Path("_apply_history_backup_fix.py").unlink(missing_ok=True)
Path(".github/workflows/_apply_history_backup_fix.yml").unlink(missing_ok=True)
