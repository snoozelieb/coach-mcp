"""Weather tools: the forecast the planner reads, and the athlete's location.

get_weather_outlook — wind, gusts, rain and daylight for the coming days at
the profile location (Open-Meteo, cached for an hour in the data dir).
set_location — stores personal.location once, from a place name (geocoded)
or coordinates.
"""
import logging
from datetime import datetime

from .. import storage, weather
from ..config import ATHLETE_FILE, DATA_DIR
from ..mcp_app import mcp

logger = logging.getLogger(__name__)


@mcp.tool(annotations={
    'readOnlyHint': False,     # caches the fetched forecast in the data dir
    'destructiveHint': False,
    'idempotentHint': True,
    'openWorldHint': True,     # Open-Meteo
})
def get_weather_outlook(days: int = 7, force_refresh: bool = False) -> dict:
    """Wind, gusts, rain and daylight for the coming days at the athlete's
    profile location — the facts for placing outdoor work in the week.

    Per day: weather word, sunrise/sunset and daylight hours, max wind and
    max gust (km/h) with its hour and direction, the max gust inside
    daylight, rain (mm, max probability), gust_over_ride_limit against the
    athlete's limit (methodology weather.outdoor_ride_gust_limit_kmh,
    default 50), the longest calm daylight block (gusts under the limit),
    and one summary per preferred training window
    (life_constraints.preferred_training_times): max gust, rain, and
    whether the window is in daylight. Plus calmest_day and windiest_day.

    Source: Open-Meteo (no key; attribution in the result), local time at
    the location, up to 16 days; cached for 60 minutes (force_refresh
    bypasses). status 'no_location' means set_location has not run;
    'unavailable' means no forecast could be fetched and none is cached;
    'stale: true' means the cache stood in for a failed fetch. Running is
    fine in rain and wind — the limit is for outdoor rides; the doctrine
    resource carries the planning rules.

    Args:
        days: how many days from today to return (1-16, default 7).
        force_refresh: fetch again even when the hourly cache is fresh.

    Returns:
        {'status', 'location', 'fetched_at', 'stale', 'limits', 'days',
        'calmest_day', 'windiest_day', 'source'} — or {'status':
        'no_location', 'hint'} / {'status': 'unavailable', 'error'}.
    """
    try:
        now = datetime.now()
        return weather.outlook(now.date(), now, days=days, force_refresh=force_refresh)
    except Exception as e:
        logger.exception("get_weather_outlook failed")
        return {'error': str(e)}


@mcp.tool(annotations={
    'readOnlyHint': False,
    'destructiveHint': False,
    'idempotentHint': True,    # keyed write: personal.location
    'openWorldHint': True,     # geocoding when a place name is given
})
def set_location(place: str | None = None, latitude: float | None = None,
                 longitude: float | None = None, label: str | None = None) -> dict:
    """Store the athlete's home location on the profile (personal.location)
    for the weather outlook. Give a place name ('Stellenbosch, South
    Africa' — geocoded by Open-Meteo; the best match is stored and the other
    matches returned so the athlete can correct it) or latitude and
    longitude. Repeat the call to change it. Nothing else on the profile
    changes.

    Args:
        place: a town or suburb name, ideally with the country.
        latitude: decimal degrees, -90..90 (with longitude; overrides place).
        longitude: decimal degrees, -180..180.
        label: how the coach should name the place (defaults to the geocoded name).

    Returns:
        {'status': 'success', 'location': {latitude, longitude, label, timezone},
        'other_matches': [...]} or {'error': ...}.
    """
    try:
        candidates: list[dict] = []
        timezone = None
        if latitude is None or longitude is None:
            if not place or not str(place).strip():
                return {'error': "give place='<town, country>' or both latitude and longitude"}
            candidates = weather.geocode_place(str(place).strip())
            if not candidates:
                return {'error': f"no place matched {place!r}; try 'town, country' or pass latitude and longitude"}
            best = candidates[0]
            latitude, longitude = best['latitude'], best['longitude']
            timezone = best.get('timezone')
            if not label:
                label = ', '.join(p for p in (best.get('name'), best.get('admin1'), best.get('country')) if p)
        lat, lon = float(latitude), float(longitude)
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return {'error': 'latitude must be within -90..90 and longitude within -180..180'}
        athlete = storage.read_json(ATHLETE_FILE, data_dir=DATA_DIR) or {}
        location = {'latitude': round(lat, 4), 'longitude': round(lon, 4)}
        if label:
            location['label'] = str(label).strip()[:80]
        if timezone:
            location['timezone'] = timezone
        athlete.setdefault('personal', {})['location'] = location
        storage.write_json(ATHLETE_FILE, athlete, data_dir=DATA_DIR)
        result = {'status': 'success', 'location': location,
                  'message': 'Location stored on the profile; the weather outlook uses it from the next call.'}
        if len(candidates) > 1:
            result['other_matches'] = candidates[1:]
        return result
    except Exception as e:
        logger.exception("set_location failed")
        return {'error': str(e)}
