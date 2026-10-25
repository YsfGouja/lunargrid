"""Sun elevation forecasts from the public NASA/JPL Horizons API."""
import csv
import json
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlencode
from urllib.request import Request, urlopen

HORIZONS_URL = "https://ssd.jpl.nasa.gov/api/horizons.api"
MAX_HORIZON_HOURS = 720
_CACHE = {}
_CACHE_LOCK = threading.Lock()


def parse_horizons_result(result: str) -> list[dict]:
    """Read the time, azimuth and elevation columns from a Horizons CSV result."""
    match = re.search(r"\$\$SOE(.*?)\$\$EOE", result, re.S)
    if not match:
        raise ValueError("Horizons returned no ephemeris rows")

    points = []
    for row in csv.reader(match.group(1).splitlines()):
        if not row:
            continue
        stamp = row[0].strip('" ')
        parsed = None
        for pattern in ("%Y-%b-%d %H:%M", "%Y-%b-%d %H:%M:%S", "%Y-%b-%d %H:%M:%S.%f"):
            try:
                parsed = datetime.strptime(stamp, pattern)
                break
            except ValueError:
                continue
        if parsed is None:
            continue
        angles = []
        for value in row[1:]:
            try:
                angles.append(float(value))
            except ValueError:
                continue
        if len(angles) < 2:
            continue
        azimuth, elevation = angles[-2:]
        points.append({
            "time": parsed,
            "azimuth_deg": azimuth,
            "elevation_deg": elevation,
            "sunlit": elevation > 0,
        })
    if not points:
        raise ValueError("Horizons returned no usable Sun positions")
    return points


def _fetch(lon: float, lat: float, start: datetime, stop: datetime, step_minutes: int) -> list[dict]:
    if stop <= start:
        raise ValueError("Sunlight forecast end must be after its start")
    if (stop - start).total_seconds() > MAX_HORIZON_HOURS * 3600:
        raise ValueError(f"Live Sun forecasts are limited to {MAX_HORIZON_HOURS} hours")

    params = {
        "format": "json",
        "COMMAND": "'10'",
        "OBJ_DATA": "'NO'",
        "MAKE_EPHEM": "'YES'",
        "EPHEM_TYPE": "'OBSERVER'",
        "CENTER": "'coord@301'",
        "COORD_TYPE": "'GEODETIC'",
        "SITE_COORD": f"'{lon:.6f},{lat:.6f},0'",
        "START_TIME": f"'{start:%Y-%b-%d %H:%M}'",
        "STOP_TIME": f"'{stop:%Y-%b-%d %H:%M}'",
        "STEP_SIZE": f"'{step_minutes} m'",
        "TIME_TYPE": "'UT'",
        "QUANTITIES": "'4'",
        "ANG_FORMAT": "'DEG'",
        "CSV_FORMAT": "'YES'",
        "APPARENT": "'AIRLESS'",
    }
    request = Request(
        f"{HORIZONS_URL}?{urlencode(params)}",
        headers={"User-Agent": "LunarGrid/1.0 (lunar planning demo)"},
    )
    with urlopen(request, timeout=12) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("error"):
        raise RuntimeError(payload["error"])
    return parse_horizons_result(payload.get("result", ""))


def get_sun_positions(lon: float, lat: float, start: datetime, stop: datetime, step_minutes: int = 60) -> list[dict]:
    """Fetch and briefly cache topocentric Moon-site Sun positions."""
    if step_minutes < 1:
        raise ValueError("step_minutes must be positive")
    key = (round(lon, 6), round(lat, 6), start, stop, step_minutes)
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and cached[0] > now:
            return [point.copy() for point in cached[1]]

    points = _fetch(lon, lat, start, stop, step_minutes)
    with _CACHE_LOCK:
        if len(_CACHE) >= 128:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (now + 300, points)
    return [point.copy() for point in points]
