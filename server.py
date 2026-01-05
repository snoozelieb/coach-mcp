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
)
from datetime import date, timedelta
from typing import Any, Union
from pathlib import Path
from collections import defaultdict
import json

# Data directory for persistent storage
DATA_DIR = Path(__file__).parent / "data"

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
def refresh_athlete_profile() -> str:
    """
    Generates/refreshes athlete profile from 6 months of Garmin history.

    Pulls activities, personal records, and calculates baseline metrics.
    Saves to data/athlete_profile.json.

    Returns:
        JSON summary of the generated profile.
    """
    try:
        client = get_garmin_client()
        today = date.today()
        six_months_ago = today - timedelta(days=180)

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

        # Build the profile
        profile = {
            'last_updated': today.isoformat(),
            'baseline': baseline,
            'personal_records': personal_records,
            'manual': {
                'injury_history': [],
                'constraints': [],
                'notes': ''
            }
        }

        # Ensure data directory exists
        DATA_DIR.mkdir(exist_ok=True)

        # Save to file
        profile_path = DATA_DIR / 'athlete_profile.json'
        with open(profile_path, 'w') as f:
            json.dump(profile, f, indent=2)

        # Return summary
        summary = {
            'status': 'success',
            'last_updated': profile['last_updated'],
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
    Returns: Resting Heart Rate, Body Battery, and Sleep Score.
    """
    try:
        client = get_garmin_client()
        today = date.today().isoformat()

        stats = client.get_user_summary(today)
        body_battery = client.get_body_battery(today)

        rhr = parse_resting_heart_rate(stats)
        sleep_score = parse_sleep_score(stats)
        current_bb = parse_body_battery(body_battery)

        return f"Status for {today}: RHR={rhr}bpm, Body Battery={current_bb}/100, Sleep Score={sleep_score}"

    except Exception as e:
        return f"Error fetching Garmin data: {str(e)}"


@mcp.tool()
def get_planning_context() -> str:
    """
    Assembles complete context for LLM training planning.

    Returns comprehensive data including:
    - Athlete profile (baseline, PRs, constraints)
    - Current training block and pillars
    - Recent activities (last 14 days)
    - Current week's compliance status
    - Today's recovery metrics
    - Upcoming events
    - Any pending suggestions

    Use this before generating or adjusting training plans.
    """
    try:
        client = get_garmin_client()
        today = date.today()

        # Load configurations
        athlete_profile = load_json_file('athlete_profile.json')
        training_config = load_training_config()

        # Get recent activities (14 days)
        start_14_days = today - timedelta(days=14)
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

        # Build full context
        context = build_planning_context(
            athlete_profile=athlete_profile,
            training_config=training_config,
            recent_activities=recent_activities,
            compliance_status=compliance,
            today_recovery=today_recovery,
            pending_suggestions=pending,
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
        if priority.upper() not in ['A', 'B', 'C']:
            return json.dumps({'error': 'Priority must be A, B, or C'})

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
def update_race_priority(name: str, new_priority: str) -> str:
    """
    Update the priority of an existing race.

    Args:
        name: Name of the event (case-insensitive partial match)
        new_priority: New priority (A, B, or C)

    Returns confirmation with updated event.
    """
    try:
        config = load_training_config()
        events = config.get('events', [])

        if new_priority.upper() not in ['A', 'B', 'C']:
            return json.dumps({'error': 'Priority must be A, B, or C'})

        # Find matching event
        name_lower = name.lower()
        for event in events:
            if name_lower in event.get('name', '').lower():
                old_priority = event.get('priority')
                event['priority'] = new_priority.upper()

                # Save back
                config['events'] = events
                from planner import save_json_file
                save_json_file('training_config.json', config)

                return json.dumps({
                    'status': 'success',
                    'message': f"Updated {event['name']}: {old_priority} -> {new_priority.upper()}",
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


if __name__ == "__main__":
    mcp.run()