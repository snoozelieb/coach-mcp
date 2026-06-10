"""
Garmin Connect client management (garminconnect 0.3.5, native auth).

Auth flow — no garth, no playwright, no browser:

1. **Token-first**: restore the saved session from ``TOKEN_DIR``
   (``Garmin().login(tokenstore=...)``). This is the only path that should
   run in steady state and needs no credentials.
2. **On token failure**: ONE non-interactive credential login
   (``return_on_mfa=True``). If Garmin asks for MFA there is nobody to type
   the code, so it is treated as auth-required — never block on input.
3. **Any credential-login failure** raises :class:`GarminAuthRequiredError`
   (its ``str()`` is the exact remediation message every tool surfaces via
   ``{'error': str(e)}``) and arms a process-level latch: for
   ``AUTH_LATCH_SECS`` every further ``get_garmin_client()`` call fails fast
   instead of hammering Garmin's SSO. One expired session must not trigger
   N login attempts during a snapshot.

Recovery: ``python scripts/garmin_login.py`` — interactive credential login
with MFA prompt; persists tokens to the same ``TOKEN_DIR`` this module reads.
"""
import logging
import os
import time

from dotenv import load_dotenv
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from .config import TOKEN_DIR, GARMIN_RATE_LIMIT_WAIT_SECS

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

AUTH_REQUIRED_MESSAGE = (
    "AUTH_REQUIRED: Garmin session expired or needs MFA. "
    "Run: python scripts/garmin_login.py"
)

# After a failed credential login, fail fast for this long instead of
# re-attempting a fresh login on every tool call.
AUTH_LATCH_SECS = 600


class GarminAuthRequiredError(Exception):
    """Garmin auth needs human intervention (fresh login and/or MFA).

    ``str()`` of this error is the exact actionable message that every
    tool's ``{'error': str(e)}`` surfaces to the athlete/coach.
    """

    def __init__(self, message: str = AUTH_REQUIRED_MESSAGE) -> None:
        super().__init__(message)


# Cache the client instance to avoid repeated logins
_cached_client: Garmin | None = None

# Monotonic timestamp of the last failed credential login (auth latch).
_auth_failed_at: float | None = None


def _auth_latch_active() -> bool:
    """True while the post-failure fail-fast window is in effect."""
    return (
        _auth_failed_at is not None
        and (time.monotonic() - _auth_failed_at) < AUTH_LATCH_SECS
    )


def _arm_auth_latch() -> None:
    global _auth_failed_at
    _auth_failed_at = time.monotonic()


def _clear_auth_latch() -> None:
    global _auth_failed_at
    _auth_failed_at = None


def _try_token_login() -> Garmin | None:
    """Restore a session from saved tokens. Returns None on any failure.

    Uses a credential-less ``Garmin()`` so this phase can ONLY load tokens —
    garminconnect's internal token-failure fallback to a credential login
    cannot fire without a username/password, which keeps the token phase and
    the single credential attempt strictly separated.
    """
    if not os.path.exists(TOKEN_DIR):
        logger.info("No saved Garmin session at %s", TOKEN_DIR)
        return None
    client = Garmin()
    try:
        logger.info("Loading saved Garmin session...")
        client.login(tokenstore=TOKEN_DIR)
    except Exception as e:
        logger.warning("Saved Garmin session invalid or expired: %s", e)
        return None
    logger.info("Garmin session restored from saved tokens.")
    return client


def _populate_profile(client: Garmin) -> None:
    """Populate display_name/full_name/unit_system after a headless login.

    ``Garmin.login()`` loads these itself, but the ``return_on_mfa=True``
    credential path returns early and skips them — and several library
    methods interpolate ``display_name`` into request URLs. The profile
    fetch doubles as token verification: if it raises, the new token is
    unusable and the caller treats the login as failed.
    """
    prof = client.connectapi("/userprofile-service/socialProfile")
    if isinstance(prof, dict):
        client.display_name = prof.get("displayName")
        client.full_name = prof.get("fullName", "")
    try:
        settings = client.connectapi(client.garmin_connect_user_settings_url)
        if isinstance(settings, dict) and "userData" in settings:
            client.unit_system = settings["userData"].get("measurementSystem")
    except Exception:
        # Unit system is cosmetic — never fail a login over it.
        logger.warning("Could not load Garmin user settings after login",
                       exc_info=True)


def _credential_login(email: str, password: str) -> Garmin:
    """ONE non-interactive credential login.

    Raises GarminAuthRequiredError if Garmin requires MFA (headless process
    cannot answer the prompt). Any other failure propagates to the caller,
    which wraps it. On success, persists tokens to TOKEN_DIR.
    """
    logger.info("Attempting fresh Garmin credential login (non-interactive)...")
    client = Garmin(email, password, return_on_mfa=True)
    mfa_status, _ = client.client.login(email, password, return_on_mfa=True)
    if mfa_status == "needs_mfa":
        logger.warning("Garmin requires MFA — cannot continue non-interactively.")
        raise GarminAuthRequiredError()
    client.client.dump(TOKEN_DIR)
    _populate_profile(client)
    logger.info("Fresh Garmin login succeeded; tokens saved to %s", TOKEN_DIR)
    return client


def get_garmin_client(force_refresh: bool = False) -> Garmin:
    """
    Return an authenticated Garmin client (token-first, native 0.3.5 auth).

    Uses a cached instance to avoid repeated logins within the same session.

    Args:
        force_refresh: If True, ignores the cached client and re-authenticates.

    Raises:
        GarminAuthRequiredError: When authentication needs human intervention
            (expired session + failed/MFA-gated credential login), or while
            the auth latch from a recent failure is active.
    """
    global _cached_client

    if _cached_client is not None and not force_refresh:
        return _cached_client

    if _auth_latch_active():
        raise GarminAuthRequiredError()

    # Drop any stale instance while re-authenticating so a failed refresh
    # can't leave a dead client in the cache.
    _cached_client = None

    # 1. Token-first
    client = _try_token_login()
    if client is not None:
        _clear_auth_latch()
        _cached_client = client
        return client

    # 2. ONE non-interactive credential login
    email = os.getenv("GARMIN_EMAIL")
    password = os.getenv("GARMIN_PASSWORD")
    if not email or not password:
        logger.error("GARMIN_EMAIL/GARMIN_PASSWORD not set — cannot log in.")
        _arm_auth_latch()
        raise GarminAuthRequiredError()

    try:
        client = _credential_login(email, password)
    except GarminAuthRequiredError:
        _arm_auth_latch()
        raise
    except Exception as e:
        logger.exception("Garmin credential login failed")
        _arm_auth_latch()
        raise GarminAuthRequiredError() from e

    _clear_auth_latch()
    _cached_client = client
    return client


def garmin_api_call(fn, *args, **kwargs):
    """
    Execute a Garmin API call with automatic auth retry and rate-limit handling.

    Wraps any function that takes a Garmin client as its first argument.
    On GarminConnectAuthenticationError (expired session), invalidates the
    cached client and retries once with a fresh login.
    On GarminConnectTooManyRequestsError (429), waits and retries once.
    All other exceptions propagate immediately — including
    GarminAuthRequiredError when re-authentication needs the human-driven
    recovery script.

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
        return client.client.post("connectapi", url, json={"date": schedule_date})

    return garmin_api_call(_schedule)
