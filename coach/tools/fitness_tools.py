"""Fitness, baseline, and athlete data tools.

Registers MCP tools for:
- refresh_athlete_baseline
- query_metrics (consolidated drill-down: fitness / intensity / daily /
  readiness / personal_records — replaces the old get_fitness_status,
  get_intensity_distribution, get_daily_metrics, get_training_readiness,
  and get_personal_records tools; bodies moved to private impls)
- refresh_fitness_history
- backfill_history (repair holes in the training diary — dry-run first)
- get_onboarding_guide
- get_athlete
"""

from typing import Literal

from fastmcp import Context
from ..mcp_app import mcp
from ..garmin_client import garmin_api_call, fetch_activity_hr_zones
from .data_tools import _daily_metrics, _personal_records
from ..parsers import parse_activities, parse_training_readiness, parse_personal_records, calculate_baseline, parse_user_profile, parse_hr_zones
from ..planner import load_athlete, load_methodology, load_json_file, save_json_file
from ..fitness import (load_fitness_history, calculate_fitness_metrics, calculate_intensity_distribution,
                     get_athlete_hr_zones, get_fitness_trend,
                     update_fitness_history, _extract_total_loads, calculate_sport_fitness_metrics,
                     backfill_fitness_history)
from ..config import DATA_DIR, PROFILE_HISTORY_DAYS, ATHLETE_BASELINE_FILE, ATHLETE_FILE
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


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': True})
async def refresh_athlete_baseline(ctx: Context) -> str:
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

        await ctx.report_progress(0, 5, "Fetching activity history")

        # Pull 6 months of activities
        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                six_months_ago.isoformat(),
                today.isoformat()
            )
        )
        activities = parse_activities(raw_activities)

        await ctx.report_progress(1, 5, "Fetching personal records")

        # Pull personal records
        pr_data = garmin_api_call(lambda c: c.get_personal_record())
        personal_records = parse_personal_records(pr_data)

        # Calculate baseline from activities
        baseline = calculate_baseline(activities)

        await ctx.report_progress(2, 5, "Fetching Garmin profile")

        # Pull user profile data (name, birth date, weight)
        garmin_profile = {}
        try:
            full_name = garmin_api_call(lambda c: c.get_full_name())
            user_profile = garmin_api_call(lambda c: c.get_user_profile())
            thirty_days_ago = (today - timedelta(days=30)).isoformat()
            body_comp = garmin_api_call(lambda c: c.get_body_composition(thirty_days_ago, today.isoformat()))
            garmin_profile = parse_user_profile(full_name, user_profile, body_comp,
                                                today=today)
        except Exception:
            logger.warning("Failed to pull Garmin profile data", exc_info=True)

        # Pull HR zones from Garmin biometric service
        garmin_hr_zones = None
        try:
            hr_zones_data = garmin_api_call(
                lambda c: c.client.connectapi('/biometric-service/heartRateZones')
            )
            garmin_hr_zones = parse_hr_zones(hr_zones_data)
            if garmin_hr_zones:
                garmin_profile['hr_zones'] = garmin_hr_zones
        except Exception:
            logger.warning("Failed to pull Garmin HR zones", exc_info=True)

        await ctx.report_progress(3, 5, "Building baseline profile")

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

        await ctx.report_progress(5, 5, "Baseline complete")

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


def _training_readiness(for_date: str) -> dict:
    """Fetch training readiness score and recovery metrics from Garmin.

    Always fetches the dedicated HRV endpoint too — Garmin's training readiness
    often returns null for hrv_status even when the device tracks HRV. The HRV
    endpoint fills in last_night_avg, weekly_avg, baseline range, and feedback.

    for_date is required — the query_metrics boundary resolves the default
    (clock discipline).
    """
    try:
        readiness_data = garmin_api_call(lambda c: c.get_training_readiness(for_date))
        try:
            hrv_data = garmin_api_call(lambda c: c.get_hrv_data(for_date))
        except Exception:
            logger.info("HRV data unavailable for %s", for_date, exc_info=True)
            hrv_data = None

        parsed = parse_training_readiness(readiness_data, hrv_data=hrv_data)

        return parsed

    except Exception as e:
        logger.exception("query_metrics(kind='readiness') failed")
        return {"error": str(e)}


def _fitness_status(days: int = 90, *, today: date) -> dict:
    """Detailed fitness analysis: CTL, ATL, TSB, ACWR — overall and per-sport.

    CTL (fitness, 42d average), ATL (fatigue, 7d average), TSB (form = CTL - ATL),
    ACWR (injury risk). Positive TSB = fresh, negative = fatigued.

    Includes per-sport breakdown (cycling, running, strength) to catch
    sport-specific spikes.
    """
    try:
        # Load fitness history
        history = load_fitness_history()
        daily_loads = history.get('daily_loads', {})

        if not daily_loads:
            return {
                'status': 'no_data',
                'message': 'No fitness history. Run refresh_fitness_history() to backfill from Garmin.',
                'action': 'Call refresh_fitness_history() first',
            }

        # Calculate current overall metrics (extract flat total loads for v2)
        total_loads = _extract_total_loads(daily_loads)
        metrics = calculate_fitness_metrics(total_loads, today)

        # Calculate per-sport metrics
        by_sport = {}
        for sport in ['cycling', 'running', 'strength']:
            sport_metrics = calculate_sport_fitness_metrics(daily_loads, sport, today)
            if sport_metrics.get('days_with_data', 0) > 0:
                by_sport[sport] = {
                    'ctl': sport_metrics['ctl'],
                    'atl': sport_metrics['atl'],
                    'tsb': sport_metrics['tsb'],
                    'acwr': sport_metrics['acwr'],
                    'acwr_status': sport_metrics['acwr_status'],
                }

        # Get trend
        trend = get_fitness_trend(days, today=today)

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

        return {
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
                    'acwr_label': 'Acute:Chronic Workload Ratio (rolling 7d:28d)',
                    # Legacy EWMA model — reference only since the
                    # 2026-06-10 cutover ({value, zone, safe, note}).
                    'acwr_ewma': metrics.get('acwr_ewma'),
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
        }

    except Exception as e:
        logger.exception("query_metrics(kind='fitness') failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': True})
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
        history = update_fitness_history(activities, today)

        # Calculate current metrics from total loads
        total_loads = _extract_total_loads(history.get('daily_loads', {}))
        metrics = calculate_fitness_metrics(total_loads, today)

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
            'note': "Fitness history updated. Use query_metrics(kind='fitness') for detailed analysis.",
        }, indent=2)

    except Exception as e:
        logger.exception("refresh_fitness_history failed")
        return json.dumps({'error': str(e)})


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': True})
def backfill_history(since: str, until: str | None = None,
                     dry_run: bool = True, skip_garmin: bool = False) -> dict:
    """
    Repair holes in the training diary (fitness_history.json).

    Use when history has gaps — e.g. after a period with no coaching
    contact or a dead morning audit (the snapshot's data_quality staleness
    flags are the usual tell). Three repairs in one pass, ADD-ONLY
    (existing entries are never replaced, so re-running is safe):

    1. Missing daily CTL/ACWR snapshot entries — recomputed locally from
       stored daily_loads (run refresh_fitness_history first if the loads
       themselves are missing).
    2. Missing sleep nights — re-fetched per-date from Garmin.
    3. Missing readiness days — re-fetched per-date from Garmin (with HRV
       overlay).

    ALWAYS run the default dry_run=True first and show the athlete what is
    missing; only call again with dry_run=False after they confirm. Apply
    mode backs fitness_history.json up to data-backups/ before writing and
    throttles Garmin calls (a large range takes a few minutes).

    Args:
        since: Range start, 'YYYY-MM-DD' (take it from the conversation —
            e.g. the start of the known gap). Required.
        until: Range end, 'YYYY-MM-DD' (default: today; future is clamped).
        dry_run: True (default) reports what is missing without writing.
        skip_garmin: True rebuilds only the local snapshot entries — no
            sleep/readiness fetches.

    Returns:
        Dict report: missing counts + compact date ranges; in apply mode
        also backup path, added/unavailable counts, and new totals.
    """
    try:
        today = date.today()
        try:
            since_d = date.fromisoformat(since)
            until_d = date.fromisoformat(until) if until else today
        except ValueError as e:
            return {'error': f'Invalid date ({e}) — use YYYY-MM-DD'}
        until_d = min(until_d, today)
        if since_d > until_d:
            return {'error': f'since ({since_d}) is after until ({until_d})'}
        if (until_d - since_d).days > 366:
            return {'error': 'Range too large — backfill at most 366 days per '
                             'call (chunk longer ranges)'}
        return backfill_fitness_history(
            since_d, until_d, today=today,
            apply=not dry_run, skip_garmin=skip_garmin)
    except Exception as e:
        logger.exception("backfill_history failed")
        return {'error': str(e)}


def _intensity_distribution(days: int = 28, *, today: date) -> dict:
    """Analyze training intensity distribution over a period.

    Checks compliance with the Norwegian 80/20 polarized model:
    - 80% low intensity (Zone 1-2: easy/aerobic)
    - 15% moderate intensity (Zone 3: tempo)
    - 5% high intensity (Zone 4-5: threshold/VO2max)

    today is required — the query_metrics boundary resolves it
    (clock discipline).
    """
    try:
        start = (today - timedelta(days=days)).isoformat()

        # Fetch activities
        raw_activities = garmin_api_call(lambda c: c.get_activities_by_date(start, today.isoformat()))

        if not raw_activities:
            return {
                'status': 'no_activities',
                'message': f'No activities found in last {days} days',
                'period': f'{start} to {today.isoformat()}',
            }

        # Parse activities and enrich with per-activity HR zone data
        activities = parse_activities(raw_activities)
        activities = fetch_activity_hr_zones(activities)

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

        return distribution

    except Exception as e:
        logger.exception("query_metrics(kind='intensity') failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
def query_metrics(kind: Literal['fitness', 'intensity', 'daily', 'readiness',
                                'personal_records'],
                  days: int = 30,
                  for_date: str | None = None) -> dict:
    """
    Read-only metrics drill-down — one tool for the metric queries that used
    to be separate tools (get_fitness_status, get_intensity_distribution,
    get_daily_metrics, get_training_readiness, get_personal_records).

    The coaching snapshot already carries the CURRENT ACWR/recovery/sleep
    signals — use this tool for drill-downs: custom windows, historical
    dates, or the full PR list.

    When to use each kind:
    - 'fitness': CTL/ATL/TSB/ACWR, overall + per-sport (cycling, running,
      strength), from local fitness history (no Garmin call). `days` sets
      the trend window (the old standalone defaulted to 90). Use for
      custom-window ACWR checks and sport-specific spike detection.
    - 'intensity': HR-zone distribution vs the 80/20 polarized model over
      `days` (28 = monthly view). Fetches activities from Garmin. Use for
      zone analysis and gray-zone detection.
    - 'daily': today's recovery basics from Garmin — RHR, body battery,
      sleep score. `days`/`for_date` are ignored (today only).
    - 'readiness': Garmin training readiness + HRV overlay for `for_date`
      (YYYY-MM-DD, default today). Use for historical readiness lookups —
      score, level, sleep_score, hrv_last_night_avg, hrv_baseline_low/high.
    - 'personal_records': all PBs from Garmin with dates and activity IDs.

    Args:
        kind: Which metric family to query (see above).
        days: Analysis window for kind='fitness' / 'intensity' (default 30).
        for_date: Date for kind='readiness' (defaults to today).

    Returns:
        A dict per kind; {'error': ...} on failure.
    """
    try:
        today = date.today()  # tool boundary — threaded into the helpers
        if kind == 'fitness':
            return _fitness_status(days, today=today)
        if kind == 'intensity':
            return _intensity_distribution(days, today=today)
        if kind == 'daily':
            return _daily_metrics(today)
        if kind == 'readiness':
            return _training_readiness(for_date or today.isoformat())
        if kind == 'personal_records':
            return _personal_records()

        return {'error': f"Unknown kind '{kind}'. Valid kinds: fitness, "
                         "intensity, daily, readiness, personal_records"}

    except Exception as e:
        logger.exception("query_metrics(kind=%s) failed", kind)
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
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


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
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
