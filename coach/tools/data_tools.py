"""Data tools - Garmin data fetching.

Registers the standalone get_activities_range tool (explicit range semantics,
pagination candidate). The old get_personal_records / get_daily_metrics tools
were consolidated into query_metrics (fitness_tools.py) — their bodies live on
as the private `_personal_records` / `_daily_metrics` functions here so the
Garmin call sites stay in this module.
"""

from ..mcp_app import mcp
from ..garmin_client import garmin_api_call
from ..parsers import (parse_resting_heart_rate, parse_body_battery,
                     parse_activities, parse_personal_records,
                     parse_training_readiness)
from ..taxonomy import types_match
from datetime import date, timedelta
import json
import logging

logger = logging.getLogger(__name__)


def _personal_records() -> dict:
    """Fetch personal records (PBs) from Garmin.

    Returns a dict with 'personal_records': list of records with record_type,
    value, value_formatted, unit, date, and activity_id.
    """
    try:
        pr_data = garmin_api_call(lambda c: c.get_personal_record())
        parsed = parse_personal_records(pr_data)

        return {'personal_records': parsed, 'count': len(parsed)}

    except Exception as e:
        logger.exception("query_metrics(kind='personal_records') failed")
        return {"error": str(e)}


MAX_RANGE_DAYS = 92  # longer windows page: the response carries next_start


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
def get_activities_range(start_date: str, end_date: str = None,
                         activity_type: str = None) -> str:
    """
    Fetches activities between two dates from Garmin — the raw history a
    prescription must be grounded in.

    Use this (or query_metrics(kind='sport_history'), which works offline
    from local history) BEFORE designing any progression, long-session
    ladder, or multi-week program: the athlete's actual recent sessions —
    durations, distances, paces — are the only valid anchor for a starting
    point (VERIFY BEFORE PRESCRIBING). snapshot.capacity summarizes this
    data; this tool is the full per-activity record.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format (defaults to today)
        activity_type: Optional sport filter — 'running', 'cycling',
            'strength', 'swimming' or any known alias ('run', 'ride', ...).
            Taxonomy-aware: 'running' also matches trail/treadmill runs.

    Returns:
        JSON array of activities with: date, name, type, duration_mins,
        distance_km, avg_hr, max_hr, calories, and pace for runs. Windows
        longer than 92 days are paged: the response is then an object with
        'activities', 'truncated': true and 'next_start' for the next call.
    """
    try:
        # Validate date formats before hitting the API
        try:
            date.fromisoformat(start_date)
        except (ValueError, TypeError):
            return json.dumps({'error': f'Invalid start_date: {start_date}. Must be YYYY-MM-DD format.'})
        if end_date is not None:
            try:
                date.fromisoformat(end_date)
            except (ValueError, TypeError):
                return json.dumps({'error': f'Invalid end_date: {end_date}. Must be YYYY-MM-DD format.'})

        if end_date is None:
            end_date = date.today().isoformat()

        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
        truncated_end = None
        if (end - start).days > MAX_RANGE_DAYS:
            # One page at a time: a 365-day pull is 1,000+ activities of
            # JSON straight into the model's context.
            truncated_end = start + timedelta(days=MAX_RANGE_DAYS)
            end_date = truncated_end.isoformat()

        activities = garmin_api_call(lambda c: c.get_activities_by_date(start_date, end_date))
        parsed = parse_activities(activities)

        if activity_type:
            parsed = [a for a in parsed
                      if types_match(activity_type, a.get('type'))]

        if truncated_end is not None:
            return json.dumps({
                'activities': parsed,
                'truncated': True,
                'window': {'start': start_date, 'end': end_date, 'max_days': MAX_RANGE_DAYS},
                'next_start': (truncated_end + timedelta(days=1)).isoformat(),
                'note': (f'Window capped at {MAX_RANGE_DAYS} days; call again from '
                         'next_start for the rest.'),
            })
        return json.dumps(parsed)

    except Exception as e:
        logger.exception("get_activities_range failed")
        return json.dumps({"error": str(e)})


def _daily_metrics(today: date) -> dict:
    """Fetch today's critical recovery metrics from Garmin.

    today is required — the query_metrics boundary resolves it
    (clock discipline).

    Returns a dict with:
        - date: Today's date
        - rhr: Resting heart rate in bpm
        - body_battery: Current body battery (0-100)
        - sleep_score: Sleep quality score (from training readiness)
    """
    try:
        today_iso = today.isoformat()

        stats = garmin_api_call(lambda c: c.get_user_summary(today_iso))
        body_battery = garmin_api_call(lambda c: c.get_body_battery(today_iso))
        readiness = garmin_api_call(lambda c: c.get_training_readiness(today_iso))

        rhr = parse_resting_heart_rate(stats)
        current_bb = parse_body_battery(body_battery)

        readiness_parsed = parse_training_readiness(readiness)
        sleep_score = readiness_parsed.get('sleep_score')
        if sleep_score is None:
            sleep_score = 'N/A'

        return {
            "date": today_iso,
            "rhr": rhr,
            "body_battery": current_bb,
            "sleep_score": sleep_score
        }

    except Exception as e:
        logger.exception("query_metrics(kind='daily') failed")
        return {"error": str(e)}
