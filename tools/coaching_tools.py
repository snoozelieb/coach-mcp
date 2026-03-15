"""Coaching analysis and snapshot tools.

Registers MCP tools for:
- get_compliance_report
- get_coaching_score
- get_coaching_snapshot

Also contains helper functions:
- _compare_planned_actual
- _get_strength_sync_summary
- _parse_readiness_for_snapshot
- _build_adaptation_patterns
- _derive_sleep_trend_direction
- _derive_hrv_trend
- _derive_compliance_rate_pct
- _analyze_sport_priorities
"""

from collections import defaultdict
from mcp_app import mcp
from garmin_client import garmin_api_call, fetch_activity_hr_zones
from parsers import parse_activities
from planner import (
    get_current_plan,
    load_athlete,
    load_methodology,
    load_coaching_log,
    get_coaching_context,
)
from rules import (
    check_weekly_compliance,
    check_safety_rules,
    get_upcoming_events,
)
from fitness import (
    load_fitness_history,
    save_fitness_history,
    calculate_fitness_metrics,
    calculate_intensity_distribution,
    get_athlete_hr_zones,
    get_sleep_summary,
    calculate_ctl_target,
    _extract_total_loads,
    calculate_sport_fitness_metrics,
    get_fitness_trend,
    get_sleep_trend,
    persist_sleep_data,
    persist_readiness_data,
    calculate_readiness_baselines,
    analyze_activity_patterns,
    update_fitness_history,
)
from config import (
    DATA_DIR,
    TRAINING_CONFIG_FILE,
    ATHLETE_FILE,
    CTL_TARGETS,
    DEFAULT_EQUIVALENCE_GROUPS,
    RACE_TIME_WEIGHTS,
    RACE_TIME_WEIGHT_DEFAULT,
    RACE_TYPE_SPORT_MAP,
    get_sport_group,
)
from datetime import date, timedelta
import json
import logging

logger = logging.getLogger(__name__)


# Import shared helper from strength_tools
from tools.strength_tools import _get_strength_baseline_data


# ---------------------------------------------------------------------------
# Helper functions (used by MCP tools below)
# ---------------------------------------------------------------------------

def _compare_planned_actual(plan: dict, activities: list, today: date,
                            daily_loads: dict = None, sleep_history: list = None) -> dict:
    """Compare planned sessions against actual activities.

    Surfaces anomalies for LLM reasoning instead of drawing conclusions.
    Status values: matched, partial, missing, unplanned, type_mismatch, pending.

    When daily_loads and sleep_history are provided, anomalies are enriched
    with surrounding context (sleep, prior day load) so the LLM can reason
    about WHY an anomaly occurred.
    """
    if not plan or not plan.get('days'):
        return {'status': 'no_plan', 'note': 'No weekly plan to compare against'}

    comparison = {
        'sessions_planned': 0,
        'sessions_completed': 0,
        'sessions_missed': 0,
        'sessions_pending': 0,
        'anomalies': [],
        'details': []
    }

    # Build set of planned dates to detect unplanned activities later
    planned_dates = set()

    for day_str, day_data in plan.get('days', {}).items():
        try:
            day_date = date.fromisoformat(day_str)
        except ValueError:
            continue

        planned = day_data.get('planned', {})
        is_rest_day = not planned or 'rest' in planned.get('type', '').lower()

        if is_rest_day:
            # Check for unplanned activity on rest day
            day_activities = [a for a in activities if a.get('date') == day_str]
            if day_activities and day_date <= today:
                for act in day_activities:
                    comparison['anomalies'].append({
                        'date': day_str,
                        'flag': 'unplanned',
                        'activity_type': act.get('type'),
                        'duration_mins': act.get('duration_mins', 0),
                    })
                    comparison['details'].append({
                        'date': day_str,
                        'status': 'unplanned',
                        'actual': act.get('type'),
                        'duration_actual': act.get('duration_mins', 0),
                    })
            continue

        planned_dates.add(day_str)
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

        # Find matching activities (all activities for this day)
        day_activities = [a for a in activities if a.get('date') == day_str]

        if day_activities:
            comparison['sessions_completed'] += 1
            planned_type = planned.get('type', '')
            planned_duration = planned.get('duration_mins', 0)

            # Find best-matching activity (prefer type match, then first)
            best_match = day_activities[0]
            for act in day_activities:
                if act.get('type', '').lower() == planned_type.lower():
                    best_match = act
                    break

            actual_type = best_match.get('type', 'unknown')
            actual_duration = best_match.get('duration_mins', 0)

            # Determine status and detect anomalies for best match
            detail = {
                'date': day_str,
                'planned_type': planned_type,
                'actual_type': actual_type,
                'duration_planned': planned_duration,
                'duration_actual': actual_duration,
            }

            # Type mismatch detection (e.g., planned=race, actual=cycling)
            if planned_type and actual_type and planned_type.lower() != actual_type.lower():
                detail['status'] = 'type_mismatch'
                comparison['anomalies'].append({
                    'date': day_str,
                    'flag': 'type_mismatch',
                    'planned_type': planned_type,
                    'actual_type': actual_type,
                })
            else:
                detail['status'] = 'matched'

            # Duration delta (always include when both values present)
            if planned_duration and actual_duration:
                delta_pct = round(
                    (actual_duration - planned_duration) / planned_duration * 100, 1
                )
                detail['duration_delta_pct'] = delta_pct

                if delta_pct < -30:
                    detail['status'] = 'partial' if detail['status'] == 'matched' else detail['status']
                    comparison['anomalies'].append({
                        'date': day_str,
                        'flag': 'duration_delta',
                        'planned_mins': planned_duration,
                        'actual_mins': actual_duration,
                        'delta_pct': delta_pct,
                    })
                elif delta_pct > 30:
                    comparison['anomalies'].append({
                        'date': day_str,
                        'flag': 'duration_delta',
                        'planned_mins': planned_duration,
                        'actual_mins': actual_duration,
                        'delta_pct': delta_pct,
                    })

            # Include all activities for the day (not just best match)
            if len(day_activities) > 1:
                detail['all_activities'] = [
                    {'type': a.get('type', 'unknown'), 'duration_mins': a.get('duration_mins', 0)}
                    for a in day_activities
                ]

            # Trim matched entries to minimal form
            if detail['status'] == 'matched' and not detail.get('all_activities'):
                comparison['details'].append({
                    'date': day_str,
                    'status': 'matched',
                })
            else:
                comparison['details'].append(detail)
        else:
            comparison['sessions_missed'] += 1
            comparison['anomalies'].append({
                'date': day_str,
                'flag': 'missing',
                'planned_type': planned.get('type'),
                'planned_mins': planned.get('duration_mins', 0),
            })
            comparison['details'].append({
                'date': day_str,
                'status': 'missing',
                'planned_type': planned.get('type'),
            })

    comparison['completion_rate'] = (
        round(comparison['sessions_completed'] / comparison['sessions_planned'] * 100, 1)
        if comparison['sessions_planned'] > 0 else None
    )

    # Enrich anomalies with surrounding context when data is available
    if (daily_loads or sleep_history) and comparison['anomalies']:
        from fitness import get_day_context
        for anomaly in comparison['anomalies']:
            ctx = get_day_context(
                anomaly['date'],
                daily_loads or {},
                sleep_history or [],
            )
            if ctx:
                anomaly['context'] = ctx

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


def _parse_readiness_for_snapshot(readiness_data: dict) -> dict:
    """Parse readiness data for snapshot. Returns structured data, no prescriptions."""
    if not readiness_data:
        return {'status': 'unavailable'}

    return {
        'score': readiness_data.get('score'),
        'level': readiness_data.get('level'),
        'hrv_status': readiness_data.get('hrvStatus'),
        'sleep_score': readiness_data.get('sleepScore'),
        'recovery_time_mins': readiness_data.get('recoveryTime'),
    }


def _build_adaptation_patterns() -> dict:
    """
    Build adaptation patterns from coaching log for LLM personalization.

    These patterns help the LLM decide where in the load_increase_guidance
    range to operate (conservative/standard/aggressive).

    Returns boolean flags (backward compat) plus quantified thresholds
    when enough numeric response data is available.
    """
    from fitness import derive_adaptation_thresholds

    try:
        log = load_coaching_log()
        responses = log.get('athlete_responses', [])

        # Extract key patterns (boolean flags — backward compat)
        patterns = {}
        for r in responses:
            pattern = r.get('pattern')
            if pattern:
                patterns[pattern] = patterns.get(pattern, 0) + 1

        result = {
            'handles_volume_well': patterns.get('handles_volume_well', 0) > patterns.get('struggles_with_volume', 0),
            'recovers_quickly': patterns.get('recovers_quickly', 0) > patterns.get('slow_recovery', 0),
            'needs_extra_rest_after_intensity': patterns.get('needs_recovery_after_intensity', 0) > 0,
            'patterns_logged': len(patterns),
            'total_responses': len(responses),
        }

        # Quantified adaptation thresholds (when numeric data available)
        thresholds = derive_adaptation_thresholds(responses)
        if thresholds.get('status') == 'quantified':
            result['quantified'] = {
                k: v for k, v in thresholds.items()
                if k != 'status'
            }

        return result
    except Exception:
        return {
            'handles_volume_well': None,  # Unknown - no data
            'recovers_quickly': None,
            'needs_extra_rest_after_intensity': None,
            'patterns_logged': 0,
            'total_responses': 0,
        }


def _derive_sleep_trend_direction(sleep_data: dict) -> str:
    """Derive sleep trend direction from recent_trend float."""
    if not sleep_data:
        return 'stable'
    recent_trend = sleep_data.get('recent_trend', 0)
    if recent_trend and recent_trend > 0.3:
        return 'improving'
    elif recent_trend and recent_trend < -0.3:
        return 'declining'
    return 'stable'


def _derive_hrv_trend(recovery: dict) -> str:
    """Derive HRV trend from recovery hrv_status level."""
    if not recovery:
        return 'unknown'
    hrv_level = recovery.get('hrv_status', '')
    if hrv_level in ['BALANCED', 'GOOD']:
        return 'stable'
    elif hrv_level in ['LOW', 'POOR']:
        return 'declining'
    return 'unknown'


def _derive_compliance_rate_pct(compliance: dict) -> float | None:
    """Calculate compliance rate from pillar counts."""
    pillars_total = 0
    pillars_met = 0
    for pillar in ['strength', 'mobility', 'long_effort']:
        if pillar in compliance:
            pillars_total += 1
            if compliance[pillar].get('compliant', False):
                pillars_met += 1
    return round(pillars_met / pillars_total * 100, 0) if pillars_total > 0 else None


def _build_compliance_diagnostics(weekly_activities_4wk: list[list], pillars: dict) -> dict:
    """Per-pillar compliance over 4 weeks from activity data.

    Identifies chronically missed pillars so the LLM can address patterns
    rather than one-off misses.

    Args:
        weekly_activities_4wk: List of 4 lists, each containing parsed activities for one week
        pillars: Athlete's training_pillars dict (name → config)

    Returns:
        Dict with per_pillar compliance and lowest_compliance_pillar.
    """
    if not pillars or not weekly_activities_4wk:
        return {'status': 'no_data'}

    per_pillar = {}
    total_weeks = len(weekly_activities_4wk)

    for pillar_name, pillar_config in pillars.items():
        target_type = pillar_config.get('target_type', 'sessions')
        pillar_types = [t.lower() for t in pillar_config.get('types', [])]
        met_weeks = 0

        for week_activities in weekly_activities_4wk:
            # Count matching activities
            matching = [
                a for a in week_activities
                if a.get('type', '').lower() in pillar_types
            ]

            if target_type == 'sessions':
                target = pillar_config.get('target_sessions_per_week', 0)
                if len(matching) >= target and target > 0:
                    met_weeks += 1
            elif target_type == 'hours':
                target_mins = pillar_config.get('target_hours_per_week', 0) * 60
                total_mins = sum(a.get('duration_mins', 0) or 0 for a in matching)
                if total_mins >= target_mins and target_mins > 0:
                    met_weeks += 1
            elif target_type == 'minutes':
                target_mins = pillar_config.get('target_minutes_per_week', 0)
                total_mins = sum(a.get('duration_mins', 0) or 0 for a in matching)
                if total_mins >= target_mins and target_mins > 0:
                    met_weeks += 1

        per_pillar[pillar_name] = {
            'met_weeks': met_weeks,
            'total_weeks': total_weeks,
            'chronic_miss': met_weeks <= total_weeks // 2,  # Missed more than half
        }

    # Find lowest compliance pillar
    lowest = None
    lowest_rate = 1.0
    for name, data in per_pillar.items():
        rate = data['met_weeks'] / data['total_weeks'] if data['total_weeks'] > 0 else 0
        if rate < lowest_rate:
            lowest_rate = rate
            lowest = name

    return {
        'per_pillar': per_pillar,
        'lowest_compliance_pillar': lowest,
    }


def _build_snapshot_flags(snapshot: dict) -> dict:
    """Build a summary flags dict for quick scanning of snapshot state.

    Returns counts and booleans only — no ranking or prioritization
    (that's the LLM's job).
    """
    flags = {}

    # ACWR warning
    acwr_warnings = snapshot.get('acwr_warnings', [])
    if acwr_warnings:
        flags['acwr_warning'] = True

    # Active injuries
    injuries = snapshot.get('injuries', [])
    if injuries:
        flags['active_injuries'] = len(injuries)

    # Anomaly count
    pva = snapshot.get('planned_vs_actual', {})
    anomalies = pva.get('anomalies', [])
    if anomalies:
        flags['anomaly_count'] = len(anomalies)

    # Sleep deficit
    sleep = snapshot.get('sleep', {})
    if sleep.get('deficit_flag') or sleep.get('trend_direction') == 'declining':
        flags['sleep_deficit'] = True

    # Pending approvals
    memory = snapshot.get('coaching_memory', {})
    pending = memory.get('pending_approvals', [])
    if pending:
        flags['pending_approvals'] = len(pending)

    # Decisions due for review (active decisions older than 7 days)
    active = memory.get('active_decisions', [])
    review_due = 0
    today = date.today()
    for d in active:
        d_date = d.get('date', '')
        try:
            if d_date and (today - date.fromisoformat(d_date)).days > 7:
                review_due += 1
        except (ValueError, TypeError):
            pass
    if review_due:
        flags['decisions_due_for_review'] = review_due

    # Compliance below 70%
    compliance = snapshot.get('compliance', {})
    rate = compliance.get('compliance_rate_pct')
    if rate is not None and rate < 70:
        flags['compliance_below_70'] = True

    return flags


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

        # Time-based weight (closer race = higher weight)
        time_weight = RACE_TIME_WEIGHT_DEFAULT
        for max_days, weight in RACE_TIME_WEIGHTS:
            if days_until <= max_days:
                time_weight = weight
                break

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
        'has_multi_sport': len(sports_analysis) > 1,
    }


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def get_compliance_report(days: int = 7) -> str:
    """
    Check whether the athlete is meeting their training pillars.

    Returns compliance status for each pillar (strength, mobility, long effort)
    plus safety warnings (consecutive hard days, rest after races). Low compliance
    may indicate the plan is too ambitious or life is getting in the way — the
    coach should investigate before adjusting.

    Use get_coaching_snapshot() for full context; use this for a focused pillar check.

    Args:
        days: Number of days to analyze (default 7 for weekly report)

    Returns:
        JSON with compliance status per pillar, deficits, safety warnings, and
        upcoming events.
    """
    try:
        today = date.today()
        start_date = today - timedelta(days=days)

        # Get activities for the period
        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                start_date.isoformat(),
                today.isoformat()
            )
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
        logger.exception("get_compliance_report failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_coaching_score() -> str:
    """
    Self-assessment: is the coaching working?

    Scores coaching effectiveness across 4 dimensions:
    - Progress (40%): CTL trajectory toward A-race goal
    - Health (30%): Injury status and ACWR safety
    - Achievability (20%): Compliance rate — is the plan realistic?
    - Adaptation (10%): Are athlete response patterns being logged?

    Use periodically (weekly or after plan changes) to catch problems early.
    A declining score means something needs to change — investigate the weakest
    component.

    Returns:
        JSON with overall score, component breakdown, trend, and feedback.
    """
    try:
        today = date.today()

        # Get fitness data for progress calculation
        history = load_fitness_history()
        daily_loads = history.get('daily_loads', {})
        total_loads = _extract_total_loads(daily_loads) if daily_loads else {}
        fitness_data = calculate_fitness_metrics(total_loads) if total_loads else {}
        current_ctl = fitness_data.get('ctl', 0) if fitness_data else 0

        # Get 4-week CTL trend from snapshots (handle v1 and v2 formats)
        snapshots = history.get('snapshots', [])
        ctl_4wk_ago = None
        for snapshot in snapshots:
            snapshot_date = date.fromisoformat(snapshot['date'])
            if (today - snapshot_date).days >= 28:
                if 'total' in snapshot:
                    ctl_4wk_ago = snapshot['total'].get('ctl', 0)
                else:
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

            # Use sport-specific CTL for the A-race sport
            race_sport = RACE_TYPE_SPORT_MAP.get(race_type)
            if race_sport and daily_loads:
                sport_m = calculate_sport_fitness_metrics(daily_loads, race_sport)
                if sport_m.get('days_with_data', 0) > 0:
                    current_ctl = sport_m['ctl']
                    progress_data['current_ctl'] = round(current_ctl, 1)
                    progress_data['ctl_source'] = f'{race_sport}_specific'

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
        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                start_date.isoformat(),
                today.isoformat()
            )
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
        logger.exception("get_coaching_score failed")
        return json.dumps({'error': str(e)})


@mcp.tool()
def get_coaching_snapshot() -> str:
    """
    MANDATORY FIRST CALL before any coaching recommendation.

    Returns everything needed to coach this athlete right now:
    - Weekly plan vs actual activities (with anomalies to investigate)
    - Fitness metrics: CTL, ATL, TSB, ACWR (overall + per-sport)
    - Load hierarchy: overall ACWR safety, sport-specific spike detection
    - Compliance, recovery, sleep (with 30-day trend)
    - Adaptation signals for load personalization
    - Sport priority breakdown (multi-sport volume distribution)
    - Active injuries and restrictions
    - Coaching memory: active decisions, pending approvals, adaptation patterns

    The planned_vs_actual.anomalies array flags things that need attention:
    type mismatches, duration deltas, missing sessions, unplanned activities.
    Investigate anomalies with the athlete before drawing conclusions.

    Returns:
        JSON with complete coaching context. Always check this first.
    """
    try:
        today = date.today()

        # 0. Auto-refresh fitness history if stale
        history = load_fitness_history()
        last_updated = history.get('last_updated')
        if last_updated is None or last_updated < (today - timedelta(days=1)).isoformat():
            try:
                # Incremental refresh: only fetch since last_updated
                refresh_start = last_updated or (today - timedelta(days=90)).isoformat()
                raw_refresh = garmin_api_call(
                    lambda c: c.get_activities_by_date(refresh_start, today.isoformat())
                )
                if raw_refresh:
                    from parsers import parse_activities as _pa
                    refreshed_activities = _pa(raw_refresh)
                    history = update_fitness_history(refreshed_activities)
            except Exception:
                logger.warning("Fitness history auto-refresh failed", exc_info=True)

        daily_loads = history.get('daily_loads', {})

        # 1. Current Weekly Plan
        current_plan = get_current_plan()

        # 2. Activities this week (actual)
        # Calendar week always starts Monday (for compliance checking)
        monday_this_week = today - timedelta(days=today.weekday())

        # Plan may start on a different date (e.g. mid-week)
        if current_plan and current_plan.get('week_start'):
            plan_start = date.fromisoformat(current_plan['week_start'])
            # Fetch from whichever is earlier: plan start or this Monday
            # This ensures we have activities for both:
            # - Compliance checking (needs full calendar week)
            # - Planned vs actual (needs plan period)
            fetch_start = min(plan_start, monday_this_week)
        else:
            plan_start = None
            fetch_start = monday_this_week

        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                fetch_start.isoformat(),
                today.isoformat()
            )
        )
        all_fetched_activities = parse_activities(raw_activities)

        # Calendar week activities — for compliance, intensity distribution, etc.
        activities_this_week = [
            a for a in all_fetched_activities
            if a.get('date') and a['date'] >= monday_this_week.isoformat()
        ]

        # 3. Planned vs Actual comparison (uses full fetch range — the comparison
        # function filters by plan dates, so extra activities are harmless)
        sleep_history = history.get('sleep_history', [])
        planned_vs_actual = _compare_planned_actual(
            current_plan, all_fetched_activities, today,
            daily_loads=daily_loads, sleep_history=sleep_history,
        )

        # 4. Fitness metrics — overall + per-sport
        #
        # LOAD HIERARCHY (injury prevention order):
        # 1. OVERALL ACWR — total body stress gate. If overall ACWR > 1.3, back off
        #    everything regardless of sport-specific numbers.
        # 2. SPORT-SPECIFIC ACWR — catches sport-specific spikes. An athlete with
        #    zero running CTL attempting a run has infinite running ACWR even if
        #    overall ACWR is fine.
        # 3. SPORT-SPECIFIC CTL — race readiness. Cycling CTL tells you if you're
        #    ready for sani2c; overall CTL does not.
        #
        # The LLM must check ALL three levels before prescribing.
        if daily_loads:
            total_loads = _extract_total_loads(daily_loads)
            overall_metrics = calculate_fitness_metrics(total_loads)

            # Per-sport metrics
            sport_fitness = {}
            for sport in ['cycling', 'running', 'strength']:
                sm = calculate_sport_fitness_metrics(daily_loads, sport)
                if sm.get('days_with_data', 0) > 0:
                    sport_fitness[sport] = {
                        'ctl': sm['ctl'], 'atl': sm['atl'],
                        'tsb': sm['tsb'], 'acwr': sm['acwr'],
                    }

            # Structured ACWR status (zone + safety boolean, no prose)
            overall_acwr = overall_metrics.get('acwr', 0)
            overall_ctl = overall_metrics.get('ctl', 0)
            acwr_zone = overall_metrics.get('acwr_status', 'unknown')

            fitness_metrics = {
                'overall': {k: v for k, v in overall_metrics.items()},
                'acwr_status': {
                    'value': round(overall_acwr, 2),
                    'zone': acwr_zone,
                    'safe': acwr_zone in ('optimal', 'low'),
                },
                'by_sport': sport_fitness,
                'load_hierarchy': {
                    'overall_acwr_safe': acwr_zone in ('optimal', 'low'),
                    'sport_acwr_concerns': [
                        sp for sp, sm in sport_fitness.items()
                        if sm.get('acwr', 0) > 1.3 or (sm['ctl'] == 0 and sm['atl'] > 0)
                    ],
                },
            }
        else:
            overall_metrics = {}
            sport_fitness = {}
            fitness_metrics = {
                'status': 'no_data',
                'action': 'Run refresh_fitness_history() to backfill from Garmin'
            }

        # 4b. ACWR warnings — overall FIRST, then sport-specific
        acwr_warnings = []

        # Overall ACWR check (primary injury gate)
        if overall_metrics:
            o_acwr = overall_metrics.get('acwr', 0)
            o_status = overall_metrics.get('acwr_status', 'unknown')
            if o_status == 'danger':
                acwr_warnings.append({
                    'level': 'overall',
                    'sport': 'all',
                    'acwr': o_acwr,
                    'zone': 'danger',
                    'reason': 'overload',
                })
            elif o_status == 'elevated':
                acwr_warnings.append({
                    'level': 'overall',
                    'sport': 'all',
                    'acwr': o_acwr,
                    'zone': 'elevated',
                    'reason': 'overload',
                })

        # Sport-specific ACWR checks (spike detection)
        for sport, sm in sport_fitness.items():
            if sm['ctl'] == 0 and sm['atl'] == 0:
                acwr_warnings.append({
                    'level': 'sport',
                    'sport': sport,
                    'acwr': 0.0,
                    'zone': 'danger',
                    'reason': 'return_to_sport',
                })
            elif sm.get('acwr', 0) > 1.5:
                acwr_warnings.append({
                    'level': 'sport',
                    'sport': sport,
                    'acwr': sm['acwr'],
                    'zone': 'danger',
                    'reason': 'overload',
                })
            elif sm.get('acwr', 0) > 1.3:
                acwr_warnings.append({
                    'level': 'sport',
                    'sport': sport,
                    'acwr': sm['acwr'],
                    'zone': 'elevated',
                    'reason': 'overload',
                })

        # 5. Compliance status
        compliance = check_weekly_compliance(activities_this_week)

        # 5b. Compliance diagnostics (4-week pattern from daily_loads)
        # Load athlete early — needed here and in section 8
        athlete_path = DATA_DIR / ATHLETE_FILE
        if athlete_path.exists():
            with open(athlete_path) as f:
                athlete = json.load(f)
        else:
            athlete = {}

        compliance_diagnostics = None
        training_pillars = athlete.get('training_pillars')
        if training_pillars and daily_loads:
            weekly_activities_4wk = []
            for week_offset in range(4):  # 0=this week, 3=oldest
                w_start = today - timedelta(days=today.weekday() + week_offset * 7)
                w_end = w_start + timedelta(days=7)
                week_acts = []
                for day_data in daily_loads.values():
                    if not isinstance(day_data, dict):
                        continue
                    for act in day_data.get('activities', []):
                        act_date = act.get('date', '')
                        if w_start.isoformat() <= act_date < w_end.isoformat():
                            week_acts.append(act)
                weekly_activities_4wk.append(week_acts)
            compliance_diagnostics = _build_compliance_diagnostics(
                weekly_activities_4wk, training_pillars
            )

        # 6. Recovery status (today) + Sleep tracking
        try:
            readiness_data = garmin_api_call(lambda c: c.get_training_readiness(today.isoformat()))
            recovery = _parse_readiness_for_snapshot(readiness_data)
            # Persist readiness for baseline tracking
            if recovery and recovery.get('status') != 'unavailable':
                readiness_rec = {
                    'date': today.isoformat(),
                    'score': recovery.get('score'),
                    'level': recovery.get('level'),
                    'hrv_status': recovery.get('hrv_status'),
                    'body_battery': recovery.get('body_battery'),
                }
                history = persist_readiness_data(readiness_rec, history)
        except Exception:
            logger.warning("Failed to fetch recovery data", exc_info=True)
            recovery = {'status': 'unavailable', 'note': 'Could not fetch readiness data'}

        # 6b. Sleep data (last 7 days) + persist to history
        sleep_data = get_sleep_summary(today, days=7)
        if sleep_data and sleep_data.get('status') != 'no_data':
            # Persist sleep records to fitness_history for 30-day trend
            sleep_recs = sleep_data.get('recent', [])
            if sleep_recs:
                history = persist_sleep_data(sleep_recs, history)

        # Save history once (covers both readiness + sleep persistence)
        save_fitness_history(history)

        # 6c. Sleep trend (30-day from persisted data)
        sleep_trend_30d = get_sleep_trend(history, days=30)

        # 6d. Readiness baselines (personal norms)
        readiness_baselines = calculate_readiness_baselines(
            history.get('sleep_history', []),
            history.get('readiness_history', []),
        )

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
        if athlete:
            injuries = athlete.get('injury_history', [])
            relevant_injuries = [
                i for i in injuries
                if i.get('status') in ['active', 'improving']
            ]
        else:
            relevant_injuries = []

        # 9. Intensity distribution (last 7 days) — enrich with per-activity zone data
        activities_this_week = fetch_activity_hr_zones(activities_this_week)
        athlete_hr_zones = get_athlete_hr_zones()
        intensity_dist = calculate_intensity_distribution(activities_this_week, athlete_hr_zones)

        # 9b. Adaptation patterns (from coaching log)
        adaptation_patterns = _build_adaptation_patterns()

        # 9b2. Relocate derived fields to root-level parents
        compliance['compliance_rate_pct'] = _derive_compliance_rate_pct(compliance)
        if recovery and recovery.get('status') != 'unavailable':
            recovery['hrv_trend'] = _derive_hrv_trend(recovery)

        # 9c. Multi-week trends (wire in get_fitness_trend + volume by sport)
        trends = {}
        if daily_loads:
            overall_trend = get_fitness_trend(28)
            trends['overall_ctl_4wk'] = {
                'direction': overall_trend.get('trend', 'unknown'),
                'change': overall_trend.get('ctl_change', 0),
            }
            # Volume trajectory (4 weeks, oldest first)
            volume_4wk = []
            volume_by_sport_4wk = defaultdict(list)
            for week in range(3, -1, -1):  # 3=oldest, 0=this week
                w_start = week * 7
                w_end = (week + 1) * 7
                week_total = 0
                sport_week_totals = defaultdict(float)
                for i in range(w_start, w_end):
                    ds = (today - timedelta(days=i)).isoformat()
                    day_data = daily_loads.get(ds)
                    if isinstance(day_data, dict):
                        week_total += day_data.get('total', 0)
                        for sp, sp_load in day_data.get('by_sport', {}).items():
                            sport_week_totals[sp] += sp_load
                    elif isinstance(day_data, (int, float)):
                        week_total += day_data
                volume_4wk.append(round(week_total, 0))
                for sp in ['cycling', 'running', 'strength']:
                    volume_by_sport_4wk[sp].append(round(sport_week_totals.get(sp, 0), 0))
            trends['volume_trajectory_4wk'] = volume_4wk
            trends['volume_by_sport_4wk'] = dict(volume_by_sport_4wk)

        # 9d. Activity pattern analysis
        activity_patterns = analyze_activity_patterns(daily_loads, today, days=28)

        # 10. Volume data (CTL targeting for A-race) - DATA ONLY
        #
        # Shows BOTH overall and sport-specific CTL:
        # - Sport-specific CTL = race readiness (can you handle sani2c?)
        # - Overall CTL = total body capacity (can you handle the training volume?)
        # The LLM must respect both: don't spike overall ACWR chasing sport-specific CTL.
        volume_data = None
        events = training_config.get('events', [])
        a_race = next((e for e in events if e.get('priority') == 'A'), None)
        if a_race and overall_metrics and overall_metrics.get('ctl'):
            race_type = a_race.get('type', 'default')
            race_sport = RACE_TYPE_SPORT_MAP.get(race_type)

            # Sport-specific CTL for race readiness
            sport_ctl = None
            if race_sport and race_sport in sport_fitness:
                sport_ctl = sport_fitness[race_sport]['ctl']

            # Overall CTL for total body capacity
            overall_ctl = overall_metrics.get('ctl', 0)

            # Use sport-specific for gap calculation (race readiness)
            # but surface both so the LLM can reason about total load
            target_ctl_input = sport_ctl if sport_ctl is not None else overall_ctl

            # Calculate TSS trend from total loads (last 4 weeks)
            total_loads_flat = _extract_total_loads(daily_loads)
            last_week_tss = sum(
                total_loads_flat.get((today - timedelta(days=i)).isoformat(), 0)
                for i in range(7)
            )
            tss_trend_4wk = []
            for week in range(4):
                w_start = week * 7
                w_end = (week + 1) * 7
                week_tss = sum(
                    total_loads_flat.get((today - timedelta(days=i)).isoformat(), 0)
                    for i in range(w_start, w_end)
                )
                tss_trend_4wk.append(round(week_tss, 0))
            tss_trend_4wk.reverse()

            ctl_target = calculate_ctl_target(
                race_date=a_race.get('date'),
                race_type=race_type,
                current_ctl=target_ctl_input,
                current_weekly_tss=last_week_tss if last_week_tss > 0 else None
            )
            if not ctl_target.get('error'):
                volume_data = {
                    'a_race': a_race.get('name'),
                    'race_date': ctl_target.get('race_date'),
                    'race_sport': race_sport,
                    'days_until_race': ctl_target.get('days_until_race'),
                    'weeks_until_race': ctl_target.get('weeks_until_race'),
                    'current_ctl': round(sport_ctl, 1) if sport_ctl is not None else round(overall_ctl, 1),
                    'current_ctl_overall': round(overall_ctl, 1),
                    'ctl_source': f'{race_sport}_specific' if sport_ctl is not None else 'overall',
                    'target_ctl_min': ctl_target.get('target_ctl_min'),
                    'target_ctl_ideal': ctl_target.get('target_ctl_ideal'),
                    'ctl_gap': ctl_target.get('ctl_gap'),
                    'on_track': ctl_target.get('on_track'),
                    'weekly_tss_required': ctl_target.get('weekly_tss_required'),
                    'weekly_hours_required': ctl_target.get('weekly_hours_required'),
                    'last_week_tss': round(last_week_tss, 0) if last_week_tss else None,
                    'tss_trend_4wk': tss_trend_4wk,
                    'load_increase_pcts': [10, 15, 25],
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

            'acwr_warnings': acwr_warnings,

            'volume_data': volume_data,

            'compliance': compliance,

            'recovery': recovery,

            'sleep': {
                **(sleep_data if isinstance(sleep_data, dict) else {}),
                'trend_30d': sleep_trend_30d if sleep_trend_30d.get('status') != 'no_data' else None,
                'trend_direction': _derive_sleep_trend_direction(sleep_data),
            } if sleep_data else {'status': 'no_data'},

            'adaptation_patterns': adaptation_patterns,

            'trends': trends,

            'activity_patterns': activity_patterns,

            'sport_priorities': sport_priorities,

            'injuries': relevant_injuries,

            'intensity_distribution': intensity_dist,

            'strength': _get_strength_sync_summary(activities_this_week),

            'readiness_baselines': readiness_baselines if readiness_baselines.get('status') != 'insufficient_data' or len(readiness_baselines) > 1 else None,

            'compliance_diagnostics': compliance_diagnostics,

        }

        # Data quality flags — tells the LLM what data it's working with vs missing
        data_quality = {}
        personal = athlete.get('personal', {})
        if not personal.get('weight_kg'):
            data_quality['weight'] = 'missing'
        if not personal.get('age'):
            data_quality['age'] = 'missing'
        if not personal.get('name'):
            data_quality['name'] = 'missing'
        if recovery.get('status') == 'unavailable':
            data_quality['recovery'] = 'unavailable'
        if not sleep_data or sleep_data.get('status') == 'no_data':
            data_quality['sleep'] = 'unavailable'
        last_updated = history.get('last_updated')
        if last_updated and last_updated < (today - timedelta(days=1)).isoformat():
            data_quality['fitness_history'] = 'stale'
        if data_quality:
            snapshot['data_quality'] = data_quality

        # Coaching memory (continuity across sessions)
        try:
            coaching_ctx = get_coaching_context()
            snapshot['coaching_memory'] = {
                'active_decisions': coaching_ctx.get('active_decisions', [])[:5],
                'pending_approvals': coaching_ctx.get('pending_approvals', []),
                'adaptation_patterns': coaching_ctx.get('response_patterns', []),
                'recent_responses': coaching_ctx.get('recent_responses', [])[:3],
            }
        except Exception:
            logger.warning("Failed to load coaching memory", exc_info=True)
            snapshot['coaching_memory'] = {'status': 'unavailable'}

        # Snapshot flags — quick-scan summary for the LLM
        flags = _build_snapshot_flags(snapshot)
        if flags:
            snapshot['flags'] = flags

        return json.dumps(snapshot, indent=2)

    except Exception as e:
        logger.exception("get_coaching_snapshot failed")
        return json.dumps({'error': str(e)})
