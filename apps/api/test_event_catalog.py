"""event_catalog.py 的单测（工作包 C，第一期交付）。

覆盖：注册查询、未知事件名拒绝（含族错误与未注册动作的区分提示）、
族查询、信封校验（缺字段/类型错/未注册事件）。
"""

from __future__ import annotations

import unittest

from app import event_catalog
from app.event_catalog import (
    CATALOG_VERSION,
    EVENT_FAMILIES,
    UnknownEventError,
    events_in_family,
    is_registered,
    require_event_name,
    validate_envelope,
)


class CatalogTests(unittest.TestCase):
    def test_initial_events_registered_across_all_families(self):
        for family in EVENT_FAMILIES:
            specs = events_in_family(family)
            self.assertTrue(specs, f"family {family} has no events")
            for spec in specs:
                self.assertEqual(spec.name.split(".")[1], family)
                self.assertGreaterEqual(spec.version, 1)

    def test_registration_lookup(self):
        self.assertTrue(is_registered("project.task.created"))
        self.assertFalse(is_registered("project.task.exploded"))

    def test_unknown_action_name_rejected(self):
        with self.assertRaises(UnknownEventError) as ctx:
            require_event_name("project.task.exploded")
        self.assertIn("not registered", str(ctx.exception))

    def test_unknown_family_rejected_with_hint(self):
        with self.assertRaises(UnknownEventError) as ctx:
            require_event_name("project.warpdrive.engaged")
        self.assertIn("registered families", str(ctx.exception))

    def test_malformed_name_rejected(self):
        for bad in ("task.created", "project.task", "PROJECT.task.created", "", None):
            with self.assertRaises(UnknownEventError):
                require_event_name(bad)

    def test_family_query_rejects_unknown_family(self):
        with self.assertRaises(UnknownEventError):
            events_in_family("warp")

    def test_every_event_name_matches_its_family_pattern(self):
        for spec in event_catalog.EVENTS.values():
            self.assertTrue(spec.name.startswith("project."))
            self.assertRegex(spec.name, r"^project\.[a-z]+\.[a-z_]+$")

    def test_catalog_version_is_positive_int(self):
        self.assertIsInstance(CATALOG_VERSION, int)
        self.assertGreaterEqual(CATALOG_VERSION, 1)


class EnvelopeValidationTests(unittest.TestCase):
    def _envelope(self, **overrides):
        base = {
            "event": "project.task.created",
            "seq": 7,
            "schema_version": 1,
            "occurred_at": "2026-09-26T12:00:00+00:00",
            "payload": {"task_id": "t-1"},
        }
        base.update(overrides)
        for key, value in list(overrides.items()):
            if value is None:
                base.pop(key, None)
        return base

    def test_valid_envelope_has_no_problems(self):
        self.assertEqual(validate_envelope(self._envelope()), [])

    def test_missing_required_field_reported_once_and_short_circuited(self):
        problems = validate_envelope(self._envelope(seq=None))
        self.assertEqual(problems, ["missing required field: seq"])

    def test_unregistered_event_is_a_problem(self):
        problems = validate_envelope(self._envelope(event="project.task.exploded"))
        self.assertTrue(any("not registered" in p for p in problems))

    def test_seq_must_be_positive_int_not_bool(self):
        for bad in (0, -1, "7", 1.5, True):
            problems = validate_envelope(self._envelope(seq=bad))
            self.assertTrue(any("seq" in p for p in problems), f"seq={bad!r}")

    def test_schema_version_must_be_positive_int(self):
        for bad in (0, "1", True):
            problems = validate_envelope(self._envelope(schema_version=bad))
            self.assertTrue(any("schema_version" in p for p in problems))

    def test_payload_must_be_object_and_occurred_at_string(self):
        problems = validate_envelope(self._envelope(payload=[1, 2], occurred_at=123))
        self.assertTrue(any("payload" in p for p in problems))
        self.assertTrue(any("occurred_at" in p for p in problems))

    def test_recommended_fields_absence_is_not_a_problem(self):
        problems = validate_envelope(self._envelope(project_id=None, actor=None))
        self.assertEqual(problems, [])


if __name__ == "__main__":
    unittest.main()
