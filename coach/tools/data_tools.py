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
from datetime import date
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


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
def get_activities_range(start_date: str, end_date: str = None) -> str:
    """
    Fetches activities between two dates from Garmin.

    Args:
        start_date: Start date in YYYY-MM-DD format
        end_date: End date in YYYY-MM-DD format (defaults to today)

    Returns:
        JSON array of activities with: date, name, type, duration_mins,
        distance_km, avg_hr, max_hr, calories, and pace for runs.
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

        activities = garmin_api_call(lambda c: c.get_activities_by_date(start_date, end_date))
        parsed = parse_activities(activities)

        return json.dumps(parsed, indent=2)

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
