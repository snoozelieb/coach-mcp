"""
Rule engine for training compliance and safety checks.

Pure Python logic - no LLM needed for validation.
"""
import json
from datetime import date, timedelta
from typing import Any

from config import (
    DATA_DIR,
    LONG_EFFORT_MIN_MINS,
    HARD_HR_AVG_THRESHOLD,
    HARD_HR_MAX_THRESHOLD,
    VOLUME_COMPLIANCE_MIN_PERCENT,
)


def load_training_config() -> dict[str, Any]:
    """Load training configuration from JSON file."""
    config_path = DATA_DIR / "training_config.json"
    if not config_path.exists():
        return {}
    with open(config_path) as f:
        return json.load(f)


def classify_activity(activity: dict[str, Any]) -> dict[str, bool]:
    """
    Classify an activity by training pillar contribution.

    Returns dict with: is_strength, is_mobility, is_long_effort, is_hard
    """
    activity_type = activity.get('type', '').lower()
    duration_mins = activity.get('duration_mins', 0) or 0

    # Strength activities
    strength_types = {'strength_training', 'indoor_cardio', 'functional_strength'}
    is_strength = activity_type in strength_types

    # Mobility activities (yoga, pilates, stretching)
    mobility_types = {'yoga', 'pilates', 'stretching', 'breathwork'}
    is_mobility = activity_type in mobility_types

    # Long effort (60+ mins of cardio)
    cardio_types = {'running', 'cycling', 'swimming', 'trail_running', 'open_water_swimming'}
    is_long_effort = activity_type in cardio_types and duration_mins >= LONG_EFFORT_MIN_MINS

    # High intensity detection
    high_intensity_types = {'ultimate_disc', 'hiit', 'interval_training', 'track_running'}
    avg_hr = activity.get('avg_hr') or 0
    max_hr = activity.get('max_hr') or 0

    # Activity is "hard" if it's a high-intensity type OR has elevated HR
    is_hard = (
        activity_type in high_intensity_types or
        avg_hr > HARD_HR_AVG_THRESHOLD or
        max_hr > HARD_HR_MAX_THRESHOLD
    )

    return {
        'is_strength': is_strength,
        'is_mobility': is_mobility,
        'is_long_effort': is_long_effort,
        'is_hard': is_hard,
    }


def check_weekly_compliance(
    activities: list[dict[str, Any]],
    pillars: dict[str, Any] = None
) -> dict[str, Any]:
    """
    Check weekly training compliance against pillar requirements.

    Args:
        activities: List of parsed activities for the week
        pillars: Pillar requirements (loads from config if None)

    Returns:
        {
            strength: {required: 2, completed: 1, deficit: 1, compliant: False},
            mobility: {required: 90, completed: 45, deficit: 45, compliant: False},
            long_effort: {required: 1, completed: 1, deficit: 0, compliant: True},
            volume: {target_hrs: 6.0, actual_hrs: 5.5, percent: 92, compliant: True},
            overall_compliant: False,
            deficits: ["strength", "mobility"]
        }
    """
    if pillars is None:
        config = load_training_config()
        pillars = config.get('pillars', {})
        volume_target = config.get('current_block', {}).get('weekly_volume_target_hrs', 0)
    else:
        volume_target = pillars.get('weekly_volume_target_hrs', 0)

    # Count pillar completions
    strength_count = 0
    mobility_mins = 0
    long_effort_count = 0
    total_volume_mins = 0

    for activity in activities:
        classification = classify_activity(activity)
        duration = activity.get('duration_mins', 0) or 0
        total_volume_mins += duration

        if classification['is_strength']:
            strength_count += 1
        if classification['is_mobility']:
            mobility_mins += duration
        if classification['is_long_effort']:
            long_effort_count += 1

    # Get requirements
    strength_required = pillars.get('strength_sessions_per_week', 0)
    mobility_required = pillars.get('mobility_minutes_per_week', 0)
    long_effort_required = pillars.get('long_effort_per_week', 0)

    # Build compliance report
    total_volume_hrs = round(total_volume_mins / 60, 1)
    volume_percent = round((total_volume_hrs / volume_target * 100)) if volume_target else 100

    result = {
        'strength': {
            'required': strength_required,
            'completed': strength_count,
            'deficit': max(0, strength_required - strength_count),
            'compliant': strength_count >= strength_required,
        },
        'mobility': {
            'required': mobility_required,
            'completed': mobility_mins,
            'deficit': max(0, mobility_required - mobility_mins),
            'compliant': mobility_mins >= mobility_required,
        },
        'long_effort': {
            'required': long_effort_required,
            'completed': long_effort_count,
            'deficit': max(0, long_effort_required - long_effort_count),
            'compliant': long_effort_count >= long_effort_required,
        },
        'volume': {
            'target_hrs': volume_target,
            'actual_hrs': total_volume_hrs,
            'percent': volume_percent,
            'compliant': volume_percent >= VOLUME_COMPLIANCE_MIN_PERCENT,
        },
    }

    # Overall compliance
    deficits = []
    for pillar in ['strength', 'mobility', 'long_effort']:
        if not result[pillar]['compliant']:
            deficits.append(pillar)
    if not result['volume']['compliant']:
        deficits.append('volume')

    result['overall_compliant'] = len(deficits) == 0
    result['deficits'] = deficits

    return result


def check_safety_rules(
    recent_activities: list[dict[str, Any]],
    today_plan: dict[str, Any] = None,
    constraints: dict[str, Any] = None
) -> dict[str, Any]:
    """
    Check safety constraints for training decisions.

    Args:
        recent_activities: Last 7-14 days of activities (most recent first)
        today_plan: Planned session for today (optional)
        constraints: Safety constraints (loads from config if None)

    Returns:
        {
            safe: True/False,
            warnings: ["Back-to-back hard days detected"],
            blocked: ["Cannot do hard session - mandatory rest after race"]
        }
    """
    if constraints is None:
        config = load_training_config()
        constraints = config.get('constraints', {})

    warnings = []
    blocked = []

    max_consecutive_hard = constraints.get('max_consecutive_hard_days', 2)
    rest_after_race = constraints.get('mandatory_rest_after_race_days', 1)
    max_volume_increase = constraints.get('max_weekly_volume_increase_percent', 10)

    # Check for consecutive hard days
    if recent_activities:
        consecutive_hard = 0
        for activity in recent_activities[:7]:  # Last 7 days
            classification = classify_activity(activity)
            if classification['is_hard']:
                consecutive_hard += 1
            else:
                break  # Reset on non-hard day

        if consecutive_hard >= max_consecutive_hard:
            warnings.append(f"{consecutive_hard} consecutive hard days - recovery recommended")

        if today_plan:
            today_classification = classify_activity(today_plan)
            if today_classification['is_hard'] and consecutive_hard >= max_consecutive_hard:
                blocked.append("Cannot schedule hard session - maximum consecutive hard days reached")

    # Check for race in recent history requiring rest
    for activity in recent_activities[:rest_after_race + 1]:
        activity_name = activity.get('name', '').lower()
        if 'race' in activity_name or 'competition' in activity_name:
            warnings.append("Recent race detected - ensure adequate recovery")
            if today_plan:
                blocked.append(f"Mandatory {rest_after_race}-day rest after race")
            break

    return {
        'safe': len(blocked) == 0,
        'warnings': warnings,
        'blocked': blocked,
    }


def get_upcoming_events(days_ahead: int = 56) -> list[dict[str, Any]]:
    """
    Get events within the specified number of days.

    Returns events sorted by date with days_until calculated.
    """
    config = load_training_config()
    events = config.get('events', [])
    today = date.today()
    cutoff = today + timedelta(days=days_ahead)

    upcoming = []
    for event in events:
        try:
            event_date = date.fromisoformat(event.get('date', ''))
            if today <= event_date <= cutoff:
                event_copy = event.copy()
                event_copy['days_until'] = (event_date - today).days
                upcoming.append(event_copy)
        except ValueError:
            continue

    return sorted(upcoming, key=lambda e: e['days_until'])
