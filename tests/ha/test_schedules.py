"""Offline protocol vectors derived from the APK schema, never from credentials."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from zoneinfo import ZoneInfo

SPEC = importlib.util.spec_from_file_location(
    "ufeeder_schedules_test",
    Path(__file__).resolve().parents[2] / "custom_components/ufeeder/schedules.py",
)
schedules = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = schedules
SPEC.loader.exec_module(schedules)
Schedule = schedules.FeedingSchedule


class ScheduleCodecTests(unittest.TestCase):
    def test_schema_golden_vector_and_optional_presence(self):
        # List field1 -> schedule id7, UTC06:30, on, Mon/Wed/Fri mask42,
        # FeedOptions compartment2,600 seconds. No per-schedule heater override.
        vector = bytes.fromhex("0a13080712040806101e1801202a2a05080210d804")
        expected = Schedule(
            id=7,
            hour=6,
            minute=30,
            weekdays=42,
            tray_compartment=2,
            duration_seconds=600,
        )
        self.assertEqual(schedules.decode_schedules(vector), [expected])
        self.assertEqual(schedules.encode_schedules([expected]), vector)
        self.assertNotIn("heatBeforeFeeding", expected.as_dict()["feedOptions"])

    def test_named_and_numeric_json(self):
        named = {
            "schedules": [
                {
                    "id": 19,
                    "start": {"hour": 20, "minute": 15},
                    "enabled": True,
                    "weekdays": 65,
                    "feedOptions": {
                        "trayCompartmentIndex": 3,
                        "feedDurationSeconds": 3600,
                        "heatBeforeFeeding": False,
                        "expectedHeatingTemperature": 24,
                    },
                }
            ]
        }
        tagged = {
            "1": [
                {
                    "1": 19,
                    "2": {"1": 20, "2": 15},
                    "3": True,
                    "4": 65,
                    "5": {"1": 3, "2": 3600, "3": False, "4": 24},
                }
            ]
        }
        expected = Schedule(
            id=19,
            hour=20,
            minute=15,
            weekdays=65,
            tray_compartment=3,
            duration_seconds=3600,
            heat_before_feeding=False,
            heating_temperature=24,
        )
        for data in (named, tagged, json.dumps(named)):
            self.assertEqual(schedules.decode_schedules(data), [expected])
        self.assertEqual(expected.as_dict(), named["schedules"][0])

    def test_removal_is_packed_uint32_not_raw_id_bytes(self):
        self.assertEqual(
            schedules.encode_remove_schedules([7, 300]), bytes.fromhex("0a0307ac02")
        )
        for ids in ([], [0], [-1], [True], [1 << 32]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                schedules.encode_remove_schedules(ids)

    def test_protobuf_defaults_and_future_fields(self):
        # A future field9=42 at each message level survives a subsequent edit.
        raw = bytes.fromhex("0a14080712040800482a180020022a040801482a482a")
        decoded = schedules.decode_schedules(raw)[0]
        self.assertFalse(decoded.enabled)
        self.assertEqual(decoded.minute, 0)
        self.assertEqual(decoded.duration_seconds, 0)
        for value in (
            decoded.unknown_fields,
            decoded.unknown_start_fields,
            decoded.unknown_option_fields,
        ):
            self.assertEqual(value, b"\x48\x2a")
        changed = replace(decoded, enabled=True, duration_seconds=180)
        self.assertEqual(
            schedules.decode_schedules(schedules.encode_schedules([changed])), [changed]
        )

    def test_signed_temperature_and_absent_boolean_are_distinct(self):
        entry = Schedule(
            heating_temperature=-1, heat_before_feeding=False, operator=123
        )
        self.assertEqual(
            schedules.decode_schedules(schedules.encode_schedules([entry])), [entry]
        )
        self.assertNotEqual(
            schedules.encode_schedules([entry]),
            schedules.encode_schedules([Schedule()]),
        )

    def test_malformed_input_rejected(self):
        for data in (
            b"\x0a\x02\x08",
            b"\x00",
            b"\x80" * 12,
            {"schedules": "bad"},
            {"schedules": [{"start": []}]},
            {"schedules": [{"enabled": "false"}]},
            {"schedules": [{"start": {"hour": 24}}]},
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                schedules.decode_schedules(data)
        self.assertEqual(schedules.decode_schedules(b""), [])
        self.assertEqual(schedules.decode_schedules({}), [])


class SchedulePlanningTests(unittest.TestCase):
    def test_local_utc_weekday_rollover(self):
        self.assertEqual(schedules.local_to_utc(0, 30, [1], 120), (22, 30, 1))
        self.assertEqual(schedules.local_to_utc(23, 45, [7], -300), (4, 45, 2))
        self.assertEqual(schedules.local_to_utc(5, 15, [1, 3, 5], 330), (23, 45, 21))
        self.assertEqual(schedules.utc_to_local(22, 30, 1, 120), (0, 30, (1,)))
        self.assertEqual(schedules.utc_to_local(4, 45, 2, -300), (23, 45, (7,)))

    def test_start_spacing_boundary_and_week_wrap(self):
        sunday = Schedule(hour=23, minute=30, weekdays=1)
        monday = Schedule(hour=0, minute=29, weekdays=2)
        self.assertTrue(schedules.schedules_conflict(sunday, monday))
        self.assertTrue(schedules.schedules_conflict(monday, sunday))
        self.assertFalse(
            schedules.schedules_conflict(sunday, replace(monday, minute=30))
        )
        self.assertFalse(
            schedules.schedules_conflict(sunday, replace(monday, enabled=False))
        )
        # App spacing uses starts, even when feeding durations overlap.
        self.assertFalse(
            schedules.schedules_conflict(
                Schedule(hour=8, weekdays=2, duration_seconds=7200),
                Schedule(hour=9, weekdays=2),
            )
        )

    def test_capacity_ids_and_ui_duration_limits(self):
        existing = [Schedule(id=i + 1, hour=i * 2) for i in range(10)]
        with self.assertRaisesRegex(ValueError, "at most 10"):
            schedules.validate_schedule_change(Schedule(hour=23), existing)
        schedules.validate_schedule_change(
            replace(existing[0], enabled=False), existing
        )
        with self.assertRaisesRegex(ValueError, "no longer exists"):
            schedules.validate_schedule_change(Schedule(id=300), existing)
        for minutes in (3, 30, 60, 120, 720):
            schedules.validate_schedule(
                Schedule(duration_seconds=minutes * 60), tray_count=3
            )
        for minutes in (0, 2, 31, 45, 90, 721):
            with self.subTest(minutes=minutes), self.assertRaises(ValueError):
                schedules.validate_schedule(Schedule(duration_seconds=minutes * 60))
        with self.assertRaises(ValueError):
            schedules.validate_schedule(Schedule(tray_compartment=4), tray_count=3)
        with self.assertRaises(ValueError):
            schedules.validate_schedule(Schedule(weekdays=0))

    def test_calendar_preserves_utc_across_dst_and_overlap_boundaries(self):
        berlin = ZoneInfo("Europe/Berlin")
        entry = Schedule(hour=6, duration_seconds=3600)
        start = datetime(2026, 10, 24, 6, 30, tzinfo=timezone.utc)
        end = datetime(2026, 10, 26, 6, 0, tzinfo=timezone.utc)
        events = schedules.next_occurrences(entry, start, end)
        self.assertEqual(len(events), 2)
        self.assertEqual([event[0].hour for event in events], [6, 6])
        self.assertEqual([event[0].astimezone(berlin).hour for event in events], [8, 7])
        self.assertTrue(
            all(
                event_end - event_start == timedelta(hours=1)
                for event_start, event_end in events
            )
        )
        self.assertEqual(
            schedules.next_occurrences(replace(entry, weekdays=0), start, end), []
        )
        self.assertEqual(
            schedules.next_occurrences(replace(entry, enabled=False), start, end), []
        )
        with self.assertRaises(ValueError):
            schedules.next_occurrences(entry, start.replace(tzinfo=None), end)


if __name__ == "__main__":
    unittest.main()
