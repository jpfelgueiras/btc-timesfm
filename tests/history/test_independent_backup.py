import hashlib
import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from btc_timesfm.history.history_backup import create_archive
from btc_timesfm.history.history_migrations import CURRENT_SCHEMA_VERSION
from btc_timesfm.history.history_store import ForecastHistoryStore
from btc_timesfm.history.independent_backup import (
    BackupAgeError,
    check_backup,
    restore_from_s3,
    upload_and_verify,
)


class IndependentBackupTests(unittest.TestCase):
    def test_upload_copies_back_and_verifies_checksum_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)
            remote: dict[str, bytes] = {}

            def fake_aws(*args: str) -> None:
                source, target = args[1], args[2]
                if source.startswith("s3://"):
                    Path(target).write_bytes(remote[source])
                else:
                    remote[target] = Path(source).read_bytes()

            with patch("btc_timesfm.history.independent_backup._aws", side_effect=fake_aws):
                result = upload_and_verify(archive, "s3://bucket/history/latest.sqlite.gz")

            self.assertTrue(result["verified"])
            expected = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(result["sha256"], expected)
            self.assertTrue(result["database_verification"]["ok"])
            manifest = json.loads(remote["s3://bucket/history/latest.sqlite.gz.manifest.json"])
            self.assertEqual(manifest["sha256"], expected)
            self.assertEqual(manifest["row_counts"]["forecast_origins"], 0)
            self.assertIsNone(manifest["latest_origin_at"])

    def test_destination_copy_failure_is_reported_without_leaking_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)

            with (
                patch(
                    "btc_timesfm.history.independent_backup.subprocess.run",
                    side_effect=OSError("secret access key"),
                ),
                self.assertRaisesRegex(RuntimeError, "operation failed") as error,
            ):
                upload_and_verify(archive, "s3://bucket/history/latest.sqlite.gz")
            self.assertNotIn("secret access key", str(error.exception))

    def test_failed_candidate_verification_does_not_switch_manifest_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)
            old_manifest = b'{"previous":"verified copy"}\n'
            objects = {"s3://bucket/history/latest.sqlite.gz.manifest.json": old_manifest}

            def fake_aws(*args: str) -> None:
                source, target = args[1], args[2]
                if source.startswith("s3://"):
                    Path(target).write_bytes(objects[source])
                else:
                    objects[target] = (
                        b"corrupt candidate"
                        if target.startswith("s3://")
                        else Path(source).read_bytes()
                    )

            with (
                patch("btc_timesfm.history.independent_backup._aws", side_effect=fake_aws),
                self.assertRaises(RuntimeError),
            ):
                upload_and_verify(archive, "s3://bucket/history/latest.sqlite.gz")
            self.assertEqual(
                objects["s3://bucket/history/latest.sqlite.gz.manifest.json"], old_manifest
            )

    def test_scratch_restore_download_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)
            output = root / "scratch.sqlite.gz"
            manifest = {
                "manifest_version": 1,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "archive_bytes": archive.stat().st_size,
                "archive_uri": "s3://bucket/history/latest.sqlite.gz.generations/test.sqlite.gz",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "schema_version": CURRENT_SCHEMA_VERSION,
                "row_counts": {
                    "forecast_origins": 0,
                    "forecast_predictions": 0,
                    "matured_predictions": 0,
                },
                "latest_origin_at": None,
            }

            def fake_aws(*args: str) -> None:
                if args[1].endswith(".manifest.json"):
                    Path(args[2]).write_text(json.dumps(manifest), encoding="utf-8")
                else:
                    shutil.copyfile(archive, Path(args[2]))

            with patch("btc_timesfm.history.independent_backup._aws", side_effect=fake_aws):
                report = restore_from_s3("s3://bucket/history/latest.sqlite.gz", output)
            self.assertTrue(report["database_verification"]["ok"])
            self.assertTrue(output.exists())

    def test_manifest_mismatch_fails_without_replacing_existing_scratch_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)
            output = root / "scratch.sqlite.gz"
            output.write_bytes(b"preexisting scratch contents")
            manifest = {
                "manifest_version": 1,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "archive_bytes": archive.stat().st_size,
                "archive_uri": "s3://bucket/history/latest.sqlite.gz.generations/test.sqlite.gz",
                "sha256": "0" * 64,
                "schema_version": CURRENT_SCHEMA_VERSION,
                "row_counts": {
                    "forecast_origins": 0,
                    "forecast_predictions": 0,
                    "matured_predictions": 0,
                },
                "latest_origin_at": None,
            }

            def fake_aws(*args: str) -> None:
                if args[1].endswith(".manifest.json"):
                    Path(args[2]).write_text(json.dumps(manifest), encoding="utf-8")
                else:
                    shutil.copyfile(archive, Path(args[2]))

            with (
                patch("btc_timesfm.history.independent_backup._aws", side_effect=fake_aws),
                self.assertRaisesRegex(RuntimeError, "checksum does not match"),
            ):
                restore_from_s3("s3://bucket/history/latest.sqlite.gz", output)
            self.assertEqual(output.read_bytes(), b"preexisting scratch contents")

    def test_manifest_row_count_or_latest_origin_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)
            output = root / "scratch.sqlite.gz"
            manifest = {
                "manifest_version": 1,
                "verified_at": datetime.now(timezone.utc).isoformat(),
                "archive_bytes": archive.stat().st_size,
                "archive_uri": "s3://bucket/history/latest.sqlite.gz.generations/test.sqlite.gz",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "schema_version": CURRENT_SCHEMA_VERSION,
                "row_counts": {
                    "forecast_origins": 1,
                    "forecast_predictions": 0,
                    "matured_predictions": 0,
                },
                "latest_origin_at": "2026-01-01T00:00:00+00:00",
            }

            def fake_aws(*args: str) -> None:
                if args[1].endswith(".manifest.json"):
                    Path(args[2]).write_text(json.dumps(manifest), encoding="utf-8")
                else:
                    shutil.copyfile(archive, Path(args[2]))

            with (
                patch("btc_timesfm.history.independent_backup._aws", side_effect=fake_aws),
                self.assertRaisesRegex(RuntimeError, "row counts do not match"),
            ):
                restore_from_s3("s3://bucket/history/latest.sqlite.gz", output)
            self.assertFalse(output.exists())

    def test_check_reports_recent_verified_copy_and_rejects_stale_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)
            now = datetime.now(timezone.utc)
            manifest = {
                "manifest_version": 1,
                "verified_at": (now - timedelta(hours=2, minutes=1)).isoformat(),
                "archive_bytes": archive.stat().st_size,
                "archive_uri": "s3://bucket/history/latest.sqlite.gz.generations/test.sqlite.gz",
                "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                "schema_version": CURRENT_SCHEMA_VERSION,
                "row_counts": {
                    "forecast_origins": 0,
                    "forecast_predictions": 0,
                    "matured_predictions": 0,
                },
                "latest_origin_at": None,
            }
            receipt = {
                "backup_sha256": manifest["sha256"],
                "restored_at": (now - timedelta(minutes=30)).isoformat(),
            }

            def fake_aws(*args: str) -> None:
                source, target = args[1], args[2]
                if source.endswith(".manifest.json"):
                    Path(target).write_text(json.dumps(manifest), encoding="utf-8")
                elif source.endswith(".restore.json"):
                    Path(target).write_text(json.dumps(receipt), encoding="utf-8")
                else:
                    shutil.copyfile(archive, Path(target))

            with patch("btc_timesfm.history.independent_backup._aws", side_effect=fake_aws):
                with self.assertRaises(BackupAgeError) as error:
                    check_backup("s3://bucket/history/latest.sqlite.gz", max_age_hours=2, now=now)
            self.assertEqual(error.exception.report["status"], "failed")
            self.assertEqual(
                error.exception.report["last_successful_restore_at"], receipt["restored_at"]
            )
            self.assertEqual(error.exception.report["sha256"], manifest["sha256"])


if __name__ == "__main__":
    unittest.main()
