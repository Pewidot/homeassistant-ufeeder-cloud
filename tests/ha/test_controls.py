"""Offline vectors and limits recovered independently from the APK schema."""

import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "ufeeder_controls_test",
    Path(__file__).resolve().parents[2] / "custom_components/ufeeder/controls.py",
)
controls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(controls)


class ControlCodecTests(unittest.TestCase):
    def test_feed_options_golden_vector(self):
        self.assertEqual(
            controls.encode_feed_options(
                tray=2,
                compartment_count=3,
                duration_seconds=600,
                preheat=True,
                temperature=24,
                user_id="1234",
            ),
            bytes.fromhex("080210d8041801201828d209"),
        )

    def test_scalar_wrappers_and_write_allowlist(self):
        self.assertEqual(
            controls.encode_scalar_property((10, 11, 8), True), b"\x08\x01"
        )
        self.assertEqual(controls.encode_scalar_property((10, 11, 8), False), b"")
        self.assertEqual(controls.encode_scalar_property((10, 11, 12), 14), b"\x08\x0e")
        for path, value in (
            ((10, 11, 9), True),
            ((10, 13, 2), "secret"),
            ((10, 11, 8), 1),
            ((10, 11, 8), "false"),
            ((10, 11, 12), 15),
            ((10, 11, 7), {}),
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                controls.encode_scalar_property(path, value)

    def test_preference_merge_preserves_other_fields(self):
        baseline = {
            "feedDurationSeconds": 600,
            "heatBeforeFeeding": True,
            "expectedHeatingTemperature": 24,
        }
        self.assertEqual(
            controls.encode_preferences(baseline, heating_temperature=28),
            bytes.fromhex("08d8041001181c"),
        )
        self.assertEqual(baseline["expectedHeatingTemperature"], 24)
        self.assertEqual(
            controls.encode_preferences(
                {"1": 600, "2": True, "3": 28}, heat_before_feeding=False
            ),
            bytes.fromhex("08d8041000181c"),
        )
        # Absent proto3 values are defaults, not reasons to invent heater settings.
        self.assertEqual(
            controls.encode_preferences(
                {"feedDurationSeconds": 600}, duration_minutes=30
            ),
            bytes.fromhex("08880e10001800"),
        )

    def test_preference_uncertainty_rejected(self):
        for baseline, changes in (
            ({}, {"heat_before_feeding": True}),
            ({"4": 99}, {"duration_minutes": 10}),
            ({"1": 600, "feedDurationSeconds": 180}, {}),
            ({"1": 600}, {"heat_before_feeding": "false"}),
            ({"1": 600}, {"heating_temperature": 27}),
        ):
            with self.subTest(baseline=baseline), self.assertRaises(ValueError):
                controls.encode_preferences(baseline, **changes)

    def test_feed_bounds_and_operator_type(self):
        baseline = dict(
            tray=1,
            compartment_count=3,
            duration_seconds=180,
            preheat=False,
            temperature=24,
            user_id=1234,
        )
        for changes in (
            {"tray": 0},
            {"tray": 4},
            {"compartment_count": 2},
            {"duration_seconds": 181},
            {"duration_seconds": 2700},
            {"preheat": 1},
            {"temperature": 25},
            {"user_id": True},
            {"user_id": -1},
            {"user_id": "1.2"},
            {"user_id": 1 << 32},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                controls.encode_feed_options(**(baseline | changes))
        self.assertIn(b"\x18\x00", controls.encode_feed_options(**baseline))

    def test_duration_menu_boundaries(self):
        for value in (3, 30, 60, 120, 720):
            self.assertEqual(controls.validate_duration_minutes(value), value)
        for value in (False, 2, 31, 45, 90, 721, 10.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                controls.validate_duration_minutes(value)

    def test_firmware_argument_schema(self):
        self.assertEqual(
            controls.encode_firmware_version("1.2.3", 42),
            bytes.fromhex("0a05312e322e33102a"),
        )
        self.assertEqual(controls.STOP_FIRMWARE_DOWNLOAD_ARGUMENT, b"\x08\x01")
        with self.assertRaises(ValueError):
            controls.encode_firmware_version("1.2.3", "42")


if __name__ == "__main__":
    unittest.main()
