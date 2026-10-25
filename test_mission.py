import unittest
from datetime import datetime, timedelta

from lunar_sun import parse_horizons_result
from mission import ROVER_PROFILES, lunar_distance_km, simulate_rover, suggest_task_order


class MissionModelTests(unittest.TestCase):
    def test_one_degree_at_equator_is_about_30_km_on_moon(self):
        self.assertAlmostEqual(lunar_distance_km(0, 0, 0, 1), 30.32, delta=0.1)

    def test_sunlight_recharges_a_concept_rover_during_work(self):
        rover = dict(ROVER_PROFILES[1])
        departure = datetime(2030, 1, 1)
        result = simulate_rover(
            rover, [], [{"name": "Work", "power_kw": 0.5, "duration_hours": 2}],
            departure, [(departure, departure + timedelta(hours=3))],
        )
        self.assertEqual(result["solar_energy_kwh"], rover["solar_kw"] * 2)
        self.assertEqual(result["end_charge_kwh"], rover["battery_kwh"])
        self.assertTrue(result["success"])

    def test_long_trip_runs_out_of_battery_before_work(self):
        rover = dict(ROVER_PROFILES[0])
        departure = datetime(2030, 1, 1)
        result = simulate_rover(
            rover, [{"distance_km": 500}], [{"name": "Work", "power_kw": 1, "duration_hours": 1}],
            departure, [],
        )
        self.assertFalse(result["success"])
        self.assertGreater(result["drive_energy_kwh"], rover["battery_kwh"])
        self.assertNotIn("Work", [point["phase"] for point in result["timeline"]])

    def test_apollo_lrv_cargo_limit_is_checked(self):
        rover = dict(ROVER_PROFILES[0])
        result = simulate_rover(
            rover, [], [{"name": "Heavy delivery", "power_kw": 0, "duration_hours": 1, "payload_kg": 600}],
            datetime(2030, 1, 1), [],
        )
        self.assertFalse(result["success"])
        self.assertIn("carries up to", result["reason"])

    def test_horizons_csv_rows_produce_sun_state(self):
        raw = "start\n$$SOE\n2026-Oct-25 12:00, 90.0, 15.0\n2026-Oct-25 13:00, 100.0, -2.0\n$$EOE\n"
        points = parse_horizons_result(raw)
        self.assertEqual([p["sunlit"] for p in points], [True, False])
        self.assertEqual(points[0]["azimuth_deg"], 90.0)

    def test_horizons_rows_with_daylight_flags_parse_angles(self):
        raw = "start\n$$SOE\n 2026-Oct-25 20:25,*,i, 91.397004, 0.238564,\n$$EOE\n"
        point = parse_horizons_result(raw)[0]
        self.assertAlmostEqual(point["azimuth_deg"], 91.397004)
        self.assertAlmostEqual(point["elevation_deg"], 0.238564)
        self.assertTrue(point["sunlit"])

    def test_fallback_sun_points_use_saved_windows(self):
        rover = dict(ROVER_PROFILES[1])
        departure = datetime(2030, 1, 1)
        sun_points = [
            {"time": departure, "azimuth_deg": None, "elevation_deg": None, "sunlit": True},
            {"time": departure + timedelta(hours=1), "azimuth_deg": None, "elevation_deg": None, "sunlit": True},
            {"time": departure + timedelta(hours=2), "azimuth_deg": None, "elevation_deg": None, "sunlit": True},
        ]
        result = simulate_rover(
            rover, [], [{"name": "Work", "power_kw": 0.5, "duration_hours": 2}],
            departure, [(departure, departure + timedelta(hours=3))],
            sun_points=sun_points,
        )
        self.assertEqual(result["solar_energy_kwh"], rover["solar_kw"] * 2)
        self.assertTrue(result["success"])
    def test_order_suggestion_returns_all_tasks(self):
        tasks = [
            {"id": 1, "name": "A", "power_kw": 0, "duration_hours": 1, "payload_kg": 0},
            {"id": 2, "name": "B", "power_kw": 0, "duration_hours": 1, "payload_kg": 0},
        ]
        ordered = suggest_task_order(ROVER_PROFILES[:1], [], tasks, datetime(2030, 1, 1), [])
        self.assertEqual({task["id"] for task in ordered}, {1, 2})


if __name__ == "__main__":
    unittest.main()
