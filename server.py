from mcp.server.fastmcp import FastMCP
from garmin_client import get_garmin_client, schedule_workout
from rules import check_weekly_compliance, check_safety_rules, get_upcoming_events, load_training_config
from planner import (
    build_planning_context,
    get_current_plan,
    save_weekly_plan,
    get_pending_suggestions as get_suggestions,
    save_suggestion,
    approve_suggestion as approve_sug,
    reject_suggestion as reject_sug,
    create_empty_week_template,
    load_json_file,
    load_athlete,
    load_methodology,
)
from config import (
    DATA_DIR,
    PROFILE_HISTORY_DAYS,
    RECENT_ACTIVITY_DAYS,
    HTTP_TIMEOUT_SECONDS,
    PAGE_TEXT_MAX_CHARS,
    ELEVATION_SIGNIFICANCE_THRESHOLD,
    HIGH_ALTITUDE_THRESHOLD,
    VALID_PRIORITIES,
    ATHLETE_BASELINE_FILE,
)
from datetime import date, timedelta
from typing import Any, Union
from collections import defaultdict
import json


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
        print("  python setup_wizard.py")
        print("\nOr create them manually in the data/ folder.")
        print("=" * 50 + "\n")
        return False

    return True


# Initialize the MCP Server
mcp = FastMCP("AI Training Coach")


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

    parsed = {
        'activity_id': activity.get('activityId'),
        'date': activity.get('startTimeLocal', '')[:10],  # YYYY-MM-DD
        'name': activity.get('activityName', 'Unnamed'),
        'type': activity_type.get('typeKey', 'unknown'),
        'parent_type': activity_type.get('parentTypeId'),
        'duration_mins': round(duration_secs / 60, 1),
        'distance_km': round(distance_m / 1000, 2) if distance_m else None,
        'avg_hr': activity.get('averageHR'),
        'max_hr': activity.get('maxHR'),
        'calories': activity.get('calories'),
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


def parse_personal_records(pr_data: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Parse personal records from Garmin response.

    Returns list of records with: record_type, value, unit, date, activity_id
    """
    records = pr_data.get('personalRecords', [])
    parsed = []

    for record in records:
        pr_type = record.get('prTypeLabelKey', record.get('typeKey', 'unknown'))
        value = record.get('value')

        # Format time-based records (in seconds) to readable format
        if value and 'time' in pr_type.lower():
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


@mcp.tool()
def refresh_athlete_baseline() -> str:
    """
    Generates/refreshes athlete baseline from 6 months of Garmin history.

    Pulls activities, personal records, and calculates baseline metrics.
    Saves to data/athlete_baseline.json (auto-generated Garmin data).

    For personal info, life constraints, injury history, and preferences,
    edit data/athlete.json directly.

    Returns:
        JSON summary of the generated baseline.
    """
    try:
        client = get_garmin_client()
        today = date.today()
        six_months_ago = today - timedelta(days=PROFILE_HISTORY_DAYS)

        # Pull 6 months of activities
        raw_activities = client.get_activities_by_date(
            six_months_ago.isoformat(),
            today.isoformat()
        )
        activities = parse_activities(raw_activities)

        # Pull personal records
        pr_data = client.get_personal_record()
        personal_records = parse_personal_records(pr_data)

        # Calculate baseline from activities
        baseline = calculate_baseline(activities)

        # Build the baseline profile (Garmin-derived only)
        profile = {
            'last_refreshed': today.isoformat(),
            'baseline': baseline,
            'personal_records': personal_records,
        }

        # Ensure data directory exists
        DATA_DIR.mkdir(exist_ok=True)

        # Save to athlete_baseline.json
        profile_path = DATA_DIR / ATHLETE_BASELINE_FILE
        with open(profile_path, 'w') as f:
            json.dump(profile, f, indent=2)

        # Return summary
        summary = {
            'status': 'success',
            'last_refreshed': profile['last_refreshed'],
            'activities_analyzed': baseline['total_activities'],
            'weeks_analyzed': baseline['weeks_analyzed'],
            'avg_weekly_volume_hrs': baseline['avg_weekly_volume_hrs'],
            'personal_records_count': len(personal_records),
            'profile_path': str(profile_path)
        }

        return json.dumps(summary, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_training_readiness(for_date: str = None) -> str:
    """
    Fetches training readiness score and recovery metrics from Garmin.

    Args:
        for_date: Date in YYYY-MM-DD format (defaults to today)

    Returns:
        JSON with: score (0-100), level (PRIME/HIGH/MODERATE/LOW),
        sleep_score, recovery_time_hrs, hrv_status, acute_load, feedback.
    """
    try:
        client = get_garmin_client()

        if for_date is None:
            for_date = date.today().isoformat()

        readiness_data = client.get_training_readiness(for_date)
        parsed = parse_training_readiness(readiness_data)

        return json.dumps(parsed, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_load_status() -> str:
    """
    Get comprehensive load status for training decisions.

    Uses Garmin's Training Readiness and recent activity data to provide
    load context and recommendations for today's training intensity.

    Returns:
        JSON with: readiness (score/level), acute_load, load_trend,
        recommendation, and any warnings.
    """
    try:
        client = get_garmin_client()
        today = date.today()

        # Get today's training readiness
        readiness_data = client.get_training_readiness(today.isoformat())
        readiness = parse_training_readiness(readiness_data)

        # Get recent activities for load trend
        week_ago = (today - timedelta(days=7)).isoformat()
        two_weeks_ago = (today - timedelta(days=14)).isoformat()

        recent_activities = client.get_activities_by_date(week_ago, today.isoformat())
        prior_activities = client.get_activities_by_date(two_weeks_ago, week_ago)

        # Calculate load trend (simple duration-based)
        recent_duration_mins = sum(
            a.get('duration', 0) / 60 for a in recent_activities
        ) if recent_activities else 0
        prior_duration_mins = sum(
            a.get('duration', 0) / 60 for a in prior_activities
        ) if prior_activities else 0

        # Calculate ACWR-like ratio (simplified)
        if prior_duration_mins > 0:
            load_ratio = recent_duration_mins / prior_duration_mins
        else:
            load_ratio = 1.0

        # Determine load trend
        if load_ratio < 0.8:
            trend = "decreasing"
        elif load_ratio > 1.15:
            trend = "increasing"
        else:
            trend = "stable"

        # Generate recommendation based on readiness level
        level = readiness.get('level', 'UNKNOWN')
        score = readiness.get('score', 0)

        if level == 'PRIME' or score >= 80:
            recommendation = "Excellent readiness - good day for key sessions or intensity"
            intensity = "high"
        elif level == 'HIGH' or score >= 60:
            recommendation = "Good readiness - proceed with planned training"
            intensity = "moderate_to_high"
        elif level == 'MODERATE' or score >= 40:
            recommendation = "Moderate readiness - consider easier session or reduced volume"
            intensity = "moderate"
        elif level == 'LOW' or score >= 20:
            recommendation = "Low readiness - recommend easy/recovery session only"
            intensity = "low"
        else:
            recommendation = "Very low readiness - rest or very light movement only"
            intensity = "rest"

        # Build warnings
        warnings = []
        if load_ratio > 1.3:
            warnings.append("Acute load significantly higher than chronic - injury risk elevated")
        if readiness.get('recovery_time_hrs', 0) > 48:
            warnings.append(f"Recovery time still {readiness.get('recovery_time_hrs')}hrs - not fully recovered")
        if readiness.get('hrv_status') in ['UNBALANCED', 'POOR']:
            warnings.append(f"HRV status is {readiness.get('hrv_status')} - consider extra recovery")

        return json.dumps({
            'readiness_score': score,
            'readiness_level': level,
            'acute_load': readiness.get('acute_load'),
            'load_ratio': round(load_ratio, 2),
            'load_trend': trend,
            'recent_volume_mins': round(recent_duration_mins),
            'prior_volume_mins': round(prior_duration_mins),
            'recommended_intensity': intensity,
            'recommendation': recommendation,
            'warnings': warnings,
            'hrv_status': readiness.get('hrv_status'),
            'recovery_time_hrs': readiness.get('recovery_time_hrs'),
            'feedback': readiness.get('feedback')
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_personal_records() -> str:
    """
    Fetches personal records (PBs) from Garmin.

    Returns JSON array of records with: record_type, value, value_formatted,
    unit, date, and activity_id.
    """
    try:
        client = get_garmin_client()
        pr_data = client.get_personal_record()
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
        client = get_garmin_client()

        if end_date is None:
            end_date = date.today().isoformat()

        activities = client.get_activities_by_date(start_date, end_date)
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
        client = get_garmin_client()
        today = date.today().isoformat()

        stats = client.get_user_summary(today)
        body_battery = client.get_body_battery(today)

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


@mcp.tool()
def get_onboarding_guide() -> str:
    """
    Get the onboarding guide for setting up a new athlete.

    Returns available personas and a conversation guide for the LLM to follow
    when discovering athlete needs and configuring their training pillars.

    Use this when:
    - Setting up a new athlete for the first time
    - An athlete wants to reconfigure their training approach
    - Transitioning to a new training focus

    The LLM should follow the returned guide to ask questions and then
    use update_athlete() to save the personalized configuration.
    """
    try:
        methodology = load_methodology()
        personas = methodology.get('personas', {})

        # Remove description keys for cleaner output
        persona_list = []
        for key, value in personas.items():
            if key.startswith('_'):
                continue
            persona_list.append({
                'id': key,
                'description': value.get('description', ''),
                'typical_weekly_hours': value.get('typical_weekly_hours', 'varies'),
                'key_focus': value.get('key_focus', ''),
                'suggested_pillars': value.get('suggested_pillars', [])
            })

        guide = {
            'coaching_principle': "You are the coach. Understand the athlete, then PRESCRIBE - don't offer a menu.",
            'available_personas': persona_list,
            'onboarding_steps': [
                {
                    'step': 1,
                    'name': 'Understand the athlete',
                    'questions': [
                        "What are your main sports or activities?",
                        "How long have you been training?",
                        "Any current injuries or limitations?"
                    ],
                    'note': "Listen and gather information. Don't give options yet."
                },
                {
                    'step': 2,
                    'name': 'Understand their goals',
                    'questions': [
                        "What do you want to achieve?",
                        "Any events or races you're targeting?",
                        "What does success look like for you in 6 months?"
                    ],
                    'note': "Understand their WHY. This informs your prescription."
                },
                {
                    'step': 3,
                    'name': 'Assess capacity',
                    'questions': [
                        "How many hours per week can you realistically commit to training?",
                        "Any days that absolutely don't work?",
                        "Morning or evening person?"
                    ],
                    'note': "Get realistic constraints. Athletes often overestimate availability."
                },
                {
                    'step': 4,
                    'name': 'PRESCRIBE the plan',
                    'instruction': "Based on everything learned, TELL them what their training pillars will be. Explain WHY. Don't ask 'does this work for you?' - state 'Based on your goals and capacity, here is what you need to do.' They can ask questions but you are the expert."
                },
                {
                    'step': 5,
                    'name': 'Save and commit',
                    'instruction': "Use update_athlete() to save training_pillars. Tell them what comes next."
                }
            ],
            'update_example': {
                'section': 'training_pillars',
                'data': {
                    'based_on_persona': 'endurance_athlete',
                    'customized': True,
                    'pillars': [
                        {'name': 'endurance', 'target_hours_per_week': 4, 'target_type': 'hours', 'types': ['running', 'cycling']},
                        {'name': 'strength', 'target_sessions_per_week': 2, 'target_type': 'sessions', 'types': ['strength_training']}
                    ]
                }
            }
        }

        return json.dumps(guide, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_athlete() -> str:
    """
    Returns the complete athlete profile.

    Includes:
    - personal: name, age, HR zones, FTP, weight
    - life_constraints: recurring commitments, preferred times, work schedule
    - injury_history: past injuries with status and notes
    - preferences: likes, dislikes, equipment, notes
    - coaching_notes: free-form notes about the athlete
    - baseline: Garmin-derived training capacity (from athlete_baseline.json)
    - personal_records: PRs from Garmin

    Edit data/athlete.json directly to update personal info.
    Use refresh_athlete_baseline() to update Garmin-derived data.
    """
    try:
        athlete = load_athlete()
        return json.dumps(athlete, indent=2)
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_athlete(
    section: str,
    data: str
) -> str:
    """
    Update a section of the athlete profile.

    Args:
        section: Which section to update. One of:
            - 'personal': name, age, max_hr, resting_hr, hr_zones, ftp, weight_kg
            - 'life_constraints': recurring_commitments, preferred_training_times, work_schedule
            - 'preferences': likes, dislikes, equipment, notes
            - 'coaching_notes': free-form coaching notes (string, not object)
            - 'add_commitment': add a recurring commitment
            - 'add_injury': add an injury to history
            - 'training_pillars': personalized training pillars (from onboarding)
            - 'swimming': swimming profile (experience, pace, strokes)
            - 'pilates': pilates profile (experience, focus areas)
        data: JSON string with the data to update/add

    Examples:
        update_athlete('personal', '{"max_hr": 185, "weight_kg": 75}')
        update_athlete('add_commitment', '{"day": "Tuesday", "activity": "swimming", "time": "morning"}')
        update_athlete('add_injury', '{"date": "2026-01-01", "type": "ankle", "description": "Rolled ankle"}')
        update_athlete('preferences', '{"likes": ["MTB", "trail running"]}')
        update_athlete('coaching_notes', '"Responds well to data-driven feedback"')
        update_athlete('training_pillars', '{"based_on_persona": "endurance_athlete", "pillars": [...]}')

    Returns confirmation with updated section.
    """
    from planner import save_json_file
    from config import ATHLETE_FILE

    try:
        athlete = load_athlete()
        # Remove baseline data before saving (it's from athlete_baseline.json)
        athlete.pop('baseline', None)
        athlete.pop('personal_records', None)

        parsed_data = json.loads(data)

        if section == 'personal':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'personal data must be an object'})
            athlete.setdefault('personal', {}).update(parsed_data)
            updated = athlete['personal']

        elif section == 'life_constraints':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'life_constraints data must be an object'})
            athlete.setdefault('life_constraints', {}).update(parsed_data)
            updated = athlete['life_constraints']

        elif section == 'preferences':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'preferences data must be an object'})
            athlete.setdefault('preferences', {}).update(parsed_data)
            updated = athlete['preferences']

        elif section == 'coaching_notes':
            if not isinstance(parsed_data, str):
                return json.dumps({'error': 'coaching_notes must be a string'})
            athlete['coaching_notes'] = parsed_data
            updated = parsed_data

        elif section == 'add_commitment':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'commitment must be an object with day, activity, time'})
            required = ['day', 'activity']
            if not all(k in parsed_data for k in required):
                return json.dumps({'error': f'commitment requires: {required}'})
            athlete.setdefault('life_constraints', {}).setdefault('recurring_commitments', [])
            athlete['life_constraints']['recurring_commitments'].append(parsed_data)
            updated = parsed_data

        elif section == 'add_injury':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'injury must be an object with date, type, description'})
            required = ['date', 'type', 'description']
            if not all(k in parsed_data for k in required):
                return json.dumps({'error': f'injury requires: {required}'})
            parsed_data.setdefault('status', 'active')
            athlete.setdefault('injury_history', []).append(parsed_data)
            updated = parsed_data

        elif section == 'training_pillars':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'training_pillars must be an object with pillars array'})
            if 'pillars' not in parsed_data:
                return json.dumps({'error': 'training_pillars requires pillars array'})
            # Add metadata
            from datetime import date
            parsed_data['last_updated'] = date.today().isoformat()
            athlete['training_pillars'] = parsed_data
            updated = parsed_data

        elif section == 'swimming':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'swimming data must be an object'})
            athlete.setdefault('swimming', {}).update(parsed_data)
            updated = athlete['swimming']

        elif section == 'pilates':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'pilates data must be an object'})
            athlete.setdefault('pilates', {}).update(parsed_data)
            updated = athlete['pilates']

        else:
            return json.dumps({
                'error': f"Unknown section '{section}'. Use: personal, life_constraints, preferences, coaching_notes, add_commitment, add_injury, training_pillars, swimming, pilates"
            })

        # Save updated athlete profile
        save_json_file(ATHLETE_FILE, athlete)

        return json.dumps({
            'status': 'success',
            'section': section,
            'updated': updated
        }, indent=2)

    except json.JSONDecodeError as e:
        return json.dumps({'error': f'Invalid JSON: {str(e)}'})
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def set_threshold_pace(
    pace: str = None,
    time_trial_mins: int = None,
    time_trial_distance_km: float = None
) -> str:
    """
    Set running threshold pace from a test result.

    The threshold pace is the running equivalent of FTP - the pace you can
    sustain for approximately 60 minutes. Pace zones are automatically
    calculated using Jack Daniels methodology.

    Provide ONE of:
    - pace: Direct pace input as "MM:SS" per km (e.g., "5:30" = 5min 30sec/km)
    - time_trial_mins + time_trial_distance_km: Calculate from a time trial
      (e.g., 30 min for 6.5 km)

    For 30-min time trials, pace is adjusted +5% (slightly slower than threshold).
    For 60-min time trials, pace equals threshold.

    Examples:
        set_threshold_pace(pace="5:15")  # Set directly to 5:15/km
        set_threshold_pace(time_trial_mins=30, time_trial_distance_km=6.2)  # From 30-min TT
        set_threshold_pace(time_trial_mins=60, time_trial_distance_km=11.5)  # From 60-min TT

    Returns the calculated threshold pace and derived pace zones.
    """
    from planner import save_json_file
    from config import ATHLETE_FILE

    try:
        athlete = load_athlete()
        athlete.pop('baseline', None)
        athlete.pop('personal_records', None)

        threshold_sec_per_km = None

        if pace:
            # Parse MM:SS format
            parts = pace.strip().split(':')
            if len(parts) == 2:
                mins, secs = int(parts[0]), int(parts[1])
                threshold_sec_per_km = mins * 60 + secs
            else:
                return json.dumps({'error': 'pace must be in MM:SS format (e.g., "5:30")'})

        elif time_trial_mins and time_trial_distance_km:
            # Calculate pace from time trial
            total_secs = time_trial_mins * 60
            pace_sec_per_km = total_secs / time_trial_distance_km

            # Adjust for test duration (30-min TT is ~5% faster than threshold)
            if time_trial_mins <= 35:
                threshold_sec_per_km = int(pace_sec_per_km * 1.05)
            elif time_trial_mins <= 50:
                threshold_sec_per_km = int(pace_sec_per_km * 1.02)
            else:
                threshold_sec_per_km = int(pace_sec_per_km)
        else:
            return json.dumps({'error': 'Provide either pace (MM:SS) or time_trial_mins + time_trial_distance_km'})

        # Calculate pace zones using Jack Daniels methodology
        pace_zones = {
            "z1_recovery": [int(threshold_sec_per_km * 1.25), int(threshold_sec_per_km * 1.30)],
            "z2_easy": [int(threshold_sec_per_km * 1.15), int(threshold_sec_per_km * 1.24)],
            "z3_tempo": [int(threshold_sec_per_km * 1.05), int(threshold_sec_per_km * 1.14)],
            "z4_threshold": [int(threshold_sec_per_km * 0.96), int(threshold_sec_per_km * 1.04)],
            "z5_interval": [int(threshold_sec_per_km * 0.85), int(threshold_sec_per_km * 0.95)],
        }

        # Update athlete profile
        athlete.setdefault('personal', {})['threshold_pace_sec_per_km'] = threshold_sec_per_km
        athlete['personal']['pace_zones'] = pace_zones

        save_json_file(ATHLETE_FILE, athlete)

        # Format for display
        def format_pace(sec_per_km):
            mins = sec_per_km // 60
            secs = sec_per_km % 60
            return f"{mins}:{secs:02d}/km"

        zones_formatted = {
            zone: f"{format_pace(vals[1])} - {format_pace(vals[0])}"
            for zone, vals in pace_zones.items()
        }

        return json.dumps({
            'status': 'success',
            'threshold_pace': format_pace(threshold_sec_per_km),
            'threshold_sec_per_km': threshold_sec_per_km,
            'pace_zones': zones_formatted
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def set_ftp(
    ftp_watts: int = None,
    test_avg_watts: int = None,
    test_duration_mins: int = 20
) -> str:
    """
    Set cycling FTP (Functional Threshold Power) from a test result.

    FTP is the maximum power you can sustain for approximately 60 minutes.
    Power zones are automatically calculated.

    Provide ONE of:
    - ftp_watts: Direct FTP value in watts
    - test_avg_watts: Average power from a test (default assumes 20-min test)
      - 20-min test: FTP = avg_power × 0.95
      - 8-min test: FTP = avg_power × 0.90
      - 60-min test: FTP = avg_power

    Examples:
        set_ftp(ftp_watts=250)  # Set directly
        set_ftp(test_avg_watts=265, test_duration_mins=20)  # From 20-min test
        set_ftp(test_avg_watts=280, test_duration_mins=8)  # From 8-min test

    Returns the FTP value and derived power zones.
    """
    from planner import save_json_file
    from config import ATHLETE_FILE

    try:
        athlete = load_athlete()
        athlete.pop('baseline', None)
        athlete.pop('personal_records', None)

        ftp = None

        if ftp_watts:
            ftp = ftp_watts
        elif test_avg_watts:
            # Apply adjustment factor based on test duration
            if test_duration_mins <= 10:
                ftp = int(test_avg_watts * 0.90)
            elif test_duration_mins <= 25:
                ftp = int(test_avg_watts * 0.95)
            elif test_duration_mins <= 40:
                ftp = int(test_avg_watts * 0.98)
            else:
                ftp = test_avg_watts
        else:
            return json.dumps({'error': 'Provide either ftp_watts or test_avg_watts'})

        # Calculate power zones (using standard 7-zone model)
        power_zones = {
            "z1_recovery": [0, int(ftp * 0.55)],
            "z2_endurance": [int(ftp * 0.56), int(ftp * 0.75)],
            "z3_tempo": [int(ftp * 0.76), int(ftp * 0.90)],
            "z4_threshold": [int(ftp * 0.91), int(ftp * 1.05)],
            "z5_vo2max": [int(ftp * 1.06), int(ftp * 1.20)],
            "z6_anaerobic": [int(ftp * 1.21), int(ftp * 1.50)],
            "z7_neuromuscular": [int(ftp * 1.51), None],
        }

        # Update athlete profile
        athlete.setdefault('personal', {})['ftp'] = ftp
        athlete['personal']['power_zones'] = power_zones

        save_json_file(ATHLETE_FILE, athlete)

        # Format zones for display
        zones_formatted = {
            zone: f"{vals[0]}-{vals[1]}W" if vals[1] else f"{vals[0]}W+"
            for zone, vals in power_zones.items()
        }

        return json.dumps({
            'status': 'success',
            'ftp': ftp,
            'power_zones': zones_formatted
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_methodology() -> str:
    """
    Returns the complete training methodology.

    Includes:
    - pillars: weekly training requirements (strength sessions, mobility mins, long effort)
    - safety_constraints: max consecutive hard days, rest after race, volume increase limits
    - race_templates: training guidance for different race types

    This data controls how compliance is calculated and what the LLM considers
    when building training plans.
    """
    from planner import load_methodology

    try:
        methodology = load_methodology()
        return json.dumps(methodology, indent=2)
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_methodology(
    section: str,
    data: str
) -> str:
    """
    Update a section of the training methodology.

    Args:
        section: Which section to update. One of:
            - 'pillars': training requirements (strength_sessions_per_week, mobility_minutes_per_week, etc.)
            - 'safety_constraints': training limits (max_consecutive_hard_days, etc.)
            - 'add_race_template': add a new race type template
            - 'update_race_template': update existing race template
        data: JSON string with the data to update/add

    Examples:
        update_methodology('pillars', '{"strength_sessions_per_week": 3}')
        update_methodology('safety_constraints', '{"max_consecutive_hard_days": 3}')
        update_methodology('add_race_template', '{"name": "gravel", "description": "...", "key_sessions": [...]}')

    Returns confirmation with updated section.
    """
    from planner import load_json_file, save_json_file
    from config import METHODOLOGY_FILE

    try:
        methodology = load_json_file(METHODOLOGY_FILE)
        parsed_data = json.loads(data)

        if section == 'pillars':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'pillars data must be an object'})
            methodology.setdefault('pillars', {}).update(parsed_data)
            updated = methodology['pillars']

        elif section == 'safety_constraints':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'safety_constraints data must be an object'})
            methodology.setdefault('safety_constraints', {}).update(parsed_data)
            updated = methodology['safety_constraints']

        elif section == 'add_race_template':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'race template must be an object'})
            if 'name' not in parsed_data:
                return json.dumps({'error': 'race template requires a name'})
            template_name = parsed_data.pop('name')
            methodology.setdefault('race_templates', {})[template_name] = parsed_data
            updated = {template_name: parsed_data}

        elif section == 'update_race_template':
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'race template update must be an object'})
            if 'name' not in parsed_data:
                return json.dumps({'error': 'race template update requires a name'})
            template_name = parsed_data.pop('name')
            if template_name not in methodology.get('race_templates', {}):
                return json.dumps({'error': f"Race template '{template_name}' not found"})
            methodology['race_templates'][template_name].update(parsed_data)
            updated = {template_name: methodology['race_templates'][template_name]}

        else:
            return json.dumps({
                'error': f"Unknown section '{section}'. Use: pillars, safety_constraints, add_race_template, update_race_template"
            })

        # Save updated methodology
        save_json_file(METHODOLOGY_FILE, methodology)

        return json.dumps({
            'status': 'success',
            'section': section,
            'updated': updated
        }, indent=2)

    except json.JSONDecodeError as e:
        return json.dumps({'error': f'Invalid JSON: {str(e)}'})
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_planning_context() -> str:
    """
    Assembles complete context for LLM training planning.

    Returns comprehensive data including:
    - WHO: Athlete profile (personal, life constraints, preferences, baseline)
    - WHAT: Current training block and upcoming events
    - HOW: Training methodology (pillars, safety constraints)
    - Recent activities (last 5 weeks)
    - Current week's compliance status
    - Today's recovery metrics
    - Any pending suggestions

    Use this before generating or adjusting training plans.
    """
    try:
        client = get_garmin_client()
        today = date.today()

        # Load configurations from new file structure
        athlete_profile = load_athlete()
        training_config = load_training_config()
        methodology = load_methodology()

        # Get recent activities (14 days)
        start_14_days = today - timedelta(days=RECENT_ACTIVITY_DAYS)
        raw_activities = client.get_activities_by_date(
            start_14_days.isoformat(),
            today.isoformat()
        )
        recent_activities = parse_activities(raw_activities)

        # Get compliance for current week
        start_7_days = today - timedelta(days=7)
        week_activities = [
            a for a in recent_activities
            if a.get('date') and date.fromisoformat(a['date']) >= start_7_days
        ]
        compliance = check_weekly_compliance(week_activities)

        # Get today's recovery metrics
        readiness_data = client.get_training_readiness(today.isoformat())
        today_recovery = parse_training_readiness(readiness_data)

        stats = client.get_user_summary(today.isoformat())
        body_battery = client.get_body_battery(today.isoformat())

        today_recovery['rhr'] = parse_resting_heart_rate(stats)
        today_recovery['body_battery'] = parse_body_battery(body_battery)
        today_recovery['sleep_score'] = parse_sleep_score(stats)

        # Calculate load status
        week_ago = (today - timedelta(days=7)).isoformat()
        two_weeks_ago = (today - timedelta(days=14)).isoformat()

        recent_load_activities = client.get_activities_by_date(week_ago, today.isoformat())
        prior_load_activities = client.get_activities_by_date(two_weeks_ago, week_ago)

        recent_duration_mins = sum(
            a.get('duration', 0) / 60 for a in recent_load_activities
        ) if recent_load_activities else 0
        prior_duration_mins = sum(
            a.get('duration', 0) / 60 for a in prior_load_activities
        ) if prior_load_activities else 0

        if prior_duration_mins > 0:
            load_ratio = recent_duration_mins / prior_duration_mins
        else:
            load_ratio = 1.0

        readiness_score = today_recovery.get('score', 0)
        readiness_level = today_recovery.get('level', 'UNKNOWN')

        if readiness_level == 'PRIME' or readiness_score >= 80:
            load_recommendation = "high"
        elif readiness_level == 'HIGH' or readiness_score >= 60:
            load_recommendation = "moderate_to_high"
        elif readiness_level == 'MODERATE' or readiness_score >= 40:
            load_recommendation = "moderate"
        elif readiness_level == 'LOW' or readiness_score >= 20:
            load_recommendation = "low"
        else:
            load_recommendation = "rest"

        load_status = {
            'readiness_score': readiness_score,
            'readiness_level': readiness_level,
            'acute_load': today_recovery.get('acute_load'),
            'load_ratio': round(load_ratio, 2),
            'load_trend': 'decreasing' if load_ratio < 0.8 else ('increasing' if load_ratio > 1.15 else 'stable'),
            'recommended_intensity': load_recommendation,
            'recent_volume_mins': round(recent_duration_mins),
            'prior_volume_mins': round(prior_duration_mins),
        }

        # Get pending suggestions
        pending = get_suggestions()

        # Build full context with new file structure
        context = build_planning_context(
            athlete_profile=athlete_profile,
            training_config=training_config,
            recent_activities=recent_activities,
            compliance_status=compliance,
            today_recovery=today_recovery,
            pending_suggestions=pending,
            methodology=methodology,
        )

        # Add load status to context
        context['load_status'] = load_status

        # Calculate goal balance
        fun_types = ['padel', 'ultimate_disc', 'social_ride', 'tennis', 'squash', 'badminton']
        strength_types = ['strength_training', 'indoor_cardio', 'functional_strength']

        race_prep_mins = 0
        fun_mins = 0
        aesthetics_mins = 0
        last_fun_date = None
        strength_count = 0

        for activity in recent_activities:
            act_type = activity.get('type', '').lower()
            duration = activity.get('duration_mins', 0)
            act_date = activity.get('date')

            if any(f in act_type for f in fun_types):
                fun_mins += duration
                if last_fun_date is None or (act_date and act_date > last_fun_date):
                    last_fun_date = act_date
            elif any(s in act_type for s in strength_types):
                aesthetics_mins += duration
                strength_count += 1
            else:
                race_prep_mins += duration

        total_mins = race_prep_mins + fun_mins + aesthetics_mins
        days_since_fun = None
        if last_fun_date:
            try:
                fun_date = date.fromisoformat(last_fun_date)
                days_since_fun = (today - fun_date).days
            except ValueError:
                pass

        context['goal_progress'] = {
            'race_preparation': {
                'mins': round(race_prep_mins),
                'pct': round(race_prep_mins / total_mins * 100) if total_mins > 0 else 0
            },
            'fun_activities': {
                'mins': round(fun_mins),
                'pct': round(fun_mins / total_mins * 100) if total_mins > 0 else 0,
                'days_since_last': days_since_fun,
                'needs_attention': days_since_fun is not None and days_since_fun > 14
            },
            'aesthetics': {
                'mins': round(aesthetics_mins),
                'strength_sessions': strength_count,
                'needs_attention': strength_count < 2
            }
        }

        return json.dumps(context, indent=2, default=str)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_weekly_plan() -> str:
    """
    Returns the current 7-day training plan.

    The plan includes for each day:
    - planned: The planned session (type, duration, intensity, notes)
    - actual: What was actually done (filled by audit)
    - status: pending, completed, missed, modified
    """
    try:
        plan = get_current_plan()
        if not plan:
            plan = create_empty_week_template()
        return json.dumps(plan, indent=2)
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_weekly_plan(plan_json: str) -> str:
    """
    Saves a new or updated weekly training plan.

    Args:
        plan_json: JSON string containing the plan with structure:
            {
                'days': {
                    'YYYY-MM-DD': {
                        'planned': {
                            'type': 'running',
                            'duration_mins': 45,
                            'intensity': 'easy',
                            'description': 'Easy recovery run'
                        },
                        'notes': 'Focus on form'
                    },
                    ...
                },
                'rationale': 'Why this plan was generated'
            }

    Returns confirmation or error.
    """
    try:
        plan = json.loads(plan_json)
        save_weekly_plan(plan)
        return json.dumps({
            'status': 'success',
            'message': 'Weekly plan saved',
            'last_updated': date.today().isoformat()
        })
    except json.JSONDecodeError as e:
        return json.dumps({'error': f'Invalid JSON: {str(e)}'})
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def push_plan_to_garmin() -> str:
    """
    Push the current 7-day plan to Garmin Connect calendar.

    AUTOMATICALLY DELETES existing workouts created during plan period
    before pushing to prevent duplicates.

    Converts each session to a Garmin workout (running, cycling, strength, yoga),
    uploads it, and schedules it to the appropriate date.

    Supported workout types:
    - Running: timed segments with warmup/main/cooldown
    - Cycling: timed segments with warmup/main/cooldown
    - Strength: exercises with sets/reps, rest uses LAP BUTTON
    - Yoga/Mobility: scheduled as Yoga activity type
    - Pilates: scheduled as Pilates activity type

    Rest days are skipped.

    Returns:
        JSON summary with count of pushed workouts, dates, and any errors.
    """
    from workout_builder import build_workout, get_workout_type_name

    try:
        client = get_garmin_client()
        plan = get_current_plan()

        if not plan or 'days' not in plan:
            return json.dumps({'error': 'No weekly plan found. Generate a plan first.'})

        # DUPLICATE PREVENTION: Delete existing workouts created during plan period
        week_start = plan.get('week_start', '2000-01-01')
        existing_workouts = client.get_workouts()
        deleted_count = 0

        for workout in existing_workouts:
            workout_id = workout.get('workoutId')
            created = workout.get('createdDate', '')[:10]

            # Delete workouts created on or after plan start date (likely ours)
            if created >= week_start:
                try:
                    client.garth.delete('connectapi', f'/workout-service/workout/{workout_id}', api=True)
                    deleted_count += 1
                except:
                    pass

        results = {
            'status': 'success',
            'duplicates_deleted': deleted_count,
            'pushed': [],
            'skipped': [],
            'errors': [],
        }

        for date_str, day_data in plan['days'].items():
            # Get the planned session(s)
            planned = day_data.get('planned')
            if not planned:
                results['skipped'].append({'date': date_str, 'reason': 'rest day'})
                continue

            # Handle both single session and list of sessions
            sessions = planned if isinstance(planned, list) else [planned]

            # Expand sessions with nested 'sessions' array into individual sub-sessions
            expanded_sessions = []
            for session in sessions:
                if 'sessions' in session and isinstance(session.get('sessions'), list):
                    # Get parent-level exercises (for strength sessions)
                    parent_exercises = session.get('exercises', [])
                    parent_intensity = session.get('intensity', 'easy')
                    # Extract sub-sessions
                    for sub in session.get('sessions', []):
                        # Skip optional sessions like pool_sauna
                        if sub.get('time') == 'optional':
                            continue
                        # Copy parent description if sub doesn't have one
                        if 'description' not in sub:
                            sub['description'] = sub.get('notes', session.get('description', ''))
                        # Copy parent intensity if sub doesn't have one
                        if 'intensity' not in sub:
                            sub['intensity'] = parent_intensity
                        # Copy exercises to strength sub-sessions
                        if sub.get('type', '').lower() == 'strength' and parent_exercises:
                            sub['exercises'] = parent_exercises
                        expanded_sessions.append(sub)
                else:
                    expanded_sessions.append(session)

            for session in expanded_sessions:
                workout_type = get_workout_type_name(session)

                if workout_type == 'skipped':
                    results['skipped'].append({
                        'date': date_str,
                        'type': session.get('type'),
                        'reason': 'rest day - not pushed'
                    })
                    continue

                if workout_type == 'unknown':
                    results['skipped'].append({
                        'date': date_str,
                        'type': session.get('type'),
                        'reason': 'unknown workout type'
                    })
                    continue

                # Build the Garmin workout
                workout = build_workout(session, date_str)

                if not workout:
                    results['skipped'].append({
                        'date': date_str,
                        'type': session.get('type'),
                        'reason': 'could not convert to workout'
                    })
                    continue

                try:
                    # Upload the workout based on type
                    if workout_type == 'cycling':
                        upload_result = client.upload_cycling_workout(workout)
                        workout_name = workout.workoutName
                    elif workout_type == 'running':
                        upload_result = client.upload_running_workout(workout)
                        workout_name = workout.workoutName
                    elif workout_type in ['yoga', 'strength', 'swimming']:
                        # Yoga, strength, and swimming use generic upload with dict format
                        upload_result = client.upload_workout(workout)
                        workout_name = workout.get('workoutName', 'Workout')
                    else:
                        results['skipped'].append({
                            'date': date_str,
                            'type': workout_type,
                            'reason': 'upload method not implemented'
                        })
                        continue

                    # Get the workout ID from the upload result
                    workout_id = upload_result.get('workoutId')

                    if not workout_id:
                        results['errors'].append({
                            'date': date_str,
                            'type': workout_type,
                            'error': 'No workout ID returned from upload'
                        })
                        continue

                    # Schedule the workout to the date
                    schedule_workout(client, workout_id, date_str)

                    result_entry = {
                        'date': date_str,
                        'type': workout_type,
                        'workout_id': workout_id,
                        'name': workout_name
                    }

                    # Add exercise count for strength workouts
                    if workout_type == 'strength' and isinstance(workout, dict):
                        result_entry['exercise_count'] = workout.get('exercise_count', 0)

                    results['pushed'].append(result_entry)

                except Exception as e:
                    results['errors'].append({
                        'date': date_str,
                        'type': workout_type,
                        'error': str(e)
                    })

        # Build summary message
        pushed_count = len(results['pushed'])
        if pushed_count > 0:
            dates = [p['date'] for p in results['pushed']]
            types_pushed = set(p['type'] for p in results['pushed'])
            results['summary'] = f"Pushed {pushed_count} workout(s) to Garmin ({', '.join(types_pushed)}): {dates[0]} to {dates[-1]}"
        else:
            results['summary'] = "No workouts pushed (all sessions were rest days)"

        return json.dumps(results, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def propose_suggestion(
    suggestion_type: str,
    description: str,
    rationale: str,
    proposed_change: str = None
) -> str:
    """
    Propose a training configuration change for user approval.

    Use this when the LLM identifies patterns that warrant pillar adjustments,
    new constraints, or other configuration changes.

    Args:
        suggestion_type: Category (e.g., 'pillar_adjustment', 'add_constraint',
                        'volume_change', 'event_timing')
        description: Short description of the suggestion
        rationale: Why this change is recommended (evidence-based)
        proposed_change: Specific change to make (e.g., 'strength_sessions: 2 -> 3')

    Returns confirmation with suggestion ID.
    """
    try:
        suggestion = {
            'type': suggestion_type,
            'description': description,
            'rationale': rationale,
            'proposed_change': proposed_change,
        }
        suggestion_id = save_suggestion(suggestion)
        return json.dumps({
            'status': 'pending',
            'suggestion_id': suggestion_id,
            'message': 'Suggestion saved. Awaiting user approval.'
        })
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def list_pending_suggestions() -> str:
    """
    Lists all pending suggestions awaiting user decision.

    Returns array of suggestions with id, type, description, and rationale.
    """
    try:
        pending = get_suggestions()
        return json.dumps(pending, indent=2)
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def approve_suggestion(suggestion_id: str) -> str:
    """
    Approve a pending suggestion.

    Args:
        suggestion_id: The ID of the suggestion to approve

    Returns the approved suggestion details or error if not found.
    """
    try:
        result = approve_sug(suggestion_id)
        if result:
            return json.dumps({
                'status': 'approved',
                'suggestion': result
            })
        else:
            return json.dumps({'error': f'Suggestion {suggestion_id} not found'})
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def reject_suggestion(suggestion_id: str, reason: str = None) -> str:
    """
    Reject a pending suggestion.

    Args:
        suggestion_id: The ID of the suggestion to reject
        reason: Optional reason for rejection

    Returns the rejected suggestion details or error if not found.
    """
    try:
        result = reject_sug(suggestion_id, reason)
        if result:
            return json.dumps({
                'status': 'rejected',
                'suggestion': result
            })
        else:
            return json.dumps({'error': f'Suggestion {suggestion_id} not found'})
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def research_race(name: str = None, url: str = None) -> str:
    """
    Research a race/event to gather training-relevant context.

    Fetches information like course profile, elevation, difficulty,
    typical conditions, and training recommendations.

    Args:
        name: Name of a configured race (will use its URL if available)
        url: Direct URL to research (overrides name lookup)

    Returns JSON with:
        - course_info: Distance, elevation, terrain
        - difficulty: Technical/physical demands
        - conditions: Typical weather, altitude
        - recommendations: Training focus areas
    """
    import requests

    try:
        # Load config for race lookup and thresholds
        config = load_training_config()

        # Get race analysis thresholds (with config.py fallbacks)
        race_analysis = config.get('race_analysis', {})
        elevation_threshold = race_analysis.get('elevation_significance_m', ELEVATION_SIGNIFICANCE_THRESHOLD)
        altitude_threshold = race_analysis.get('high_altitude_m', HIGH_ALTITUDE_THRESHOLD)

        # Get URL from race config if name provided
        if name and not url:
            events = config.get('events', [])
            name_lower = name.lower()
            for event in events:
                if name_lower in event.get('name', '').lower():
                    url = event.get('url')
                    if not url:
                        return json.dumps({
                            'error': f"Race '{event['name']}' has no URL. Provide one or add it with update_race()."
                        })
                    break
            else:
                return json.dumps({'error': f"No race found matching '{name}'"})

        if not url:
            return json.dumps({'error': 'Provide either a race name or URL'})

        # Fetch the page
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
        response = requests.get(url, headers=headers, timeout=HTTP_TIMEOUT_SECONDS)
        response.raise_for_status()

        # Extract text content (basic HTML stripping)
        from html.parser import HTMLParser
        from io import StringIO

        class HTMLStripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self.reset()
                self.strict = False
                self.convert_charrefs = True
                self.text = StringIO()
                self.skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style', 'nav', 'footer', 'header'):
                    self.skip = True

            def handle_endtag(self, tag):
                if tag in ('script', 'style', 'nav', 'footer', 'header'):
                    self.skip = False

            def handle_data(self, data):
                if not self.skip:
                    self.text.write(data + ' ')

            def get_text(self):
                return self.text.getvalue()

        stripper = HTMLStripper()
        stripper.feed(response.text)
        page_text = stripper.get_text()

        # Truncate to reasonable size for analysis
        page_text = page_text[:PAGE_TEXT_MAX_CHARS]

        # Build research summary
        # Look for key patterns in the text
        text_lower = page_text.lower()

        research = {
            'url': url,
            'raw_content_preview': page_text[:500] + '...',
            'detected_info': {},
            'training_relevance': []
        }

        # Detect distance
        import re
        distance_match = re.search(r'(\d+)\s*km', text_lower)
        if distance_match:
            research['detected_info']['distance_km'] = int(distance_match.group(1))

        # Detect elevation
        elevation_match = re.search(r'(\d[\d,]*)\s*m.*(?:elevation|climb|ascent)', text_lower)
        if elevation_match:
            elev = elevation_match.group(1).replace(',', '')
            research['detected_info']['elevation_m'] = int(elev)
            if int(elev) > elevation_threshold:
                research['training_relevance'].append('Significant climbing - include hill training')

        # Detect duration hints
        if 'stage' in text_lower or 'multi-day' in text_lower or 'day 1' in text_lower:
            research['detected_info']['multi_day'] = True
            research['training_relevance'].append('Multi-day event - build back-to-back endurance')

        # Detect terrain type
        if 'technical' in text_lower or 'singletrack' in text_lower:
            research['detected_info']['technical_terrain'] = True
            research['training_relevance'].append('Technical terrain - practice bike handling skills')

        if 'gravel' in text_lower:
            research['detected_info']['surface'] = 'gravel'
        elif 'road' in text_lower and 'off-road' not in text_lower:
            research['detected_info']['surface'] = 'road'
        elif 'trail' in text_lower or 'mountain' in text_lower:
            research['detected_info']['surface'] = 'trail/mtb'

        # Detect altitude
        altitude_match = re.search(r'(\d[\d,]*)\s*m.*(?:altitude|above sea level)', text_lower)
        if altitude_match:
            alt = int(altitude_match.group(1).replace(',', ''))
            if alt > altitude_threshold:
                research['detected_info']['high_altitude'] = True
                research['training_relevance'].append(f'High altitude ({alt}m) - consider acclimatization')

        # Add general note
        research['note'] = 'Review raw_content_preview for additional context. Use this info to adjust training focus.'

        return json.dumps(research, indent=2)

    except requests.RequestException as e:
        return json.dumps({'error': f'Failed to fetch URL: {str(e)}'})
    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def list_races() -> str:
    """
    Lists all configured races/events with priority and days until.

    Returns JSON array of events sorted by date with:
    - name, date, priority (A/B/C), type, distance, days_until
    """
    try:
        config = load_training_config()
        events = config.get('events', [])
        today = date.today()

        result = []
        for event in events:
            event_copy = event.copy()
            try:
                event_date = date.fromisoformat(event.get('date', ''))
                event_copy['days_until'] = (event_date - today).days
            except ValueError:
                event_copy['days_until'] = None
            result.append(event_copy)

        # Sort by date
        result.sort(key=lambda e: e.get('date', ''))

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def add_race(
    name: str,
    race_date: str,
    priority: str,
    race_type: str = None,
    distance_km: float = None,
    duration_days: int = 1,
    target_time: str = None,
    url: str = None,
    notes: str = None
) -> str:
    """
    Add a new race/event to the training calendar.

    Args:
        name: Event name (e.g., "Cape Town Cycle Tour")
        race_date: Start date in YYYY-MM-DD format
        priority: A (goal race), B (important), or C (training race)
        race_type: Type of event (e.g., "road_cycling", "mtb", "running", "tournament")
        distance_km: Distance in kilometers (if applicable)
        duration_days: Number of days (default 1, use >1 for stage races/tournaments)
        target_time: Target finish time (optional)
        url: Event website URL (optional)
        notes: Additional notes (optional)

    Returns confirmation with updated event list.
    """
    try:
        config = load_training_config()
        events = config.get('events', [])

        # Validate priority
        if priority.upper() not in VALID_PRIORITIES:
            return json.dumps({'error': 'Priority must be A, B, or C'})

        # Check for duplicate priority (only 1 race per priority allowed)
        existing_with_priority = [e for e in events if e.get('priority') == priority.upper()]
        if existing_with_priority:
            existing_name = existing_with_priority[0].get('name', 'Unknown')
            return json.dumps({
                'error': f"Priority {priority.upper()} already assigned to '{existing_name}'. "
                         f"Only one race per priority allowed. Update or remove the existing race first."
            })

        # Build new event
        new_event = {
            'date': race_date,
            'name': name,
            'priority': priority.upper(),
        }

        if race_type:
            new_event['type'] = race_type
        if distance_km:
            new_event['distance_km'] = distance_km
        if duration_days > 1:
            new_event['duration_days'] = duration_days
            # Calculate end date
            start = date.fromisoformat(race_date)
            end = start + timedelta(days=duration_days - 1)
            new_event['end_date'] = end.isoformat()
        if target_time:
            new_event['target_time'] = target_time
        if url:
            new_event['url'] = url
        if notes:
            new_event['notes'] = notes

        events.append(new_event)

        # Sort by date
        events.sort(key=lambda e: e.get('date', ''))

        # Save back
        config['events'] = events
        from planner import save_json_file
        save_json_file('training_config.json', config)

        return json.dumps({
            'status': 'success',
            'message': f"Added {priority.upper()}-race: {name} on {race_date}",
            'event': new_event
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def remove_race(name: str) -> str:
    """
    Remove a race/event from the training calendar.

    Args:
        name: Name of the event to remove (case-insensitive partial match)

    Returns confirmation or error if not found.
    """
    try:
        config = load_training_config()
        events = config.get('events', [])

        # Find matching event
        name_lower = name.lower()
        matching = [e for e in events if name_lower in e.get('name', '').lower()]

        if not matching:
            return json.dumps({'error': f"No event found matching '{name}'"})

        if len(matching) > 1:
            names = [e.get('name') for e in matching]
            return json.dumps({
                'error': f"Multiple matches found: {names}. Be more specific."
            })

        removed = matching[0]
        events.remove(removed)

        # Save back
        config['events'] = events
        from planner import save_json_file
        save_json_file('training_config.json', config)

        return json.dumps({
            'status': 'success',
            'message': f"Removed: {removed.get('name')}",
            'removed_event': removed
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_race(
    name: str,
    new_date: str = None,
    new_priority: str = None,
    new_name: str = None,
    target_time: str = None,
    distance_km: float = None,
    notes: str = None,
    url: str = None
) -> str:
    """
    Update any field of an existing race/event.

    Args:
        name: Name of the event to update (case-insensitive partial match)
        new_date: New date in YYYY-MM-DD format (optional)
        new_priority: New priority A/B/C (optional)
        new_name: Rename the event (optional)
        target_time: Target finish time (optional)
        distance_km: Distance in km (optional)
        notes: Updated notes (optional)
        url: Event URL (optional)

    Returns confirmation with updated event.
    """
    try:
        config = load_training_config()
        events = config.get('events', [])

        # Find matching event
        name_lower = name.lower()
        for event in events:
            if name_lower in event.get('name', '').lower():
                changes = []

                if new_date:
                    event['date'] = new_date
                    changes.append(f"date -> {new_date}")
                    # Update end_date if multi-day
                    if event.get('duration_days', 1) > 1:
                        start = date.fromisoformat(new_date)
                        end = start + timedelta(days=event['duration_days'] - 1)
                        event['end_date'] = end.isoformat()

                if new_priority:
                    if new_priority.upper() not in VALID_PRIORITIES:
                        return json.dumps({'error': 'Priority must be A, B, or C'})
                    # Check for duplicate priority (only 1 race per priority allowed)
                    # Exclude current event from check
                    other_events = [e for e in events if e.get('name') != event.get('name')]
                    existing_with_priority = [e for e in other_events if e.get('priority') == new_priority.upper()]
                    if existing_with_priority:
                        existing_name = existing_with_priority[0].get('name', 'Unknown')
                        return json.dumps({
                            'error': f"Priority {new_priority.upper()} already assigned to '{existing_name}'. "
                                     f"Only one race per priority allowed."
                        })
                    event['priority'] = new_priority.upper()
                    changes.append(f"priority -> {new_priority.upper()}")

                if new_name:
                    event['name'] = new_name
                    changes.append(f"name -> {new_name}")

                if target_time:
                    event['target_time'] = target_time
                    changes.append(f"target_time -> {target_time}")

                if distance_km:
                    event['distance_km'] = distance_km
                    changes.append(f"distance -> {distance_km}km")

                if notes:
                    event['notes'] = notes
                    changes.append("notes updated")

                if url:
                    event['url'] = url
                    changes.append("url updated")

                if not changes:
                    return json.dumps({'error': 'No updates provided'})

                # Re-sort by date
                events.sort(key=lambda e: e.get('date', ''))

                # Save back
                config['events'] = events
                from planner import save_json_file
                save_json_file('training_config.json', config)

                return json.dumps({
                    'status': 'success',
                    'message': f"Updated {event['name']}: {', '.join(changes)}",
                    'event': event
                }, indent=2)

        return json.dumps({'error': f"No event found matching '{name}'"})

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_compliance_report(days: int = 7) -> str:
    """
    Generates weekly pillar compliance report.

    Analyzes recent activities against training pillars (strength, mobility,
    long effort) and volume targets defined in training_config.json.

    Args:
        days: Number of days to analyze (default 7 for weekly report)

    Returns:
        JSON with compliance status for each pillar, deficits, and safety warnings.
    """
    try:
        client = get_garmin_client()
        today = date.today()
        start_date = today - timedelta(days=days)

        # Get activities for the period
        raw_activities = client.get_activities_by_date(
            start_date.isoformat(),
            today.isoformat()
        )
        activities = parse_activities(raw_activities)

        # Check compliance against pillars
        compliance = check_weekly_compliance(activities)

        # Check safety rules
        safety = check_safety_rules(activities)

        # Get upcoming events for context
        upcoming = get_upcoming_events(days_ahead=56)

        report = {
            'period': {
                'start': start_date.isoformat(),
                'end': today.isoformat(),
                'days': days,
            },
            'compliance': compliance,
            'safety': safety,
            'upcoming_events': upcoming[:3],  # Next 3 events
            'activities_count': len(activities),
        }

        return json.dumps(report, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def diagnose_injury(location: str, answers: str = None) -> str:
    """
    Clinical assessment tool for sports injuries. Uses a two-phase approach.

    Phase 1 - Get assessment questions:
        Call with just location to receive clinical questions to ask the athlete.
        Example: diagnose_injury(location="shin")

    Phase 2 - Get diagnosis:
        Call with location + answers (JSON string) to receive diagnosis.
        Example: diagnose_injury(location="shin", answers='{"onset": "Gradual", ...}')

    Args:
        location: Body part (shin, knee, ankle, back, shoulder, hip, foot, calf)
        answers: JSON string of answers to assessment questions (optional)

    Returns:
        Phase 1: JSON with clinical assessment questions
        Phase 2: JSON with possible conditions, severity, and recommendations
    """
    from config import INJURY_ASSESSMENT_QUESTIONS, INJURY_SEVERITY_LEVELS

    try:
        location_lower = location.lower().strip()

        # Map common terms to our body regions
        location_map = {
            "left shin": "shin", "right shin": "shin",
            "left knee": "knee", "right knee": "knee",
            "left ankle": "ankle", "right ankle": "ankle",
            "lower back": "back", "upper back": "back",
            "left shoulder": "shoulder", "right shoulder": "shoulder",
            "left hip": "hip", "right hip": "hip",
            "left foot": "foot", "right foot": "foot",
            "left calf": "calf", "right calf": "calf",
        }

        # Extract body region
        body_region = location_map.get(location_lower, location_lower)
        for key in location_map:
            if key in location_lower:
                body_region = location_map[key]
                break

        # Phase 1: Return assessment questions
        if answers is None:
            # Get default questions + region-specific questions
            questions = INJURY_ASSESSMENT_QUESTIONS.get("default", []).copy()
            region_questions = INJURY_ASSESSMENT_QUESTIONS.get(body_region, [])
            questions.extend(region_questions)

            return json.dumps({
                "location": location,
                "body_region": body_region,
                "phase": "assessment",
                "questions": questions,
                "instructions": "Ask these questions to gather clinical information, then call again with answers."
            }, indent=2)

        # Phase 2: Analyze answers and provide diagnosis
        try:
            answers_dict = json.loads(answers)
        except json.JSONDecodeError as e:
            return json.dumps({"error": f"Invalid answers JSON: {str(e)}"})

        # Build clinical picture from answers
        clinical_picture = {
            "location": location,
            "body_region": body_region,
            "onset": "acute" if "sudden" in answers_dict.get("onset", "").lower() else "gradual",
            "pain_type": answers_dict.get("pain_type", "unknown"),
            "timing": answers_dict.get("timing", "unknown"),
            "swelling": "yes" in answers_dict.get("swelling", "").lower(),
            "location_specific": answers_dict.get("location_specific", ""),
            "aggravating_factors": answers_dict.get("aggravating", ""),
            "history": "yes" in answers_dict.get("history", "").lower(),
            "recent_changes": answers_dict.get("recent_changes", ""),
        }

        # Determine severity based on answers
        severity = "mild"
        severity_reasoning = []

        if "rest" in answers_dict.get("timing", "").lower() or "constant" in answers_dict.get("timing", "").lower():
            severity = "severe"
            severity_reasoning.append("Pain at rest or constant pain")
        elif "during and after" in answers_dict.get("timing", "").lower():
            severity = "moderate"
            severity_reasoning.append("Pain persists after activity")

        if "significant" in answers_dict.get("swelling", "").lower():
            severity = "severe"
            severity_reasoning.append("Significant swelling present")
        elif "slight" in answers_dict.get("swelling", "").lower():
            if severity == "mild":
                severity = "moderate"
            severity_reasoning.append("Some swelling present")

        if not severity_reasoning:
            severity_reasoning.append("Pain only during activity, no swelling")

        # Build possible conditions based on location and symptoms
        possible_conditions = _get_possible_conditions(body_region, clinical_picture, answers_dict)

        # Determine red flags
        red_flags = []
        if severity == "severe":
            red_flags.append("Severe symptoms present - consider professional evaluation")
        if "radiation" in answers_dict and "past" in answers_dict.get("radiation", "").lower():
            red_flags.append("Radiating pain may indicate nerve involvement")
        if answers_dict.get("weight_bearing") == "No, too painful":
            red_flags.append("Unable to bear weight - may need imaging")
        if answers_dict.get("weakness") and "significant" in answers_dict.get("weakness", "").lower():
            red_flags.append("Significant weakness - may indicate tear or nerve issue")

        # Build recommendations
        if severity == "severe":
            recommended_action = "Rest immediately. Ice if swelling. See a healthcare professional within 24-48 hours."
        elif severity == "moderate":
            recommended_action = "Rest from aggravating activities. Ice 15-20 mins 3x/day. See physio if no improvement in 7-10 days."
        else:
            recommended_action = "Reduce activity intensity. Ice after exercise. Monitor for worsening symptoms."

        return json.dumps({
            "location": location,
            "phase": "diagnosis",
            "clinical_picture": clinical_picture,
            "possible_conditions": possible_conditions,
            "severity_assessment": severity,
            "severity_reasoning": severity_reasoning,
            "red_flags": red_flags if red_flags else ["None identified based on assessment"],
            "recommended_action": recommended_action,
            "red_flags_to_watch": [
                "Pain becoming severe or constant",
                "Swelling increases",
                "Unable to bear weight",
                "Numbness or tingling",
                "No improvement after 7-10 days rest"
            ],
            "disclaimer": "This is informational only, not medical advice. Consult a healthcare professional for diagnosis and treatment."
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


def _get_possible_conditions(body_region: str, clinical: dict, answers: dict) -> list:
    """Helper to determine possible conditions based on body region and symptoms."""

    conditions = []

    if body_region == "shin":
        if "anterior" in clinical.get("location_specific", "").lower() or "front" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Anterior Tibialis Tendinitis",
                "likelihood": "high" if clinical["onset"] == "gradual" else "medium",
                "description": "Inflammation of the anterior tibialis tendon from overuse",
                "why_matches": [f for f in [
                    "Front of shin" if "anterior" in clinical.get("location_specific", "").lower() else None,
                    "Gradual onset" if clinical["onset"] == "gradual" else None,
                    "Volume increase" if "increase" in answers.get("recent_changes", "").lower() else None,
                ] if f],
                "typical_causes": ["Overuse", "Sudden volume increase", "Tight calves", "Hill running"],
            })
        if "medial" in clinical.get("location_specific", "").lower() or "along the bone" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Shin Splints (MTSS)",
                "likelihood": "high" if clinical["onset"] == "gradual" else "medium",
                "description": "Medial tibial stress syndrome - inflammation along the shin bone",
                "why_matches": [f for f in [
                    "Medial/bone location",
                    "Gradual onset" if clinical["onset"] == "gradual" else None,
                    "Running aggravates" if "running" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Overuse", "Hard surfaces", "Poor footwear", "Flat feet"],
            })
        if clinical["swelling"] and clinical["onset"] == "gradual":
            conditions.append({
                "name": "Stress Fracture (tibial)",
                "likelihood": "low",
                "description": "Micro-fracture of the tibia from repetitive stress",
                "why_matches": ["Gradual onset with swelling - needs professional evaluation"],
                "typical_causes": ["Overtraining", "Rapid volume increase", "Poor bone density"],
                "warning": "If pain is localized to one spot and severe, seek imaging"
            })

    elif body_region == "knee":
        if "front" in clinical.get("location_specific", "").lower() or "kneecap" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Patellofemoral Pain Syndrome (Runner's Knee)",
                "likelihood": "high",
                "description": "Pain around or behind the kneecap",
                "why_matches": [f for f in [
                    "Front/kneecap location",
                    "Worse with stairs" if "stairs" in answers.get("aggravating", "").lower() else None,
                    "Worse with squatting" if "squat" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Muscle imbalance", "Overuse", "Poor tracking"],
            })
        if "lateral" in clinical.get("location_specific", "").lower() or "outer" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "IT Band Syndrome",
                "likelihood": "high" if "running" in answers.get("aggravating", "").lower() else "medium",
                "description": "Inflammation where IT band crosses the knee",
                "why_matches": [f for f in [
                    "Lateral/outer location",
                    "Running aggravates" if "running" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Running", "Cycling", "Weak hip abductors"],
            })

    elif body_region == "ankle":
        if "lateral" in clinical.get("location_specific", "").lower() or "outer" in clinical.get("location_specific", "").lower():
            if clinical["onset"] == "acute":
                conditions.append({
                    "name": "Lateral Ankle Sprain",
                    "likelihood": "high",
                    "description": "Stretching or tearing of lateral ankle ligaments",
                    "why_matches": ["Sudden onset", "Outer ankle location"],
                    "typical_causes": ["Rolling ankle inward", "Uneven surface", "Landing awkwardly"],
                })
            else:
                conditions.append({
                    "name": "Peroneal Tendinitis",
                    "likelihood": "high",
                    "description": "Inflammation of tendons on outer ankle",
                    "why_matches": ["Gradual onset", "Lateral location"],
                    "typical_causes": ["Overuse", "Running on uneven surfaces"],
                })
        if "achilles" in clinical.get("location_specific", "").lower() or "back" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Achilles Tendinitis/Tendinopathy",
                "likelihood": "high",
                "description": "Inflammation or degeneration of the Achilles tendon",
                "why_matches": [f for f in [
                    "Posterior/Achilles location",
                    "Gradual onset" if clinical["onset"] == "gradual" else None,
                    "Worse with pushing off" if "push" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Overuse", "Tight calves", "Hill running", "Sudden volume increase"],
            })

    elif body_region == "foot":
        if "heel" in clinical.get("location_specific", "").lower() and "bottom" in clinical.get("location_specific", "").lower():
            conditions.append({
                "name": "Plantar Fasciitis",
                "likelihood": "high" if "first steps" in answers.get("aggravating", "").lower() else "medium",
                "description": "Inflammation of the plantar fascia under the foot",
                "why_matches": [f for f in [
                    "Heel/bottom location",
                    "Worse with first steps in morning" if "morning" in answers.get("aggravating", "").lower() else None,
                ] if f],
                "typical_causes": ["Overuse", "Tight calves", "Poor arch support", "Sudden volume increase"],
            })

    elif body_region == "calf":
        if clinical["onset"] == "acute":
            conditions.append({
                "name": "Calf Muscle Strain",
                "likelihood": "high",
                "description": "Tear or strain of gastrocnemius or soleus muscle",
                "why_matches": ["Sudden onset", "Calf location"],
                "typical_causes": ["Explosive movement", "Sprinting", "Jumping", "Fatigue"],
            })
        else:
            conditions.append({
                "name": "Calf Muscle Tightness/Overuse",
                "likelihood": "high",
                "description": "Muscle fatigue and tightness from overuse",
                "why_matches": ["Gradual onset"],
                "typical_causes": ["Overtraining", "Inadequate stretching", "Volume increase"],
            })

    # If no specific conditions matched, add general options
    if not conditions:
        conditions.append({
            "name": "Soft Tissue Injury (unspecified)",
            "likelihood": "medium",
            "description": f"Injury to {body_region} area - may be muscular, tendon, or ligament",
            "why_matches": ["Location and symptoms suggest soft tissue involvement"],
            "typical_causes": ["Overuse", "Trauma", "Muscle imbalance"],
        })

    return conditions


@mcp.tool()
def research_injury(injury_type: str, severity: str = "moderate", url: str = None) -> str:
    """
    Research treatment protocols and recovery timelines for a specific injury.

    Fetches information from provided URL or tries common medical sources.
    Each injury is researched uniquely rather than using static protocols.

    Args:
        injury_type: Name of the injury (e.g., "anterior tibialis tendinitis")
        severity: mild, moderate, or severe (provides context for research)
        url: Optional direct URL to a resource about this injury

    Returns:
        JSON with researched information including treatment approaches,
        recovery expectations, and sources.

    Usage patterns:
        1. Direct URL: research_injury("shin splints", url="https://en.wikipedia.org/wiki/Shin_splints")
        2. Auto-search: research_injury("shin splints") - tries Wikipedia and other sources
    """
    from config import INJURY_SEVERITY_LEVELS, HTTP_TIMEOUT_SECONDS, PAGE_TEXT_MAX_CHARS
    import requests
    import re

    try:
        if severity.lower() not in INJURY_SEVERITY_LEVELS:
            severity = "moderate"

        research_result = {
            "injury": injury_type,
            "severity": severity,
            "researched_info": {},
            "sources": [],
            "raw_findings": [],
        }

        # Format injury name for URL
        injury_url_name = injury_type.replace(' ', '_')

        # Build list of URLs to try
        if url:
            # Use provided URL first
            search_sources = [{"name": "Provided URL", "url": url, "type": "direct"}]
        else:
            # Try common medical sources
            search_sources = []

        # Always add fallback sources
        search_sources.extend([
            # Wikipedia - usually accessible and has good medical content
            {
                "name": "Wikipedia",
                "url": f"https://en.wikipedia.org/wiki/{injury_url_name}",
                "type": "clinical"
            },
        ])

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        # HTML text extractor
        from html.parser import HTMLParser
        from io import StringIO

        class HTMLStripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self.reset()
                self.strict = False
                self.convert_charrefs = True
                self.text = StringIO()
                self.skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style', 'nav', 'footer', 'header', 'aside'):
                    self.skip = True

            def handle_endtag(self, tag):
                if tag in ('script', 'style', 'nav', 'footer', 'header', 'aside'):
                    self.skip = False

            def handle_data(self, data):
                if not self.skip:
                    self.text.write(data + ' ')

            def get_text(self):
                return self.text.getvalue()

        # Try to fetch from sports medicine sources
        fetched_content = None
        for source in search_sources:
            try:
                response = requests.get(
                    source["url"],
                    headers=headers,
                    timeout=HTTP_TIMEOUT_SECONDS,
                    allow_redirects=True
                )
                if response.status_code == 200:
                    stripper = HTMLStripper()
                    stripper.feed(response.text)
                    content = stripper.get_text()[:PAGE_TEXT_MAX_CHARS]

                    # Check if we got meaningful clinical content (not just a 404 or search page)
                    content_lower = content.lower()
                    clinical_indicators = ['treatment', 'diagnosis', 'symptoms', 'rehabilitation', 'causes', 'clinical', 'management']
                    has_clinical_content = any(ind in content_lower for ind in clinical_indicators)

                    if len(content) > 500 and has_clinical_content:
                        fetched_content = content
                        research_result["sources"].append(response.url)  # Use actual URL (after redirects)
                        break
            except Exception:
                continue

        # Extract relevant information from fetched content
        if fetched_content:
            content_lower = fetched_content.lower()

            # Look for treatment-related content
            treatment_keywords = ["treatment", "management", "therapy", "intervention"]
            rehab_keywords = ["rehabilitation", "exercise", "stretching", "strengthening"]
            timeline_keywords = ["recovery", "healing", "duration", "weeks", "days"]

            # Extract sentences containing relevant keywords
            sentences = re.split(r'[.!?]+', fetched_content)
            treatment_findings = []
            rehab_findings = []
            timeline_findings = []

            for sentence in sentences:
                sentence_clean = sentence.strip()
                sentence_lower = sentence_clean.lower()
                if len(sentence_clean) > 20:  # Skip very short fragments
                    if any(kw in sentence_lower for kw in treatment_keywords):
                        treatment_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in rehab_keywords):
                        rehab_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in timeline_keywords):
                        timeline_findings.append(sentence_clean)

            research_result["researched_info"] = {
                "treatment_approaches": treatment_findings[:5] if treatment_findings else ["Research specific treatment protocols with your physiotherapist"],
                "rehabilitation": rehab_findings[:5] if rehab_findings else ["Gradual return to activity under professional guidance"],
                "recovery_insights": timeline_findings[:3] if timeline_findings else ["Recovery time varies based on severity and individual factors"],
            }

            # Try to extract specific recommendations
            research_result["raw_findings"] = {
                "content_preview": fetched_content[:1000],
                "note": "Review the content above for detailed information specific to this injury"
            }

        else:
            # Couldn't fetch - provide guidance on what to research
            research_result["researched_info"] = {
                "note": f"Unable to fetch current research for '{injury_type}'. Recommend searching:",
                "suggested_searches": [
                    f"{injury_type} treatment protocol",
                    f"{injury_type} rehabilitation exercises",
                    f"{injury_type} recovery timeline athlete",
                    f"{injury_type} return to sport criteria",
                ],
                "recommended_sources": [
                    "physio-pedia.com",
                    "sportsinjuryclinic.net",
                    "Your local sports physiotherapist",
                ]
            }

        # Add severity-based general guidance
        severity_guidance = {
            "mild": {
                "general_approach": "Often manageable with relative rest and self-care",
                "typical_timeframe": "Usually improves within 1-3 weeks with appropriate management",
                "professional_advice": "See a physio if no improvement after 7-10 days",
            },
            "moderate": {
                "general_approach": "May require modified activities and structured rehabilitation",
                "typical_timeframe": "Expect 3-6 weeks for significant improvement",
                "professional_advice": "Professional assessment recommended for proper diagnosis and treatment plan",
            },
            "severe": {
                "general_approach": "Requires professional evaluation and structured treatment",
                "typical_timeframe": "Recovery often takes 6-12+ weeks",
                "professional_advice": "See a healthcare professional promptly - may need imaging or specialist referral",
            },
        }

        research_result["severity_context"] = severity_guidance.get(severity, severity_guidance["moderate"])

        # Activity guidance based on injury location/type
        injury_lower = injury_type.lower()
        if any(term in injury_lower for term in ["shin", "tibialis", "calf", "achilles", "plantar", "foot", "ankle"]):
            research_result["activity_guidance"] = {
                "likely_safe": ["cycling", "swimming", "upper body strength", "core work"],
                "likely_restricted": ["running", "jumping", "high-impact activities"],
                "note": "Confirm specific restrictions with your physiotherapist based on your assessment"
            }
        elif any(term in injury_lower for term in ["knee", "patella", "it band", "meniscus"]):
            research_result["activity_guidance"] = {
                "likely_safe": ["swimming", "upper body strength", "non-weight-bearing activities"],
                "likely_restricted": ["running", "squatting", "stairs", "cycling (depends on injury)"],
                "note": "Confirm specific restrictions with your physiotherapist based on your assessment"
            }
        elif any(term in injury_lower for term in ["shoulder", "rotator"]):
            research_result["activity_guidance"] = {
                "likely_safe": ["lower body activities", "walking", "cycling", "core work"],
                "likely_restricted": ["overhead movements", "swimming (depends)", "pushing/pulling"],
                "note": "Confirm specific restrictions with your physiotherapist based on your assessment"
            }
        elif any(term in injury_lower for term in ["back", "spine", "disc"]):
            research_result["activity_guidance"] = {
                "likely_safe": ["walking", "swimming", "gentle movement"],
                "likely_restricted": ["heavy lifting", "high-impact", "prolonged sitting"],
                "note": "Back injuries vary significantly - professional assessment essential"
            }
        else:
            research_result["activity_guidance"] = {
                "general": "Avoid activities that aggravate symptoms",
                "cross_training": "Usually possible to maintain fitness with alternative activities",
                "note": "Confirm specific restrictions with your physiotherapist"
            }

        research_result["disclaimer"] = "This is researched information, not medical advice. Each injury is unique - consult a healthcare professional for diagnosis and personalized treatment."

        return json.dumps(research_result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def list_exercises(
    category: str = None,
    muscle: str = None,
    injury_prevention: str = None,
    search: str = None,
    limit: int = 50
) -> str:
    """
    Browse the exercise library with optional filters.

    Args:
        category: Filter by Garmin category (e.g., "DEADLIFT", "SQUAT", "PLANK")
        muscle: Filter by muscle group (e.g., "hamstrings", "glutes", "core")
        injury_prevention: Filter exercises for injury prevention (e.g., "hamstring", "knee", "ankle")
        search: Text search in exercise names
        limit: Max results to return (default 50)

    Returns:
        JSON with matching exercises and available categories/muscles.
    """
    try:
        # Load exercise library
        exercises_file = DATA_DIR / "exercises.json"
        if not exercises_file.exists():
            return json.dumps({
                "error": "Exercise library not found. Run fetch_exercises.py first.",
                "hint": "python fetch_exercises.py"
            })

        with open(exercises_file) as f:
            library = json.load(f)

        exercises = library.get("exercises", {})
        categories = library.get("categories", [])
        injury_mappings = library.get("injury_mappings", {})

        # Build result
        result = {
            "filters_applied": {},
            "matches": [],
            "total_in_library": len(exercises),
        }

        # Apply filters
        matches = []
        for name, data in exercises.items():
            include = True

            # Category filter
            if category:
                if category.upper() != data.get("garmin_category", "").upper():
                    include = False

            # Muscle filter
            if muscle and include:
                muscles = data.get("muscles", [])
                if not any(muscle.lower() in m.lower() for m in muscles):
                    include = False

            # Injury prevention filter
            if injury_prevention and include:
                prevention = data.get("injury_prevention", [])
                if not any(injury_prevention.lower() in p.lower() for p in prevention):
                    include = False

            # Text search
            if search and include:
                if search.lower() not in name.lower():
                    include = False

            if include:
                matches.append({
                    "name": name,
                    "category": data.get("garmin_category"),
                    "primary_muscles": data.get("primary_muscles", []),
                    "secondary_muscles": data.get("secondary_muscles", []),
                    "injury_prevention": data.get("injury_prevention", []),
                })

        # Apply limit
        result["matches"] = matches[:limit]
        result["match_count"] = len(matches)

        # Record applied filters
        if category:
            result["filters_applied"]["category"] = category
        if muscle:
            result["filters_applied"]["muscle"] = muscle
        if injury_prevention:
            result["filters_applied"]["injury_prevention"] = injury_prevention
        if search:
            result["filters_applied"]["search"] = search

        # Include available options if no filters
        if not any([category, muscle, injury_prevention, search]):
            result["available_categories"] = categories[:20]
            result["categories_note"] = f"{len(categories)} total categories"
            result["injury_prevention_types"] = list(injury_mappings.keys())

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def generate_strength_workout(
    focus: str = "full_body",
    duration_mins: int = 45,
    equipment: str = "gym"
) -> str:
    """
    Generate a smart strength workout based on context.

    Automatically adjusts based on:
    - Recent activities (avoids legs after long cycle/frisbee/padel)
    - Injury history (always includes relevant prehab exercises)
    - Current training load and recovery

    Args:
        focus: Target area - "upper_body", "lower_body", "full_body", "core"
               Will be auto-adjusted based on recent activities.
        duration_mins: Target workout duration (default 45)
        equipment: "gym", "home", "minimal" (affects exercise selection)

    Returns:
        JSON with exercises, sets, reps, rationale, and auto-adjustments.
    """
    try:
        client = get_garmin_client()
        today = date.today()

        # Get today's and recent activities
        yesterday = today - timedelta(days=1)
        raw_activities = client.get_activities_by_date(
            yesterday.isoformat(),
            today.isoformat()
        )
        recent_activities = parse_activities(raw_activities)

        # Get athlete profile for injury history
        athlete = load_athlete()
        injury_history = athlete.get("injury_history", [])
        active_injuries = [i for i in injury_history if i.get("status") == "active"]
        past_injuries = [i for i in injury_history if i.get("status") in ["resolved", "improving"]]

        # Load exercise library
        exercises_file = DATA_DIR / "exercises.json"
        if not exercises_file.exists():
            return json.dumps({
                "error": "Exercise library not found. Run fetch_exercises.py first."
            })

        with open(exercises_file) as f:
            library = json.load(f)

        exercises = library.get("exercises", {})
        injury_mappings = library.get("injury_mappings", {})

        # Analyze recent activities for auto-adjustment
        adjustments = []
        original_focus = focus
        avoid_muscle_groups = set()
        reduce_volume_groups = set()

        for activity in recent_activities:
            activity_type = activity.get("type", "").lower()
            duration = activity.get("duration_mins", 0) or 0

            # Long cycle (>60min) today → reduce leg volume
            if "cycling" in activity_type or "ride" in activity_type:
                if duration > 60:
                    reduce_volume_groups.update(["quadriceps", "glutes", "hamstrings", "calves"])
                    adjustments.append(f"Long cycle ({int(duration)}min) - reducing leg volume")
                    if focus == "lower_body":
                        focus = "upper_body"
                        adjustments.append("Switched focus to upper_body")
                    elif focus == "full_body":
                        adjustments.append("Will limit leg exercises in full_body workout")

            # Ultimate/Frisbee/Padel → avoid legs completely
            if any(sport in activity_type for sport in ["ultimate", "frisbee", "padel", "tennis", "squash"]):
                avoid_muscle_groups.update(["quadriceps", "glutes", "hamstrings", "calves"])
                adjustments.append(f"{activity_type.title()} done - avoiding leg exercises")
                if focus in ["lower_body", "full_body"]:
                    focus = "upper_body"
                    adjustments.append("Switched focus to upper_body")

            # Running → reduce calf/quad work
            if "running" in activity_type or "run" in activity_type:
                if duration > 30:
                    reduce_volume_groups.update(["calves", "quadriceps"])
                    adjustments.append(f"Running done ({int(duration)}min) - reducing calf/quad work")

        # Handle active injuries - avoid affected areas
        for injury in active_injuries:
            injury_type = injury.get("type", "").lower()
            if "ankle" in injury_type or "peroneal" in injury_type:
                avoid_muscle_groups.update(["calves"])
                adjustments.append(f"Active {injury_type} - avoiding calf exercises")
            if "knee" in injury_type:
                avoid_muscle_groups.update(["quadriceps"])
                adjustments.append(f"Active {injury_type} - reducing quad exercises")
            if "shoulder" in injury_type:
                adjustments.append(f"Active {injury_type} - avoiding overhead pressing")
            if "back" in injury_type:
                adjustments.append(f"Active {injury_type} - avoiding heavy spinal loading")

        # Determine prehab exercises from injury history
        prehab_exercises = []
        for injury in injury_history:
            injury_type = injury.get("type", "").lower()

            # Find matching injury prevention exercises
            for prevention_type, exercise_list in injury_mappings.items():
                if prevention_type in injury_type or injury_type in prevention_type:
                    for ex_name in exercise_list:
                        if ex_name in exercises:
                            ex_data = exercises[ex_name]
                            ex_muscles = ex_data.get("muscles", [])

                            # Skip if this exercise targets avoided muscles
                            if any(m in avoid_muscle_groups for m in ex_muscles):
                                continue

                            if ex_name not in [p["name"] for p in prehab_exercises]:
                                prehab_exercises.append({
                                    "name": ex_name,
                                    "reason": f"Injury prevention ({injury_type})",
                                    "sets": 2,
                                    "reps": 10,
                                    "rest_secs": 45
                                })
                                break  # One exercise per injury type

        # Select main exercises based on focus
        workout_exercises = []

        # Exercise templates by focus
        focus_templates = {
            "upper_body": {
                "push": ["BENCH_PRESS", "DUMBBELL_BENCH_PRESS", "PUSH_UP", "SHOULDER_PRESS", "DUMBBELL_SHOULDER_PRESS"],
                "pull": ["BENT_OVER_ROW", "DUMBBELL_ROW", "LAT_PULLDOWN", "SEATED_ROW", "PULL_UP"],
                "accessory": ["BICEP_CURL", "DUMBBELL_CURL", "TRICEP_EXTENSION", "FACE_PULL", "LATERAL_RAISE"]
            },
            "lower_body": {
                "compound": ["SQUAT", "BARBELL_SQUAT", "LEG_PRESS", "DEADLIFT", "ROMANIAN_DEADLIFT"],
                "isolation": ["LEG_EXTENSION", "LEG_CURL", "HAMSTRING_CURL", "CALF_RAISE", "HIP_THRUST"],
                "unilateral": ["LUNGE", "BULGARIAN_SPLIT_SQUAT", "STEP_UP", "SINGLE_LEG_DEADLIFT"]
            },
            "full_body": {
                "upper_push": ["BENCH_PRESS", "PUSH_UP", "SHOULDER_PRESS"],
                "upper_pull": ["BENT_OVER_ROW", "LAT_PULLDOWN", "PULL_UP"],
                "lower": ["SQUAT", "DEADLIFT", "LUNGE", "LEG_PRESS"],
                "core": ["PLANK", "DEAD_BUG", "RUSSIAN_TWIST"]
            },
            "core": {
                "anti_extension": ["PLANK", "DEAD_BUG", "ROLLOUT"],
                "anti_rotation": ["PALLOF_PRESS", "SIDE_PLANK", "BIRD_DOG"],
                "flexion": ["CRUNCH", "HANGING_LEG_RAISE", "CABLE_CRUNCH"]
            }
        }

        template = focus_templates.get(focus, focus_templates["full_body"])

        # Select exercises from each group
        for group_name, exercise_list in template.items():
            # Skip leg exercises if avoiding
            if avoid_muscle_groups and group_name in ["lower", "compound", "isolation", "unilateral"]:
                continue

            # Find available exercise from library
            for ex_name in exercise_list:
                if ex_name in exercises:
                    ex_data = exercises[ex_name]
                    muscles = ex_data.get("muscles", [])

                    # Skip if targets avoided muscles
                    if any(m in avoid_muscle_groups for m in muscles):
                        continue

                    # Determine sets/reps based on muscle group and volume adjustment
                    sets = 3
                    reps = 10
                    if any(m in reduce_volume_groups for m in muscles):
                        sets = 2
                        reps = 8

                    workout_exercises.append({
                        "name": ex_name,
                        "category": ex_data.get("garmin_category"),
                        "primary_muscles": ex_data.get("primary_muscles", []),
                        "sets": sets,
                        "reps": reps,
                        "rest_secs": 60 if "compound" not in group_name else 90
                    })
                    break  # One per group

        # Add prehab exercises at the end (limit to 2)
        prehab_to_add = prehab_exercises[:2]

        # Calculate estimated duration
        main_exercise_time = sum(
            (ex["sets"] * 45 + (ex["sets"] - 1) * ex["rest_secs"])
            for ex in workout_exercises
        ) / 60

        prehab_time = sum(
            (ex["sets"] * 30 + (ex["sets"] - 1) * ex["rest_secs"])
            for ex in prehab_to_add
        ) / 60

        warmup_time = 5
        estimated_duration = warmup_time + main_exercise_time + prehab_time

        # Build result
        result = {
            "focus": focus,
            "original_focus": original_focus if focus != original_focus else None,
            "auto_adjustments": adjustments if adjustments else ["No adjustments needed"],
            "exercises": workout_exercises,
            "prehab_exercises": prehab_to_add,
            "estimated_duration_mins": round(estimated_duration),
            "workout_structure": {
                "warmup": "5 mins dynamic stretching",
                "main_sets": len(workout_exercises),
                "prehab_sets": len(prehab_to_add)
            },
            "active_injuries": [i.get("type") for i in active_injuries] if active_injuries else None,
            "avoid_muscles": list(avoid_muscle_groups) if avoid_muscle_groups else None,
            "note": "Review and adjust exercises based on available equipment and preferences."
        }

        # Remove None values
        result = {k: v for k, v in result.items() if v is not None}

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def add_exercise(
    name: str,
    category: str,
    primary_muscles: str,
    secondary_muscles: str = None,
    injury_prevention: str = None
) -> str:
    """
    Add a custom exercise to the library.

    Use this when the LLM suggests an exercise not in Garmin's database.

    Args:
        name: Exercise name in UPPERCASE_WITH_UNDERSCORES (e.g., "NORDIC_CURL")
        category: Garmin category to group under (e.g., "HAMSTRING_CURL", "CUSTOM")
        primary_muscles: Comma-separated primary muscles (e.g., "hamstrings,glutes")
        secondary_muscles: Comma-separated secondary muscles (optional)
        injury_prevention: Comma-separated injury types this prevents (e.g., "hamstring,knee")

    Returns:
        Confirmation with the added exercise details.

    Example:
        add_exercise(
            name="NORDIC_CURL",
            category="HAMSTRING_CURL",
            primary_muscles="hamstrings",
            injury_prevention="hamstring"
        )
    """
    try:
        # Load exercise library
        exercises_file = DATA_DIR / "exercises.json"
        if not exercises_file.exists():
            return json.dumps({
                "error": "Exercise library not found. Run fetch_exercises.py first."
            })

        with open(exercises_file) as f:
            library = json.load(f)

        exercises = library.get("exercises", {})

        # Normalize name
        normalized_name = name.upper().replace(" ", "_").replace("-", "_")

        # Check if already exists
        if normalized_name in exercises:
            return json.dumps({
                "error": f"Exercise '{normalized_name}' already exists",
                "existing": exercises[normalized_name]
            })

        # Parse muscle lists
        primary = [m.strip().lower() for m in primary_muscles.split(",")]
        secondary = [m.strip().lower() for m in secondary_muscles.split(",")] if secondary_muscles else []
        prevention = [p.strip().lower() for p in injury_prevention.split(",")] if injury_prevention else []

        # Create exercise entry
        new_exercise = {
            "category": category.upper(),
            "garmin_category": category.upper(),
            "garmin_name": normalized_name,
            "muscles": primary + secondary,
            "primary_muscles": primary,
            "secondary_muscles": secondary,
            "injury_prevention": prevention,
            "custom": True  # Mark as user-added
        }

        # Add to library
        exercises[normalized_name] = new_exercise

        # Track in custom_exercises list
        if "custom_exercises" not in library:
            library["custom_exercises"] = []
        if normalized_name not in library["custom_exercises"]:
            library["custom_exercises"].append(normalized_name)

        # Update metadata
        library["metadata"]["exercise_count"] = len(exercises)

        # Save
        with open(exercises_file, 'w') as f:
            json.dump(library, f, indent=2)

        return json.dumps({
            "status": "success",
            "message": f"Added custom exercise: {normalized_name}",
            "exercise": new_exercise,
            "total_exercises": len(exercises),
            "custom_exercises_count": len(library["custom_exercises"])
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def update_injury_status(
    injury_date: str,
    new_status: str = None,
    notes: str = None,
    severity: str = None
) -> str:
    """
    Update an existing injury's status and add progress notes.

    Args:
        injury_date: Date of the injury to update (YYYY-MM-DD)
        new_status: New status (active, improving, resolved)
        notes: Progress note to add
        severity: Updated severity (mild, moderate, severe)

    Returns:
        Confirmation with updated injury details.
    """
    from planner import save_json_file
    from config import ATHLETE_FILE, INJURY_STATUS_OPTIONS, INJURY_SEVERITY_LEVELS

    try:
        athlete = load_athlete()
        # Remove baseline data before saving
        athlete.pop('baseline', None)
        athlete.pop('personal_records', None)

        injury_history = athlete.get('injury_history', [])

        # Find the injury by date
        found_injury = None
        for injury in injury_history:
            if injury.get('date') == injury_date:
                found_injury = injury
                break

        if not found_injury:
            return json.dumps({
                "error": f"No injury found for date {injury_date}",
                "existing_injuries": [i.get('date') for i in injury_history]
            })

        changes = []

        if new_status:
            if new_status.lower() not in INJURY_STATUS_OPTIONS:
                return json.dumps({
                    "error": f"Invalid status. Must be one of: {INJURY_STATUS_OPTIONS}"
                })
            found_injury['status'] = new_status.lower()
            changes.append(f"status -> {new_status.lower()}")

        if severity:
            if severity.lower() not in INJURY_SEVERITY_LEVELS:
                return json.dumps({
                    "error": f"Invalid severity. Must be one of: {INJURY_SEVERITY_LEVELS}"
                })
            found_injury['severity'] = severity.lower()
            changes.append(f"severity -> {severity.lower()}")

        if notes:
            # Add to progress notes
            if 'progress_notes' not in found_injury:
                found_injury['progress_notes'] = []
            found_injury['progress_notes'].append({
                "date": date.today().isoformat(),
                "note": notes
            })
            changes.append("progress note added")

        if not changes:
            return json.dumps({"error": "No updates provided"})

        # Save back
        save_json_file(ATHLETE_FILE, athlete)

        return json.dumps({
            "status": "success",
            "message": f"Updated injury from {injury_date}: {', '.join(changes)}",
            "injury": found_injury
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


# =============================================================================
# GOAL BALANCE TRACKING
# =============================================================================

@mcp.tool()
def get_goal_progress(days: int = 14) -> str:
    """
    Get progress toward the three goal categories.

    Tracks balance across:
    - Race Preparation (50%): Training volume and key sessions
    - Fun Activities (25%): Padel, Ultimate, social activities
    - Aesthetics (25%): Strength sessions, upper body focus

    Args:
        days: Number of days to look back (default 14)

    Returns:
        JSON with progress for each goal category and recommendations.
    """
    try:
        client = get_garmin_client()
        today = date.today()
        start = (today - timedelta(days=days)).isoformat()

        # Get activities
        raw_activities = client.get_activities_by_date(start, today.isoformat())
        activities = parse_activities(raw_activities)

        # Load training config for goal definitions
        training_config = load_training_config()
        goal_balance = training_config.get('goal_balance', {})

        # Categorize activities
        race_prep_mins = 0
        fun_mins = 0
        aesthetics_mins = 0
        last_fun_date = None
        strength_count = 0

        fun_types = ['padel', 'ultimate_disc', 'social_ride', 'tennis', 'squash', 'badminton']
        strength_types = ['strength_training', 'indoor_cardio', 'functional_strength']

        for activity in activities:
            act_type = activity.get('type', '').lower()
            duration = activity.get('duration_mins', 0)
            act_date = activity.get('date')

            # Fun activities
            if any(f in act_type for f in fun_types):
                fun_mins += duration
                if last_fun_date is None or act_date > last_fun_date:
                    last_fun_date = act_date
            # Strength/aesthetics
            elif any(s in act_type for s in strength_types):
                aesthetics_mins += duration
                strength_count += 1
            # All others count as race prep
            else:
                race_prep_mins += duration

        total_mins = race_prep_mins + fun_mins + aesthetics_mins

        # Calculate percentages
        if total_mins > 0:
            race_prep_pct = round(race_prep_mins / total_mins * 100)
            fun_pct = round(fun_mins / total_mins * 100)
            aesthetics_pct = round(aesthetics_mins / total_mins * 100)
        else:
            race_prep_pct = fun_pct = aesthetics_pct = 0

        # Calculate days since last fun activity
        days_since_fun = None
        if last_fun_date:
            try:
                fun_date = date.fromisoformat(last_fun_date)
                days_since_fun = (today - fun_date).days
            except ValueError:
                pass

        # Generate recommendations
        recommendations = []
        prompt_fun = goal_balance.get('fun_activities', {}).get('prompt_if_missing_days', 14)

        if days_since_fun is not None and days_since_fun > prompt_fun:
            recommendations.append(f"Fun activity missing for {days_since_fun} days - schedule Padel or Frisbee soon!")
        elif days_since_fun is None:
            recommendations.append("No fun activities found recently - remember to include Padel or Frisbee!")

        if aesthetics_pct < 20 and strength_count < 2:
            recommendations.append("Upper body/aesthetics underrepresented - add a strength session")

        if race_prep_pct > 80:
            recommendations.append("Heavy race prep focus - make sure to balance with fun and gym")

        # Target vs actual
        targets = {
            'race_preparation': {'target': 50, 'actual': race_prep_pct},
            'fun_activities': {'target': 25, 'actual': fun_pct},
            'aesthetics': {'target': 25, 'actual': aesthetics_pct}
        }

        return json.dumps({
            'period_days': days,
            'total_training_mins': round(total_mins),
            'goal_progress': {
                'race_preparation': {
                    'mins': round(race_prep_mins),
                    'pct': race_prep_pct,
                    'target_pct': 50,
                    'status': 'on_track' if race_prep_pct >= 40 else 'low'
                },
                'fun_activities': {
                    'mins': round(fun_mins),
                    'pct': fun_pct,
                    'target_pct': 25,
                    'days_since_last': days_since_fun,
                    'status': 'on_track' if fun_pct >= 15 else ('missing' if days_since_fun and days_since_fun > prompt_fun else 'low')
                },
                'aesthetics': {
                    'mins': round(aesthetics_mins),
                    'pct': aesthetics_pct,
                    'target_pct': 25,
                    'strength_sessions': strength_count,
                    'status': 'on_track' if strength_count >= 2 else 'low'
                }
            },
            'recommendations': recommendations,
            'balance_score': 'good' if len(recommendations) == 0 else ('needs_attention' if len(recommendations) <= 1 else 'rebalance_needed')
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


# =============================================================================
# COACHING DECISION PERSISTENCE TOOLS
# =============================================================================

def load_coaching_log() -> dict[str, Any]:
    """Load the coaching log file."""
    return load_json_file('coaching_log.json')


def save_coaching_log(log: dict[str, Any]) -> None:
    """Save the coaching log file."""
    from planner import save_json_file
    log['metadata']['last_updated'] = date.today().isoformat()
    save_json_file('coaching_log.json', log)


@mcp.tool()
def log_coaching_decision(
    decision_type: str,
    decision: str,
    rationale: str,
    review_days: int = 7
) -> str:
    """
    Log a coaching decision for persistence across sessions.

    Use this to record significant coaching decisions that should influence
    future planning. Examples: volume adjustments, exercise modifications,
    phase-related changes.

    Args:
        decision_type: Category of decision (load_adjustment, exercise_selection,
                       intensity_change, recovery_protocol, injury_accommodation)
        decision: What was decided
        rationale: Why this decision was made (cite data)
        review_days: Days until this decision should be reviewed (default 7)

    Returns:
        Confirmation with the decision ID.
    """
    try:
        log = load_coaching_log()

        # Ensure structure exists
        if 'decisions' not in log:
            log['decisions'] = []
        if 'metadata' not in log:
            log['metadata'] = {'created': date.today().isoformat()}

        # Generate ID
        decision_count = len([d for d in log['decisions'] if d['date'] == date.today().isoformat()])
        decision_id = f"d_{date.today().strftime('%Y%m%d')}_{decision_count + 1:03d}"

        new_decision = {
            'id': decision_id,
            'date': date.today().isoformat(),
            'type': decision_type,
            'decision': decision,
            'rationale': rationale,
            'status': 'active',
            'outcome': None,
            'review_date': (date.today() + timedelta(days=review_days)).isoformat()
        }

        log['decisions'].append(new_decision)
        save_coaching_log(log)

        return json.dumps({
            'status': 'logged',
            'decision_id': decision_id,
            'message': f'Decision logged: {decision}',
            'review_date': new_decision['review_date']
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_active_decisions() -> str:
    """
    Get all active coaching decisions.

    Returns decisions that are currently influencing training plans.
    Use this at the start of planning to maintain continuity.

    Returns:
        List of active decisions with their rationale and review dates.
    """
    try:
        log = load_coaching_log()
        decisions = log.get('decisions', [])

        # Filter for active decisions
        active = [d for d in decisions if d.get('status') == 'active']

        # Also get decisions due for review
        today = date.today()
        due_for_review = []
        for d in active:
            review_date = d.get('review_date')
            if review_date:
                try:
                    review = date.fromisoformat(review_date)
                    if review <= today:
                        due_for_review.append(d['id'])
                except ValueError:
                    pass

        return json.dumps({
            'active_decisions': active,
            'count': len(active),
            'due_for_review': due_for_review,
            'note': 'These decisions should influence current planning'
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_decision_status(
    decision_id: str,
    new_status: str,
    outcome: str = None
) -> str:
    """
    Update the status of a coaching decision.

    Args:
        decision_id: ID of the decision to update
        new_status: New status (active, completed, superseded, cancelled)
        outcome: Optional outcome note (what happened as a result)

    Returns:
        Confirmation of the update.
    """
    try:
        log = load_coaching_log()
        decisions = log.get('decisions', [])

        valid_statuses = ['active', 'completed', 'superseded', 'cancelled']
        if new_status not in valid_statuses:
            return json.dumps({'error': f'Invalid status. Must be one of: {valid_statuses}'})

        for d in decisions:
            if d.get('id') == decision_id:
                d['status'] = new_status
                if outcome:
                    d['outcome'] = outcome
                d['status_updated'] = date.today().isoformat()

                save_coaching_log(log)
                return json.dumps({
                    'status': 'updated',
                    'decision_id': decision_id,
                    'new_status': new_status,
                    'outcome': outcome
                }, indent=2)

        return json.dumps({'error': f'Decision {decision_id} not found'})

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def propose_major_change(
    change_type: str,
    proposal: str,
    rationale: str,
    impact: str = "high"
) -> str:
    """
    Propose a major coaching change that requires user approval.

    Use this for significant changes like phase transitions, large volume
    adjustments, or goal rebalancing. The user must approve before these
    become active.

    Args:
        change_type: Type of change (phase_transition, volume_change_major,
                     goal_rebalance, skip_session, add_race)
        proposal: What change is being proposed
        rationale: Why this change is recommended (cite data)
        impact: Impact level (high, medium)

    Returns:
        Proposal ID for user to approve/reject.
    """
    from config import MAJOR_DECISION_TYPES

    try:
        log = load_coaching_log()

        if 'pending_approvals' not in log:
            log['pending_approvals'] = []
        if 'metadata' not in log:
            log['metadata'] = {'created': date.today().isoformat()}

        # Generate ID
        proposal_count = len(log['pending_approvals'])
        proposal_id = f"p_{date.today().strftime('%Y%m%d')}_{proposal_count + 1:03d}"

        new_proposal = {
            'id': proposal_id,
            'proposed_date': date.today().isoformat(),
            'type': change_type,
            'proposal': proposal,
            'rationale': rationale,
            'impact': impact,
            'expires': (date.today() + timedelta(days=3)).isoformat()
        }

        log['pending_approvals'].append(new_proposal)
        save_coaching_log(log)

        return json.dumps({
            'status': 'proposed',
            'proposal_id': proposal_id,
            'message': f'Proposal awaiting approval: {proposal}',
            'expires': new_proposal['expires'],
            'action_required': 'User must approve or reject this change'
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def list_pending_approvals() -> str:
    """
    List all pending coaching change proposals.

    Returns:
        List of proposals awaiting user approval.
    """
    try:
        log = load_coaching_log()
        pending = log.get('pending_approvals', [])

        # Filter out expired proposals
        today = date.today()
        active_pending = []
        expired = []
        for p in pending:
            expires = p.get('expires')
            if expires:
                try:
                    exp_date = date.fromisoformat(expires)
                    if exp_date < today:
                        expired.append(p['id'])
                        continue
                except ValueError:
                    pass
            active_pending.append(p)

        return json.dumps({
            'pending_approvals': active_pending,
            'count': len(active_pending),
            'expired': expired,
            'instructions': 'Use approve_coaching_change(id) or reject_coaching_change(id, reason) to act on proposals'
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def approve_coaching_change(proposal_id: str) -> str:
    """
    Approve a pending coaching change proposal.

    The approved change becomes an active decision.

    Args:
        proposal_id: ID of the proposal to approve

    Returns:
        Confirmation and the new active decision.
    """
    try:
        log = load_coaching_log()
        pending = log.get('pending_approvals', [])
        decisions = log.get('decisions', [])

        # Find the proposal
        found = None
        for i, p in enumerate(pending):
            if p.get('id') == proposal_id:
                found = pending.pop(i)
                break

        if not found:
            return json.dumps({'error': f'Proposal {proposal_id} not found'})

        # Convert to active decision
        decision_count = len([d for d in decisions if d['date'] == date.today().isoformat()])
        decision_id = f"d_{date.today().strftime('%Y%m%d')}_{decision_count + 1:03d}"

        new_decision = {
            'id': decision_id,
            'date': date.today().isoformat(),
            'type': found['type'],
            'decision': found['proposal'],
            'rationale': found['rationale'],
            'status': 'active',
            'outcome': None,
            'review_date': (date.today() + timedelta(days=14)).isoformat(),
            'approved_from': proposal_id
        }

        decisions.append(new_decision)
        log['pending_approvals'] = pending
        log['decisions'] = decisions
        save_coaching_log(log)

        return json.dumps({
            'status': 'approved',
            'proposal_id': proposal_id,
            'decision_id': decision_id,
            'message': f'Approved: {found["proposal"]}',
            'now_active': True
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def reject_coaching_change(proposal_id: str, reason: str = None) -> str:
    """
    Reject a pending coaching change proposal.

    Args:
        proposal_id: ID of the proposal to reject
        reason: Optional reason for rejection (helps LLM learn)

    Returns:
        Confirmation of rejection.
    """
    try:
        log = load_coaching_log()
        pending = log.get('pending_approvals', [])

        if 'rejected_proposals' not in log:
            log['rejected_proposals'] = []

        # Find and remove the proposal
        found = None
        for i, p in enumerate(pending):
            if p.get('id') == proposal_id:
                found = pending.pop(i)
                break

        if not found:
            return json.dumps({'error': f'Proposal {proposal_id} not found'})

        # Archive to rejected
        found['rejected_date'] = date.today().isoformat()
        found['rejection_reason'] = reason
        log['rejected_proposals'].append(found)
        log['pending_approvals'] = pending
        save_coaching_log(log)

        return json.dumps({
            'status': 'rejected',
            'proposal_id': proposal_id,
            'reason': reason,
            'message': f'Rejected: {found["proposal"]}'
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def record_athlete_response(
    stimulus: str,
    response: str,
    pattern: str = None
) -> str:
    """
    Record how the athlete responded to a training stimulus.

    Use this to track adaptation patterns that inform future planning.

    Args:
        stimulus: What training was done (e.g., "Long ride 2.5hrs Z2")
        response: How athlete responded (e.g., "Training Readiness 72 next day")
        pattern: Optional pattern identified (e.g., "Responds well to long Z2")

    Returns:
        Confirmation of recorded response.
    """
    try:
        log = load_coaching_log()

        if 'athlete_responses' not in log:
            log['athlete_responses'] = []
        if 'metadata' not in log:
            log['metadata'] = {'created': date.today().isoformat()}

        new_response = {
            'date': date.today().isoformat(),
            'stimulus': stimulus,
            'response': response
        }
        if pattern:
            new_response['pattern'] = pattern

        log['athlete_responses'].append(new_response)

        # Keep only last 50 responses
        log['athlete_responses'] = log['athlete_responses'][-50:]

        save_coaching_log(log)

        return json.dumps({
            'status': 'recorded',
            'message': f'Response recorded: {response}',
            'pattern': pattern
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_response_patterns() -> str:
    """
    Get identified athlete response patterns.

    Returns patterns from recorded responses to inform planning.

    Returns:
        List of patterns and recent responses.
    """
    try:
        log = load_coaching_log()
        responses = log.get('athlete_responses', [])

        # Extract patterns
        patterns = {}
        for r in responses:
            pattern = r.get('pattern')
            if pattern:
                if pattern not in patterns:
                    patterns[pattern] = {'count': 0, 'last_seen': r['date']}
                patterns[pattern]['count'] += 1
                if r['date'] > patterns[pattern]['last_seen']:
                    patterns[pattern]['last_seen'] = r['date']

        # Get recent responses (last 10)
        recent = responses[-10:] if responses else []

        return json.dumps({
            'patterns': patterns,
            'pattern_count': len(patterns),
            'recent_responses': recent,
            'note': 'Use these patterns to inform training decisions'
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def research_sport(sport_name: str, url: str = None) -> str:
    """
    Research training principles and methodology for a specific sport.

    Use this when onboarding an athlete in an unfamiliar sport. Fetches
    information about training approaches, periodization, common injuries,
    and key performance metrics.

    Args:
        sport_name: Name of the sport (e.g., "rock climbing", "CrossFit", "rowing")
        url: Optional direct URL to a training resource for this sport

    Returns:
        JSON with training principles, typical periodization, common injuries,
        and key metrics for this sport.

    Usage:
        research_sport("rock climbing")
        research_sport("CrossFit", url="https://example.com/crossfit-training")
    """
    import requests
    import re

    try:
        research_result = {
            "sport": sport_name,
            "researched_info": {},
            "sources": [],
            "training_implications": [],
        }

        # Format sport name for Wikipedia URL
        sport_url_name = sport_name.replace(' ', '_').title()

        # Build list of URLs to try
        if url:
            search_sources = [{"name": "Provided URL", "url": url, "type": "direct"}]
        else:
            search_sources = []

        # Add Wikipedia as primary source
        search_sources.extend([
            {
                "name": "Wikipedia",
                "url": f"https://en.wikipedia.org/wiki/{sport_url_name}",
                "type": "general"
            },
            # Try with "_training" suffix for training-specific articles
            {
                "name": "Wikipedia Training",
                "url": f"https://en.wikipedia.org/wiki/{sport_url_name}_training",
                "type": "training"
            },
        ])

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        # HTML text extractor
        from html.parser import HTMLParser
        from io import StringIO

        class HTMLStripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self.reset()
                self.strict = False
                self.convert_charrefs = True
                self.text = StringIO()
                self.skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style', 'nav', 'footer', 'header', 'aside'):
                    self.skip = True

            def handle_endtag(self, tag):
                if tag in ('script', 'style', 'nav', 'footer', 'header', 'aside'):
                    self.skip = False

            def handle_data(self, data):
                if not self.skip:
                    self.text.write(data + ' ')

            def get_text(self):
                return self.text.getvalue()

        # Try to fetch from sources
        fetched_content = None
        for source in search_sources:
            try:
                response = requests.get(
                    source["url"],
                    headers=headers,
                    timeout=HTTP_TIMEOUT_SECONDS,
                    allow_redirects=True
                )
                if response.status_code == 200:
                    stripper = HTMLStripper()
                    stripper.feed(response.text)
                    content = stripper.get_text()[:PAGE_TEXT_MAX_CHARS]

                    # Check if we got meaningful sport/training content
                    content_lower = content.lower()
                    sport_indicators = ['training', 'competition', 'athlete', 'technique', 'performance', 'exercise', 'strength', 'endurance']
                    has_sport_content = any(ind in content_lower for ind in sport_indicators)

                    if len(content) > 500 and has_sport_content:
                        fetched_content = content
                        research_result["sources"].append(response.url)
                        break
            except Exception:
                continue

        # Extract relevant information from fetched content
        if fetched_content:
            content_lower = fetched_content.lower()
            sentences = re.split(r'[.!?]+', fetched_content)

            # Keywords for different aspects of sport training
            training_keywords = ["training", "workout", "practice", "conditioning", "preparation"]
            periodization_keywords = ["season", "off-season", "peak", "competition", "periodization", "cycle", "phase"]
            injury_keywords = ["injury", "injuries", "strain", "overuse", "prevention", "risk"]
            strength_keywords = ["strength", "power", "muscle", "resistance", "weight"]
            endurance_keywords = ["endurance", "aerobic", "cardio", "stamina", "cardiovascular"]
            technique_keywords = ["technique", "skill", "form", "mechanics", "coordination"]

            training_findings = []
            periodization_findings = []
            injury_findings = []
            physical_demands = []
            technique_findings = []

            for sentence in sentences:
                sentence_clean = sentence.strip()
                sentence_lower = sentence_clean.lower()
                if len(sentence_clean) > 30:  # Skip very short fragments
                    if any(kw in sentence_lower for kw in training_keywords):
                        training_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in periodization_keywords):
                        periodization_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in injury_keywords):
                        injury_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in strength_keywords + endurance_keywords):
                        physical_demands.append(sentence_clean)
                    if any(kw in sentence_lower for kw in technique_keywords):
                        technique_findings.append(sentence_clean)

            research_result["researched_info"] = {
                "training_approaches": training_findings[:5] if training_findings else [f"Research specific training protocols for {sport_name}"],
                "periodization": periodization_findings[:3] if periodization_findings else ["Periodization varies by competition schedule"],
                "common_injuries": injury_findings[:4] if injury_findings else [f"Research common {sport_name} injuries for prevention planning"],
                "physical_demands": physical_demands[:4] if physical_demands else ["Assess physical demands through athlete interview"],
                "technique_notes": technique_findings[:3] if technique_findings else ["Technical development is sport-specific"],
            }

            # Generate training implications for the coach
            implications = []
            if any("endurance" in s.lower() for s in physical_demands):
                implications.append("Include aerobic base building in training")
            if any("strength" in s.lower() or "power" in s.lower() for s in physical_demands):
                implications.append("Strength training is important for this sport")
            if any("technique" in s.lower() or "skill" in s.lower() for s in technique_findings):
                implications.append("Allocate time for sport-specific skill work")
            if injury_findings:
                implications.append("Plan injury prevention work based on common injury patterns")

            research_result["training_implications"] = implications if implications else [
                f"Gather more specific information about {sport_name} training requirements from the athlete"
            ]

            research_result["content_preview"] = fetched_content[:1500]

        else:
            # Couldn't fetch - provide guidance
            research_result["researched_info"] = {
                "note": f"Unable to fetch research for '{sport_name}'. Gather info via athlete interview:",
                "questions_to_ask": [
                    f"What does a typical {sport_name} training week look like?",
                    "What are the main physical demands of the sport?",
                    "What injuries are common in this sport?",
                    "What does your competition schedule look like?",
                    "What does peak performance look like vs off-season?"
                ]
            }

        return json.dumps(research_result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def research_exercise(exercise_name: str, url: str = None) -> str:
    """
    Research proper form, muscles worked, and progressions for an exercise.

    Use when building strength programs or finding injury-safe alternatives.

    Args:
        exercise_name: Name of the exercise (e.g., "deadlift", "turkish get-up")
        url: Optional direct URL to an exercise guide

    Returns:
        JSON with proper form cues, muscles targeted, common mistakes,
        progressions, and alternative exercises.
    """
    import requests
    import re

    try:
        research_result = {
            "exercise": exercise_name,
            "researched_info": {},
            "sources": [],
        }

        # Format exercise name for URL
        exercise_url_name = exercise_name.replace(' ', '_').lower()

        # Build list of URLs to try
        if url:
            search_sources = [{"name": "Provided URL", "url": url, "type": "direct"}]
        else:
            search_sources = []

        # Add Wikipedia as source
        search_sources.extend([
            {
                "name": "Wikipedia",
                "url": f"https://en.wikipedia.org/wiki/{exercise_url_name}",
                "type": "general"
            },
            {
                "name": "Wikipedia Exercise",
                "url": f"https://en.wikipedia.org/wiki/{exercise_url_name}_(exercise)",
                "type": "exercise"
            },
        ])

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        }

        # HTML text extractor
        from html.parser import HTMLParser
        from io import StringIO

        class HTMLStripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self.reset()
                self.strict = False
                self.convert_charrefs = True
                self.text = StringIO()
                self.skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ('script', 'style', 'nav', 'footer', 'header', 'aside'):
                    self.skip = True

            def handle_endtag(self, tag):
                if tag in ('script', 'style', 'nav', 'footer', 'header', 'aside'):
                    self.skip = False

            def handle_data(self, data):
                if not self.skip:
                    self.text.write(data + ' ')

            def get_text(self):
                return self.text.getvalue()

        # Try to fetch from sources
        fetched_content = None
        for source in search_sources:
            try:
                response = requests.get(
                    source["url"],
                    headers=headers,
                    timeout=HTTP_TIMEOUT_SECONDS,
                    allow_redirects=True
                )
                if response.status_code == 200:
                    stripper = HTMLStripper()
                    stripper.feed(response.text)
                    content = stripper.get_text()[:PAGE_TEXT_MAX_CHARS]

                    # Check if we got meaningful exercise content
                    content_lower = content.lower()
                    exercise_indicators = ['muscle', 'exercise', 'movement', 'form', 'technique', 'strength', 'weight']
                    has_exercise_content = any(ind in content_lower for ind in exercise_indicators)

                    if len(content) > 300 and has_exercise_content:
                        fetched_content = content
                        research_result["sources"].append(response.url)
                        break
            except Exception:
                continue

        # Extract relevant information
        if fetched_content:
            sentences = re.split(r'[.!?]+', fetched_content)

            muscle_keywords = ["muscle", "muscles", "works", "targets", "activates", "engages"]
            form_keywords = ["form", "technique", "position", "stance", "grip", "posture"]
            safety_keywords = ["avoid", "mistake", "common error", "injury", "safety", "caution"]
            variation_keywords = ["variation", "alternative", "progression", "modification", "regression"]

            muscle_findings = []
            form_findings = []
            safety_findings = []
            variation_findings = []

            for sentence in sentences:
                sentence_clean = sentence.strip()
                sentence_lower = sentence_clean.lower()
                if len(sentence_clean) > 25:
                    if any(kw in sentence_lower for kw in muscle_keywords):
                        muscle_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in form_keywords):
                        form_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in safety_keywords):
                        safety_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in variation_keywords):
                        variation_findings.append(sentence_clean)

            research_result["researched_info"] = {
                "muscles_worked": muscle_findings[:4] if muscle_findings else [f"Research primary and secondary muscles for {exercise_name}"],
                "form_cues": form_findings[:4] if form_findings else ["Focus on controlled movement through full range of motion"],
                "safety_notes": safety_findings[:3] if safety_findings else ["Start light, master form before adding load"],
                "variations": variation_findings[:3] if variation_findings else [f"Explore progressions and regressions for {exercise_name}"],
            }

            research_result["content_preview"] = fetched_content[:1000]

        else:
            research_result["researched_info"] = {
                "note": f"Unable to fetch research for '{exercise_name}'.",
                "recommendations": [
                    "Check exercise library with list_exercises() for Garmin-supported exercises",
                    "Ask athlete to demonstrate current technique",
                    f"Search for '{exercise_name} proper form' for detailed guides"
                ]
            }

        return json.dumps(research_result, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


if __name__ == "__main__":
    if check_setup():
        mcp.run()
    else:
        import sys
        sys.exit(1)