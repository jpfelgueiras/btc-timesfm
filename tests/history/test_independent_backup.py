import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from btc_timesfm.history.history_backup import create_archive
from btc_timesfm.history.history_store import ForecastHistoryStore
from btc_timesfm.history.independent_backup import restore_from_s3, upload_and_verify


class IndependentBackupTests(unittest.TestCase):
    def test_upload_copies_back_and_verifies_checksum_and_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)
            remote = root / "remote.sqlite.gz"

            def fake_aws(*args: str) -> None:
                if args[2].startswith("s3://"):
                    shutil.copyfile(Path(args[1]), remote)
                else:
                    shutil.copyfile(remote, Path(args[2]))

            with patch("btc_timesfm.history.independent_backup._aws", side_effect=fake_aws):
                result = upload_and_verify(archive, "s3://bucket/history/latest.sqlite.gz")

            self.assertTrue(result["verified"])
            expected = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(result["sha256"], expected)
            self.assertTrue(result["database_verification"]["ok"])

    def test_destination_copy_failure_is_reported_without_leaking_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)

            with patch(
                "btc_timesfm.history.independent_backup.subprocess.run",
                side_effect=OSError("secret access key"),
            ), self.assertRaisesRegex(RuntimeError, "operation failed") as error:
                upload_and_verify(archive, "s3://bucket/history/latest.sqlite.gz")
            self.assertNotIn("secret access key", str(error.exception))

    def test_scratch_restore_download_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            db = root / "history.sqlite"
            ForecastHistoryStore(db)
            archive = root / "history.sqlite.gz"
            create_archive(db, archive)
            output = root / "scratch.sqlite.gz"

            def fake_aws(*args: str) -> None:
                shutil.copyfile(archive, Path(args[2]))

            with patch("btc_timesfm.history.independent_backup._aws", side_effect=fake_aws):
                report = restore_from_s3("s3://bucket/history/latest.sqlite.gz", output)
            self.assertTrue(report["database_verification"]["ok"])


if __name__ == "__main__":
    unittest.main()
