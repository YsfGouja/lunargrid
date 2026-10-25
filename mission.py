"""Rover mission calculations and clearly labelled reference profiles."""
from bisect import bisect_right
from itertools import permutations
from datetime import datetime, timedelta
from math import asin, cos, radians, sin, sqrt

MOON_RADIUS_KM = 1737.4
ROVER_PROFILES = [
    {
        "id": "apollo_lrv", "name": "Apollo Lunar Roving Vehicle",
        "description": "Historic rover; its battery, payload and speed are NASA data.",
        "data_type": "NASA historical data + derived energy estimate",
        "source_url": "https://nssdc.gsfc.nasa.gov/planetary/lunar/apollo_lrv.html",
        "battery_kwh": 8.71, "drive_kwh_per_km": 0.243, "speed_kmh": 9.2,
        "solar_kw": 0.0, "max_payload_kg": 490.0, "reserve_fraction": 0.15,
    },
    {
        "id": "scout_concept", "name": "Lunar Scout (concept)",
        "description": "Planning estimate for a small unpressurized rover.",
        "data_type": "Scenario estimate; not a built rover",
        "source_url": None,
        "battery_kwh": 105.0, "drive_kwh_per_km": 0.72, "speed_kmh": 9.0,
        "solar_kw": 1.5, "max_payload_kg": 250.0, "reserve_fraction": 0.15,
    },
    {
        "id": "cargo_concept", "name": "Cargo Hauler (concept)",
        "description": "Planning estimate for a slow, heavy cargo rover.",
        "data_type": "Scenario estimate; not a built rover",
        "source_url": None,
        "battery_kwh": 300.0, "drive_kwh_per_km": 0.68, "speed_kmh": 4.0,
        "solar_kw": 4.0, "max_payload_kg": 1500.0, "reserve_fraction": 0.15,
    },
]


def lunar_distance_km(lat_a: float, lon_a: float, lat_b: float, lon_b: float) -> float:
    """Great-circle distance on a spherical Moon; routes can add waypoint legs."""
    lat1, lat2 = radians(lat_a), radians(lat_b)
    dlat, dlon = lat2 - lat1, radians(lon_b - lon_a)
    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    return 2 * MOON_RADIUS_KM * asin(min(1.0, sqrt(a)))


def _sun_factor(at, sun_points, light_windows, sun_times):
    if sun_points:
        index = bisect_right(sun_times, at) - 1
        if index >= 0:
            elevation = sun_points[index].get("elevation_deg")
            if elevation is not None:
                from math import radians, sin
                return max(0.0, sin(radians(elevation)))
    return 1.0 if any(start <= at < end for start, end in light_windows) else 0.0

def simulate_rover(rover, route_legs, tasks, departure_time, light_windows,
                   sun_points=None, return_trip=False, start_charge_kwh=None):
    """Run each route leg and task in order; record a point after each hour."""
    capacity = rover["battery_kwh"]
    charge = capacity if start_charge_kwh is None else min(capacity, max(0.0, start_charge_kwh))
    reserve = capacity * rover["reserve_fraction"]
    time = departure_time
    timeline = [{"time": time, "battery_kwh": round(charge, 2), "phase": "Start"}]
    route_distance = sum(leg["distance_km"] for leg in route_legs)
    payload = sum(task.get("payload_kg", 0.0) for task in tasks)
    work_energy = sum(task["power_kw"] * task["duration_hours"] for task in tasks)
    trips = [(leg, "Travel") for leg in route_legs]
    if return_trip:
        trips.extend((leg, "Return") for leg in reversed(route_legs))
    travel_hours = sum(leg["distance_km"] / rover["speed_kmh"] for leg, _ in trips)
    drive_energy = sum(leg["distance_km"] * rover["drive_kwh_per_km"] for leg, _ in trips)
    solar_energy = 0.0
    unmet_energy = 0.0
    failed_phase = None
    sun_times = [point["time"] for point in sun_points or []]

    # shortcut: travel has no solar gain and uses straight-line waypoint legs; upgrade when a lunar terrain grid is added.
    segments = [
        (leg["distance_km"] / rover["speed_kmh"], rover["drive_kwh_per_km"] * rover["speed_kmh"], phase, False)
        for leg, phase in trips
    ]
    segments.extend((task["duration_hours"], task["power_kw"], task["name"], True) for task in tasks)
    for duration, load_kw, phase, can_charge in segments:
        remaining = duration
        while remaining > 1e-9:
            step = min(1.0, remaining)
            midpoint = time + timedelta(hours=step / 2)
            generation_kw = rover["solar_kw"] * _sun_factor(midpoint, sun_points or [], light_windows, sun_times) if can_charge else 0.0
            solar_energy += generation_kw * step
            net_energy = (load_kw - generation_kw) * step
            if net_energy > charge:
                unmet_energy += net_energy - charge
                charge = 0.0
                failed_phase = phase
            else:
                charge = min(capacity, charge - net_energy)
            time += timedelta(hours=step)
            remaining -= step
            timeline.append({"time": time, "battery_kwh": round(charge, 2), "phase": phase})
            if failed_phase:
                break
        if failed_phase:
            break

    payload_ok = payload <= rover["max_payload_kg"]
    power_ok = unmet_energy <= 1e-9 and charge >= reserve
    success = payload_ok and power_ok
    if not payload_ok:
        reason = f"Payload is {payload:.0f} kg; this rover carries up to {rover['max_payload_kg']:.0f} kg."
    elif unmet_energy > 1e-9:
        reason = f"Runs out of power during {failed_phase.lower()} (short by {unmet_energy:.1f} kWh)."
    elif charge < reserve:
        reason = f"Finishes below the {rover['reserve_fraction']:.0%} battery reserve."
    else:
        reason = f"Finishes with {charge:.1f} kWh, above the {reserve:.1f} kWh reserve."
    return {
        "rover_id": rover["id"], "rover_name": rover["name"], "success": success,
        "reason": reason, "data_type": rover["data_type"], "source_url": rover["source_url"],
        "battery_capacity_kwh": capacity, "start_charge_kwh": round(capacity if start_charge_kwh is None else start_charge_kwh, 2),
        "end_charge_kwh": round(charge, 2), "reserve_kwh": round(reserve, 2),
        "max_payload_kg": rover["max_payload_kg"], "payload_kg": round(payload, 2),
        "drive_energy_kwh": round(drive_energy, 2), "work_energy_kwh": round(work_energy, 2),
        "solar_energy_kwh": round(solar_energy, 2), "travel_hours": round(travel_hours, 2),
        "mission_hours": round((time - departure_time).total_seconds() / 3600, 2),
        "distance_km": round(route_distance * (2 if return_trip else 1), 2), "timeline": timeline,
    }


def suggest_task_order(rover_profiles, route_legs, tasks, departure_time,
                       light_windows, sun_points=None, return_trip=False,
                       start_charges=None):
    """Choose the order that lets the most rover profiles finish with the most charge."""
    best_order, best_score = list(tasks), None
    for candidate in permutations(tasks):
        results = [simulate_rover(
            rover, route_legs, candidate, departure_time, light_windows,
            sun_points, return_trip, (start_charges or {}).get(rover["id"]),
        ) for rover in rover_profiles]
        score = (sum(result["success"] for result in results),
                 sum(result["end_charge_kwh"] for result in results))
        if best_score is None or score > best_score:
            best_order, best_score = list(candidate), score
    return best_order
