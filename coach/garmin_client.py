import os
import logging
import time
from datetime import date
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from dotenv import load_dotenv

from .config import TOKEN_DIR, GARMIN_RATE_LIMIT_WAIT_SECS

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Cache the client instance to avoid repeated logins
_cached_client: Garmin = None


def get_garmin_client(force_refresh: bool = False) -> Garmin:
    """
    Authenticates with Garmin using the new 'Garth' backend.
    Returns an authenticated Garmin client.

    Uses a cached instance to avoid repeated logins within the same session.

    Args:
        force_refresh: If True, forces a fresh login even if cached.
    """
    global _cached_client

    # Return cached client if available and not forcing refresh
    if _cached_client is not None and not force_refresh:
        return _cached_client

    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")

    client = Garmin(email, password)

    # 1. Try to load saved tokens from the directory
    try:
        if os.path.exists(TOKEN_DIR):
            logger.info("Loading saved session...")
            client.garth.load(TOKEN_DIR)

            # Verify the session is valid by trying a lightweight call
            client.get_user_summary(date.today().isoformat())
            logger.info("Session loaded successfully!")
            _cached_client = client
            return client
    except Exception as e:
        logger.warning(f"Session invalid or expired: {e}. Logging in fresh...")

    # 2. If load failed (or no tokens), perform a full login
    try:
        client.login()

        # 3. Save the new tokens for next time
        if not os.path.exists(TOKEN_DIR):
            os.makedirs(TOKEN_DIR)

        client.garth.dump(TOKEN_DIR)
        logger.info("New session saved.")

    except Exception as e:
        logger.error(f"Login failed! Check credentials. Error: {e}")
        raise

    _cached_client = client
    return client


def garmin_api_call(fn, *args, **kwargs):
    """
    Execute a Garmin API call with automatic auth retry and rate-limit handling.

    Wraps any function that takes a Garmin client as its first argument.
    On GarminConnectAuthenticationError (expired session), invalidates the
    cached client and retries once with a fresh login.
    On GarminConnectTooManyRequestsError (429), waits and retries once.
    All other exceptions propagate immediately.

    Args:
        fn: Callable that receives a Garmin client as first arg, e.g.
            garmin_api_call(lambda c: c.get_activities_by_date(start, end))
        *args, **kwargs: Additional positional/keyword args forwarded to fn
            after the client.

    Returns:
        The return value of fn(client, *args, **kwargs).
    """
    client = get_garmin_client()
    try:
        return fn(client, *args, **kwargs)
    except GarminConnectAuthenticationError:
        logger.warning("Auth error — refreshing session and retrying...")
        client = get_garmin_client(force_refresh=True)
        return fn(client, *args, **kwargs)
    except GarminConnectTooManyRequestsError:
        logger.warning(
            f"Rate limited (429) — waiting {GARMIN_RATE_LIMIT_WAIT_SECS}s and retrying..."
        )
        time.sleep(GARMIN_RATE_LIMIT_WAIT_SECS)
        return fn(client, *args, **kwargs)


def fetch_activity_hr_zones(activities: list[dict]) -> list[dict]:
    """Enrich parsed activities with hr_time_in_zones from Garmin API.

    Calls get_activity_hr_in_timezones() per activity and attaches the parsed
    result as activity['hr_time_in_zones']. Failures are logged and skipped
    (the activity falls back to avg HR classification in intensity distribution).

    Args:
        activities: List of parsed activity dicts (must have 'activity_id').

    Returns:
        The same list with hr_time_in_zones added where available.
    """
    from .parsers import parse_hr_time_in_zones

    for activity in activities:
        activity_id = activity.get('activity_id')
        if not activity_id:
            continue
        try:
            raw = garmin_api_call(
                lambda c, aid=activity_id: c.get_activity_hr_in_timezones(aid)
            )
            zones = parse_hr_time_in_zones(raw)
            if zones:
                activity['hr_time_in_zones'] = zones
        except Exception:
            logger.warning(
                "Failed to fetch HR zones for activity %s", activity_id,
                exc_info=True,
            )
    return activities


def schedule_workout(workout_id: int, schedule_date: str) -> dict:
    """
    Schedule a workout to a specific date on Garmin Connect calendar.

    Args:
        workout_id: ID of the workout to schedule (from upload_workout)
        schedule_date: Date to schedule (YYYY-MM-DD format)

    Returns:
        API response dict
    """
    def _schedule(client):
        url = f"{client.garmin_workouts_schedule_url}/{workout_id}"
        return client.garth.post("connectapi", url, json={"date": schedule_date})

    return garmin_api_call(_schedule)