from __future__ import annotations

import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from information_boundary import (
    FailClosedObservationController,
    FileObservation,
    InformationBoundaryAudit,
)


class InformationBoundaryAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp_dir.name) / "workspace"
        self.workspace.mkdir()
        self.input_file = self.workspace / "input" / "data.csv"
        self.input_file.parent.mkdir()
        self.input_file.write_text("x\n1\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_declared_system_observation_is_allowed(self) -> None:
        observation = FileObservation(str(self.input_file), observation_source="system")
        result = InformationBoundaryAudit.evaluate(
            str(self.workspace),
            ["input/data.csv"],
            [observation],
            {"observation_mode": "system"},
        )
        self.assertTrue(result["allowed"])
        self.assertEqual(result["audit_status"], "captured")
        self.assertEqual(result["violations"], [])

    def test_missing_system_observation_is_major_and_fail_closed(self) -> None:
        result = InformationBoundaryAudit.evaluate(
            str(self.workspace),
            ["input/data.csv"],
            [],
            {"observation_mode": "system"},
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["audit_status"], "not_captured")
        self.assertEqual(result["violations"][0]["code"], "input_observation_not_captured")

    def test_undeclared_and_outside_reads_are_distinguished(self) -> None:
        undeclared = InformationBoundaryAudit.evaluate(
            str(self.workspace),
            ["input/data.csv"],
            [FileObservation(str(self.workspace / "input" / "other.csv"), observation_source="system")],
            {"observation_mode": "system"},
        )
        self.assertFalse(undeclared["allowed"])
        self.assertEqual(undeclared["violations"][0]["code"], "undeclared_input_file")

        outside = InformationBoundaryAudit.evaluate(
            str(self.workspace),
            ["input/data.csv"],
            [FileObservation(str(Path(self.temp_dir.name) / "secret.csv"), observation_source="system")],
            {"observation_mode": "system"},
        )
        self.assertFalse(outside["allowed"])
        self.assertEqual(outside["violations"][0]["severity"], "fatal")

    def test_future_data_is_blocked_by_decision_time_and_data_cutoff(self) -> None:
        observation = FileObservation(
            str(self.input_file),
            observation_source="system",
            available_at=datetime(2026, 9, 14, 12, tzinfo=UTC),
            data_time_end=datetime(2026, 9, 14, 12, tzinfo=UTC),
        )
        result = InformationBoundaryAudit.evaluate(
            str(self.workspace),
            ["input/data.csv"],
            [observation],
            {
                "observation_mode": "system",
                "decision_time": "2026-09-14T10:00:00+00:00",
                "data_cutoff": "2026-09-14T10:00:00+00:00",
            },
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["future_data"], [str(self.input_file)])
        self.assertEqual({item["code"] for item in result["violations"]}, {
            "future_data_available_after_decision",
            "future_data_beyond_cutoff",
        })

    def test_observer_unavailable_is_explicitly_not_captured(self) -> None:
        capture = FailClosedObservationController.capture(None, str(self.workspace), required=True)
        self.assertEqual(capture.status, "not_captured")
        self.assertEqual(capture.reason, "file_access_observer_unavailable")


if __name__ == "__main__":
    unittest.main()
