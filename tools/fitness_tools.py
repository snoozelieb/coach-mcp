"""Fitness, baseline, and athlete data tools.

Registers MCP tools for:
- refresh_athlete_baseline
- get_training_readiness
- get_load_status
- get_fitness_status
- refresh_fitness_history
- get_intensity_distribution
- get_onboarding_guide
- get_athlete
"""

from mcp_app import mcp
from garmin_client import garmin_api_call
from parsers import parse_activities, parse_training_readiness, parse_personal_records, calculate_baseline, parse_user_profile, parse_hr_zones
from planner import load_athlete, load_methodology, load_json_file, save_json_file
from fitness import (load_fitness_history, calculate_fitness_metrics, calculate_intensity_distribution,
                     get_athlete_hr_zones, get_fitness_trend,
                     update_fitness_history, _extract_total_loads, calculate_sport_fitness_metrics)
from config import DATA_DIR, PROFILE_HISTORY_DAYS, ATHLETE_BASELINE_FILE, ATHLETE_FILE
from datetime import date, timedelta
import json
import logging

logger = logging.getLogger(__name__)


def _auto_populate_athlete(garmin_profile: dict) -> None:
    """Auto-fill None fields in athlete.json personal section from Garmin profile.

    Only fills fields that are None — never overwrites manually set values.
    """
    athlete = load_json_file(ATHLETE_FILE)
    if not athlete:
        return

    personal = athlete.get('personal', {})
    changed = False

    field_map = {
        'name': 'full_name',
        'weight_kg': 'weight_kg',
        'age': 'age',
        'max_hr': 'max_hr',
    }

    for athlete_key, profile_key in field_map.items():
        if personal.get(athlete_key) is None and garmin_profile.get(profile_key) is not None:
            personal[athlete_key] = garmin_profile[profile_key]
            changed = True

    # Always sync HR zones from Garmin (these are device-configured, not manually set)
    garmin_zones = garmin_profile.get('hr_zones')
    if garmin_zones:
        zone_data = {k: v for k, v in garmin_zones.items() if k.startswith('z')}
        if zone_data != personal.get('hr_zones'):
            personal['hr_zones'] = zone_data
            # Also sync max_hr and resting_hr from zone data if available
            if garmin_zones.get('max_hr'):
                personal['max_hr'] = garmin_zones['max_hr']
            changed = True

    if changed:
        athlete['personal'] = personal
        save_json_file(ATHLETE_FILE, athlete)
        logger.info("Auto-populated athlete.json from Garmin profile: %s",
                     [k for k, v in field_map.items() if personal.get(k) is not None])


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
        today = date.today()
        six_months_ago = today - timedelta(days=PROFILE_HISTORY_DAYS)

        # Pull 6 months of activities
        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                six_months_ago.isoformat(),
                today.isoformat()
            )
        )
        activities = parse_activities(raw_activities)

        # Pull personal records
        pr_data = garmin_api_call(lambda c: c.get_personal_record())
        personal_records = parse_personal_records(pr_data)

        # Calculate baseline from activities
        baseline = calculate_baseline(activities)

        # Pull user profile data (name, birth date, weight)
        garmin_profile = {}
        try:
            full_name = garmin_api_call(lambda c: c.get_full_name())
            user_profile = garmin_api_call(lambda c: c.get_user_profile())
            thirty_days_ago = (today - timedelta(days=30)).isoformat()
            body_comp = garmin_api_call(lambda c: c.get_body_composition(thirty_days_ago, today.isoformat()))
            garmin_profile = parse_user_profile(full_name, user_profile, body_comp)
        except Exception:
            logger.warning("Failed to pull Garmin profile data", exc_info=True)

        # Pull HR zones from Garmin biometric service
        garmin_hr_zones = None
        try:
            hr_zones_data = garmin_api_call(
                lambda c: c.garth.connectapi('/biometric-service/heartRateZones')
            )
            garmin_hr_zones = parse_hr_zones(hr_zones_data)
            if garmin_hr_zones:
                garmin_profile['hr_zones'] = garmin_hr_zones
        except Exception:
            logger.warning("Failed to pull Garmin HR zones", exc_info=True)

        # Build the baseline profile (Garmin-derived only)
        profile = {
            'last_refreshed': today.isoformat(),
            'baseline': baseline,
            'personal_records': personal_records,
            'garmin_profile': garmin_profile,
        }

        # Ensure data directory exists
        DATA_DIR.mkdir(exist_ok=True)

        # Save to athlete_baseline.json
        profile_path = DATA_DIR / ATHLETE_BASELINE_FILE
        with open(profile_path, 'w') as f:
            json.dump(profile, f, indent=2)

        # Auto-populate athlete.json personal section from Garmin profile
        # Only fills None fields — never overwrites manually set values
        if garmin_profile:
            _auto_populate_athlete(garmin_profile)

        # Return summary
        summary = {
            'status': 'success',
            'last_refreshed': profile['last_refreshed'],
            'activities_analyzed': baseline['total_activities'],
            'weeks_analyzed': baseline['weeks_analyzed'],
            'avg_weekly_volume_hrs': baseline['avg_weekly_volume_hrs'],
            'personal_records_count': len(personal_records),
            'garmin_profile': garmin_profile,
            'profile_path': str(profile_path)
        }

        return json.dumps(summary, indent=2)

    except Exception as e:
        logger.exception("refresh_athlete_baseline failed")
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
        if for_date is None:
            for_date = date.today().isoformat()

        readiness_data = garmin_api_call(lambda c: c.get_training_readiness(for_date))
        parsed = parse_training_readiness(readiness_data)

        return json.dumps(parsed, indent=2)

    except Exception as e:
        logger.exception("get_training_readiness failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_load_status() -> str:
    """
    Quick check: is the athlete ready to train today?

    Returns readiness score/level, acute load, load trend, and ACWR-like
    ratio from recent vs prior week volume. Includes warnings for elevated
    load ratio (> 1.3 = injury risk), prolonged recovery time, or poor HRV.

    ACWR zones: < 0.8 undertrained, 0.8-1.3 sweet spot, > 1.3 elevated risk.
    Check overall ACWR first (total body gate), then sport-specific if needed.

    Use get_coaching_snapshot() for full context including plan comparison,
    fitness metrics, and coaching memory.
    """
    try:
        today = date.today()

        # Get today's training readiness
        readiness_data = garmin_api_call(lambda c: c.get_training_readiness(today.isoformat()))
        readiness = parse_training_readiness(readiness_data)

        # Get recent activities for load trend
        week_ago = (today - timedelta(days=7)).isoformat()
        two_weeks_ago = (today - timedelta(days=14)).isoformat()

        recent_activities = garmin_api_call(lambda c: c.get_activities_by_date(week_ago, today.isoformat()))
        prior_activities = garmin_api_call(lambda c: c.get_activities_by_date(two_weeks_ago, week_ago))

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
        logger.exception("get_load_status failed")
        return json.dumps({"error": str(e)})


@mcp.tool()
def get_fitness_status(days: int = 90) -> str:
    """
    Detailed fitness analysis: CTL, ATL, TSB, ACWR — overall and per-sport.

    CTL (fitness, 42d average), ATL (fatigue, 7d average), TSB (form = CTL - ATL),
    ACWR (injury risk). Positive TSB = fresh, negative = fatigued.

    Includes per-sport breakdown (cycling, running, strength) to catch sport-specific
    spikes. An athlete with zero running CTL attempting a run has dangerous running
    ACWR even if overall ACWR is safe.

    Args:
        days: Number of days for trend analysis (default 90)

    Returns:
        JSON with overall + per-sport metrics, trend direction, data quality,
        and contextual insights.
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

        # Calculate current overall metrics (extract flat total loads for v2)
        total_loads = _extract_total_loads(daily_loads)
        metrics = calculate_fitness_metrics(total_loads)

        # Calculate per-sport metrics
        by_sport = {}
        for sport in ['cycling', 'running', 'strength']:
            sport_metrics = calculate_sport_fitness_metrics(daily_loads, sport)
            if sport_metrics.get('days_with_data', 0) > 0:
                by_sport[sport] = {
                    'ctl': sport_metrics['ctl'],
                    'atl': sport_metrics['atl'],
                    'tsb': sport_metrics['tsb'],
                    'acwr': sport_metrics['acwr'],
                    'acwr_status': sport_metrics['acwr_status'],
                }

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

        # Sport-specific insights
        for sport, sm in by_sport.items():
            if sm['acwr_status'] == 'danger':
                insights.append(f"{sport.capitalize()} ACWR danger ({sm['acwr']}) - reduce {sport} load")
            elif sm['ctl'] == 0 and sm['atl'] == 0:
                insights.append(f"No {sport} load recorded - return-to-{sport} protocol needed if resuming")

        # Trend insights
        if trend['trend'] == 'building':
            insights.append(f"Fitness building (+{trend['ctl_change']} over {trend['period_days']} days)")
        elif trend['trend'] == 'declining':
            insights.append(f"Fitness declining ({trend['ctl_change']} over {trend['period_days']} days)")
            recommendations.append("Consider if this is intentional (taper) or concerning")

        return json.dumps({
            'metrics': {
                'overall': {
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
                'by_sport': by_sport,
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
        logger.exception("get_fitness_status failed")
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
        today = date.today()
        start = (today - timedelta(days=days)).isoformat()

        # Fetch activities from Garmin
        raw_activities = garmin_api_call(lambda c: c.get_activities_by_date(start, today.isoformat()))

        if not raw_activities:
            return json.dumps({
                'status': 'no_activities',
                'message': f'No activities found in last {days} days',
            })

        # Parse activities
        activities = parse_activities(raw_activities)

        # Update fitness history (v2 sport-aware format)
        history = update_fitness_history(activities)

        # Calculate current metrics from total loads
        total_loads = _extract_total_loads(history.get('daily_loads', {}))
        metrics = calculate_fitness_metrics(total_loads)

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
        logger.exception("refresh_fitness_history failed")
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
        today = date.today()
        start = (today - timedelta(days=days)).isoformat()

        # Fetch activities
        raw_activities = garmin_api_call(lambda c: c.get_activities_by_date(start, today.isoformat()))

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
        logger.exception("get_intensity_distribution failed")
        return json.dumps({'error': str(e)})


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
        logger.exception("get_onboarding_guide failed")
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
        logger.exception("get_athlete failed")
        return json.dumps({'error': str(e)})
