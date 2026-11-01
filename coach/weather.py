"""Weather for planning: wind, gusts, rain and daylight from Open-Meteo.

Facts only. The location comes from the athlete profile
(``personal.location``, set with the ``set_location`` tool), the training
windows from ``life_constraints.preferred_training_times``, and the limits
from ``methodology.json`` (``weather`` block) over ``WEATHER_DEFAULTS``.
What to do with a windy day is coaching judgment and lives in the doctrine
resource and the sport skills, not here.

No wall-clock reads in this module: ``today`` and ``now`` are threaded in
from the tool boundary (tests/test_clock_discipline.py).

Source: Open-Meteo (https://open-meteo.com), free for non-commercial use,
no API key, attribution required (CC BY 4.0). Verified live 2026-10-30:
hourly wind_gusts_10m / precipitation_probability and daily sunrise /
sunset / daylight_duration, up to 16 days, ``timezone=auto``.
"""
import logging
from datetime import date, datetime, timedelta

import requests

from . import storage
from .config import (
    ATHLETE_FILE,
    DATA_DIR,
    METHODOLOGY_FILE,
    WEATHER_CACHE_FILE,
    WEATHER_CACHE_TTL_MINUTES,
    WEATHER_DEFAULTS,
    WEATHER_FORECAST_URL,
    WEATHER_GEOCODING_URL,
    WEATHER_HTTP_TIMEOUT_SECONDS,
    WEATHER_MAX_FORECAST_DAYS,
)
from .taxonomy import workout_family_for
from .workout_builder import is_indoor_cycling

logger = logging.getLogger(__name__)

ATTRIBUTION = "Weather data by Open-Meteo.com (CC BY 4.0)"

HOURLY_VARS = ("temperature_2m", "precipitation", "precipitation_probability",
               "wind_speed_10m", "wind_gusts_10m", "wind_direction_10m", "weather_code")
DAILY_VARS = ("sunrise", "sunset", "daylight_duration", "wind_speed_10m_max",
              "wind_gusts_10m_max", "precipitation_sum", "precipitation_probability_max",
              "temperature_2m_max", "temperature_2m_min", "weather_code")

# WMO weather interpretation codes -> one word the coach can quote.
WMO_WORDS = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "rime fog",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle",
    56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain",
    66: "freezing rain", 67: "freezing rain",
    71: "light snow", 73: "snow", 75: "heavy snow", 77: "snow grains",
    80: "light showers", 81: "showers", 82: "violent showers",
    85: "snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with hail", 99: "thunderstorm with heavy hail",
}

_COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

# Fields the snapshot and week constraints carry per day (the full outlook
# adds the hourly windows and the temperatures).
SNAPSHOT_DAY_FIELDS = ("date", "weekday", "weather", "sunrise", "sunset", "daylight_hours",
                       "wind_max_kmh", "gust_max_kmh", "daylight_gust_max_kmh", "wind_direction",
                       "rain_mm", "rain_probability_max_pct", "gust_over_ride_limit",
                       "calm_daylight_block")


# ---------------------------------------------------------------------------
# Profile and methodology inputs
# ---------------------------------------------------------------------------

def weather_rules(methodology: dict | None = None) -> dict:
    """The limits: methodology.json ``weather`` over WEATHER_DEFAULTS (numbers
    only replace numbers)."""
    if methodology is None:
        methodology = storage.read_json(METHODOLOGY_FILE, data_dir=DATA_DIR) or {}
    rules = dict(WEATHER_DEFAULTS)
    block = methodology.get("weather") if isinstance(methodology, dict) else None
    if isinstance(block, dict):
        for key, value in block.items():
            if key in rules and isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                rules[key] = value
    return rules


def profile_location(athlete: dict | None = None) -> dict | None:
    """``personal.location`` as {latitude, longitude, label, timezone}, or None."""
    if athlete is None:
        athlete = storage.read_json(ATHLETE_FILE, data_dir=DATA_DIR) or {}
    location = (athlete.get("personal") or {}).get("location") if isinstance(athlete, dict) else None
    if not isinstance(location, dict):
        return None
    try:
        latitude, longitude = float(location["latitude"]), float(location["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return {"latitude": latitude, "longitude": longitude,
            "label": location.get("label"), "timezone": location.get("timezone")}


def training_windows(athlete: dict | None = None) -> list[tuple[str, str]]:
    """``life_constraints.preferred_training_times`` ('HH:MM-HH:MM' strings) as
    (start, end) pairs; malformed entries are skipped."""
    if athlete is None:
        athlete = storage.read_json(ATHLETE_FILE, data_dir=DATA_DIR) or {}
    raw = ((athlete.get("life_constraints") or {}).get("preferred_training_times")
           if isinstance(athlete, dict) else None)
    windows: list[tuple[str, str]] = []
    for item in raw or []:
        if not isinstance(item, str) or "-" not in item:
            continue
        start, _, end = item.partition("-")
        start, end = start.strip(), end.strip()
        if _minutes(start) is None or _minutes(end) is None:
            continue
        windows.append((start, end))
    return windows


# ---------------------------------------------------------------------------
# Network (the only two functions that leave the machine)
# ---------------------------------------------------------------------------

def geocode_place(place: str, timeout: float = WEATHER_HTTP_TIMEOUT_SECONDS) -> list[dict]:
    """Open-Meteo geocoding: the best matches for a place name, best first."""
    response = requests.get(WEATHER_GEOCODING_URL,
                            params={"name": place, "count": 5, "language": "en", "format": "json"},
                            timeout=timeout)
    response.raise_for_status()
    results = (response.json() or {}).get("results") or []
    matches = []
    for entry in results:
        if entry.get("latitude") is None or entry.get("longitude") is None:
            continue
        matches.append({
            "name": entry.get("name"), "admin1": entry.get("admin1"), "country": entry.get("country"),
            "latitude": float(entry["latitude"]), "longitude": float(entry["longitude"]),
            "timezone": entry.get("timezone"),
        })
    return matches


def fetch_forecast(latitude: float, longitude: float, days: int = WEATHER_MAX_FORECAST_DAYS,
                   timeout: float = WEATHER_HTTP_TIMEOUT_SECONDS) -> dict:
    """The raw Open-Meteo forecast payload (hourly + daily, local time at the
    location, wind in km/h). Raises on HTTP or shape errors."""
    params = {
        "latitude": latitude, "longitude": longitude,
        "hourly": ",".join(HOURLY_VARS), "daily": ",".join(DAILY_VARS),
        "forecast_days": max(1, min(int(days), WEATHER_MAX_FORECAST_DAYS)),
        "timezone": "auto", "wind_speed_unit": "kmh",
    }
    response = requests.get(WEATHER_FORECAST_URL, params=params, timeout=timeout)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("daily"), dict):
        raise ValueError(f"unexpected Open-Meteo payload: {str(payload)[:200]}")
    return payload


# ---------------------------------------------------------------------------
# Parsing (pure)
# ---------------------------------------------------------------------------

def _minutes(hhmm: str | None) -> int | None:
    if not hhmm or len(hhmm) < 4 or ":" not in hhmm:
        return None
    hours, _, mins = hhmm.partition(":")
    try:
        hours, mins = int(hours), int(mins)
    except ValueError:
        return None
    if not (0 <= hours <= 24 and 0 <= mins < 60):
        return None
    return hours * 60 + mins


def _hhmm(iso: str | None) -> str | None:
    return iso[11:16] if isinstance(iso, str) and len(iso) >= 16 else None


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _item(series: dict, key: str, index: int):
    values = series.get(key)
    if isinstance(values, list) and index < len(values):
        return values[index]
    return None


def _compass(degrees: float | None) -> str | None:
    if degrees is None:
        return None
    return _COMPASS[int((degrees % 360) / 45 + 0.5) % 8]


def _overlap_minutes(start: str, end: str, sunrise: str | None, sunset: str | None) -> int:
    a, b, c, d = _minutes(start), _minutes(end), _minutes(sunrise), _minutes(sunset)
    if None in (a, b, c, d):
        return 0
    return max(0, min(b, d) - max(a, c))


def _longest_calm_block(hours: list[dict], limit: float) -> dict | None:
    """The longest run of consecutive daylight hours with gusts under the
    limit: {'start', 'end', 'hours'} or None."""
    best, run = None, []
    for hour in hours + [None]:
        if hour is not None and hour["gust"] is not None and hour["gust"] < limit:
            run.append(hour)
            continue
        if run and (best is None or len(run) > best["hours"]):
            last = _minutes(run[-1]["time"]) or 0
            end = min(last + 60, 24 * 60)
            best = {"start": run[0]["time"], "end": f"{end // 60:02d}:{end % 60:02d}", "hours": len(run)}
        run = []
    return best


def _window_summary(window: tuple[str, str], hours: list[dict], sunrise: str | None, sunset: str | None) -> dict:
    start, end = window
    inside = [h for h in hours if start <= h["time"] < end]
    gusts = [h["gust"] for h in inside if h["gust"] is not None]
    probs = [h["prob"] for h in inside if h["prob"] is not None]
    rain = [h["rain"] for h in inside if h["rain"] is not None]
    length = max(0, (_minutes(end) or 0) - (_minutes(start) or 0))
    daylight = _overlap_minutes(start, end, sunrise, sunset)
    light = "dark" if daylight == 0 else ("daylight" if daylight >= length else "partly")
    return {
        "window": f"{start}-{end}",
        "gust_max_kmh": max(gusts) if gusts else None,
        "rain_probability_max_pct": max(probs) if probs else None,
        "rain_mm": round(sum(rain), 1) if rain else None,
        "daylight_minutes": daylight,
        "light": light,
    }


def parse_forecast(payload: dict, today: date, windows: list[tuple[str, str]], rules: dict) -> dict:
    """Per-day planning facts from a raw Open-Meteo payload, today onward.

    Pure: no I/O, no clock. ``rules['outdoor_ride_gust_limit_kmh']`` only
    labels the days (gust_over_ride_limit, calm_daylight_block); nothing here
    decides what the athlete does.
    """
    daily = payload.get("daily") or {}
    hourly = payload.get("hourly") or {}
    limit = float(rules.get("outdoor_ride_gust_limit_kmh", WEATHER_DEFAULTS["outdoor_ride_gust_limit_kmh"]))

    hours_by_date: dict[str, list[dict]] = {}
    for i, stamp in enumerate(hourly.get("time") or []):
        if not isinstance(stamp, str) or len(stamp) < 16:
            continue
        hours_by_date.setdefault(stamp[:10], []).append({
            "time": stamp[11:16],
            "gust": _num(_item(hourly, "wind_gusts_10m", i)),
            "wind": _num(_item(hourly, "wind_speed_10m", i)),
            "direction": _num(_item(hourly, "wind_direction_10m", i)),
            "rain": _num(_item(hourly, "precipitation", i)),
            "prob": _num(_item(hourly, "precipitation_probability", i)),
            "temp": _num(_item(hourly, "temperature_2m", i)),
        })

    days = []
    for i, day_str in enumerate(daily.get("time") or []):
        try:
            day_date = date.fromisoformat(str(day_str))
        except ValueError:
            continue
        if day_date < today:
            continue
        sunrise, sunset = _hhmm(_item(daily, "sunrise", i)), _hhmm(_item(daily, "sunset", i))
        hours = hours_by_date.get(day_str, [])
        daylight_hours = [h for h in hours if sunrise and sunset and sunrise <= h["time"] < sunset]
        daylight_gusts = [h["gust"] for h in daylight_hours if h["gust"] is not None]
        gust_max = _num(_item(daily, "wind_gusts_10m_max", i))
        if gust_max is None:
            all_gusts = [h["gust"] for h in hours if h["gust"] is not None]
            gust_max = max(all_gusts) if all_gusts else None
        peak = max((h for h in hours if h["gust"] is not None), key=lambda h: h["gust"], default=None)
        daylight_gust_max = max(daylight_gusts) if daylight_gusts else None
        reference_gust = daylight_gust_max if daylight_gust_max is not None else gust_max
        duration = _num(_item(daily, "daylight_duration", i))
        code = _item(daily, "weather_code", i)
        days.append({
            "date": day_str,
            "weekday": day_date.strftime("%A"),
            "weather": WMO_WORDS.get(int(code)) if isinstance(code, (int, float)) else None,
            "sunrise": sunrise,
            "sunset": sunset,
            "daylight_hours": round(duration / 3600, 1) if duration is not None else None,
            "temp_min_c": _num(_item(daily, "temperature_2m_min", i)),
            "temp_max_c": _num(_item(daily, "temperature_2m_max", i)),
            "wind_max_kmh": _num(_item(daily, "wind_speed_10m_max", i)),
            "gust_max_kmh": gust_max,
            "gust_max_hour": peak["time"] if peak else None,
            "wind_direction": _compass(peak["direction"]) if peak else None,
            "daylight_gust_max_kmh": daylight_gust_max,
            "rain_mm": _num(_item(daily, "precipitation_sum", i)),
            "rain_probability_max_pct": _num(_item(daily, "precipitation_probability_max", i)),
            "gust_over_ride_limit": (reference_gust is not None and reference_gust > limit),
            "calm_daylight_block": _longest_calm_block(daylight_hours, limit),
            "windows": [_window_summary(w, hours, sunrise, sunset) for w in windows],
        })

    def _ref(day: dict) -> float:
        value = day["daylight_gust_max_kmh"] if day["daylight_gust_max_kmh"] is not None else day["gust_max_kmh"]
        return value if value is not None else float("inf")

    calmest = min(days, key=_ref) if days else None
    windiest = max(days, key=_ref) if days else None
    return {
        "timezone": payload.get("timezone"),
        "limits": {"outdoor_ride_gust_limit_kmh": limit},
        "days": days,
        "calmest_day": calmest["date"] if calmest else None,
        "windiest_day": windiest["date"] if windiest else None,
    }


# ---------------------------------------------------------------------------
# The outlook: cache -> fetch -> parse
# ---------------------------------------------------------------------------

def _usable_cache(cache: dict, location: dict, now: datetime, force_refresh: bool) -> tuple[dict | None, str | None, bool]:
    """(payload, fetched_at, stale) from the cache file for this location."""
    if not isinstance(cache, dict) or not isinstance(cache.get("payload"), dict):
        return None, None, True
    try:
        same_place = (abs(float(cache.get("latitude")) - location["latitude"]) < 1e-3
                      and abs(float(cache.get("longitude")) - location["longitude"]) < 1e-3)
    except (TypeError, ValueError):
        same_place = False
    if not same_place:
        return None, None, True
    fetched_at = cache.get("fetched_at")
    stale = True
    if not force_refresh and isinstance(fetched_at, str):
        try:
            age = now - datetime.fromisoformat(fetched_at)
            stale = age < timedelta(0) or age > timedelta(minutes=WEATHER_CACHE_TTL_MINUTES)
        except ValueError:
            stale = True
    return cache["payload"], fetched_at if isinstance(fetched_at, str) else None, stale


def outlook(today: date, now: datetime, days: int = 7, force_refresh: bool = False,
            allow_fetch: bool = True, timeout: float = WEATHER_HTTP_TIMEOUT_SECONDS) -> dict:
    """The planning outlook for the profile location.

    status 'ok' carries location, fetched_at, stale, limits, days (today
    onward, at most ``days``), calmest_day, windiest_day and the source
    attribution; 'no_location' names the fix; 'unavailable' carries the
    fetch error when nothing is cached. A fetch failure with a cached
    forecast returns the cache with ``stale: true`` and ``fetch_error``.
    """
    athlete = storage.read_json(ATHLETE_FILE, data_dir=DATA_DIR) or {}
    location = profile_location(athlete)
    if location is None:
        return {"status": "no_location",
                "hint": ("No location on the profile. Call set_location(place='<town, country>') "
                         "or set_location(latitude=.., longitude=..) once; the outlook uses it from then on.")}
    rules = weather_rules()
    windows = training_windows(athlete)
    days = max(1, min(int(days or 7), WEATHER_MAX_FORECAST_DAYS))

    cache = storage.read_json(WEATHER_CACHE_FILE, data_dir=DATA_DIR) or {}
    payload, fetched_at, stale = _usable_cache(cache, location, now, force_refresh)
    fetch_error = None
    if (payload is None or stale) and allow_fetch:
        try:
            fresh = fetch_forecast(location["latitude"], location["longitude"],
                                   WEATHER_MAX_FORECAST_DAYS, timeout)
        except Exception as exc:  # network, HTTP, shape — the cache (if any) stands in
            logger.warning("Weather fetch failed: %s", exc)
            fetch_error = f"{exc.__class__.__name__}: {exc}"
        else:
            payload, stale = fresh, False
            fetched_at = now.isoformat(timespec="minutes")
            storage.write_json(WEATHER_CACHE_FILE, {
                "fetched_at": fetched_at, "latitude": location["latitude"],
                "longitude": location["longitude"], "payload": payload,
            }, data_dir=DATA_DIR)
    if payload is None:
        return {"status": "unavailable", "location": location,
                "error": fetch_error or "no forecast cached and fetching is off"}

    parsed = parse_forecast(payload, today, windows, rules)
    parsed["days"] = parsed["days"][:days]
    result = {"status": "ok", "location": location, "fetched_at": fetched_at,
              "stale": bool(stale), "source": ATTRIBUTION, **parsed}
    if fetch_error:
        result["fetch_error"] = fetch_error
    return result


def snapshot_block(today: date, now: datetime, days: int = 7) -> dict:
    """The compact outlook the snapshot and get_week_constraints carry. Never raises."""
    try:
        full = outlook(today, now, days=days)
    except Exception as exc:
        logger.warning("Weather outlook failed: %s", exc, exc_info=True)
        return {"status": "unavailable", "error": f"{exc.__class__.__name__}: {exc}"}
    if full.get("status") != "ok":
        return full
    location = full["location"]
    return {
        "status": "ok",
        "location": location.get("label") or f"{location['latitude']:.3f},{location['longitude']:.3f}",
        "fetched_at": full.get("fetched_at"),
        "stale": full.get("stale"),
        "limits": full.get("limits"),
        "calmest_day": full.get("calmest_day"),
        "windiest_day": full.get("windiest_day"),
        "days": [{k: day.get(k) for k in SNAPSHOT_DAY_FIELDS} for day in full.get("days", [])],
        "source": ATTRIBUTION,
        "hint": "get_weather_outlook(days) adds the hourly training windows and temperatures.",
    }


# ---------------------------------------------------------------------------
# Grounding: planned outdoor rides against the forecast
# ---------------------------------------------------------------------------

def _planned_sessions(day: dict) -> list[dict]:
    planned = day.get("planned") if isinstance(day, dict) else None
    if isinstance(planned, list):
        return [s for s in planned if isinstance(s, dict)]
    return [planned] if isinstance(planned, dict) else []


def grounding_warnings(plan_days: dict, today: date, now: datetime) -> list[dict]:
    """Outdoor rides planned on days whose daylight gusts exceed the limit.

    Warn-only, like the capacity grounding: empty without a location, on
    any failure, and for indoor rides and runs (running is fine in wind).
    """
    try:
        block = outlook(today, now, days=WEATHER_MAX_FORECAST_DAYS)
    except Exception:
        logger.warning("Weather grounding skipped", exc_info=True)
        return []
    if block.get("status") != "ok":
        return []
    by_date = {day["date"]: day for day in block.get("days", [])}
    limit = block["limits"]["outdoor_ride_gust_limit_kmh"]
    warnings = []
    for day_str, day in sorted((plan_days or {}).items()):
        forecast = by_date.get(day_str)
        if not forecast:
            continue
        gust = forecast.get("daylight_gust_max_kmh")
        if gust is None:
            gust = forecast.get("gust_max_kmh")
        if gust is None or gust <= limit:
            continue
        for session in _planned_sessions(day):
            session_type = str(session.get("type") or "")
            if workout_family_for(session_type.lower()) != "cycling" or is_indoor_cycling(session):
                continue
            name = session.get("name") or session.get("description") or session_type
            calm = forecast.get("calm_daylight_block")
            calm_text = (f"the calm daylight block {calm['start']}-{calm['end']} ({calm['hours']} h)"
                         if calm else "no calm daylight block that day")
            warnings.append({
                "type": "wind_over_ride_limit",
                "sport": "cycling",
                "date": day_str,
                "session": name,
                "planned_mins": session.get("duration_mins"),
                "gust_max_kmh": gust,
                "limit_kmh": limit,
                "calm_daylight_block": calm,
                "calmest_day": block.get("calmest_day"),
                "message": (
                    f"{day_str}: forecast gusts of {gust:.0f} km/h in daylight exceed the "
                    f"{limit:.0f} km/h outdoor-ride limit for '{name}'. Make it an indoor "
                    f"session with the same purpose, fit it into {calm_text}, or move it to "
                    f"the calmest day ({block.get('calmest_day')})."),
            })
    return warnings
