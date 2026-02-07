"""Data tools - Garmin data fetching (personal records, activities, daily metrics)."""

from mcp_app import mcp
from garmin_client import garmin_api_call
from parsers import (parse_resting_heart_rate, parse_sleep_score, parse_body_battery,
                     parse_activities, parse_personal_records)
from datetime import date
import json


@mcp.tool()
def get_personal_records() -> str:
    """
    Fetches personal records (PBs) from Garmin.

    Returns JSON array of records with: record_type, value, value_formatted,
    unit, date, and activity_id.
    """
    try:
        pr_data = garmin_api_call(lambda c: c.get_personal_record())
        parsed = parse_personal_records(pr_data)

        return json.dumps(parsed, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
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
        if end_date is None:
            end_date = date.today().isoformat()

        activities = garmin_api_call(lambda c: c.get_activities_by_date(start_date, end_date))
        parsed = parse_activities(activities)

        return json.dumps(parsed, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_daily_metrics() -> str:
    """
    Fetches today's critical recovery metrics from Garmin.

    Returns JSON with:
        - date: Today's date
        - rhr: Resting heart rate in bpm
        - body_battery: Current body battery (0-100)
        - sleep_score: Sleep quality score
    """
    try:
        today = date.today().isoformat()

        stats = garmin_api_call(lambda c: c.get_user_summary(today))
        body_battery = garmin_api_call(lambda c: c.get_body_battery(today))

        rhr = parse_resting_heart_rate(stats)
        sleep_score = parse_sleep_score(stats)
        current_bb = parse_body_battery(body_battery)

        return json.dumps({
            "date": today,
            "rhr": rhr,
            "body_battery": current_bb,
            "sleep_score": sleep_score
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})
