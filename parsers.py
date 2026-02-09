"""
Pure parsing functions for Garmin API responses.

These functions have zero MCP dependency and transform raw Garmin data
into structured formats used by the coaching tools.
"""
from collections import defaultdict
from datetime import date
from typing import Any, Union

from config import DATA_DIR
import logging

logger = logging.getLogger(__name__)


def check_setup() -> bool:
    """
    Check if required data files exist.

    Returns True if setup is complete, False if setup wizard needs to run.
    """
    required_files = [
        ("athlete.json", "Athlete profile"),
        ("training_config.json", "Training configuration"),
    ]

    missing = []
    for filename, description in required_files:
        if not (DATA_DIR / filename).exists():
            missing.append(f"  - {filename} ({description})")

    if missing:
        print("\n" + "=" * 50)
        print("  Setup Required")
        print("=" * 50)
        print("\nMissing data files:")
        print("\n".join(missing))
        print("\nRun the setup wizard to create them:")
        print("  python scripts/setup_wizard.py")
        print("\nOr create them manually in the data/ folder.")
        print("=" * 50 + "\n")
        return False

    return True


def parse_resting_heart_rate(stats: dict[str, Any]) -> Union[int, str]:
    """Extract resting heart rate from Garmin stats response."""
    return stats.get('restingHeartRate', 'N/A')


def parse_sleep_score(stats: dict[str, Any]) -> Union[int, str]:
    """Extract sleep score from Garmin stats response."""
    return stats.get('sleepScore', 'N/A')


def parse_body_battery(body_battery: list[dict[str, Any]]) -> Union[int, str]:
    """
    Extract current body battery value from Garmin response.

    Body battery response structure:
    [
        {
            'date': '2026-01-05',
            'bodyBatteryValuesArray': [[timestamp, value], ...],
            ...
        }
    ]
    """
    if not body_battery or not isinstance(body_battery, list):
        return 'N/A'

    try:
        bb_values = body_battery[0].get('bodyBatteryValuesArray', [])
        if bb_values:
            return bb_values[-1][1]  # [timestamp, value] - get value
    except (IndexError, KeyError, TypeError):
        pass

    return 'N/A'


def parse_activity(activity: dict[str, Any]) -> dict[str, Any]:
    """
    Extract relevant fields from a Garmin activity for coaching context.

    Returns simplified activity dict with:
    - date, name, type, duration_mins, distance_km
    - avg_hr, max_hr, calories
    - For runs: avg_pace
    """
    activity_type = activity.get('activityType', {})
    duration_secs = activity.get('duration', 0) or 0
    distance_m = activity.get('distance', 0) or 0

    # Moving duration (excludes stopped time)
    moving_secs = activity.get('movingDuration')

    # Cadence: sport-specific keys
    avg_cadence = (
        activity.get('averageRunningCadenceInStepsPerMinute')
        or activity.get('averageBikingCadenceInRevPerMinute')
    )

    # Event type (race vs training vs uncategorized)
    event_type_obj = activity.get('eventType', {})
    event_type = event_type_obj.get('typeKey') if isinstance(event_type_obj, dict) else None

    # Training effect
    aerobic_te = activity.get('aerobicTrainingEffect')
    anaerobic_te = activity.get('anaerobicTrainingEffect')

    parsed = {
        'activity_id': activity.get('activityId'),
        'date': activity.get('startTimeLocal', '')[:10],  # YYYY-MM-DD
        'start_time': activity.get('startTimeLocal'),
        'name': activity.get('activityName', 'Unnamed'),
        'type': activity_type.get('typeKey', 'unknown'),
        'parent_type': activity_type.get('parentTypeId'),
        'event_type': event_type,
        'description': activity.get('description'),
        'duration_mins': round(duration_secs / 60, 1),
        'moving_duration_mins': round(moving_secs / 60, 1) if moving_secs else None,
        'distance_km': round(distance_m / 1000, 2) if distance_m else None,
        'elevation_gain': activity.get('elevationGain'),
        'elevation_loss': activity.get('elevationLoss'),
        'avg_hr': activity.get('averageHR'),
        'max_hr': activity.get('maxHR'),
        'calories': activity.get('calories'),
        'avg_power': activity.get('avgPower'),
        'max_power': activity.get('maxPower'),
        'norm_power': activity.get('normPower'),
        'garmin_training_load': activity.get('activityTrainingLoad'),
        'avg_cadence': avg_cadence,
        'training_effect': {
            'aerobic': aerobic_te,
            'anaerobic': anaerobic_te,
        } if aerobic_te is not None or anaerobic_te is not None else None,
    }

    # Add pace for running activities
    if distance_m and duration_secs and 'running' in parsed['type']:
        pace_per_km = duration_secs / (distance_m / 1000) / 60  # mins per km
        parsed['avg_pace_min_km'] = round(pace_per_km, 2)

    return parsed


def parse_activities(activities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parse a list of activities into simplified format."""
    return [parse_activity(a) for a in activities]


def parse_training_readiness(readiness_data: dict[str, Any]) -> dict[str, Any]:
    """
    Parse training readiness from Garmin response.

    Returns: score, level, recovery metrics, and feedback.
    """
    if not readiness_data:
        return {'error': 'No readiness data available'}

    # Handle both single-day and list responses
    if isinstance(readiness_data, list):
        readiness_data = readiness_data[0] if readiness_data else {}

    return {
        'date': readiness_data.get('calendarDate'),
        'score': readiness_data.get('score'),
        'level': readiness_data.get('level'),  # e.g., "PRIME", "HIGH", "MODERATE", "LOW"
        'sleep_score': readiness_data.get('sleepScore'),
        'recovery_time_hrs': readiness_data.get('recoveryTimeInHours'),
        'hrv_status': readiness_data.get('hrvStatus'),
        'acute_load': readiness_data.get('acuteLoad'),
        'feedback': readiness_data.get('feedbackPhrase'),
    }


def calculate_baseline(activities: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Calculate baseline training metrics from activity history.

    Returns:
        - avg_weekly_volume_hrs: Average hours per week
        - max_weekly_volume_hrs: Peak week volume
        - activity_distribution: {type: count} breakdown
        - typical_week: {type: avg sessions per week}
    """
    if not activities:
        return {
            'avg_weekly_volume_hrs': 0,
            'max_weekly_volume_hrs': 0,
            'activity_distribution': {},
            'typical_week': {},
            'total_activities': 0,
        }

    # Group activities by week
    weeks = defaultdict(list)
    type_counts = defaultdict(int)

    for activity in activities:
        activity_date = activity.get('date', '')
        if activity_date:
            # Get ISO week number
            try:
                d = date.fromisoformat(activity_date)
                week_key = f"{d.isocalendar()[0]}-W{d.isocalendar()[1]:02d}"
                weeks[week_key].append(activity)
            except ValueError:
                pass

        # Count by type
        activity_type = activity.get('type', 'unknown')
        type_counts[activity_type] += 1

    # Calculate weekly volumes
    weekly_volumes = []
    for week_activities in weeks.values():
        total_mins = sum(a.get('duration_mins', 0) or 0 for a in week_activities)
        weekly_volumes.append(total_mins / 60)  # Convert to hours

    avg_weekly = sum(weekly_volumes) / len(weekly_volumes) if weekly_volumes else 0
    max_weekly = max(weekly_volumes) if weekly_volumes else 0

    # Calculate typical week (avg sessions per week by type)
    num_weeks = len(weeks) or 1
    typical_week = {
        activity_type: round(count / num_weeks, 1)
        for activity_type, count in type_counts.items()
    }

    return {
        'avg_weekly_volume_hrs': round(avg_weekly, 1),
        'max_weekly_volume_hrs': round(max_weekly, 1),
        'activity_distribution': dict(type_counts),
        'typical_week': typical_week,
        'total_activities': len(activities),
        'weeks_analyzed': len(weeks),
    }


def parse_personal_records(pr_data) -> list[dict[str, Any]]:
    """
    Parse personal records from Garmin response.

    Returns list of records with: record_type, value, unit, date, activity_id
    """
    if isinstance(pr_data, list):
        records = pr_data
    elif isinstance(pr_data, dict):
        records = pr_data.get('personalRecords', [])
    else:
        records = []
    parsed = []

    for record in records:
        pr_type = record.get('prTypeLabelKey') or record.get('typeKey') or 'unknown'
        value = record.get('value')

        # Format time-based records (in seconds) to readable format
        if value and pr_type and 'time' in pr_type.lower():
            mins, secs = divmod(int(value), 60)
            hours, mins = divmod(mins, 60)
            if hours:
                value_formatted = f"{hours}:{mins:02d}:{secs:02d}"
            else:
                value_formatted = f"{mins}:{secs:02d}"
        else:
            value_formatted = value

        parsed.append({
            'record_type': pr_type,
            'value': value,
            'value_formatted': value_formatted,
            'unit': record.get('unitKey'),
            'date': record.get('prStartTimeGmtFormatted', '')[:10] if record.get('prStartTimeGmtFormatted') else None,
            'activity_id': record.get('activityId'),
        })

    return parsed


def parse_user_profile(
    full_name: dict = None,
    user_profile: dict = None,
    body_composition: dict = None,
) -> dict:
    """
    Extract athlete profile data from Garmin API responses.

    Pure function — no I/O, no side effects.

    Args:
        full_name: Response from get_full_name() — typically a dict with firstName/lastName
        user_profile: Response from get_user_profile() — contains userData with birthDate etc.
        body_composition: Response from get_body_composition() — contains dateWeightList + totalAverage

    Returns:
        Dict with: full_name, display_name, weight_kg, weight_date, birth_date, age, max_hr (all nullable)
    """
    result = {
        'full_name': None,
        'display_name': None,
        'weight_kg': None,
        'weight_date': None,
        'birth_date': None,
        'age': None,
        'max_hr': None,
    }

    # Parse full name (Garmin returns a plain string or a dict)
    if full_name:
        if isinstance(full_name, str):
            name = full_name.strip()
            if name:
                result['full_name'] = name
                result['display_name'] = name
        elif isinstance(full_name, dict):
            first = full_name.get('firstName', '')
            last = full_name.get('lastName', '')
            name = f"{first} {last}".strip()
            if name:
                result['full_name'] = name
            display = full_name.get('displayName')
            if display:
                result['display_name'] = display

    # Parse weight from body composition — use most recent weigh-in from dateWeightList
    if body_composition and isinstance(body_composition, dict):
        weight_list = body_composition.get('dateWeightList', [])
        if weight_list:
            # Sort by calendarDate descending to get most recent
            sorted_entries = sorted(
                [e for e in weight_list if e.get('weight') and e['weight'] > 0],
                key=lambda e: e.get('calendarDate', ''),
                reverse=True,
            )
            if sorted_entries:
                latest = sorted_entries[0]
                result['weight_kg'] = round(latest['weight'] / 1000, 1)
                result['weight_date'] = latest.get('calendarDate')

        # Fallback to totalAverage if no dateWeightList entries
        if result['weight_kg'] is None:
            total_avg = body_composition.get('totalAverage', {})
            weight_grams = total_avg.get('weight')
            if weight_grams and weight_grams > 0:
                result['weight_kg'] = round(weight_grams / 1000, 1)

    # Parse birth date and calculate age from user profile
    if user_profile and isinstance(user_profile, dict):
        user_data = user_profile.get('userData', user_profile)
        birth_date_str = user_data.get('birthDate')
        if birth_date_str:
            result['birth_date'] = birth_date_str
            try:
                birth = date.fromisoformat(birth_date_str)
                today = date.today()
                age = today.year - birth.year
                if (today.month, today.day) < (birth.month, birth.day):
                    age -= 1
                result['age'] = age
            except (ValueError, TypeError):
                pass

        # Parse max heart rate from user settings
        max_hr = user_data.get('maxHeartRate')
        if max_hr and isinstance(max_hr, (int, float)) and max_hr > 0:
            result['max_hr'] = int(max_hr)

        # Fallback: display name from user profile
        if not result['display_name']:
            display = user_data.get('displayName')
            if display:
                result['display_name'] = display

    return result
