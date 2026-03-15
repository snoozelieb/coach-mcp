"""
Rule engine for training compliance and safety checks.

Pure Python logic - no LLM needed for validation.
"""
import json
from datetime import date, timedelta
from typing import Any

from .config import (
    DATA_DIR,
    LONG_EFFORT_MIN_MINS,
    HARD_HR_AVG_THRESHOLD,
    HARD_HR_MAX_THRESHOLD,
    VOLUME_COMPLIANCE_MIN_PERCENT,
    TRAINING_CONFIG_FILE,
    METHODOLOGY_FILE,
    ATHLETE_FILE,
)
import logging

logger = logging.getLogger(__name__)


def get_thresholds() -> dict[str, Any]:
    """
    Load thresholds from training_config.json with config.py fallbacks.

    Thresholds control activity classification and compliance checking.
    Users can customize via training_config.json; config.py provides defaults.
    """
    config_path = DATA_DIR / TRAINING_CONFIG_FILE
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
        thresholds = config.get('thresholds', {})
    else:
        thresholds = {}

    # Return thresholds with config.py defaults as fallbacks
    return {
        'hard_hr_avg': thresholds.get('hard_hr_avg', HARD_HR_AVG_THRESHOLD),
        'hard_hr_max': thresholds.get('hard_hr_max', HARD_HR_MAX_THRESHOLD),
        'long_effort_min_mins': thresholds.get('long_effort_min_mins', LONG_EFFORT_MIN_MINS),
        'volume_compliance_percent': thresholds.get('volume_compliance_percent', VOLUME_COMPLIANCE_MIN_PERCENT),
    }


def load_athlete_pillars() -> dict[str, Any] | None:
    """
    Load personalized training pillars from athlete.json.

    Returns the training_pillars dict if athlete has personalized pillars,
    or None if not configured (falls back to methodology defaults).

    New format pillars have:
    - pillars: list of {name, target_*, target_type, types[], priority}
    - based_on_persona: which persona template was used
    - customized: whether athlete has modified the defaults
    """
    athlete_path = DATA_DIR / ATHLETE_FILE
    if not athlete_path.exists():
        return None

    with open(athlete_path) as f:
        athlete = json.load(f)

    training_pillars = athlete.get('training_pillars')
    if not training_pillars or not training_pillars.get('pillars'):
        return None

    return training_pillars


def convert_athlete_pillars_to_legacy(athlete_pillars: dict[str, Any]) -> dict[str, Any]:
    """
    Convert new flexible pillar format to legacy format for backward compatibility.

    New format: [{"name": "strength", "target_sessions_per_week": 2, ...}, ...]
    Legacy format: {"strength_sessions_per_week": 2, "mobility_minutes_per_week": 90, ...}
    """
    legacy = {}
    for pillar in athlete_pillars.get('pillars', []):
        name = pillar.get('name', '').lower()
        target_type = pillar.get('target_type', 'sessions')

        if target_type == 'sessions':
            key = f"{name}_sessions_per_week"
            legacy[key] = pillar.get('target_sessions_per_week', 0)
        elif target_type == 'minutes':
            key = f"{name}_minutes_per_week"
            legacy[key] = pillar.get('target_mins_per_week', 0)
        elif target_type == 'hours':
            # Convert hours to minutes for consistency
            key = f"{name}_hours_per_week"
            legacy[key] = pillar.get('target_hours_per_week', 0)

    return legacy


def load_training_config() -> dict[str, Any]:
    """
    Load training configuration merged with methodology and athlete pillars.

    Priority for pillars:
    1. Athlete-specific pillars from athlete.json (if configured)
    2. Default pillars from methodology.json (fallback)

    Returns training_config.json data (events, current_block) merged with
    pillars (from athlete or methodology) and constraints.
    """
    # Load training config
    config_path = DATA_DIR / TRAINING_CONFIG_FILE
    if config_path.exists():
        with open(config_path) as f:
            config = json.load(f)
    else:
        config = {}

    # Load methodology for constraints and race templates
    methodology_path = DATA_DIR / METHODOLOGY_FILE
    if methodology_path.exists():
        with open(methodology_path) as f:
            methodology = json.load(f)
        config['constraints'] = methodology.get('safety_constraints', {})
        config['race_requirements'] = methodology.get('race_templates', {})
        config['personas'] = methodology.get('personas', {})
        # Default pillars from methodology (legacy format)
        default_pillars = methodology.get('default_pillar_templates', {})
        # Also check old 'pillars' key for backward compat
        if not default_pillars:
            default_pillars = methodology.get('pillars', {})
    else:
        default_pillars = {}

    # Try to load athlete-specific pillars first
    athlete_pillars = load_athlete_pillars()
    if athlete_pillars:
        # Convert new format to legacy format for backward compat
        config['pillars'] = convert_athlete_pillars_to_legacy(athlete_pillars)
        config['athlete_pillars'] = athlete_pillars  # Keep original for advanced use
        config['pillars_source'] = 'athlete'
    else:
        # Fall back to methodology defaults
        config['pillars'] = default_pillars
        config['pillars_source'] = 'methodology_default'

    return config


def get_activity_classifications() -> dict[str, set]:
    """
    Load activity type classifications from methodology.json.

    Returns dict with sets: strength_types, mobility_types, cardio_types, high_intensity_types
    """
    methodology_path = DATA_DIR / METHODOLOGY_FILE
    if methodology_path.exists():
        with open(methodology_path) as f:
            methodology = json.load(f)
        classifications = methodology.get('activity_classification', {})
    else:
        classifications = {}

    # Return classifications with hardcoded defaults as fallbacks
    return {
        'strength_types': set(classifications.get('strength_types', ['strength_training', 'indoor_cardio', 'functional_strength'])),
        'mobility_types': set(classifications.get('mobility_types', ['yoga', 'pilates', 'stretching', 'breathwork'])),
        'cardio_types': set(classifications.get('cardio_types', ['running', 'cycling', 'swimming', 'trail_running', 'open_water_swimming'])),
        'high_intensity_types': set(classifications.get('high_intensity_types', ['ultimate_disc', 'hiit', 'interval_training', 'track_running'])),
    }


def classify_activity(
    activity: dict[str, Any],
    thresholds: dict[str, Any] = None,
    athlete_max_hr: int = None,
) -> dict[str, Any]:
    """
    Classify an activity by training pillar contribution.

    Args:
        activity: Parsed activity dict
        thresholds: Optional thresholds override (loads from config if None)
        athlete_max_hr: Athlete's max HR for relative intensity. When provided,
            is_hard uses % of max HR (>78%) instead of absolute thresholds.

    Returns dict with: is_strength, is_mobility, is_long_effort, is_hard,
        hr_intensity_pct (float 0.0-1.0 or None)
    """
    if thresholds is None:
        thresholds = get_thresholds()

    # Load activity type classifications from methodology
    classifications = get_activity_classifications()
    strength_types = classifications['strength_types']
    mobility_types = classifications['mobility_types']
    cardio_types = classifications['cardio_types']
    high_intensity_types = classifications['high_intensity_types']

    activity_type = activity.get('type', '').lower()
    duration_mins = activity.get('duration_mins', 0) or 0

    # Strength activities
    is_strength = activity_type in strength_types

    # Mobility activities
    is_mobility = activity_type in mobility_types

    # Long effort (configurable mins of cardio)
    long_effort_min = thresholds.get('long_effort_min_mins', LONG_EFFORT_MIN_MINS)
    is_long_effort = activity_type in cardio_types and duration_mins >= long_effort_min

    # High intensity detection — athlete-relative when max HR known
    avg_hr = activity.get('avg_hr') or 0
    max_hr = activity.get('max_hr') or 0

    # Calculate relative intensity
    hr_intensity_pct = round(avg_hr / athlete_max_hr, 2) if athlete_max_hr and avg_hr else None

    if athlete_max_hr and avg_hr:
        # Athlete-relative: >78% of max HR is "hard"
        is_hard = (
            activity_type in high_intensity_types or
            hr_intensity_pct > 0.78
        )
    else:
        # Fallback to absolute thresholds (no athlete profile available)
        hard_hr_avg = thresholds.get('hard_hr_avg', HARD_HR_AVG_THRESHOLD)
        hard_hr_max = thresholds.get('hard_hr_max', HARD_HR_MAX_THRESHOLD)
        is_hard = (
            activity_type in high_intensity_types or
            avg_hr > hard_hr_avg or
            max_hr > hard_hr_max
        )

    return {
        'is_strength': is_strength,
        'is_mobility': is_mobility,
        'is_long_effort': is_long_effort,
        'is_hard': is_hard,
        'hr_intensity_pct': hr_intensity_pct,
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
            volume: {target_hrs: 6.0, actual_hrs: 5.5, percent: 92, compliant: True, over_volume: False},
            overall_compliant: False,
            deficits: ["strength", "mobility"],
            warnings: []
        }
    """
    # Load thresholds from config
    thresholds = get_thresholds()
    volume_compliance_min = thresholds.get('volume_compliance_percent', VOLUME_COMPLIANCE_MIN_PERCENT)

    # Load config for defaults
    config = load_training_config()

    if pillars is None:
        pillars = config.get('pillars', {})
        volume_target = config.get('current_block', {}).get('weekly_volume_target_hrs', 0)
    else:
        # If pillars explicitly passed, use volume target from it (for testing/override)
        volume_target = pillars.get('weekly_volume_target_hrs', 0)
        if volume_target == 0:
            # Fallback to config if not in pillars
            volume_target = config.get('current_block', {}).get('weekly_volume_target_hrs', 0)

    warnings = []

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

    # Handle volume percentage calculation
    if volume_target > 0:
        volume_percent = round((total_volume_hrs / volume_target * 100), 1)
        volume_compliant = volume_percent >= volume_compliance_min
        over_volume = volume_percent > 150  # Flag if >150% of target
        if over_volume:
            warnings.append(f"Volume at {volume_percent}% of target - potential overtraining risk")
    else:
        volume_percent = None  # No target set
        volume_compliant = True  # Can't fail if no target
        over_volume = False
        warnings.append("No volume target set in current_block")

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
            'compliant': volume_compliant,
            'over_volume': over_volume,
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
    result['warnings'] = warnings

    return result


def check_safety_rules(
    recent_activities: list[dict[str, Any]],
    today_plan: dict[str, Any] = None,
    constraints: dict[str, Any] = None
) -> dict[str, Any]:
    """
    Check safety constraints for training decisions.

    Args:
        recent_activities: Last 7-14 days of activities, sorted by date descending (most recent first)
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

    # Sort activities by date descending to ensure correct order
    sorted_activities = sorted(
        recent_activities,
        key=lambda a: a.get('date', ''),
        reverse=True
    )

    # Check for consecutive hard days
    if sorted_activities:
        consecutive_hard = 0
        for activity in sorted_activities[:7]:  # Last 7 days
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
    # Look for both "race" in name AND race activity types
    race_types = {'race', 'competition', 'event', 'triathlon', 'marathon'}
    for activity in sorted_activities[:rest_after_race + 1]:
        activity_name = (activity.get('name', '') or '').lower()
        activity_type = (activity.get('type', '') or '').lower()

        is_race = (
            'race' in activity_name or
            'competition' in activity_name or
            activity_type in race_types
        )

        if is_race:
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
