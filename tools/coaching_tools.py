"""Coaching analysis and snapshot tools.

Registers MCP tools for:
- get_compliance_report
- get_coaching_score
- get_coaching_snapshot

Also contains helper functions:
- _compare_planned_actual
- _get_strength_sync_summary
- _parse_readiness_for_snapshot
- _readiness_to_recommendation
- _build_adaptation_signals
- _analyze_sport_priorities
- _generate_sport_blend_recommendation
"""

from collections import defaultdict
from mcp_app import mcp
from garmin_client import garmin_api_call
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
    get_load_athlete_max_hr,
    get_sleep_summary,
    calculate_ctl_target,
    _extract_total_loads,
    calculate_sport_fitness_metrics,
    get_fitness_trend,
    get_sleep_trend,
    persist_sleep_data,
    analyze_activity_patterns,
    update_fitness_history,
)
from config import (
    DATA_DIR,
    TRAINING_CONFIG_FILE,
    ATHLETE_FILE,
    CTL_TARGETS,
    DEFAULT_EQUIVALENCE_GROUPS,
    get_sport_group,
)
from datetime import date, timedelta
import json


# Import shared helper from strength_tools
from tools.strength_tools import _get_strength_baseline_data


# ---------------------------------------------------------------------------
# Helper functions (used by MCP tools below)
# ---------------------------------------------------------------------------

def _compare_planned_actual(plan: dict, activities: list, today: date) -> dict:
    """Compare planned sessions against actual activities.

    Surfaces anomalies for LLM reasoning instead of drawing conclusions.
    Status values: matched, partial, missing, unplanned, type_mismatch, pending.
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
            _sport_map = {
                'multi_day_mtb': 'cycling', 'road_cycling': 'cycling',
                'trail_ultra': 'running', 'running_marathon': 'running',
                'running_half': 'running', 'running_ultra': 'running',
            }
            race_sport = _sport_map.get(race_type)
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
                    max_hr = get_load_athlete_max_hr()
                    athlete_data = load_athlete()
                    ftp = athlete_data.get('personal', {}).get('ftp') if athlete_data else None
                    history = update_fitness_history(refreshed_activities, max_hr, ftp)
            except Exception:
                pass  # Non-fatal: proceed with stale data

        daily_loads = history.get('daily_loads', {})

        # 1. Current Weekly Plan
        current_plan = get_current_plan()

        # 2. Activities this week (actual)
        # Use plan dates if available, otherwise use calendar week (Mon-Sun)
        if current_plan and current_plan.get('week_start'):
            week_start = date.fromisoformat(current_plan['week_start'])
        else:
            week_start = today - timedelta(days=today.weekday())  # Monday

        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                week_start.isoformat(),
                today.isoformat()
            )
        )
        activities_this_week = parse_activities(raw_activities)

        # 3. Planned vs Actual comparison
        planned_vs_actual = _compare_planned_actual(current_plan, activities_this_week, today)

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
                    'risk': f'Overall ACWR {o_acwr} — HIGH total body injury risk. '
                            f'Reduce ALL training load before adding any sport-specific volume.',
                })
            elif o_status == 'elevated':
                acwr_warnings.append({
                    'level': 'overall',
                    'sport': 'all',
                    'acwr': o_acwr,
                    'risk': f'Overall ACWR {o_acwr} — elevated total body load. '
                            f'Do not add new training stimulus. Maintain or reduce.',
                })

        # Sport-specific ACWR checks (spike detection)
        for sport, sm in sport_fitness.items():
            if sm['ctl'] == 0 and sm['atl'] == 0:
                acwr_warnings.append({
                    'level': 'sport',
                    'sport': sport,
                    'acwr': 0.0,
                    'risk': f'Return-to-{sport} protocol required. Zero chronic {sport} load '
                            f'means ANY {sport} is a spike. Start cautiously.',
                })
            elif sm.get('acwr', 0) > 1.5:
                acwr_warnings.append({
                    'level': 'sport',
                    'sport': sport,
                    'acwr': sm['acwr'],
                    'risk': f'{sport.capitalize()} ACWR {sm["acwr"]} — HIGH sport-specific injury risk. '
                            f'Reduce {sport} load even if overall ACWR is safe.',
                })
            elif sm.get('acwr', 0) > 1.3:
                acwr_warnings.append({
                    'level': 'sport',
                    'sport': sport,
                    'acwr': sm['acwr'],
                    'risk': f'{sport.capitalize()} ACWR {sm["acwr"]} — elevated sport-specific risk.',
                })

        # 5. Compliance status
        compliance = check_weekly_compliance(activities_this_week)

        # 6. Recovery status (today) + Sleep tracking
        try:
            readiness_data = garmin_api_call(lambda c: c.get_training_readiness(today.isoformat()))
            recovery = _parse_readiness_for_snapshot(readiness_data)
        except Exception:
            recovery = {'status': 'unavailable', 'note': 'Could not fetch readiness data'}

        # 6b. Sleep data (last 7 days) + persist to history
        sleep_data = get_sleep_summary(today, days=7)
        if sleep_data and sleep_data.get('status') != 'no_data':
            # Persist sleep records to fitness_history for 30-day trend
            sleep_recs = sleep_data.get('recent', [])
            if sleep_recs:
                history = persist_sleep_data(sleep_recs, history)
                save_fitness_history(history)

        # 6c. Sleep trend (30-day from persisted data)
        sleep_trend_30d = get_sleep_trend(history, days=30)

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
            relevant_injuries = [
                i for i in injuries
                if i.get('status') in ['active', 'improving']
            ]
        else:
            athlete = {}
            relevant_injuries = []

        # 9. Intensity distribution (last 7 days)
        athlete_hr_zones = get_athlete_hr_zones()
        intensity_dist = calculate_intensity_distribution(activities_this_week, athlete_hr_zones)

        # 9b. Adaptation signals
        adaptation_signals = _build_adaptation_signals(
            sleep_data=sleep_data,
            recovery=recovery,
            compliance=compliance,
            daily_loads=daily_loads,
            today=today
        )

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
            _sport_map = {
                'multi_day_mtb': 'cycling', 'road_cycling': 'cycling',
                'trail_ultra': 'running', 'running_marathon': 'running',
                'running_half': 'running', 'running_ultra': 'running',
            }
            race_sport = _sport_map.get(race_type)

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
                o_acwr = overall_metrics.get('acwr', 1.0)
                o_acwr_status = overall_metrics.get('acwr_status', 'optimal')

                volume_data = {
                    'a_race': a_race.get('name'),
                    'race_date': ctl_target.get('race_date'),
                    'race_sport': race_sport,
                    'days_until_race': ctl_target.get('days_until_race'),
                    'weeks_until_race': ctl_target.get('weeks_until_race'),

                    # BOTH CTL views — the LLM needs both
                    'current_ctl': {
                        'overall': round(overall_ctl, 1),
                        'sport_specific': round(sport_ctl, 1) if sport_ctl is not None else None,
                        'sport': race_sport,
                        'note': (
                            f'{race_sport.capitalize()} CTL {sport_ctl} shows race-specific fitness. '
                            f'Overall CTL {overall_ctl} shows total body capacity. '
                            f'Build {race_sport} CTL but do NOT spike overall ACWR doing it.'
                        ) if sport_ctl is not None else None,
                    },
                    'target_ctl': {
                        'min': ctl_target.get('target_ctl_min'),
                        'ideal': ctl_target.get('target_ctl_ideal'),
                        'compared_against': f'{race_sport}_specific' if sport_ctl is not None else 'overall',
                    },
                    'ctl_gap': ctl_target.get('ctl_gap'),
                    'on_track': ctl_target.get('on_track'),
                    'weekly_tss_to_reach_target': {
                        'required': ctl_target.get('weekly_tss_required'),
                        'hours_estimate': ctl_target.get('weekly_hours_required'),
                    },
                    'last_week_tss': round(last_week_tss, 0) if last_week_tss else None,
                    'tss_trend_4wk': tss_trend_4wk,
                    'load_increase_guidance': {
                        'conservative_pct': 10,
                        'standard_pct': 15,
                        'aggressive_pct': 25,
                        'constraint': 'Overall ACWR must stay below 1.3 regardless of sport-specific targets',
                    },
                    'acwr': {
                        'overall': round(o_acwr, 2),
                        'overall_status': o_acwr_status,
                        'sport_specific': round(sport_fitness[race_sport]['acwr'], 2) if race_sport and race_sport in sport_fitness else None,
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

            'acwr_warnings': acwr_warnings,

            'volume_data': volume_data,

            'compliance': compliance,

            'recovery': recovery,

            'sleep': {
                **(sleep_data if isinstance(sleep_data, dict) else {}),
                'trend_30d': sleep_trend_30d if sleep_trend_30d.get('status') != 'no_data' else None,
            } if sleep_data else {'status': 'no_data'},

            'adaptation_signals': adaptation_signals,

            'trends': trends,

            'activity_patterns': activity_patterns,

            'sport_priorities': sport_priorities,

            'injuries': relevant_injuries,

            'intensity_distribution': intensity_dist,

            'strength': _get_strength_sync_summary(activities_this_week),

            'coaching_checklist': {
                'has_current_plan': bool(current_plan and current_plan.get('days')),
                'has_fitness_data': bool(daily_loads),
                'acwr_safe': overall_metrics.get('acwr_status') in ['optimal', 'low'] if overall_metrics else False,
                'sport_acwr_warnings': len(acwr_warnings),
                'compliance_ok': compliance.get('overall_compliant', False),
                'no_blocking_injuries': len([i for i in relevant_injuries if i.get('severity') == 'severe']) == 0,
                'sleep_adequate': sleep_data.get('status') == 'adequate' if sleep_data else False,
                'has_injuries_needing_rehab': any(i.get('rehab_protocol') for i in relevant_injuries),
            },
        }

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
            snapshot['coaching_memory'] = {'status': 'unavailable'}

        return json.dumps(snapshot, indent=2)

    except Exception as e:
        return json.dumps({'error': str(e)})
