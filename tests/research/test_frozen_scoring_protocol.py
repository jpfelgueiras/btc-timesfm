"""Integrity and contract checks for the pre-evaluation scoring protocol."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from btc_timesfm.research.frozen_scoring_protocol import (
    PROTOCOL_PATH,
    load_frozen_protocol,
    paired_attempts,
    protocol_digest,
    validate_no_lookahead,
)


def attempt(model: str, *, status: str = "scored", origin: str = "2026-01-01T00:00:00Z") -> dict:
    return {
        "model": model,
        "origin_at": origin,
        "target_at": "2026-01-01T02:00:00+00:00",
        "horizon_hours": 2,
        "vintage_id": "snapshot-sha256",
        "vintage_sha256": "a" * 64,
        "outcome": "close-100",
        "status": status,
    }


class FrozenScoringProtocolTests(unittest.TestCase):
    def test_committed_protocol_is_hashed_and_frozen(self) -> None:
        protocol = load_frozen_protocol()
        self.assertEqual(protocol["status"], "frozen_before_evaluation")
        self.assertEqual(protocol["protocol_sha256"], protocol_digest(protocol))

    def test_pairing_uses_exact_utc_key_and_retains_failures_outside_score_pairs(self) -> None:
        left, right = attempt("candidate"), attempt("champion")
        unmatched = attempt("candidate", origin="2026-01-01T01:00:00Z")
        unmatched["target_at"] = "2026-01-01T03:00:00Z"
        self.assertEqual(
            paired_attempts([left, right, unmatched], "candidate", "champion"), [(left, right)]
        )
        right["status"] = "failed"
        self.assertEqual(paired_attempts([left, right], "candidate", "champion"), [])

    def test_pairing_rejects_naive_time_wrong_target_fractional_mismatch_duplicates_and_vintage(
        self,
    ) -> None:
        left, right = attempt("candidate"), attempt("champion")
        right["origin_at"] = "2026-01-01T00:00:00"
        with self.assertRaisesRegex(ValueError, "UTC"):
            paired_attempts([left, right], "candidate", "champion")
        right = attempt("champion")
        right["target_at"] = "2026-01-01T02:00:00.0000001Z"
        with self.assertRaisesRegex(ValueError, "microsecond precision"):
            paired_attempts([left, right], "candidate", "champion")
        right = attempt("champion")
        right["target_at"] = "2026-01-01T03:00:00Z"
        with self.assertRaisesRegex(ValueError, "does not match"):
            paired_attempts([left, right], "candidate", "champion")
        right = attempt("champion")
        right["target_at"] = "2026-01-01T02:00:00.000001Z"
        with self.assertRaisesRegex(ValueError, "does not match"):
            paired_attempts([left, right], "candidate", "champion")
        right = attempt("champion")
        right["vintage_id"] = "other"
        with self.assertRaisesRegex(ValueError, "vintage"):
            paired_attempts([left, right], "candidate", "champion")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            paired_attempts([left, left, right], "candidate", "champion")

    def test_no_lookahead_checks_input_and_selection_cutoff(self) -> None:
        validate_no_lookahead(
            {
                "origin_at": "2026-01-01T00:00:00Z",
                "input_available_at": "2026-01-01T00:00:00Z",
                "selection_data_through": "2026-01-01T00:00:00Z",
                "vintage_id": "snapshot-sha256",
                "vintage_sha256": "a" * 64,
            }
        )
        with self.assertRaisesRegex(ValueError, "missing required provenance"):
            validate_no_lookahead(
                {
                    "origin_at": "2026-01-01T00:00:00Z",
                    "vintage_id": "snapshot-sha256",
                    "vintage_sha256": "a" * 64,
                }
            )
        with self.assertRaisesRegex(ValueError, "vintage_sha256"):
            validate_no_lookahead(
                {
                    "origin_at": "2026-01-01T00:00:00Z",
                    "input_available_at": "2026-01-01T00:00:00Z",
                    "selection_data_through": "2026-01-01T00:00:00Z",
                    "vintage_id": "snapshot-sha256",
                }
            )
        with self.assertRaisesRegex(ValueError, "after forecast origin"):
            validate_no_lookahead(
                {
                    "origin_at": "2026-01-01T00:00:00Z",
                    "selection_data_through": "2026-01-01T00:01:00Z",
                    "input_available_at": "2026-01-01T00:00:00Z",
                    "vintage_id": "snapshot-sha256",
                    "vintage_sha256": "a" * 64,
                }
            )

    def test_tampered_protocol_digest_fails_closed(self) -> None:
        artifact = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
        artifact["purpose"] = "tampered"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protocol.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "SHA-256"):
                load_frozen_protocol(path)

    def test_schema_rejects_unrecognized_fields_even_with_valid_digest(self) -> None:
        artifact = json.loads(PROTOCOL_PATH.read_text(encoding="utf-8"))
        artifact["unregistered_extension"] = True
        artifact["protocol_sha256"] = protocol_digest(artifact)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "protocol.json"
            path.write_text(json.dumps(artifact), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "schema validation failed"):
                load_frozen_protocol(path)


if __name__ == "__main__":
    unittest.main()
