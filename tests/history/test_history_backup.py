import gzip
import json
import tempfile
import unittest
from pathlib import Path

from btc_timesfm.history.history_backup import (
    backup_asset_name,
    create_archive,
    restore_archive,
    retention_plan,
    verify_archive,
)
from btc_timesfm.history.history_migrations import (
    CURRENT_SCHEMA_VERSION,
    MIGRATIONS,
    migrate_database,
    validate_database,
)
from btc_timesfm.history.history_store import ForecastHistoryStore


class HistoryBackupTests(unittest.TestCase):
    def _valid_database(self, root: Path) -> Path:
        db = root / "forecast_history.sqlite"
        ForecastHistoryStore(db)
        verification = validate_database(db)
        self.assertEqual(verification["integrity"], "ok")
        return db

    def test_backup_asset_name_is_versioned_and_rejects_unsafe_generations(self) -> None:
        self.assertEqual(
            backup_asset_name("20260905T220000Z-123"),
            "forecast_history.backup-20260905T220000Z-123.sqlite.gz",
        )
        for invalid in ("", "with space", "../escape", "slash/name"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                backup_asset_name(invalid)

    def test_create_and_verify_archive_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = self._valid_database(root)
            archive = root / "backup.sqlite.gz"

            created = create_archive(db, archive)
            verified = verify_archive(archive)

            self.assertTrue(archive.exists())
            self.assertGreater(created["archive_bytes"], 0)
            self.assertEqual(created["sha256"], verified["sha256"])
            self.assertTrue(created["database_verification"]["ok"])
            self.assertTrue(verified["database_verification"]["ok"])

    def test_create_archive_upgrades_older_schema_without_modifying_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "legacy.sqlite"
            migrate_database(db, migrations=MIGRATIONS[:3], target_version=3)
            self.assertEqual(validate_database(db, expected_version=3)["schema_version"], 3)

            archive = root / "legacy-backup.sqlite.gz"
            created = create_archive(db, archive)
            verified = verify_archive(archive)

            # The rollback source remains byte-compatible with its original schema,
            # while the archive is immediately restorable by the current code.
            self.assertEqual(validate_database(db, expected_version=3)["schema_version"], 3)
            self.assertEqual(
                created["database_verification"]["schema_version"], CURRENT_SCHEMA_VERSION
            )
            self.assertEqual(
                verified["database_verification"]["schema_version"], CURRENT_SCHEMA_VERSION
            )

    def test_restore_archive_recovers_valid_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = self._valid_database(root)
            archive = root / "backup.sqlite.gz"
            create_archive(db, archive)

            restored = root / "restored.sqlite"
            report = restore_archive(archive, restored)

            self.assertTrue(restored.exists())
            self.assertTrue(report["restored_database_verification"]["ok"])
            self.assertEqual(validate_database(restored)["integrity"], "ok")

    def test_corrupt_archive_does_not_overwrite_existing_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "broken.sqlite.gz"
            archive.write_bytes(b"not-gzip")
            destination = root / "destination.sqlite"
            destination.write_bytes(b"keep-me")

            with self.assertRaises(RuntimeError):
                restore_archive(archive, destination)

            self.assertEqual(destination.read_bytes(), b"keep-me")

    def test_invalid_database_inside_gzip_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "invalid.sqlite.gz"
            with gzip.open(archive, "wb") as handle:
                handle.write(b"not a sqlite database")

            with self.assertRaises(Exception):
                verify_archive(archive)

    def test_generation_size_limit_is_enforced_without_leaving_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = self._valid_database(root)
            archive = root / "too-large.sqlite.gz"

            with self.assertRaises(RuntimeError):
                create_archive(db, archive, max_generation_bytes=1)

            self.assertFalse(archive.exists())

    def test_retention_ignores_nonversioned_assets_and_keeps_newest_generations(self) -> None:
        assets = [
            {
                "id": 1,
                "name": "forecast_history.sqlite.gz",
                "size": 10,
                "created_at": "2026-09-05T00:00:00Z",
            },
            {
                "id": 2,
                "name": "forecast_history.previous.sqlite.gz",
                "size": 10,
                "created_at": "2026-09-05T00:00:00Z",
            },
            {
                "id": 10,
                "name": backup_asset_name("001"),
                "size": 10,
                "created_at": "2026-09-01T00:00:00Z",
            },
            {
                "id": 11,
                "name": backup_asset_name("002"),
                "size": 10,
                "created_at": "2026-09-02T00:00:00Z",
            },
            {
                "id": 12,
                "name": backup_asset_name("003"),
                "size": 10,
                "created_at": "2026-09-03T00:00:00Z",
            },
        ]

        plan = retention_plan(assets, keep_generations=2, max_total_bytes=100)

        self.assertEqual(plan["backup_assets_seen"], 3)
        self.assertEqual([item["id"] for item in plan["keep"]], [12, 11])
        self.assertEqual([item["id"] for item in plan["delete"]], [10])

    def test_retention_respects_total_byte_budget(self) -> None:
        assets = [
            {
                "id": 1,
                "name": backup_asset_name("new"),
                "size": 80,
                "created_at": "2026-09-03T00:00:00Z",
            },
            {
                "id": 2,
                "name": backup_asset_name("middle"),
                "size": 30,
                "created_at": "2026-09-02T00:00:00Z",
            },
            {
                "id": 3,
                "name": backup_asset_name("old"),
                "size": 10,
                "created_at": "2026-09-01T00:00:00Z",
            },
        ]

        plan = retention_plan(assets, keep_generations=7, max_total_bytes=100)

        self.assertEqual([item["id"] for item in plan["keep"]], [1, 3])
        self.assertEqual([item["id"] for item in plan["delete"]], [2])
        self.assertEqual(plan["kept_bytes"], 90)

    def test_retention_always_keeps_newest_generation(self) -> None:
        assets = [
            {
                "id": 1,
                "name": backup_asset_name("new"),
                "size": 200,
                "created_at": "2026-09-03T00:00:00Z",
            },
            {
                "id": 2,
                "name": backup_asset_name("old"),
                "size": 1,
                "created_at": "2026-09-02T00:00:00Z",
            },
        ]

        plan = retention_plan(assets, keep_generations=7, max_total_bytes=100)
        self.assertEqual([item["id"] for item in plan["keep"]], [1])
        self.assertEqual([item["id"] for item in plan["delete"]], [2])

    def test_retention_rejects_invalid_policy(self) -> None:
        with self.assertRaises(ValueError):
            retention_plan([], keep_generations=0)
        with self.assertRaises(ValueError):
            retention_plan([], max_total_bytes=0)

    def test_assets_json_shape_is_serializable_for_workflow(self) -> None:
        plan = retention_plan(
            [
                {
                    "id": 42,
                    "name": backup_asset_name("run-42"),
                    "size": 123,
                    "created_at": "2026-09-05T22:00:00Z",
                }
            ]
        )
        encoded = json.dumps(plan, sort_keys=True)
        self.assertIn('"id": 42', encoded)


if __name__ == "__main__":
    unittest.main()
