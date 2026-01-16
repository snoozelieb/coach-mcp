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
    load_coaching_log,
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
    TRAINING_CONFIG_FILE,
    ATHLETE_FILE,
    CTL_TARGETS,
)
from fitness import (
    load_fitness_history,
    calculate_fitness_metrics,
    calculate_intensity_distribution,
    get_load_athlete_max_hr,
    get_athlete_hr_zones,
    get_fitness_trend,
    update_fitness_history,
    get_sleep_summary,
    calculate_ctl_target,
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
def get_fitness_status(days: int = 90) -> str:
    """
    Get comprehensive fitness status with CTL, ATL, TSB, and ACWR.

    Science-based metrics for training load management:
    - CTL (Chronic Training Load): Your fitness level (42-day weighted average)
    - ATL (Acute Training Load): Your fatigue level (7-day weighted average)
    - TSB (Training Stress Balance): Your form (CTL - ATL). Positive = fresh, negative = fatigued
    - ACWR (Acute:Chronic Workload Ratio): Injury risk indicator (0.8-1.3 is sweet spot)

    Args:
        days: Number of days to analyze for trend (default 90)

    Returns:
        JSON with fitness metrics, trend analysis, and recommendations.

    Use this to:
    - Understand current fitness level relative to history
    - Check if training load is in safe range (ACWR)
    - See if fitness is building toward race goals
    - Determine if athlete is fresh (positive TSB) or fatigued (negative TSB)
    """
    try:
        # Load fitness history
        history = load_fitness_history()
        daily_loads = history.get('daily_loads', {})

        if not daily_loads:
            return json.dumps({
                'status': 'no_data',
                'message': 'No fitness history. Run refresh_fitness_history() to backfill from Garmin.',
                'action': 'Call refresh_fitness_history() first',
            })

        # Calculate current metrics
        metrics = calculate_fitness_metrics(daily_loads)

        # Get trend
        trend = get_fitness_trend(days)

        # Generate coaching insights
        insights = []
        recommendations = []

        # CTL insights
        if metrics['ctl'] < 20:
            insights.append("Low chronic load - still building base fitness")
        elif metrics['ctl'] < 40:
            insights.append("Moderate fitness base established")
        else:
            insights.append(f"Good fitness foundation (CTL: {metrics['ctl']})")

        # TSB insights
        if metrics['tsb'] > 15:
            insights.append("Very fresh - may be losing fitness if rest continues")
            recommendations.append("Good time for a key session or test")
        elif metrics['tsb'] > 0:
            insights.append("Fresh and ready to perform")
            recommendations.append("Good form for quality sessions")
        elif metrics['tsb'] > -15:
            insights.append("Slightly fatigued but functional")
            recommendations.append("Normal training can continue")
        elif metrics['tsb'] > -30:
            insights.append("Fatigued - accumulating training stress")
            recommendations.append("Monitor recovery, consider easier day soon")
        else:
            insights.append("Heavily fatigued - deep in training block")
            recommendations.append("Recovery day needed to absorb training")

        # ACWR insights
        if metrics['acwr_status'] == 'optimal':
            insights.append("Training load in sweet spot (ACWR 0.8-1.3)")
        elif metrics['acwr_status'] == 'low':
            recommendations.append("Load is low - safe to increase training")
        elif metrics['acwr_status'] == 'elevated':
            recommendations.append("Load spike detected - be cautious with intensity")
        elif metrics['acwr_status'] == 'danger':
            recommendations.append("HIGH INJURY RISK - reduce load immediately")

        # Trend insights
        if trend['trend'] == 'building':
            insights.append(f"Fitness building (+{trend['ctl_change']} over {trend['period_days']} days)")
        elif trend['trend'] == 'declining':
            insights.append(f"Fitness declining ({trend['ctl_change']} over {trend['period_days']} days)")
            recommendations.append("Consider if this is intentional (taper) or concerning")

        return json.dumps({
            'metrics': {
                'ctl': metrics['ctl'],
                'ctl_label': 'Chronic Training Load (Fitness)',
                'atl': metrics['atl'],
                'atl_label': 'Acute Training Load (Fatigue)',
                'tsb': metrics['tsb'],
                'tsb_label': 'Training Stress Balance (Form)',
                'acwr': metrics['acwr'],
                'acwr_status': metrics['acwr_status'],
                'acwr_label': 'Acute:Chronic Workload Ratio',
            },
            'trend': {
                'direction': trend['trend'],
                'ctl_change': trend.get('ctl_change', 0),
                'projected_ctl_30_days': trend.get('projected_ctl_30_days'),
                'period_days': trend.get('period_days', days),
            },
            'data_quality': {
                'days_with_data': metrics['days_with_data'],
                'data_sufficient': metrics['data_sufficient'],
                'as_of_date': metrics['as_of_date'],
            },
            'insights': insights,
            'recommendations': recommendations,
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def refresh_fitness_history(days: int = 180) -> str:
    """
    Refresh fitness history by fetching activities from Garmin.

    Calculates training load for each day and updates CTL/ATL history.
    Run this periodically to keep fitness metrics current, or once with
    a large window (365+) to backfill historical data.

    Args:
        days: Number of days to fetch (default 180, max recommended 365)

    Returns:
        JSON with summary of updated data and current fitness metrics.
    """
    try:
        client = get_garmin_client()
        today = date.today()
        start = (today - timedelta(days=days)).isoformat()

        # Fetch activities from Garmin
        raw_activities = client.get_activities_by_date(start, today.isoformat())

        if not raw_activities:
            return json.dumps({
                'status': 'no_activities',
                'message': f'No activities found in last {days} days',
            })

        # Parse activities
        activities = parse_activities(raw_activities)

        # Get athlete's max HR for load calculation
        max_hr = get_load_athlete_max_hr()

        # Update fitness history
        history = update_fitness_history(activities, max_hr)

        # Calculate current metrics
        metrics = calculate_fitness_metrics(history.get('daily_loads', {}))

        return json.dumps({
            'status': 'success',
            'activities_processed': len(activities),
            'days_with_load': len(history.get('daily_loads', {})),
            'period': f'{start} to {today.isoformat()}',
            'current_metrics': {
                'ctl': metrics['ctl'],
                'atl': metrics['atl'],
                'tsb': metrics['tsb'],
                'acwr': metrics['acwr'],
                'acwr_status': metrics['acwr_status'],
            },
            'note': 'Fitness history updated. Use get_fitness_status() for detailed analysis.',
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_intensity_distribution(days: int = 28) -> str:
    """
    Analyze training intensity distribution over a period.

    Checks compliance with the Norwegian 80/20 polarized model:
    - 80% low intensity (Zone 1-2: easy/aerobic)
    - 15% moderate intensity (Zone 3: tempo)
    - 5% high intensity (Zone 4-5: threshold/VO2max)

    Args:
        days: Number of days to analyze (default 28 for monthly view)

    Returns:
        JSON with zone distribution, polarization score, and recommendations.

    Use this to:
    - Check if training is properly polarized
    - Identify if too much time in "gray zone" (moderate)
    - Plan intensity for upcoming sessions
    """
    try:
        client = get_garmin_client()
        today = date.today()
        start = (today - timedelta(days=days)).isoformat()

        # Fetch activities
        raw_activities = client.get_activities_by_date(start, today.isoformat())

        if not raw_activities:
            return json.dumps({
                'status': 'no_activities',
                'message': f'No activities found in last {days} days',
                'period': f'{start} to {today.isoformat()}',
            })

        # Parse activities
        activities = parse_activities(raw_activities)

        # Get HR zones
        hr_zones = get_athlete_hr_zones()

        # Calculate distribution
        distribution = calculate_intensity_distribution(activities, hr_zones)

        # Add period info
        distribution['period'] = {
            'start': start,
            'end': today.isoformat(),
            'days': days,
            'activities_count': len(activities),
        }

        # Add coaching context
        zone_dist = distribution.get('zone_distribution', {})
        low_pct = zone_dist.get('low_z1_z2_pct', 0)

        if low_pct < 70:
            distribution['warning'] = "Training too intense - risk of overtraining and injury"
        elif low_pct > 90 and days > 14:
            distribution['note'] = "Very conservative training - safe but may limit fitness gains"

        return json.dumps(distribution, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


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
            - 'strength_baseline': strength exercise baselines (exercises, equivalence_groups)
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

        elif section == 'strength_baseline':
            # Merge update into existing strength baseline
            if not isinstance(parsed_data, dict):
                return json.dumps({'error': 'strength_baseline data must be an object'})
            athlete.setdefault('strength_baseline', {'exercises': {}, 'equivalence_groups': {}})
            if 'exercises' in parsed_data:
                athlete['strength_baseline']['exercises'].update(parsed_data['exercises'])
            if 'equivalence_groups' in parsed_data:
                athlete['strength_baseline']['equivalence_groups'].update(parsed_data['equivalence_groups'])
            if 'last_synced' in parsed_data:
                athlete['strength_baseline']['last_synced'] = parsed_data['last_synced']
            updated = athlete['strength_baseline']

        else:
            return json.dumps({
                'error': f"Unknown section '{section}'. Use: personal, life_constraints, preferences, coaching_notes, add_commitment, add_injury, training_pillars, swimming, pilates, strength_baseline"
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
def analyze_ftp_test(activity_id: str = None) -> str:
    """
    Analyze a completed FTP cycling test in detail.

    Provides structured analysis including:
    - Protocol phases (warmup, blowout, recovery, test, cooldown)
    - Pacing analysis (power consistency, surges, crashes)
    - FTP estimate with adjustment factor
    - Coach recommendation

    Args:
        activity_id: Specific activity ID. If omitted, finds most recent FTP test
                     (looks for cycling activities with 'ftp', 'test', or 'threshold' in name).

    Returns:
        Structured JSON with complete test analysis for coaching decisions.
    """
    try:
        client = get_garmin_client()
        today = date.today()

        # 1. FIND TEST ACTIVITY
        if activity_id:
            target_activity_id = int(activity_id)
            # Fetch activity details
            week_ago = today - timedelta(days=30)
            raw_activities = client.get_activities_by_date(
                week_ago.isoformat(),
                today.isoformat()
            )
            activity_summary = None
            for act in raw_activities:
                if act.get('activityId') == target_activity_id:
                    activity_summary = act
                    break
            if not activity_summary:
                return json.dumps({
                    'status': 'not_found',
                    'error': f'Activity {activity_id} not found in last 30 days'
                })
        else:
            # Search recent activities for FTP test
            week_ago = today - timedelta(days=30)
            raw_activities = client.get_activities_by_date(
                week_ago.isoformat(),
                today.isoformat()
            )

            # Filter: cycling + name contains FTP-related keywords
            ftp_keywords = ['ftp', 'test', 'threshold', '20min', '20-min', 'baseline']
            ftp_tests = [
                a for a in raw_activities
                if a.get('activityType', {}).get('typeKey') in ['cycling', 'indoor_cycling']
                and any(keyword in a.get('activityName', '').lower() for keyword in ftp_keywords)
            ]

            if not ftp_tests:
                return json.dumps({
                    'status': 'not_found',
                    'error': 'No FTP tests found in last 30 days. Look for cycling activities with "ftp", "test", or "threshold" in name.'
                })

            activity_summary = ftp_tests[0]  # Most recent
            target_activity_id = activity_summary.get('activityId')

        # 2. FETCH LAP DATA
        try:
            splits = client.get_activity_splits(target_activity_id)
            laps = splits.get('lapDTOs', [])
        except Exception:
            laps = []

        # 3. EXTRACT SESSION SUMMARY
        session_summary = {
            'total_duration_mins': round(activity_summary.get('duration', 0) / 60, 1),
            'total_distance_km': round(activity_summary.get('distance', 0) / 1000, 1),
            'avg_power': activity_summary.get('avgPower'),
            'max_power': activity_summary.get('maxPower'),
            'norm_power': activity_summary.get('normPower'),
            'avg_hr': activity_summary.get('averageHR'),
            'max_hr': activity_summary.get('maxHR'),
            'avg_cadence': activity_summary.get('averageBikingCadenceInRevPerMinute'),
            'max_20min_power': activity_summary.get('max20MinPower'),
        }

        # 4. PARSE PROTOCOL PHASES FROM LAPS
        protocol_phases = []
        phase_map = {
            'WARMUP': 'warmup',
            'ACTIVE': 'active',
            'RECOVERY': 'recovery',
            'COOLDOWN': 'cooldown',
            'REST': 'rest',
        }

        for lap in laps:
            intensity = lap.get('intensityType', 'ACTIVE')
            phase_name = phase_map.get(intensity, 'active')

            protocol_phases.append({
                'phase': phase_name,
                'duration_mins': round(lap.get('duration', 0) / 60, 1),
                'avg_power': lap.get('averagePower'),
                'max_power': lap.get('maxPower'),
                'min_power': lap.get('minPower'),
                'norm_power': lap.get('normalizedPower'),
                'avg_hr': lap.get('averageHR'),
                'max_hr': lap.get('maxHR'),
                'avg_cadence': lap.get('averageBikeCadence'),
            })

        # 5. IDENTIFY TEST PORTION
        # Test laps are ACTIVE laps after recovery (typically laps 4+ in standard FTP test)
        # Find recovery lap, then get subsequent ACTIVE laps
        recovery_idx = None
        for i, phase in enumerate(protocol_phases):
            if phase['phase'] == 'recovery' and phase['duration_mins'] >= 3:
                recovery_idx = i
                break

        test_laps = []
        if recovery_idx is not None:
            for phase in protocol_phases[recovery_idx + 1:]:
                if phase['phase'] == 'active':
                    test_laps.append(phase)
                elif phase['phase'] == 'cooldown':
                    break

        # Calculate test metrics
        if test_laps:
            test_duration = sum(lap['duration_mins'] for lap in test_laps)
            test_powers = [lap['avg_power'] for lap in test_laps if lap['avg_power']]
            test_avg_power = round(sum(p * d for p, d in zip(
                [lap['avg_power'] for lap in test_laps if lap['avg_power']],
                [lap['duration_mins'] for lap in test_laps if lap['avg_power']]
            )) / test_duration, 0) if test_powers else None

            max_powers = [lap['max_power'] for lap in test_laps if lap['max_power']]
            min_powers = [lap['min_power'] for lap in test_laps if lap['min_power']]
            test_max_power = max(max_powers) if max_powers else None
            test_min_power = min(min_powers) if min_powers else None

            # Pacing analysis
            if len(test_laps) >= 2:
                first_half = test_laps[:len(test_laps)//2]
                second_half = test_laps[len(test_laps)//2:]

                first_half_avg = round(sum(l['avg_power'] for l in first_half if l['avg_power']) / len(first_half), 0) if first_half else None
                second_half_avg = round(sum(l['avg_power'] for l in second_half if l['avg_power']) / len(second_half), 0) if second_half else None
            else:
                first_half_avg = test_avg_power
                second_half_avg = test_avg_power

            # Detect surges and crashes
            surge_detected = test_max_power and test_avg_power and test_max_power > test_avg_power * 1.30
            crash_detected = test_min_power is not None and test_min_power < 100

            # Pacing verdict
            if crash_detected and surge_detected:
                pacing_verdict = f"Surged to {test_max_power}W then crashed to {test_min_power}W. Pacing error."
            elif crash_detected:
                pacing_verdict = f"Power dropped to {test_min_power}W. Blew up before completion."
            elif surge_detected:
                pacing_verdict = f"Large surge to {test_max_power}W detected. Consider steadier pacing."
            elif first_half_avg and second_half_avg and abs(first_half_avg - second_half_avg) <= 5:
                pacing_verdict = "Excellent pacing - very consistent power throughout."
            elif first_half_avg and second_half_avg and first_half_avg > second_half_avg:
                pacing_verdict = f"Started too hard ({first_half_avg}W) and faded ({second_half_avg}W)."
            else:
                pacing_verdict = "Pacing acceptable."

            # Test completion
            target_duration = 20  # Standard FTP test
            test_completed = test_duration >= target_duration - 1  # Allow 1 min tolerance
            completion_pct = round(min(100, test_duration / target_duration * 100), 1)

        else:
            # Fallback if can't identify test laps
            test_duration = 0
            test_avg_power = session_summary.get('avg_power')
            test_max_power = session_summary.get('max_power')
            test_min_power = None
            first_half_avg = None
            second_half_avg = None
            surge_detected = False
            crash_detected = False
            pacing_verdict = "Could not identify test portion from laps."
            test_completed = False
            completion_pct = 0

        # 6. ESTIMATE FTP
        # Use max_20min_power if available, otherwise estimate from test portion
        if session_summary.get('max_20min_power'):
            raw_power = round(session_summary['max_20min_power'], 0)
            adjustment_factor = 0.95
            method = '20min_garmin'
        elif test_avg_power and test_duration >= 18:
            raw_power = test_avg_power
            adjustment_factor = 0.95
            method = '20min_test'
        elif test_avg_power and test_duration >= 13:
            raw_power = test_avg_power
            adjustment_factor = 0.88  # 15-min adjustment
            method = f'{int(test_duration)}min_adjusted'
        elif test_avg_power and test_duration >= 8:
            raw_power = test_avg_power
            adjustment_factor = 0.85  # 10-min adjustment
            method = f'{int(test_duration)}min_adjusted'
        else:
            raw_power = test_avg_power or session_summary.get('avg_power')
            adjustment_factor = 0.80  # Very conservative
            method = 'estimated_conservative'

        estimated_ftp = int(raw_power * adjustment_factor) if raw_power else None

        # Confidence level
        if test_completed and not crash_detected:
            confidence = 'high'
        elif test_duration >= 15 and not crash_detected:
            confidence = 'medium'
        elif crash_detected:
            confidence = 'low'
        else:
            confidence = 'low'

        # 7. COACH RECOMMENDATION
        if crash_detected:
            suggested_ftp = int(estimated_ftp * 0.95) if estimated_ftp else None  # Extra conservative
            rationale = f"Athlete crashed during test. Set conservative FTP to ensure proper zone training."
            retest_weeks = 4
        elif not test_completed:
            suggested_ftp = int(estimated_ftp * 0.97) if estimated_ftp else None
            rationale = f"Test incomplete ({completion_pct}%). Slightly conservative FTP recommended."
            retest_weeks = 6
        else:
            suggested_ftp = estimated_ftp
            rationale = "Clean test completion. FTP estimate is reliable."
            retest_weeks = 8

        # 8. IDENTIFY BLOWOUT PHASE (first ACTIVE lap before recovery)
        blowout_phase = None
        for i, phase in enumerate(protocol_phases):
            if phase['phase'] == 'active' and recovery_idx and i < recovery_idx:
                blowout_phase = phase
                break

        # Build result
        result = {
            'status': 'success',
            'activity_id': target_activity_id,
            'test_date': activity_summary.get('startTimeLocal', '')[:10],
            'test_name': activity_summary.get('activityName'),

            'session_summary': session_summary,
            'protocol_phases': protocol_phases,

            'test_analysis': {
                'test_duration_mins': round(test_duration, 1) if test_duration else None,
                'test_avg_power': test_avg_power,
                'test_max_power': test_max_power,
                'test_min_power': test_min_power,
                'test_completed': test_completed,
                'completion_pct': completion_pct,

                'pacing': {
                    'first_half_avg': first_half_avg,
                    'second_half_avg': second_half_avg,
                    'power_drop': round(first_half_avg - second_half_avg, 0) if first_half_avg and second_half_avg else None,
                    'surge_detected': surge_detected,
                    'crash_detected': crash_detected,
                    'pacing_verdict': pacing_verdict,
                },

                'blowout_phase': {
                    'duration_mins': blowout_phase['duration_mins'] if blowout_phase else None,
                    'avg_power': blowout_phase['avg_power'] if blowout_phase else None,
                    'max_power': blowout_phase['max_power'] if blowout_phase else None,
                    'effective': blowout_phase['max_power'] > 300 if blowout_phase and blowout_phase.get('max_power') else None,
                } if blowout_phase else None,

                'hr_analysis': {
                    'peak_hr': session_summary.get('max_hr'),
                    'avg_hr': session_summary.get('avg_hr'),
                    'max_effort_likely': session_summary.get('max_hr') and session_summary['max_hr'] >= 180,
                },
            },

            'ftp_estimate': {
                'method': method,
                'raw_power': raw_power,
                'adjustment_factor': adjustment_factor,
                'estimated_ftp': estimated_ftp,
                'confidence': confidence,
            },

            'coach_recommendation': {
                'suggested_ftp': suggested_ftp,
                'rationale': rationale,
                'retest_in_weeks': retest_weeks,
            },
        }

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({'status': 'error', 'error': str(e)})


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
def get_periodization_status() -> str:
    """
    Get current position in the season periodization plan.

    Shows:
    - Current phase and week within phase
    - Days/weeks until A-race
    - Remaining phases before race
    - Current vs target fitness trajectory
    - Phase-specific guidance (key sessions, intensity targets)

    Use this to understand WHERE we are in the season and WHAT the
    current phase demands. The LLM can then adapt weekly plans accordingly.
    """
    try:
        today = date.today()
        config = load_training_config()

        periodization = config.get('periodization', {})
        current_block = config.get('current_block', {})
        events = config.get('events', [])

        # Find A-race
        a_race = None
        for event in events:
            if event.get('priority') == 'A':
                try:
                    race_date = date.fromisoformat(event.get('date', ''))
                    if race_date > today:
                        a_race = {
                            'name': event.get('name'),
                            'date': event.get('date'),
                            'days_until': (race_date - today).days,
                            'weeks_until': (race_date - today).days // 7,
                            'type': event.get('type'),
                        }
                        break
                except ValueError:
                    continue

        # Current phase info
        current_phase = periodization.get('current_phase', current_block.get('phase', 'unknown'))
        phases = periodization.get('phases', {})
        phase_info = phases.get(current_phase, {})

        # Calculate week within phase
        phase_start = periodization.get('phase_start', current_block.get('start_date'))
        if phase_start:
            try:
                start = date.fromisoformat(phase_start)
                weeks_in_phase = (today - start).days // 7 + 1
            except ValueError:
                weeks_in_phase = 1
        else:
            weeks_in_phase = 1

        typical_weeks = phase_info.get('typical_weeks', 4)

        # Get fitness status if available
        fitness_metrics = None
        try:
            history = load_fitness_history()
            daily_loads = history.get('daily_loads', {})
            if daily_loads:
                fitness_metrics = calculate_fitness_metrics(daily_loads)
        except Exception:
            pass

        # Build remaining phases
        phase_order = ['base', 'build', 'peak', 'taper']
        remaining_phases = []
        found_current = False
        for phase in phase_order:
            if phase == current_phase:
                found_current = True
                # Add remaining weeks of current phase
                remaining_weeks = max(0, typical_weeks - weeks_in_phase)
                if remaining_weeks > 0:
                    remaining_phases.append({
                        'phase': phase,
                        'weeks': remaining_weeks,
                        'status': 'current',
                    })
            elif found_current and phase in phases:
                remaining_phases.append({
                    'phase': phase,
                    'weeks': phases[phase].get('typical_weeks', 4),
                    'status': 'upcoming',
                })

        # Phase-specific guidance
        guidance = {
            'focus': phase_info.get('focus', 'General training'),
            'intensity_distribution': phase_info.get('intensity_distribution', {}),
            'volume_trend': phase_info.get('volume_trend', 'stable'),
            'key_sessions': phase_info.get('key_sessions', []),
        }

        result = {
            'current_phase': {
                'name': current_phase,
                'week': weeks_in_phase,
                'of_weeks': typical_weeks,
                'progress_pct': round(weeks_in_phase / typical_weeks * 100) if typical_weeks > 0 else 0,
            },
            'a_race': a_race,
            'remaining_phases': remaining_phases,
            'phase_guidance': guidance,
            'weekly_volume_target_hrs': current_block.get('weekly_volume_target_hrs'),
        }

        if fitness_metrics:
            result['fitness_status'] = {
                'ctl': fitness_metrics['ctl'],
                'tsb': fitness_metrics['tsb'],
                'acwr': fitness_metrics['acwr'],
                'acwr_status': fitness_metrics['acwr_status'],
            }

        # Add coaching notes based on phase
        notes = []
        if current_phase == 'base':
            notes.append("Focus on volume over intensity. Build aerobic foundation.")
            notes.append("Strength work is critical now - easier to build when load is lower.")
        elif current_phase == 'build':
            notes.append("Add race-specific intensity. Maintain (don't increase) volume.")
            notes.append("Start practicing race nutrition and pacing strategies.")
        elif current_phase == 'peak':
            notes.append("Race simulation efforts. Start reducing volume.")
            notes.append("Confidence-building sessions - you're ready.")
        elif current_phase == 'taper':
            notes.append("Sharp volume reduction. Maintain short sharp efforts.")
            notes.append("Trust your fitness. Rest is training now.")

        if a_race and a_race['days_until'] < 14:
            notes.append(f"RACE WEEK APPROACHING: {a_race['days_until']} days to {a_race['name']}")

        result['coaching_notes'] = notes

        return json.dumps(result, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_weekly_prescription() -> str:
    """
    Get this week's training prescription based on periodization and fitness.

    Combines:
    - Current phase demands (from periodization)
    - Current fitness status (CTL, ACWR, TSB)
    - Recovery status (Garmin readiness)
    - Pillar compliance (what's behind?)

    Returns a PRESCRIPTION that the LLM can adapt based on conversation
    with the athlete. This is the bridge between block planning and daily execution.

    The prescription includes:
    - Target volume for the week
    - Number and type of key sessions
    - Intensity distribution targets
    - Flexibility notes (what can be moved/swapped)
    - Constraints (injuries, life events)
    """
    try:
        today = date.today()
        client = get_garmin_client()

        # Load all context
        config = load_training_config()
        athlete = load_athlete()
        periodization = config.get('periodization', {})
        current_block = config.get('current_block', {})

        # Get current phase info
        current_phase = periodization.get('current_phase', current_block.get('phase', 'base'))
        phases = periodization.get('phases', {})
        phase_info = phases.get(current_phase, {})

        # Get fitness metrics
        fitness_metrics = None
        try:
            history = load_fitness_history()
            daily_loads = history.get('daily_loads', {})
            if daily_loads:
                fitness_metrics = calculate_fitness_metrics(daily_loads)
        except Exception:
            pass

        # Get today's readiness
        readiness_data = client.get_training_readiness(today.isoformat())
        readiness = parse_training_readiness(readiness_data)

        # Get recent compliance
        start_7_days = today - timedelta(days=7)
        raw_activities = client.get_activities_by_date(
            start_7_days.isoformat(),
            today.isoformat()
        )
        recent_activities = parse_activities(raw_activities)
        compliance = check_weekly_compliance(recent_activities)

        # Calculate prescribed volume
        base_volume_hrs = current_block.get('weekly_volume_target_hrs', 6.0)
        volume_trend = phase_info.get('volume_trend', 'stable')

        # Adjust volume based on ACWR if available
        if fitness_metrics:
            acwr = fitness_metrics['acwr']
            if acwr > 1.3:
                volume_adjustment = 0.85  # Reduce 15% if overloaded
                volume_note = "Reduced due to elevated ACWR"
            elif acwr < 0.8:
                volume_adjustment = 1.10  # Can increase 10% if undertrained
                volume_note = "Safe to push - load ratio is low"
            else:
                volume_adjustment = 1.0
                volume_note = "Load ratio in sweet spot"
        else:
            volume_adjustment = 1.0
            volume_note = "No fitness history - using base target"

        prescribed_volume_hrs = round(base_volume_hrs * volume_adjustment, 1)

        # Get intensity targets from phase
        intensity_targets = phase_info.get('intensity_distribution', {
            'z1_z2_pct': 80,
            'z3_pct': 15,
            'z4_z5_pct': 5,
        })

        # Get key sessions for this phase
        key_sessions = phase_info.get('key_sessions', ['long_effort', 'strength'])

        # Check what pillars need attention
        pillar_priorities = []
        if not compliance.get('strength', {}).get('compliant', True):
            deficit = compliance.get('strength', {}).get('deficit', 0)
            pillar_priorities.append(f"Strength: {deficit} session(s) behind")
        if not compliance.get('mobility', {}).get('compliant', True):
            deficit = compliance.get('mobility', {}).get('deficit', 0)
            pillar_priorities.append(f"Mobility: {deficit} mins behind")

        # Check for active injuries
        injury_constraints = []
        injuries = athlete.get('injury_history', [])
        for injury in injuries:
            if injury.get('status') == 'active':
                restricted = injury.get('restricted_activities', [])
                injury_constraints.append({
                    'injury': injury.get('type', 'Unknown'),
                    'restricted': restricted,
                    'safe': injury.get('safe_activities', []),
                })

        # Check for life events this week
        life_events = []
        constraints = athlete.get('life_constraints', {})
        travel = constraints.get('travel', [])
        for trip in travel:
            try:
                trip_date = date.fromisoformat(trip.get('date', ''))
                trip_end = date.fromisoformat(trip.get('end_date', trip.get('date', '')))
                # Check if trip overlaps with this week
                week_end = today + timedelta(days=7)
                if trip_date <= week_end and trip_end >= today:
                    life_events.append({
                        'event': trip.get('type', 'travel'),
                        'dates': f"{trip.get('date')} to {trip.get('end_date', trip.get('date'))}",
                        'notes': trip.get('notes'),
                    })
            except ValueError:
                continue

        # Build prescription
        prescription = {
            'volume': {
                'target_hrs': prescribed_volume_hrs,
                'base_target_hrs': base_volume_hrs,
                'adjustment': volume_adjustment,
                'adjustment_reason': volume_note,
            },
            'intensity': {
                'targets': intensity_targets,
                'note': f"Phase: {current_phase} - {phase_info.get('focus', 'General')}",
            },
            'key_sessions': {
                'required': key_sessions,
                'count': len([k for k in key_sessions if k not in ['mobility', 'rest']]),
                'note': "These sessions drive adaptation - prioritize them",
            },
            'pillar_priorities': pillar_priorities if pillar_priorities else ["All pillars on track"],
            'constraints': {
                'injuries': injury_constraints,
                'life_events': life_events,
            },
            'readiness': {
                'score': readiness.get('score'),
                'level': readiness.get('level'),
                'recommendation': 'Rest or easy' if readiness.get('score', 50) < 40 else 'Normal training',
            },
            'flexibility': {
                'swappable': "Long efforts can move within week based on weather/life",
                'fixed': "Strength days benefit from consistency (same days each week)",
                'note': "Adapt the plan through conversation - this is guidance, not law",
            },
        }

        if fitness_metrics:
            prescription['current_fitness'] = {
                'ctl': fitness_metrics['ctl'],
                'tsb': fitness_metrics['tsb'],
                'form_status': 'Fresh' if fitness_metrics['tsb'] > 0 else 'Fatigued',
            }

        return json.dumps(prescription, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def update_phase(new_phase: str, notes: str = None) -> str:
    """
    Transition to a new training phase.

    Use this when the athlete is ready to move to the next phase of their
    periodization plan. This is a significant coaching decision.

    Args:
        new_phase: The phase to transition to (base, build, peak, taper)
        notes: Optional notes about why the transition is happening

    Returns:
        Confirmation with updated phase information.
    """
    valid_phases = ['base', 'build', 'peak', 'taper', 'recovery', 'maintenance']

    if new_phase.lower() not in valid_phases:
        return json.dumps({
            'error': f"Invalid phase '{new_phase}'. Valid phases: {valid_phases}"
        })

    try:
        config_path = DATA_DIR / TRAINING_CONFIG_FILE
        with open(config_path) as f:
            config = json.load(f)

        today = date.today()
        old_phase = config.get('periodization', {}).get('current_phase', 'unknown')

        # Update periodization
        if 'periodization' not in config:
            config['periodization'] = {}

        config['periodization']['current_phase'] = new_phase.lower()
        config['periodization']['phase_start'] = today.isoformat()

        # Update current_block
        if 'current_block' not in config:
            config['current_block'] = {}

        config['current_block']['phase'] = new_phase.lower()
        config['current_block']['start_date'] = today.isoformat()

        # Get phase-specific defaults
        phases = config.get('periodization', {}).get('phases', {})
        phase_info = phases.get(new_phase.lower(), {})

        if phase_info:
            config['current_block']['focus'] = phase_info.get('key_sessions', [])
            config['current_block']['notes'] = phase_info.get('focus', f'{new_phase} phase')

        # Save
        with open(config_path, 'w') as f:
            json.dump(config, f, indent=2)

        # Log the decision
        try:
            from planner import load_coaching_log, save_coaching_log
            log = load_coaching_log()
            if 'decisions' not in log:
                log['decisions'] = []
            log['decisions'].append({
                'id': f"phase_{today.isoformat()}",
                'date': today.isoformat(),
                'type': 'phase_transition',
                'decision': f"Transitioned from {old_phase} to {new_phase}",
                'rationale': notes or "Athlete ready for next phase",
                'status': 'active',
            })
            save_coaching_log(log)
        except Exception:
            pass  # Non-critical

        return json.dumps({
            'status': 'success',
            'transition': {
                'from': old_phase,
                'to': new_phase.lower(),
                'date': today.isoformat(),
            },
            'phase_info': phase_info if phase_info else 'Custom phase - no template',
            'notes': notes,
        }, indent=2)

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
                    # Get parent-level fields (for nested sessions)
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
                        # Copy protocol for test sessions (FTP, threshold tests)
                        if 'protocol' in sub or 'protocol' in session:
                            sub['protocol'] = sub.get('protocol', session.get('protocol', []))
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
def get_coaching_score() -> str:
    """
    Calculate overall coaching effectiveness score.

    Measures how well the coaching is working across 4 dimensions:
    - Progress (40%): Is the athlete on track for their goals? (CTL trajectory)
    - Health (30%): Is the athlete staying healthy? (injuries, ACWR)
    - Achievability (20%): Is the plan realistic? (compliance rate)
    - Adaptation (10%): Are we learning what works? (response patterns)

    This is a META-TOOL for coaching self-assessment. Use to evaluate
    whether the coaching approach is effective and identify weak areas.

    Returns:
        JSON with component scores, overall score, and coaching feedback.
    """
    try:
        client = get_garmin_client()
        today = date.today()

        # Get fitness data for progress calculation
        history = load_fitness_history()
        daily_loads = history.get('daily_loads', {})
        fitness_data = calculate_fitness_metrics(daily_loads) if daily_loads else {}
        current_ctl = fitness_data.get('ctl', 0) if fitness_data else 0

        # Get 4-week CTL trend from snapshots
        snapshots = history.get('snapshots', [])
        ctl_4wk_ago = None
        for snapshot in snapshots:
            snapshot_date = date.fromisoformat(snapshot['date'])
            if (today - snapshot_date).days >= 28:
                ctl_4wk_ago = snapshot.get('ctl', 0)
                break
        ctl_gain_4wk = current_ctl - (ctl_4wk_ago or current_ctl)

        # Get A-race target
        training_config_path = DATA_DIR / TRAINING_CONFIG_FILE
        if training_config_path.exists():
            with open(training_config_path) as f:
                training_config = json.load(f)
        else:
            training_config = {}

        events = training_config.get('events', [])
        a_race = next((e for e in events if e.get('priority') == 'A'), None)

        # Calculate progress score (40% weight)
        progress_score = 50  # Default
        progress_data = {
            'current_ctl': round(current_ctl, 1),
            'ctl_gain_4wk': round(ctl_gain_4wk, 1),
            'target_ctl': None,
            'days_remaining': None,
            'trajectory': 'unknown',
        }

        if a_race:
            race_type = a_race.get('type', 'default')
            target_config = CTL_TARGETS.get(race_type, CTL_TARGETS['default'])
            target_ctl = target_config['ideal']
            progress_data['target_ctl'] = target_ctl

            try:
                race_dt = date.fromisoformat(a_race.get('date'))
                days_remaining = (race_dt - today).days
                progress_data['days_remaining'] = days_remaining

                if days_remaining > 0:
                    # Calculate required CTL gain rate
                    required_gain = target_ctl - current_ctl
                    weeks_remaining = days_remaining / 7

                    if required_gain <= 0:
                        progress_score = 100
                        progress_data['trajectory'] = 'ahead'
                    elif weeks_remaining > 0:
                        # Check if current gain rate is sufficient
                        required_weekly_gain = required_gain / weeks_remaining
                        actual_weekly_gain = ctl_gain_4wk / 4
                        if actual_weekly_gain >= required_weekly_gain:
                            progress_score = 90
                            progress_data['trajectory'] = 'on_track'
                        elif actual_weekly_gain >= required_weekly_gain * 0.7:
                            progress_score = 70
                            progress_data['trajectory'] = 'slightly_behind'
                        else:
                            progress_score = 50
                            progress_data['trajectory'] = 'behind'
            except (ValueError, TypeError):
                pass

        # Calculate health score (30% weight)
        health_score = 90  # Default - assume healthy
        health_data = {
            'injuries_active': 0,
            'acwr_status': 'unknown',
            'acwr': None,
            'overtraining_risk': 'low',
        }

        # Check injuries (active OR improving with restrictions)
        athlete_path = DATA_DIR / ATHLETE_FILE
        if athlete_path.exists():
            with open(athlete_path) as f:
                athlete = json.load(f)
            # Count injuries that are active OR improving but still have restrictions
            relevant_injuries = [
                i for i in athlete.get('injury_history', [])
                if i.get('status') in ['active', 'improving']
            ]
            active_injuries = [i for i in relevant_injuries if i.get('status') == 'active']
            improving_injuries = [i for i in relevant_injuries if i.get('status') == 'improving']

            health_data['injuries_active'] = len(active_injuries)
            health_data['injuries_improving'] = len(improving_injuries)
            health_data['restricted_activities'] = []

            # Collect all restricted activities
            for inj in relevant_injuries:
                restrictions = inj.get('restricted_activities', [])
                health_data['restricted_activities'].extend(restrictions)
            health_data['restricted_activities'] = list(set(health_data['restricted_activities']))

            # Score impact: active = -20, improving = -10
            if len(active_injuries) > 0:
                health_score -= 20 * len(active_injuries)
            if len(improving_injuries) > 0:
                health_score -= 10 * len(improving_injuries)  # Less impact but still counts

        # Check ACWR
        if fitness_data:
            acwr = fitness_data.get('acwr', 1.0)
            acwr_status = fitness_data.get('acwr_status', 'optimal')
            health_data['acwr'] = round(acwr, 2)
            health_data['acwr_status'] = acwr_status

            if acwr_status == 'danger':
                health_score -= 30
                health_data['overtraining_risk'] = 'high'
            elif acwr_status == 'elevated':
                health_score -= 15
                health_data['overtraining_risk'] = 'moderate'
            elif acwr_status == 'low':
                health_data['overtraining_risk'] = 'low'  # Undertrained, not dangerous

        health_score = max(0, health_score)

        # Calculate achievability score (20% weight)
        # Based on compliance rate over last 4 weeks
        start_date = today - timedelta(days=28)
        raw_activities = client.get_activities_by_date(
            start_date.isoformat(),
            today.isoformat()
        )
        activities = parse_activities(raw_activities)
        compliance = check_weekly_compliance(activities)

        achievability_score = 70  # Default
        achievability_data = {
            'compliance_rate': None,
            'strength_compliant': compliance.get('strength', {}).get('compliant', True),
            'mobility_compliant': compliance.get('mobility', {}).get('compliant', True),
        }

        # Simplified compliance rate calculation
        pillars_total = 0
        pillars_met = 0
        for pillar in ['strength', 'mobility', 'long_effort']:
            if pillar in compliance:
                pillars_total += 1
                if compliance[pillar].get('compliant', False):
                    pillars_met += 1

        if pillars_total > 0:
            compliance_rate = pillars_met / pillars_total * 100
            achievability_data['compliance_rate'] = round(compliance_rate, 0)
            if compliance_rate >= 90:
                achievability_score = 95
            elif compliance_rate >= 75:
                achievability_score = 80
            elif compliance_rate >= 60:
                achievability_score = 65
            else:
                achievability_score = 50

        # Calculate adaptation score (10% weight)
        adaptation_score = 50  # Default - no data
        adaptation_data = {
            'responses_logged': 0,
            'patterns_identified': 0,
            'positive_responses': 0,
            'negative_responses': 0,
        }

        try:
            log = load_coaching_log()
            responses = log.get('athlete_responses', [])
            adaptation_data['responses_logged'] = len(responses)

            # Count patterns
            patterns = set()
            positive_count = 0
            negative_count = 0
            for r in responses:
                if r.get('pattern'):
                    patterns.add(r['pattern'])
                response_type = r.get('response', '')
                if 'positive' in response_type.lower() or 'good' in response_type.lower():
                    positive_count += 1
                elif 'negative' in response_type.lower() or 'poor' in response_type.lower():
                    negative_count += 1

            adaptation_data['patterns_identified'] = len(patterns)
            adaptation_data['positive_responses'] = positive_count
            adaptation_data['negative_responses'] = negative_count

            # Score based on data richness
            if len(responses) >= 10:
                adaptation_score = 80
            elif len(responses) >= 5:
                adaptation_score = 65
            elif len(responses) >= 1:
                adaptation_score = 50
            else:
                adaptation_score = 30  # No data is a problem
        except Exception:
            pass

        # Calculate overall score (weighted average)
        overall_score = (
            progress_score * 0.4 +
            health_score * 0.3 +
            achievability_score * 0.2 +
            adaptation_score * 0.1
        )

        # Generate coaching feedback (DATA not prescriptions)
        feedback = []
        if progress_data['trajectory'] == 'behind':
            feedback.append(f"CTL trajectory behind target (gained {ctl_gain_4wk:.1f} in 4 weeks)")
        elif progress_data['trajectory'] == 'on_track':
            feedback.append(f"CTL building well (+{ctl_gain_4wk:.1f} in 4 weeks)")
        elif progress_data['trajectory'] == 'ahead':
            feedback.append("Already at or above target CTL")

        if health_data['injuries_active'] > 0:
            feedback.append(f"{health_data['injuries_active']} active injury/injuries")
        if health_data['overtraining_risk'] == 'high':
            feedback.append("High overtraining risk (ACWR elevated)")

        if achievability_data['compliance_rate'] and achievability_data['compliance_rate'] >= 80:
            feedback.append("High compliance suggests realistic plan")
        elif achievability_data['compliance_rate'] and achievability_data['compliance_rate'] < 60:
            feedback.append("Low compliance - plan may be too ambitious")

        if adaptation_data['responses_logged'] < 5:
            feedback.append("Limited athlete response data - log more patterns")

        return json.dumps({
            'overall_score': round(overall_score, 0),
            'trend': 'improving' if ctl_gain_4wk > 0 else 'declining' if ctl_gain_4wk < 0 else 'stable',
            'components': {
                'progress': {
                    'score': progress_score,
                    'weight': '40%',
                    'data': progress_data,
                },
                'health': {
                    'score': health_score,
                    'weight': '30%',
                    'data': health_data,
                },
                'achievability': {
                    'score': achievability_score,
                    'weight': '20%',
                    'data': achievability_data,
                },
                'adaptation': {
                    'score': adaptation_score,
                    'weight': '10%',
                    'data': adaptation_data,
                },
            },
            'feedback': feedback,
        }, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_coaching_snapshot() -> str:
    """
    MANDATORY FIRST CALL before making any coaching recommendations.

    Returns a comprehensive snapshot of the athlete's current state including:
    - Current weekly plan (what's planned)
    - Activities done this week (what's actual)
    - Planned vs actual comparison (gaps/surpluses)
    - Fitness metrics (CTL, ATL, TSB, ACWR)
    - Compliance status (pillars met/missing)
    - Recovery status (today's readiness)
    - Sport priority breakdown (for multi-sport athletes)
    - Active injuries and restrictions

    This prevents the coaching error of proposing plans without seeing current state.

    Multi-sport handling:
    - Analyzes all upcoming races by sport type
    - Calculates relative priority based on days until race + priority level
    - Recommends weekly volume distribution across sports
    - Identifies shared sessions (strength, mobility) vs sport-specific

    Returns:
        JSON with complete coaching context. Check this BEFORE making any recommendations.
    """
    try:
        today = date.today()
        client = get_garmin_client()

        # 1. Current Weekly Plan
        current_plan = get_current_plan()

        # 2. Activities this week (actual)
        # Use plan dates if available, otherwise use calendar week (Mon-Sun)
        if current_plan and current_plan.get('week_start'):
            week_start = date.fromisoformat(current_plan['week_start'])
        else:
            week_start = today - timedelta(days=today.weekday())  # Monday

        raw_activities = client.get_activities_by_date(
            week_start.isoformat(),
            today.isoformat()
        )
        activities_this_week = parse_activities(raw_activities)

        # 3. Planned vs Actual comparison
        planned_vs_actual = _compare_planned_actual(current_plan, activities_this_week, today)

        # 4. Fitness metrics
        history = load_fitness_history()
        daily_loads = history.get('daily_loads', {})
        if daily_loads:
            fitness_metrics = calculate_fitness_metrics(daily_loads)
            # Add coaching interpretation
            fitness_metrics['coaching_insight'] = _interpret_fitness_metrics(fitness_metrics)
        else:
            fitness_metrics = {
                'status': 'no_data',
                'action': 'Run refresh_fitness_history() to backfill from Garmin'
            }

        # 5. Compliance status
        compliance = check_weekly_compliance(activities_this_week)

        # 6. Recovery status (today) + Sleep tracking
        try:
            readiness_data = client.get_training_readiness(today.isoformat())
            recovery = _parse_readiness_for_snapshot(readiness_data)
        except Exception:
            recovery = {'status': 'unavailable', 'note': 'Could not fetch readiness data'}

        # 6b. Sleep data (last 7 days)
        sleep_data = get_sleep_summary(client, today, days=7)

        # 7. Sport priority breakdown (multi-sport analysis)
        training_config_path = DATA_DIR / TRAINING_CONFIG_FILE
        if training_config_path.exists():
            with open(training_config_path) as f:
                training_config = json.load(f)
        else:
            training_config = {}

        methodology = load_methodology()
        sport_priorities = _analyze_sport_priorities(
            training_config.get('events', []),
            training_config.get('current_block', {}),
            methodology.get('race_templates', {})
        )

        # 8. Active + improving injuries (both need attention)
        athlete_path = DATA_DIR / ATHLETE_FILE
        if athlete_path.exists():
            with open(athlete_path) as f:
                athlete = json.load(f)
            injuries = athlete.get('injury_history', [])
            # Include BOTH active AND improving - improving still have restrictions and rehab
            relevant_injuries = [
                i for i in injuries
                if i.get('status') in ['active', 'improving']
            ]
        else:
            relevant_injuries = []

        # 9. Intensity distribution (last 7 days)
        athlete_hr_zones = get_athlete_hr_zones()
        intensity_dist = calculate_intensity_distribution(activities_this_week, athlete_hr_zones)

        # 9b. Adaptation signals - DATA for LLM to reason about personalization
        # These signals help the LLM decide where in the load_increase_guidance range to operate
        adaptation_signals = _build_adaptation_signals(
            sleep_data=sleep_data,
            recovery=recovery,
            compliance=compliance,
            daily_loads=daily_loads,
            today=today
        )

        # 10. Volume data (CTL targeting for A-race) - DATA ONLY, no prescriptions
        volume_data = None
        events = training_config.get('events', [])
        a_race = next((e for e in events if e.get('priority') == 'A'), None)
        if a_race and isinstance(fitness_metrics, dict) and fitness_metrics.get('ctl'):
            current_ctl = fitness_metrics.get('ctl', 0)
            # Calculate TSS trend from daily loads (last 4 weeks)
            last_week_tss = sum(
                daily_loads.get((today - timedelta(days=i)).isoformat(), 0)
                for i in range(7)
            )
            # Calculate 4-week TSS trend
            tss_trend_4wk = []
            for week in range(4):
                week_start = week * 7
                week_end = (week + 1) * 7
                week_tss = sum(
                    daily_loads.get((today - timedelta(days=i)).isoformat(), 0)
                    for i in range(week_start, week_end)
                )
                tss_trend_4wk.append(round(week_tss, 0))
            tss_trend_4wk.reverse()  # Oldest first

            ctl_target = calculate_ctl_target(
                race_date=a_race.get('date'),
                race_type=a_race.get('type', 'default'),
                current_ctl=current_ctl,
                current_weekly_tss=last_week_tss if last_week_tss > 0 else None
            )
            if not ctl_target.get('error'):
                # ACWR facts only (no prescriptions)
                acwr = fitness_metrics.get('acwr', 1.0)
                acwr_status = fitness_metrics.get('acwr_status', 'optimal')

                # Volume data - FACTS and RANGES only, LLM decides what to do
                volume_data = {
                    # Race context
                    'a_race': a_race.get('name'),
                    'race_date': ctl_target.get('race_date'),
                    'days_until_race': ctl_target.get('days_until_race'),
                    'weeks_until_race': ctl_target.get('weeks_until_race'),

                    # CTL facts
                    'current_ctl': current_ctl,
                    'target_ctl': {
                        'min': ctl_target.get('target_ctl_min'),
                        'ideal': ctl_target.get('target_ctl_ideal'),
                    },
                    'ctl_gap': ctl_target.get('ctl_gap'),
                    'on_track': ctl_target.get('on_track'),

                    # Weekly TSS facts and ranges
                    'weekly_tss_to_reach_target': {
                        'required': ctl_target.get('weekly_tss_required'),
                        'hours_estimate': ctl_target.get('weekly_hours_required'),
                    },
                    'last_week_tss': round(last_week_tss, 0) if last_week_tss else None,
                    'tss_trend_4wk': tss_trend_4wk,

                    # Load increase guidance as RANGES (LLM chooses based on adaptation signals)
                    'load_increase_guidance': {
                        'conservative_pct': 10,   # For poor recovery/sleep
                        'standard_pct': 15,       # Baseline guidance
                        'aggressive_pct': 25,     # For excellent adaptation signals
                    },

                    # ACWR facts only
                    'acwr': {
                        'current': round(acwr, 2),
                        'status': acwr_status,  # low/optimal/elevated/danger
                        'optimal_range': [0.8, 1.3],
                        'risk_threshold': 1.5,
                    },
                }

        snapshot = {
            'snapshot_date': today.isoformat(),
            'day_of_week': today.strftime('%A'),

            'weekly_plan': {
                'week_start': current_plan.get('week_start') if current_plan else None,
                'week_end': current_plan.get('week_end') if current_plan else None,
                'days': current_plan.get('days', {}) if current_plan else {},
                'has_plan': bool(current_plan and current_plan.get('days')),
            },

            'activities_this_week': {
                'count': len(activities_this_week),
                'activities': activities_this_week,
                'total_duration_mins': sum(a.get('duration_mins', 0) or 0 for a in activities_this_week),
            },

            'planned_vs_actual': planned_vs_actual,

            'fitness_metrics': fitness_metrics,

            'volume_data': volume_data,

            'compliance': compliance,

            'recovery': recovery,

            'sleep': sleep_data,

            'adaptation_signals': adaptation_signals,

            'sport_priorities': sport_priorities,

            # All injuries that need attention (active OR improving - both have restrictions/rehab)
            'injuries': relevant_injuries,

            'intensity_distribution': intensity_dist,

            'strength': _get_strength_sync_summary(activities_this_week),

            'coaching_checklist': {
                'has_current_plan': bool(current_plan and current_plan.get('days')),
                'has_fitness_data': bool(daily_loads),
                'acwr_safe': fitness_metrics.get('acwr_status') in ['optimal', 'low'] if isinstance(fitness_metrics, dict) else False,
                'compliance_ok': compliance.get('overall_compliant', False),
                'no_blocking_injuries': len([i for i in relevant_injuries if i.get('severity') == 'severe']) == 0,
                'sleep_adequate': sleep_data.get('status') == 'adequate' if sleep_data else False,
                'has_injuries_needing_rehab': any(i.get('rehab_protocol') for i in relevant_injuries),
            },
        }

        return json.dumps(snapshot, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})


def _compare_planned_actual(plan: dict, activities: list, today: date) -> dict:
    """Compare planned sessions against actual activities."""
    if not plan or not plan.get('days'):
        return {'status': 'no_plan', 'note': 'No weekly plan to compare against'}

    comparison = {
        'sessions_planned': 0,
        'sessions_completed': 0,
        'sessions_missed': 0,
        'sessions_pending': 0,
        'gaps': [],
        'surpluses': [],
        'details': []
    }

    for day_str, day_data in plan.get('days', {}).items():
        try:
            day_date = date.fromisoformat(day_str)
        except ValueError:
            continue

        planned = day_data.get('planned', {})
        if not planned or planned.get('type', '').lower() == 'rest':
            continue

        comparison['sessions_planned'] += 1

        # Check if this day has passed
        if day_date > today:
            comparison['sessions_pending'] += 1
            comparison['details'].append({
                'date': day_str,
                'status': 'pending',
                'planned': planned.get('type'),
            })
            continue

        # Find matching activity
        day_activities = [a for a in activities if a.get('date') == day_str]

        if day_activities:
            comparison['sessions_completed'] += 1
            actual_type = day_activities[0].get('type', 'unknown')
            actual_duration = day_activities[0].get('duration_mins', 0)
            planned_duration = planned.get('duration_mins', 0)

            comparison['details'].append({
                'date': day_str,
                'status': 'completed',
                'planned': planned.get('type'),
                'actual': actual_type,
                'duration_planned': planned_duration,
                'duration_actual': actual_duration,
            })

            # Check if duration significantly different
            if planned_duration and actual_duration:
                diff_pct = (actual_duration - planned_duration) / planned_duration * 100
                if diff_pct < -30:
                    comparison['gaps'].append(f"{day_str}: {planned.get('type')} was shorter than planned ({actual_duration}min vs {planned_duration}min)")
                elif diff_pct > 30:
                    comparison['surpluses'].append(f"{day_str}: {planned.get('type')} was longer than planned ({actual_duration}min vs {planned_duration}min)")
        else:
            comparison['sessions_missed'] += 1
            comparison['gaps'].append(f"{day_str}: Missed {planned.get('type')}")
            comparison['details'].append({
                'date': day_str,
                'status': 'missed',
                'planned': planned.get('type'),
            })

    comparison['completion_rate'] = (
        round(comparison['sessions_completed'] / comparison['sessions_planned'] * 100, 1)
        if comparison['sessions_planned'] > 0 else None
    )

    return comparison


def _get_strength_sync_summary(activities: list) -> dict:
    """Get strength sync summary for coaching snapshot."""
    try:
        # Check if there are any recent strength activities to sync
        strength_activities = [
            a for a in activities
            if a.get('type') in ['strength_training', 'indoor_cardio', 'gym']
        ]

        # Load current baseline
        baseline = _get_strength_baseline_data()
        last_synced = baseline.get('last_synced')
        exercises = baseline.get('exercises', {})

        # Check for pending progressions
        pending_progressions = []
        for ex_key, ex_data in exercises.items():
            progression = ex_data.get('progression')
            if progression and progression.get('status') == 'pending':
                current = ex_data.get('current', {})
                pending_progressions.append({
                    'exercise': ex_key,
                    'current_kg': current.get('weight_kg'),
                    'suggested_kg': progression.get('suggested_weight_kg'),
                    'rationale': progression.get('rationale')
                })

        # Check if there are unsynced strength sessions
        unsynced_activities = []
        if last_synced and strength_activities:
            for activity in strength_activities:
                activity_date = activity.get('date')
                if activity_date and activity_date > last_synced:
                    unsynced_activities.append({
                        'activity_id': activity.get('activity_id'),
                        'date': activity_date,
                        'duration_mins': activity.get('duration_mins')
                    })

        return {
            'last_synced': last_synced,
            'exercises_tracked': len(exercises),
            'pending_progressions': pending_progressions,
            'unsynced_sessions': unsynced_activities,
            'needs_sync': len(unsynced_activities) > 0
        }

    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def _interpret_fitness_metrics(metrics: dict) -> str:
    """Generate coaching interpretation of fitness metrics."""
    insights = []

    # TSB interpretation
    tsb = metrics.get('tsb', 0)
    if tsb > 15:
        insights.append("Very fresh - consider adding stimulus")
    elif tsb > 0:
        insights.append("Fresh and ready for quality work")
    elif tsb > -15:
        insights.append("Manageable fatigue - normal training OK")
    elif tsb > -30:
        insights.append("Fatigued - monitor recovery")
    else:
        insights.append("Heavy fatigue - recovery day recommended")

    # ACWR interpretation
    acwr_status = metrics.get('acwr_status', '')
    if acwr_status == 'optimal':
        insights.append("Load in sweet spot (0.8-1.3)")
    elif acwr_status == 'low':
        insights.append("Load is low - safe to build")
    elif acwr_status == 'elevated':
        insights.append("Load spike - caution advised")
    elif acwr_status == 'danger':
        insights.append("HIGH INJURY RISK - reduce load")

    return "; ".join(insights)


def _parse_readiness_for_snapshot(readiness_data: dict) -> dict:
    """Parse readiness data for snapshot."""
    if not readiness_data:
        return {'status': 'unavailable'}

    return {
        'score': readiness_data.get('score'),
        'level': readiness_data.get('level'),
        'hrv_status': readiness_data.get('hrvStatus'),
        'sleep_score': readiness_data.get('sleepScore'),
        'recovery_time_mins': readiness_data.get('recoveryTime'),
        'recommendation': _readiness_to_recommendation(readiness_data.get('level', ''))
    }


def _readiness_to_recommendation(level: str) -> str:
    """Convert readiness level to coaching recommendation."""
    recommendations = {
        'PRIME': 'Excellent recovery - ideal for hard session or test',
        'HIGH': 'Good recovery - quality training supported',
        'MODERATE': 'Normal recovery - regular training OK',
        'LOW': 'Poor recovery - consider easier session or rest',
        'POOR': 'Very low recovery - rest day recommended',
    }
    return recommendations.get(level, 'Check Garmin for details')


def _build_adaptation_signals(
    sleep_data: dict,
    recovery: dict,
    compliance: dict,
    daily_loads: dict,
    today: date
) -> dict:
    """
    Build adaptation signals for LLM personalization decisions.

    These signals help the LLM decide where in the load_increase_guidance
    range to operate (conservative/standard/aggressive).

    Returns DATA only - no prescriptions.
    """
    # 1. Sleep trends
    sleep_signals = {
        'avg_7d_hrs': sleep_data.get('avg_duration_hrs') if sleep_data else None,
        'avg_score_7d': sleep_data.get('avg_score') if sleep_data else None,
        'recent_avg_hrs': sleep_data.get('recent_avg_duration') if sleep_data else None,
        'recent_trend': sleep_data.get('recent_trend', 0) if sleep_data else 0,  # Positive = improving
        'status': sleep_data.get('status') if sleep_data else 'unknown',
        'acute_status': sleep_data.get('acute_status') if sleep_data else 'unknown',
        'deficit_days_7d': sleep_data.get('poor_quality_nights', 0) if sleep_data else 0,
    }

    # Determine sleep trend direction
    if sleep_signals['recent_trend'] and sleep_signals['recent_trend'] > 0.3:
        sleep_signals['trend'] = 'improving'
    elif sleep_signals['recent_trend'] and sleep_signals['recent_trend'] < -0.3:
        sleep_signals['trend'] = 'declining'
    else:
        sleep_signals['trend'] = 'stable'

    # 2. Recovery signals (from Garmin readiness)
    recovery_signals = {
        'readiness_score': recovery.get('score') if recovery else None,
        'readiness_level': recovery.get('level') if recovery else None,
        'hrv_status': recovery.get('hrv_status') if recovery else None,
    }

    # Infer HRV trend from level (simplified - would need history for true trend)
    hrv_level = recovery_signals.get('hrv_status', '')
    if hrv_level in ['BALANCED', 'GOOD']:
        recovery_signals['hrv_trend'] = 'stable'
    elif hrv_level in ['LOW', 'POOR']:
        recovery_signals['hrv_trend'] = 'declining'
    else:
        recovery_signals['hrv_trend'] = 'unknown'

    # 3. Compliance signals
    compliance_signals = {
        'overall_compliant': compliance.get('overall_compliant', False),
        'strength_compliant': compliance.get('strength', {}).get('compliant', True),
        'mobility_compliant': compliance.get('mobility', {}).get('compliant', True),
    }

    # Calculate compliance rate (simplified - based on current week's pillars)
    pillars_total = 0
    pillars_met = 0
    for pillar in ['strength', 'mobility', 'long_effort']:
        if pillar in compliance:
            pillars_total += 1
            if compliance[pillar].get('compliant', False):
                pillars_met += 1
    compliance_signals['rate_this_week'] = round(pillars_met / pillars_total * 100, 0) if pillars_total > 0 else None

    # 4. Adaptation patterns (from coaching log)
    try:
        log = load_coaching_log()
        responses = log.get('athlete_responses', [])

        # Extract key patterns
        patterns = {}
        for r in responses:
            pattern = r.get('pattern')
            if pattern:
                patterns[pattern] = patterns.get(pattern, 0) + 1

        adaptation_patterns = {
            'handles_volume_well': patterns.get('handles_volume_well', 0) > patterns.get('struggles_with_volume', 0),
            'recovers_quickly': patterns.get('recovers_quickly', 0) > patterns.get('slow_recovery', 0),
            'needs_extra_rest_after_intensity': patterns.get('needs_recovery_after_intensity', 0) > 0,
            'patterns_logged': len(patterns),
            'total_responses': len(responses),
        }
    except Exception:
        adaptation_patterns = {
            'handles_volume_well': None,  # Unknown - no data
            'recovers_quickly': None,
            'needs_extra_rest_after_intensity': None,
            'patterns_logged': 0,
            'total_responses': 0,
        }

    return {
        'sleep': sleep_signals,
        'recovery': recovery_signals,
        'compliance': compliance_signals,
        'adaptation_patterns': adaptation_patterns,
    }


def _analyze_sport_priorities(events: list, current_block: dict, race_templates: dict) -> dict:
    """
    Analyze multi-sport priorities based on upcoming races.

    Returns recommended volume distribution across sports and identifies
    which sessions are shared (strength, mobility) vs sport-specific.
    """
    today = date.today()

    # Map race types to sports (defined at function level for reuse)
    sport_mapping = {
        'multi_day_mtb': 'cycling',
        'road_cycling': 'cycling',
        'trail_ultra': 'running',
        'marathon': 'running',
        'half_marathon': 'running',
        '10k': 'running',
        '5k': 'running',
        'triathlon': 'triathlon',
        'swimming': 'swimming',
        'tournament': 'multi_sport',
    }

    # Categorize events by sport type
    sports_analysis = {}
    for event in events:
        try:
            event_date = date.fromisoformat(event.get('date', ''))
            days_until = (event_date - today).days
            if days_until < 0:
                continue  # Skip past events
        except ValueError:
            continue

        sport_type = event.get('type', 'unknown')
        priority = event.get('priority', 'D')
        sport = sport_mapping.get(sport_type, 'other')

        # Calculate priority weight (closer + higher priority = more weight)
        priority_weights = {'A': 4, 'B': 3, 'C': 2, 'D': 1}
        priority_weight = priority_weights.get(priority, 1)

        # Time-based weight (races in next 8 weeks get more weight)
        if days_until <= 14:
            time_weight = 4  # Very close - peak/taper
        elif days_until <= 28:
            time_weight = 3  # Close - build/peak
        elif days_until <= 56:
            time_weight = 2  # Medium - build
        else:
            time_weight = 1  # Far - base

        score = priority_weight * time_weight

        if sport not in sports_analysis:
            sports_analysis[sport] = {
                'events': [],
                'total_score': 0,
                'primary_focus': False,
            }

        sports_analysis[sport]['events'].append({
            'name': event.get('name'),
            'days_until': days_until,
            'priority': priority,
            'type': sport_type,
            'score': score,
        })
        sports_analysis[sport]['total_score'] += score

    # Calculate percentage distribution
    total_score = sum(s['total_score'] for s in sports_analysis.values())
    if total_score > 0:
        for sport in sports_analysis:
            sports_analysis[sport]['volume_pct'] = round(
                sports_analysis[sport]['total_score'] / total_score * 100, 1
            )
            # Mark primary sport
            if sports_analysis[sport]['total_score'] == max(
                s['total_score'] for s in sports_analysis.values()
            ):
                sports_analysis[sport]['primary_focus'] = True

    # Get shared sessions (apply to all sports)
    shared_sessions = ['strength', 'mobility', 'recovery']

    # Get sport-specific key sessions from race templates
    sport_specific_sessions = {}
    for sport_type, template in race_templates.items():
        sport = sport_mapping.get(sport_type, sport_type)
        if sport not in sport_specific_sessions:
            sport_specific_sessions[sport] = []
        key_sessions = template.get('key_sessions', [])
        for session in key_sessions:
            session_type = session.get('type')
            if session_type and session_type not in sport_specific_sessions[sport]:
                sport_specific_sessions[sport].append(session_type)

    return {
        'sports': sports_analysis,
        'shared_sessions': shared_sessions,
        'sport_specific_sessions': sport_specific_sessions,
        'recommendation': _generate_sport_blend_recommendation(sports_analysis, current_block),
        'has_multi_sport': len(sports_analysis) > 1,
    }


def _generate_sport_blend_recommendation(sports_analysis: dict, current_block: dict) -> str:
    """Generate recommendation for blending multiple sports."""
    if not sports_analysis:
        return "No upcoming events - focus on general fitness"

    if len(sports_analysis) == 1:
        sport = list(sports_analysis.keys())[0]
        return f"Single sport focus: {sport}. Follow sport-specific periodization."

    # Multi-sport recommendations
    primary = None
    secondary = []
    for sport, data in sports_analysis.items():
        if data['primary_focus']:
            primary = sport
        else:
            secondary.append(sport)

    if primary and secondary:
        secondary_str = ', '.join(secondary)
        return (
            f"Multi-sport: Primary focus on {primary} ({sports_analysis[primary]['volume_pct']}%). "
            f"Maintain {secondary_str} with complementary sessions. "
            f"Shared strength/mobility benefits all sports."
        )

    return "Balance training across all sports based on race proximity"


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
def research_exercise(exercise_name: str, save_to_library: bool = True) -> str:
    """
    Research proper form, cues, and common mistakes for an exercise.

    Fetches information from fitness resources and optionally saves to the
    exercise library for use in Garmin workout notes.

    Args:
        exercise_name: Name of the exercise (e.g., "Romanian deadlift", "Bulgarian split squat")
        save_to_library: Whether to cache the form cues for future workouts (default True)

    Returns:
        JSON with form cues, setup instructions, common mistakes, and modifications.

    Usage:
        research_exercise("Romanian deadlift")
        research_exercise("hip thrust", save_to_library=True)
    """
    from config import HTTP_TIMEOUT_SECONDS, PAGE_TEXT_MAX_CHARS, EXERCISE_LIBRARY_FILE
    import requests
    import re

    try:
        # Normalize exercise name
        exercise_normalized = exercise_name.strip().lower()
        exercise_url_name = exercise_name.replace(' ', '_').replace('-', '_')

        result = {
            "exercise": exercise_name,
            "form_cues": {},
            "sources": [],
            "cached": False,
        }

        # Check if already in library
        library_path = DATA_DIR / EXERCISE_LIBRARY_FILE
        library = {}
        if library_path.exists():
            with open(library_path) as f:
                library = json.load(f)

            if exercise_normalized in library:
                cached = library[exercise_normalized]
                cached["cached"] = True
                cached["note"] = "Retrieved from exercise library. Use research_exercise with a new name to research different exercises."
                return json.dumps(cached, indent=2)

        # Primary source: muscleandstrength.com has excellent form guides with videos
        # URL format: muscleandstrength.com/exercises/exercise-name.html
        exercise_url_slug = exercise_name.lower().replace(' ', '-').replace('_', '-')

        # Search sources for exercise info - prioritize muscleandstrength for video content
        search_sources = [
            {
                "name": "Muscle & Strength",
                "url": f"https://www.muscleandstrength.com/exercises/{exercise_url_slug}.html",
                "type": "form",
                "has_video": True
            },
            {
                "name": "ExRx",
                "url": f"https://exrx.net/WeightExercises/search?q={exercise_url_name}",
                "type": "form",
                "has_video": False
            },
        ]

        # Track video URL for successful source
        video_url = None

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
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

        # Try to fetch content
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

                    # Check for exercise-related content
                    content_lower = content.lower()
                    exercise_indicators = ['muscle', 'exercise', 'movement', 'form', 'position', 'repetition', 'set']
                    has_exercise_content = any(ind in content_lower for ind in exercise_indicators)

                    if len(content) > 300 and has_exercise_content:
                        fetched_content = content
                        result["sources"].append(response.url)
                        # Capture video URL if source has videos
                        if source.get("has_video"):
                            video_url = source["url"]
                        break
            except Exception:
                continue

        # Extract relevant information
        if fetched_content:
            sentences = re.split(r'[.!?]+', fetched_content)

            # Keywords for different aspects
            setup_keywords = ['position', 'stance', 'grip', 'feet', 'hands', 'setup', 'starting']
            execution_keywords = ['lower', 'raise', 'push', 'pull', 'extend', 'flex', 'drive', 'squeeze', 'contract']
            cue_keywords = ['keep', 'maintain', 'avoid', 'ensure', 'focus', 'engage', 'brace']
            mistake_keywords = ['avoid', 'don\'t', 'never', 'mistake', 'wrong', 'error', 'common']
            muscle_keywords = ['muscle', 'target', 'work', 'engage', 'activate']

            setup_findings = []
            execution_findings = []
            cue_findings = []
            mistake_findings = []
            muscle_findings = []

            for sentence in sentences:
                sentence_clean = sentence.strip()
                sentence_lower = sentence_clean.lower()
                if len(sentence_clean) > 15 and len(sentence_clean) < 200:
                    if any(kw in sentence_lower for kw in setup_keywords):
                        setup_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in execution_keywords):
                        execution_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in cue_keywords):
                        cue_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in mistake_keywords):
                        mistake_findings.append(sentence_clean)
                    if any(kw in sentence_lower for kw in muscle_keywords):
                        muscle_findings.append(sentence_clean)

            result["form_cues"] = {
                "setup": setup_findings[:3] if setup_findings else ["Set up with proper stance and alignment"],
                "execution": execution_findings[:4] if execution_findings else ["Control the movement through full range of motion"],
                "key_cues": cue_findings[:4] if cue_findings else ["Focus on mind-muscle connection"],
                "common_mistakes": mistake_findings[:3] if mistake_findings else ["Avoid using momentum or excessive weight"],
                "muscles_worked": muscle_findings[:2] if muscle_findings else [],
            }

            # Generate a concise note for Garmin workouts
            garmin_note_parts = []
            if setup_findings:
                garmin_note_parts.append(setup_findings[0][:80])
            if cue_findings:
                garmin_note_parts.append(cue_findings[0][:80])

            # Build garmin note with video link
            note_text = ". ".join(garmin_note_parts) if garmin_note_parts else f"Perform {exercise_name} with controlled form"

            # Add video URL to note (shortened domain for Garmin display)
            if video_url:
                result["video_url"] = video_url
                # Shorten URL for Garmin note display
                short_url = video_url.replace("https://www.", "").replace("https://", "")
                result["garmin_note"] = f"{note_text[:180]}. Video: {short_url}"[:250]
            else:
                result["garmin_note"] = note_text[:250]

        else:
            # Couldn't fetch - provide fallback video URL and guidance
            fallback_video = f"https://www.muscleandstrength.com/exercises/{exercise_url_slug}.html"
            result["form_cues"] = {
                "note": f"Unable to fetch form guide for '{exercise_name}'.",
                "suggested_searches": [
                    f"{exercise_name} form guide",
                    f"{exercise_name} how to",
                    f"{exercise_name} technique",
                ],
            }
            result["video_url"] = fallback_video
            result["garmin_note"] = f"Perform {exercise_name} with controlled form. Video: muscleandstrength.com/exercises/{exercise_url_slug}"[:250]

        # Add modifications guidance
        result["modifications"] = {
            "easier": "Reduce weight, decrease range of motion, or use assisted variation",
            "harder": "Add weight, slow tempo, add pause at bottom, or use unilateral variation",
            "equipment_alternatives": "Check gym equipment available or ask for substitution"
        }

        # Save to library if requested
        if save_to_library and result.get("form_cues"):
            library[exercise_normalized] = {
                "exercise": exercise_name,
                "form_cues": result["form_cues"],
                "garmin_note": result.get("garmin_note", ""),
                "video_url": result.get("video_url", ""),
                "modifications": result["modifications"],
                "sources": result["sources"],
                "researched_date": date.today().isoformat(),
            }

            # Ensure data directory exists
            DATA_DIR.mkdir(exist_ok=True)
            with open(library_path, 'w') as f:
                json.dump(library, f, indent=2)

            result["cached"] = True
            result["cache_note"] = "Saved to exercise library. Will be included in Garmin workout notes."

        return json.dumps(result, indent=2)

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


# ============================================================================
# Strength Sync Tools
# ============================================================================

def _get_canonical_exercise_group(exercise_name: str, category: str, equivalence_groups: dict) -> str:
    """Map exercise name to canonical group."""
    from config import DEFAULT_EQUIVALENCE_GROUPS

    # Merge default with custom groups
    groups = {**DEFAULT_EQUIVALENCE_GROUPS, **equivalence_groups}

    # Check if category is a known group
    if category in groups:
        return category

    # Check if exercise is in any group
    for group, exercises in groups.items():
        if exercise_name in exercises:
            return group

    # Fallback: use category as group
    return category


def _calculate_progression(current_weight: float, target_reps: int, actual_reps: int, actual_sets: int) -> dict:
    """Calculate progression suggestion based on performance."""
    from config import PROGRESSION_INCREMENT_KG, MIN_SETS_FOR_PROGRESSION

    if actual_sets >= MIN_SETS_FOR_PROGRESSION and actual_reps >= target_reps:
        return {
            "suggested_weight_kg": current_weight + PROGRESSION_INCREMENT_KG,
            "suggested_reps": target_reps,
            "rationale": f"Completed {actual_sets}x{actual_reps} @ {current_weight}kg - ready for +{PROGRESSION_INCREMENT_KG}kg",
            "status": "pending"
        }
    return None


def _get_strength_baseline_data() -> dict:
    """Load strength baseline from athlete profile."""
    from config import DEFAULT_EQUIVALENCE_GROUPS

    athlete = load_athlete()
    baseline = athlete.get('strength_baseline', {})

    # Ensure equivalence groups exist
    if 'equivalence_groups' not in baseline:
        baseline['equivalence_groups'] = DEFAULT_EQUIVALENCE_GROUPS

    if 'exercises' not in baseline:
        baseline['exercises'] = {}

    return baseline


def _save_strength_baseline(baseline: dict) -> None:
    """Save strength baseline to athlete profile."""
    from planner import save_json_file
    from config import ATHLETE_FILE

    athlete = load_athlete()

    # Remove read-only fields before saving
    athlete.pop('baseline', None)
    athlete.pop('personal_records', None)
    athlete.pop('baseline_last_refreshed', None)

    athlete['strength_baseline'] = baseline

    save_json_file(ATHLETE_FILE, athlete)


@mcp.tool()
def sync_strength_session(activity_id: str = None) -> str:
    """
    Sync completed strength session from Garmin and update exercise baselines.

    Pulls exercise data (sets, reps, weights) from a completed strength workout
    and updates the athlete's strength baseline. Suggests progression when
    target reps are completed.

    Args:
        activity_id: Specific activity ID to sync. If omitted, syncs the most
                     recent strength session.

    Returns:
        JSON with synced exercises, baseline updates, PRs, and progression suggestions.

    Usage:
        sync_strength_session()  # Sync most recent strength session
        sync_strength_session("21536055257")  # Sync specific activity
    """
    from config import WEIGHT_GRAM_TO_KG, DEFAULT_EQUIVALENCE_GROUPS

    try:
        client = get_garmin_client()
        today = date.today()

        # Find strength activity to sync
        if activity_id:
            target_activity_id = int(activity_id)
        else:
            # Find most recent strength activity
            week_ago = today - timedelta(days=7)
            raw_activities = client.get_activities_by_date(
                week_ago.isoformat(),
                today.isoformat()
            )
            activities = parse_activities(raw_activities)

            strength_activities = [
                a for a in activities
                if a.get('type') in ['strength_training', 'indoor_cardio', 'gym']
            ]

            if not strength_activities:
                return json.dumps({
                    "status": "no_activity",
                    "message": "No strength sessions found in the last 7 days"
                })

            # Get most recent
            target_activity_id = strength_activities[0]['activity_id']

        # Fetch exercise sets from Garmin
        try:
            exercise_sets = client.get_activity_exercise_sets(target_activity_id)
        except Exception as e:
            return json.dumps({
                "status": "error",
                "message": f"Could not fetch exercise sets: {str(e)}"
            })

        if not exercise_sets or 'exerciseSets' not in exercise_sets:
            return json.dumps({
                "status": "no_data",
                "message": "No exercise set data found for this activity"
            })

        # Load current baseline
        baseline = _get_strength_baseline_data()
        equivalence_groups = baseline.get('equivalence_groups', DEFAULT_EQUIVALENCE_GROUPS)

        # Process exercise sets
        exercise_summary = {}  # group -> {sets, reps, weight, variation}

        for exercise_set in exercise_sets['exerciseSets']:
            if exercise_set.get('setType') != 'ACTIVE':
                continue

            exercises = exercise_set.get('exercises', [])
            if not exercises:
                continue

            # Take the first exercise (highest probability)
            exercise = exercises[0]
            exercise_name = exercise.get('name')
            category = exercise.get('category')

            if not exercise_name or not category:
                continue

            # Map to canonical group
            group = _get_canonical_exercise_group(exercise_name, category, equivalence_groups)
            group_key = group.lower().replace('_', ' ').replace(' ', '_')

            # Get set data
            reps = exercise_set.get('repetitionCount', 0)
            weight_grams = exercise_set.get('weight', 0) or 0
            weight_kg = weight_grams / WEIGHT_GRAM_TO_KG

            # Aggregate by group
            if group_key not in exercise_summary:
                exercise_summary[group_key] = {
                    'canonical_name': group,
                    'variation': exercise_name,
                    'sets': 0,
                    'total_reps': 0,
                    'max_weight_kg': 0,
                    'weights': []
                }

            exercise_summary[group_key]['sets'] += 1
            exercise_summary[group_key]['total_reps'] += reps
            if weight_kg > 0:
                exercise_summary[group_key]['weights'].append(weight_kg)
                exercise_summary[group_key]['max_weight_kg'] = max(
                    exercise_summary[group_key]['max_weight_kg'],
                    weight_kg
                )

        # Update baselines
        updates = []
        prs = []
        progression_suggestions = []
        activity_date = today.isoformat()

        for group_key, data in exercise_summary.items():
            sets = data['sets']
            avg_reps = data['total_reps'] // sets if sets > 0 else 0
            weight_kg = data['max_weight_kg']
            variation = data['variation']

            # Get or create exercise baseline
            if group_key not in baseline['exercises']:
                baseline['exercises'][group_key] = {
                    'canonical_name': data['canonical_name'],
                    'preferred_variation': variation,
                    'current': None,
                    'history': [],
                    'progression': None
                }

            exercise_baseline = baseline['exercises'][group_key]
            previous = exercise_baseline.get('current')

            # Check for PR
            if previous and weight_kg > previous.get('weight_kg', 0):
                prs.append({
                    'exercise': group_key,
                    'previous_kg': previous.get('weight_kg'),
                    'new_kg': weight_kg,
                    'improvement_kg': weight_kg - previous.get('weight_kg', 0)
                })

            # Update current
            exercise_baseline['current'] = {
                'weight_kg': weight_kg if weight_kg > 0 else (previous.get('weight_kg') if previous else None),
                'reps': avg_reps,
                'sets': sets,
                'last_performed': activity_date
            }

            # Update preferred variation
            exercise_baseline['preferred_variation'] = variation

            # Add to history
            exercise_baseline['history'].append({
                'date': activity_date,
                'weight_kg': weight_kg,
                'reps': avg_reps,
                'sets': sets,
                'variation': variation
            })

            # Keep history to last 20 entries
            exercise_baseline['history'] = exercise_baseline['history'][-20:]

            # Calculate progression suggestion
            current_weight = exercise_baseline['current'].get('weight_kg', 0)
            if current_weight and current_weight > 0:
                progression = _calculate_progression(
                    current_weight,
                    target_reps=12,  # Default target
                    actual_reps=avg_reps,
                    actual_sets=sets
                )
                if progression:
                    exercise_baseline['progression'] = progression
                    progression_suggestions.append({
                        'exercise': group_key,
                        'current_kg': current_weight,
                        'suggested_kg': progression['suggested_weight_kg'],
                        'rationale': progression['rationale']
                    })

            updates.append({
                'exercise': group_key,
                'previous': previous,
                'current': exercise_baseline['current']
            })

        # Update last synced
        baseline['last_synced'] = activity_date

        # Save updated baseline
        _save_strength_baseline(baseline)

        return json.dumps({
            "status": "success",
            "activity_id": target_activity_id,
            "activity_date": activity_date,
            "exercises_synced": len(exercise_summary),
            "updates": updates,
            "prs": prs,
            "progression_suggestions": progression_suggestions
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_strength_baseline(exercise: str = None) -> str:
    """
    View current strength baselines for exercises.

    Returns the current weights, reps, and progression status for all
    tracked exercises or a specific exercise group.

    Args:
        exercise: Specific exercise group to view (e.g., "bench_press").
                  If omitted, returns all baselines.

    Returns:
        JSON with current baselines, pending progressions, and history.

    Usage:
        get_strength_baseline()  # View all
        get_strength_baseline("bench_press")  # View specific
    """
    try:
        baseline = _get_strength_baseline_data()

        if exercise:
            # Normalize exercise name
            exercise_key = exercise.lower().replace(' ', '_')

            if exercise_key not in baseline.get('exercises', {}):
                return json.dumps({
                    "status": "not_found",
                    "exercise": exercise,
                    "available": list(baseline.get('exercises', {}).keys())
                })

            exercise_data = baseline['exercises'][exercise_key]
            return json.dumps({
                "exercise": exercise_key,
                "canonical_name": exercise_data.get('canonical_name'),
                "preferred_variation": exercise_data.get('preferred_variation'),
                "current": exercise_data.get('current'),
                "pending_progression": exercise_data.get('progression'),
                "recent_history": exercise_data.get('history', [])[-5:]
            }, indent=2)

        # Return summary of all exercises
        exercises_summary = {}
        pending_progressions = []

        for ex_key, ex_data in baseline.get('exercises', {}).items():
            current = ex_data.get('current', {})
            progression = ex_data.get('progression')

            exercises_summary[ex_key] = {
                'current_weight_kg': current.get('weight_kg'),
                'current_reps': current.get('reps'),
                'current_sets': current.get('sets'),
                'preferred_variation': ex_data.get('preferred_variation'),
                'last_performed': current.get('last_performed'),
                'has_pending_progression': progression is not None and progression.get('status') == 'pending'
            }

            if progression and progression.get('status') == 'pending':
                pending_progressions.append({
                    'exercise': ex_key,
                    'current_kg': current.get('weight_kg'),
                    'suggested_kg': progression.get('suggested_weight_kg'),
                    'rationale': progression.get('rationale')
                })

        return json.dumps({
            "last_synced": baseline.get('last_synced'),
            "exercises": exercises_summary,
            "pending_progressions": pending_progressions,
            "total_exercises_tracked": len(exercises_summary)
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def approve_progression(exercise: str) -> str:
    """
    Approve a suggested weight progression for an exercise.

    When a progression is approved, the suggested weight becomes the new
    baseline and will be used in future workout plans.

    Args:
        exercise: Exercise group to approve progression for (e.g., "bench_press")

    Returns:
        JSON confirmation with old and new weights.

    Usage:
        approve_progression("bench_press")
    """
    try:
        baseline = _get_strength_baseline_data()
        exercise_key = exercise.lower().replace(' ', '_')

        if exercise_key not in baseline.get('exercises', {}):
            return json.dumps({
                "status": "error",
                "message": f"Exercise '{exercise}' not found in baseline",
                "available": list(baseline.get('exercises', {}).keys())
            })

        exercise_data = baseline['exercises'][exercise_key]
        progression = exercise_data.get('progression')

        if not progression or progression.get('status') != 'pending':
            return json.dumps({
                "status": "error",
                "message": f"No pending progression for '{exercise}'"
            })

        # Get old and new weights
        old_weight = exercise_data['current'].get('weight_kg', 0)
        new_weight = progression['suggested_weight_kg']

        # Update current weight
        exercise_data['current']['weight_kg'] = new_weight

        # Mark progression as approved
        exercise_data['progression']['status'] = 'approved'
        exercise_data['progression']['approved_date'] = date.today().isoformat()

        # Save
        _save_strength_baseline(baseline)

        return json.dumps({
            "status": "success",
            "exercise": exercise_key,
            "old_weight_kg": old_weight,
            "new_weight_kg": new_weight,
            "message": f"{exercise.replace('_', ' ').title()} progression approved. Next session: {exercise_data['current'].get('sets', 3)}x{exercise_data['current'].get('reps', 12)} @ {new_weight}kg"
        }, indent=2)

    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
def set_exercise_preference(exercise_group: str, preferred_variation: str) -> str:
    """
    Set the preferred variation for an exercise group.

    When building workouts, the system will use your preferred variation
    instead of a generic exercise name.

    Args:
        exercise_group: Canonical group (e.g., "BENCH_PRESS", "ROW")
        preferred_variation: Specific exercise name (e.g., "BARBELL_BENCH_PRESS")

    Returns:
        JSON confirmation with updated preference.

    Usage:
        set_exercise_preference("BENCH_PRESS", "BARBELL_BENCH_PRESS")
    """
    from config import DEFAULT_EQUIVALENCE_GROUPS

    try:
        baseline = _get_strength_baseline_data()
        exercise_key = exercise_group.lower().replace(' ', '_')

        # Validate the group exists
        all_groups = {**DEFAULT_EQUIVALENCE_GROUPS, **baseline.get('equivalence_groups', {})}
        group_upper = exercise_group.upper()

        if group_upper not in all_groups:
            return json.dumps({
                "status": "error",
                "message": f"Unknown exercise group: {exercise_group}",
                "available_groups": list(all_groups.keys())
            })

        # Validate the variation is in the group
        if preferred_variation not in all_groups[group_upper]:
            return json.dumps({
                "status": "error",
                "message": f"'{preferred_variation}' is not in the {group_upper} group",
                "available_variations": all_groups[group_upper]
            })

        # Update or create the exercise entry
        if exercise_key not in baseline['exercises']:
            baseline['exercises'][exercise_key] = {
                'canonical_name': group_upper,
                'preferred_variation': preferred_variation,
                'current': None,
                'history': [],
                'progression': None
            }
        else:
            baseline['exercises'][exercise_key]['preferred_variation'] = preferred_variation

        # Save
        _save_strength_baseline(baseline)

        return json.dumps({
            "status": "success",
            "exercise_group": group_upper,
            "preferred_variation": preferred_variation,
            "message": f"Future {group_upper.lower().replace('_', ' ')} exercises will use {preferred_variation}"
        }, indent=2)

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