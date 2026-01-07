from mcp.server.fastmcp import FastMCP
from garmin_client import get_garmin_client
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

# Initialize the MCP Server
mcp = FastMCP("My Coach")


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
        data: JSON string with the data to update/add

    Examples:
        update_athlete('personal', '{"max_hr": 185, "weight_kg": 75}')
        update_athlete('add_commitment', '{"day": "Tuesday", "activity": "swimming", "time": "morning"}')
        update_athlete('add_injury', '{"date": "2026-01-01", "type": "ankle", "description": "Rolled ankle"}')
        update_athlete('preferences', '{"likes": ["MTB", "trail running"]}')
        update_athlete('coaching_notes', '"Responds well to data-driven feedback"')

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

        else:
            return json.dumps({
                'error': f"Unknown section '{section}'. Use: personal, life_constraints, preferences, coaching_notes, add_commitment, add_injury"
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


if __name__ == "__main__":
    mcp.run()