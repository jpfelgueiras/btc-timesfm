"""Tests for safe output from the repository security audit."""

from __future__ import annotations

import contextlib
import io
import unittest
from unittest.mock import patch

from btc_timesfm.ops.security_audit import main


class SecurityAuditTests(unittest.TestCase):
    def test_findings_are_not_written_to_stdout(self) -> None:
        secret_finding = "settings.py: possible literal secret-value"
        output = io.StringIO()
        with patch(
            "btc_timesfm.ops.security_audit.audit_repository", return_value=[secret_finding]
        ):
            with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as raised:
                main()

        self.assertEqual(raised.exception.code, 1)
        self.assertIn("details are suppressed", output.getvalue())
        self.assertNotIn(secret_finding, output.getvalue())
        self.assertNotIn("secret-value", output.getvalue())


if __name__ == "__main__":
    unittest.main()
