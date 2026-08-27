"""Coaching analysis and snapshot tools.

Registers MCP tools for:
- get_compliance_report
- get_coaching_score
- get_coaching_snapshot

Also contains ~20 private helpers for snapshot assembly (sections, week_grid,
sleep gate, planned-vs-actual comparison, season lifecycle, sport priorities,
coaching score).
"""

from collections import defaultdict
from fastmcp import Context
from ..mcp_app import mcp
from ..garmin_client import (
    garmin_api_call,
    fetch_activity_hr_zones,
    GarminAuthRequiredError,
)
from ..parsers import parse_activities, build_current_time_context
from ..planner import (
    get_current_plan,
    load_athlete,
    load_methodology,
    load_coaching_log,
    get_coaching_context,
    validate_season_config,
)
from ..rules import (
    check_weekly_compliance,
    check_safety_rules,
    get_upcoming_events,
    load_training_config,
    pillars_as_name_dict,
    pillar_target_minutes,
)
from ..fitness import (
    load_fitness_history,
    save_fitness_history,
    calculate_fitness_metrics,
    calculate_intensity_distribution,
    get_athlete_hr_zones,
    parse_sleep_payload,
    summarize_sleep_records,
    calculate_ctl_target,
    _extract_total_loads,
    calculate_sport_fitness_metrics,
    get_fitness_trend,
    get_sleep_trend,
    detect_bedtime_drift,
    persist_sleep_data,
    persist_readiness_data,
    calculate_readiness_baselines,
    analyze_activity_patterns,
    update_fitness_history,
)
from ..config import (
    DATA_DIR,
    TRAINING_CONFIG_FILE,
    ATHLETE_FILE,
    CTL_TARGETS,
    DEFAULT_EQUIVALENCE_GROUPS,
    RACE_TIME_WEIGHTS,
    RACE_TIME_WEIGHT_DEFAULT,
)
from ..taxonomy import (
    types_match,
    types_match_with_name,
    is_mobility_by_name,
    is_known_type,
    race_sport_for,
)
from datetime import date, datetime, timedelta
import json
import logging
import time

logger = logging.getLogger(__name__)


# Import shared helper from strength_tools
from .strength_tools import _get_strength_baseline_data

# Phase 3 memory + season lifecycles (anomaly persistence, pattern
# normalization, decision review, tagged auto-proposals) are owned by
# decision_tools — the snapshot drives them.
from .decision_tools import (
    _slugify,
    auto_transition_due_decisions,
    ensure_tagged_proposal,
    normalize_adaptation_pattern,
    register_detected_anomalies,
    summarize_decisions_due_review,
)


# ---------------------------------------------------------------------------
# Snapshot sections
# ---------------------------------------------------------------------------
# The default snapshot is the CORE payload (~2-3K tokens): time context,
# flags, week_grid, ACWR status (+ legacy EWMA reference), injuries, plan
# adherence, today/tomorrow plan, coaching memory, open anomalies, the sleep
# gate and data_quality. Named sections add the heavier detail on request;
# sections=['full'] adds everything.

SNAPSHOT_NAMED_SECTIONS = (
    'plan', 'activities', 'fitness', 'sleep', 'recovery',
    'strength', 'memory', 'goals', 'patterns', 'sport_priorities',
)
SNAPSHOT_VALID_SECTIONS = ('core', 'full') + SNAPSHOT_NAMED_SECTIONS


def _resolve_snapshot_sections(sections: list[str] | None) -> tuple[set | None, str | None]:
    """Normalize the sections argument. Returns (named_extras, error).

    named_extras is the set of NAMED sections to include on top of core
    (core itself is always included). Unknown names produce an error string
    listing the valid vocabulary; named_extras is None in that case.
    """
    if not sections:
        return set(), None
    if isinstance(sections, str):  # tolerate a bare 'full' instead of ['full']
        sections = [sections]
    requested = {str(s).strip().lower() for s in sections}
    unknown = sorted(requested - set(SNAPSHOT_VALID_SECTIONS))
    if unknown:
        return None, (
            f"Unknown snapshot section(s): {', '.join(unknown)}. "
            f"Valid sections: {', '.join(SNAPSHOT_VALID_SECTIONS)}"
        )
    if 'full' in requested:
        return set(SNAPSHOT_NAMED_SECTIONS), None
    requested.discard('core')
    return requested, None


# ---------------------------------------------------------------------------
# Short-TTL Garmin fetch cache
# ---------------------------------------------------------------------------
# Back-to-back snapshot calls in one conversation would otherwise re-fetch
# identical Garmin data. Successful fetches are cached for
# GARMIN_CACHE_TTL_SECS (monotonic clock); get_coaching_snapshot's
# force_refresh=True bypasses the cache. Keys embed str(DATA_DIR) (so tests
# with redirected data dirs can never share entries) and a purpose namespace
# ('activities_ingest' vs 'activities_range') so the always-run ingestion
# side effect can never be confused with an unrelated range fetch.

GARMIN_CACHE_TTL_SECS = 300.0
_garmin_fetch_cache: dict[tuple, tuple[float, object]] = {}


def _cached_garmin_fetch(key: tuple, fetch, force_refresh: bool = False):
    """Return fetch(), reusing a cached value younger than the TTL.

    Only successful fetches are cached — exceptions propagate uncached so
    the next call retries.
    """
    full_key = (str(DATA_DIR),) + tuple(key)
    now = time.monotonic()
    if not force_refresh:
        hit = _garmin_fetch_cache.get(full_key)
        if hit is not None and (now - hit[0]) < GARMIN_CACHE_TTL_SECS:
            return hit[1]
    value = fetch()
    # Opportunistic prune so a long-lived server can't grow the cache unbounded
    for stale_key in [k for k, (ts, _v) in _garmin_fetch_cache.items()
                      if (now - ts) >= GARMIN_CACHE_TTL_SECS]:
        _garmin_fetch_cache.pop(stale_key, None)
    _garmin_fetch_cache[full_key] = (time.monotonic(), value)
    return value


# ---------------------------------------------------------------------------
# Helper functions (used by MCP tools below)
# ---------------------------------------------------------------------------

def _compare_planned_actual(plan: dict, activities: list, today: date,
                            daily_loads: dict = None, sleep_history: list = None) -> dict:
    """Compare planned sessions against actual activities.

    Pairing is TYPE-HONEST: a planned session only pairs with an actual
    activity whose type satisfies taxonomy.types_match (plus a name-hint
    fallback for mobility work Garmin logs as type 'other'). When several
    actuals match by type, the closest duration wins. Leftover planned
    sessions become 'missing' — but ONLY for dates before today; today's
    sessions stay 'pending' until the day is over. Leftover actuals become
    'unplanned' (load included so a hard unplanned ride is visible).

    Mismatched types are NEVER paired just because both are left over
    (the June 2026 padel/cycling crosswire). One narrow exception survives:
    when the day's only unmatched planned session has a type the taxonomy
    cannot classify (e.g. 'race') and exactly one actual remains, the pair
    surfaces as 'type_mismatch' — a plausible substitute to ask about.

    Status values: matched, partial, missing, unplanned, type_mismatch, pending.
    'as_of' anchors the date the comparison was computed against; every
    anomaly carries 'days_ago' relative to it.

    When daily_loads and sleep_history are provided, anomalies are enriched
    with surrounding context (sleep, prior day load) so the LLM can reason
    about WHY an anomaly occurred.
    """
    if not plan or not plan.get('days'):
        return {'status': 'no_plan', 'as_of': today.isoformat(),
                'note': 'No weekly plan to compare against'}

    comparison = {
        'as_of': today.isoformat(),
        'sessions_planned': 0,
        'sessions_completed': 0,
        'sessions_missed': 0,
        'sessions_pending': 0,
        'anomalies': [],
        'details': []
    }

    def _unplanned_anomaly(day_str, act, on_rest_day=False):
        anomaly = {
            'date': day_str,
            'flag': 'unplanned',
            'activity_type': act.get('type'),
            'duration_mins': act.get('duration_mins', 0),
        }
        load = act.get('load')
        if load is None:
            load = act.get('garmin_training_load')
        if load is not None:
            anomaly['load'] = load
        if on_rest_day:
            anomaly['on_rest_day'] = True
        return anomaly

    for day_str, day_data in plan.get('days', {}).items():
        try:
            day_date = date.fromisoformat(day_str)
        except ValueError:
            continue

        raw_planned = day_data.get('planned', {})
        sessions_for_day = raw_planned if isinstance(raw_planned, list) else [raw_planned]
        non_rest_sessions = [
            s for s in sessions_for_day
            if s and 'rest' not in str(s.get('type', '')).lower()
        ]
        is_rest_day = not non_rest_sessions

        if is_rest_day:
            # Check for unplanned activity on rest day
            day_activities = [a for a in activities if a.get('date') == day_str]
            if day_activities and day_date <= today:
                for act in day_activities:
                    comparison['anomalies'].append(
                        _unplanned_anomaly(day_str, act, on_rest_day=True))
                    comparison['details'].append({
                        'date': day_str,
                        'status': 'unplanned',
                        'actual': act.get('type'),
                        'duration_actual': act.get('duration_mins', 0),
                    })
            continue

        day_activities = [a for a in activities if a.get('date') == day_str]
        unmatched_activities = list(day_activities)
        unmatched_planned = []
        day_details = []

        def _paired_detail(planned_type, planned_duration, act, status):
            """Detail + duration-delta anomalies for a planned/actual pair."""
            actual_type = act.get('type', 'unknown')
            actual_duration = act.get('duration_mins', 0)
            detail = {
                'date': day_str,
                'planned_type': planned_type,
                'actual_type': actual_type,
                'duration_planned': planned_duration,
                'duration_actual': actual_duration,
                'status': status,
            }
            if planned_duration and actual_duration:
                delta_pct = round(
                    (actual_duration - planned_duration) / planned_duration * 100, 1
                )
                detail['duration_delta_pct'] = delta_pct
                if delta_pct < -30 or delta_pct > 30:
                    if delta_pct < -30 and detail['status'] == 'matched':
                        detail['status'] = 'partial'
                    comparison['anomalies'].append({
                        'date': day_str,
                        'flag': 'duration_delta',
                        'planned_mins': planned_duration,
                        'actual_mins': actual_duration,
                        'delta_pct': delta_pct,
                    })
            return detail

        for planned in non_rest_sessions:
            comparison['sessions_planned'] += 1
            planned_type = planned.get('type', '')
            planned_duration = planned.get('duration_mins', 0)

            if day_date > today:
                comparison['sessions_pending'] += 1
                day_details.append({
                    'date': day_str,
                    'status': 'pending',
                    'planned': planned_type,
                })
                continue

            # Type-honest pairing: only taxonomy matches qualify (with the
            # mobility-by-name fallback for Garmin type 'other'); among
            # several candidates the closest duration wins.
            candidates = [
                (i, act) for i, act in enumerate(unmatched_activities)
                if types_match_with_name(planned_type, act.get('type', ''),
                                         act.get('name'))
            ]
            if candidates:
                if planned_duration:
                    match_idx = min(
                        candidates,
                        key=lambda c: abs((c[1].get('duration_mins') or 0)
                                          - planned_duration),
                    )[0]
                else:
                    match_idx = candidates[0][0]
                best_match = unmatched_activities.pop(match_idx)
                comparison['sessions_completed'] += 1
                day_details.append(_paired_detail(
                    planned_type, planned_duration, best_match, 'matched'))
            else:
                unmatched_planned.append(planned)

        # Narrow substitute exception: ONE leftover planned session whose
        # type the taxonomy can't classify (e.g. 'race') + exactly ONE
        # leftover actual -> surface as type_mismatch for the coach to ask
        # about. Taxonomy-KNOWN plan types (padel, long_run, ...) never
        # pair with a non-matching actual.
        if (len(unmatched_planned) == 1 and len(unmatched_activities) == 1
                and unmatched_planned[0].get('type')
                and not is_known_type(unmatched_planned[0].get('type'))):
            planned = unmatched_planned.pop(0)
            substitute = unmatched_activities.pop(0)
            comparison['sessions_completed'] += 1
            comparison['anomalies'].append({
                'date': day_str,
                'flag': 'type_mismatch',
                'planned_type': planned.get('type', ''),
                'actual_type': substitute.get('type', 'unknown'),
            })
            day_details.append(_paired_detail(
                planned.get('type', ''), planned.get('duration_mins', 0),
                substitute, 'type_mismatch'))

        # Leftover planned sessions: missing only for PAST days. Today's
        # sessions are pending until the day is over — a 06:27 snapshot
        # must never report today's ride as missed.
        for planned in unmatched_planned:
            planned_type = planned.get('type', '')
            if day_date >= today:
                comparison['sessions_pending'] += 1
                day_details.append({
                    'date': day_str,
                    'status': 'pending',
                    'planned': planned_type,
                })
            else:
                comparison['sessions_missed'] += 1
                comparison['anomalies'].append({
                    'date': day_str,
                    'flag': 'missing',
                    'planned_type': planned_type,
                    'planned_mins': planned.get('duration_mins', 0),
                })
                day_details.append({
                    'date': day_str,
                    'status': 'missing',
                    'planned_type': planned_type,
                })

        # Leftover actuals: unplanned, never silently absorbed as a
        # substitute for a non-matching planned session.
        if day_date <= today:
            for act in unmatched_activities:
                comparison['anomalies'].append(_unplanned_anomaly(day_str, act))
                day_details.append({
                    'date': day_str,
                    'status': 'unplanned',
                    'actual': act.get('type'),
                    'duration_actual': act.get('duration_mins', 0),
                })

        # If the day had more activities than planned sessions, attach
        # all_activities to the first detail for context. Preserves the
        # single-session-day-with-multiple-activities behavior.
        has_extra_activities = len(day_activities) > len(non_rest_sessions)

        for i, detail in enumerate(day_details):
            promote = has_extra_activities and i == 0
            if promote:
                detail['all_activities'] = [
                    {'type': a.get('type', 'unknown'), 'duration_mins': a.get('duration_mins', 0)}
                    for a in day_activities
                ]
                comparison['details'].append(detail)
            elif detail.get('status') == 'matched' and 'all_activities' not in detail:
                comparison['details'].append({
                    'date': day_str,
                    'status': 'matched',
                })
            else:
                comparison['details'].append(detail)

    comparison['completion_rate'] = (
        round(comparison['sessions_completed'] / comparison['sessions_planned'] * 100, 1)
        if comparison['sessions_planned'] > 0 else None
    )

    # Temporal self-anchoring: every anomaly knows how far back it happened
    # (0 = today, 1 = yesterday) so a long conversation can't drift dates.
    for anomaly in comparison['anomalies']:
        try:
            anomaly['days_ago'] = (today - date.fromisoformat(anomaly['date'])).days
        except (ValueError, TypeError, KeyError):
            pass

    # Enrich anomalies with surrounding context when data is available
    if (daily_loads or sleep_history) and comparison['anomalies']:
        from ..fitness import get_day_context
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


def _parse_readiness_for_snapshot(readiness_data: dict,
                                  hrv_data: dict | None = None) -> dict:
    """Parse readiness data for snapshot. Returns structured data, no prescriptions.

    When hrv_data is provided (from c.get_hrv_data()), its fields override
    null/missing values from readiness. Garmin's training readiness often
    returns null for hrv_status even when the athlete's device tracks HRV —
    the dedicated /hrv-service/hrv endpoint has the real data.
    """
    if not readiness_data and not hrv_data:
        return {'status': 'unavailable'}

    if isinstance(readiness_data, list):
        readiness_data = readiness_data[0] if readiness_data else {}
    readiness_data = readiness_data or {}

    from ..parsers import parse_hrv_data
    hrv_parsed = parse_hrv_data(hrv_data) if hrv_data else None

    result = {
        'score': readiness_data.get('score'),
        'level': readiness_data.get('level'),
        'hrv_status': readiness_data.get('hrvStatus'),
        'sleep_score': readiness_data.get('sleepScore'),
        'recovery_time_mins': readiness_data.get('recoveryTime'),
        'recovery_time_hrs': readiness_data.get('recoveryTimeInHours'),
    }

    if hrv_parsed:
        if result.get('hrv_status') is None:
            result['hrv_status'] = hrv_parsed.get('status')
        result['hrv_last_night_avg'] = hrv_parsed.get('last_night_avg')
        result['hrv_weekly_avg'] = hrv_parsed.get('weekly_avg')
        result['hrv_baseline_low'] = hrv_parsed.get('baseline_low')
        result['hrv_baseline_high'] = hrv_parsed.get('baseline_high')
        result['hrv_feedback'] = hrv_parsed.get('feedback')
    return result


def _build_adaptation_patterns() -> dict:
    """
    Build adaptation patterns from coaching log for LLM personalization.

    These patterns help the LLM decide where in the load_increase_guidance
    range to operate (conservative/standard/aggressive).

    Returns boolean flags (backward compat) plus quantified thresholds
    when enough numeric response data is available.
    """
    from ..fitness import derive_adaptation_thresholds

    try:
        log = load_coaching_log()
        responses = log.get('athlete_responses', [])

        # Count by CANONICAL pattern key (config.ADAPTATION_PATTERN_REGISTRY)
        # so case/spacing variants of the same pattern aggregate — that's
        # what lets a pattern actually trigger coaching behavior.
        counts = {}
        unrecognized = {}
        for r in responses:
            raw = r.get('pattern')
            if not raw:
                continue
            canonical, recognized = normalize_adaptation_pattern(raw)
            bucket = counts if recognized else unrecognized
            bucket[canonical] = bucket.get(canonical, 0) + 1

        result = {
            'handles_volume_well': counts.get('handles_volume_well', 0) > counts.get('struggles_with_volume', 0),
            'recovers_quickly': counts.get('recovers_quickly', 0) > counts.get('slow_recovery', 0),
            'needs_extra_rest_after_intensity': counts.get('needs_extra_rest_after_intensity', 0) > 0,
            'pattern_counts': counts,
            'patterns_logged': len(counts) + len(unrecognized),
            'total_responses': len(responses),
        }
        if unrecognized:
            result['unrecognized_patterns'] = unrecognized

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
                target_mins = pillar_target_minutes(pillar_config)
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


def _summarize_goal_progress(activities: list, training_config: dict,
                             today: date, period_days: int = 14) -> dict:
    """Distribute activity time across race_preparation / fun / aesthetics goals.

    Surfaced as snapshot.goal_progress — the standalone get_goal_progress tool
    was removed in Phase 2 of the tool rationalization. Pure function, no I/O.
    """
    fun_types = ['padel', 'ultimate_disc', 'social_ride', 'tennis', 'squash', 'badminton']
    strength_types = ['strength_training', 'indoor_cardio', 'functional_strength']
    goal_balance = (training_config or {}).get('goal_balance', {})
    prompt_fun = goal_balance.get('fun_activities', {}).get('prompt_if_missing_days', 14)

    race_prep_mins = 0.0
    fun_mins = 0.0
    aesthetics_mins = 0.0
    last_fun_date = None
    strength_count = 0

    cutoff = (today - timedelta(days=period_days)).isoformat()
    for activity in activities:
        act_date = activity.get('date')
        if not act_date or act_date < cutoff:
            continue
        act_type = (activity.get('type') or '').lower()
        duration = activity.get('duration_mins', 0) or 0
        if any(f in act_type for f in fun_types):
            fun_mins += duration
            if last_fun_date is None or act_date > last_fun_date:
                last_fun_date = act_date
        elif any(s in act_type for s in strength_types):
            aesthetics_mins += duration
            strength_count += 1
        else:
            race_prep_mins += duration

    total_mins = race_prep_mins + fun_mins + aesthetics_mins
    if total_mins > 0:
        race_prep_pct = round(race_prep_mins / total_mins * 100)
        fun_pct = round(fun_mins / total_mins * 100)
        aesthetics_pct = round(aesthetics_mins / total_mins * 100)
    else:
        race_prep_pct = fun_pct = aesthetics_pct = 0

    days_since_fun = None
    if last_fun_date:
        try:
            fun_date = date.fromisoformat(last_fun_date)
            days_since_fun = (today - fun_date).days
        except ValueError:
            pass

    recommendations = []
    if days_since_fun is not None and days_since_fun > prompt_fun:
        recommendations.append(f"Fun activity missing for {days_since_fun} days — schedule one soon")
    elif days_since_fun is None:
        recommendations.append("No fun activities in the window — include one")
    if aesthetics_pct < 20 and strength_count < 2:
        recommendations.append("Aesthetics/upper-body underrepresented — add a strength session")
    if race_prep_pct > 80:
        recommendations.append("Heavy race-prep focus — balance with fun + gym")

    return {
        'period_days': period_days,
        'total_training_mins': round(total_mins),
        'goal_progress': {
            'race_preparation': {
                'mins': round(race_prep_mins),
                'pct': race_prep_pct,
                'target_pct': 50,
                'status': 'on_track' if race_prep_pct >= 40 else 'low',
            },
            'fun_activities': {
                'mins': round(fun_mins),
                'pct': fun_pct,
                'target_pct': 25,
                'days_since_last': days_since_fun,
                'status': 'on_track' if fun_pct >= 15 else (
                    'missing' if days_since_fun and days_since_fun > prompt_fun else 'low'
                ),
            },
            'aesthetics': {
                'mins': round(aesthetics_mins),
                'pct': aesthetics_pct,
                'target_pct': 25,
                'strength_sessions': strength_count,
                'status': 'on_track' if strength_count >= 2 else 'low',
            },
        },
        'recommendations': recommendations,
        'balance_score': 'good' if len(recommendations) == 0 else (
            'needs_attention' if len(recommendations) <= 1 else 'rebalance_needed'
        ),
    }


def _summarize_plan_adherence_by_pillar(plan: dict, activities: list,
                                        today: date) -> dict:
    """Per-pillar plan adherence summary for the current week.

    For each pillar (strength, mobility, long_effort) returns:
      - planned: count of planned sessions matching the pillar
      - completed: count of planned-then-matched sessions
      - skipped_dates: list of ISO dates where a pillar session was planned but
          no matching activity happened (only dates BEFORE today — today's
          unfinished sessions are pending, never skipped)
      - deficit: planned - completed (skipped + still-pending count)

    This closes the "planned 5 strength, completed 3, skipped Monday + Wednesday"
    gap that aggregate compliance metrics hide.
    """
    from ..rules import classify_activity

    pillars = ('strength', 'mobility', 'long_effort')
    result = {p: {'planned': 0, 'completed': 0,
                  'skipped_dates': [], 'pending_dates': []}
              for p in pillars}

    if not plan or not plan.get('days'):
        return {p: dict(data, deficit=0) for p, data in result.items()}

    def _session_pillars(session: dict) -> set:
        if not session:
            return set()
        if 'rest' in str(session.get('type', '')).lower():
            return set()
        flags = classify_activity(session)
        found = set()
        if flags.get('is_strength'):
            found.add('strength')
        if flags.get('is_mobility'):
            found.add('mobility')
        if flags.get('is_long_effort'):
            found.add('long_effort')
        return found

    for day_str, day_data in plan.get('days', {}).items():
        try:
            day_date = date.fromisoformat(day_str)
        except ValueError:
            continue

        planned = day_data.get('planned') or {}
        sessions = planned if isinstance(planned, list) else [planned]

        planned_pillars = set()
        for s in sessions:
            planned_pillars |= _session_pillars(s)
        if not planned_pillars:
            continue

        # Was this pillar actually done on this day? (Garmin logs mobility
        # sessions as type 'other' — the activity NAME decides for those.)
        day_acts = [a for a in activities if a.get('date') == day_str]
        actual_pillars = set()
        for a in day_acts:
            actual_pillars |= _session_pillars(a)
            if is_mobility_by_name(a.get('type'), a.get('name')):
                actual_pillars.add('mobility')

        for pillar in planned_pillars:
            result[pillar]['planned'] += 1
            if pillar in actual_pillars:
                result[pillar]['completed'] += 1
            elif day_date >= today:
                # Today is pending until the day is over — never 'skipped'
                result[pillar]['pending_dates'].append(day_str)
            else:
                result[pillar]['skipped_dates'].append(day_str)

    for p, data in result.items():
        data['skipped_dates'].sort()
        data['pending_dates'].sort()
        data['deficit'] = data['planned'] - data['completed']
    return result


def _build_week_grid(activities: list, today: date,
                     daily_loads: dict | None = None) -> dict:
    """7-day rolling activity grid ending today.

    Rest days are explicit ('types_summary' = 'REST'). Aggregate metrics (CTL,
    ACWR, compliance totals) hide zero-activity days — the grid surfaces them
    so the coach can see the full week at a glance.
    """
    grid = {}
    for offset in range(6, -1, -1):
        day = today - timedelta(days=offset)
        day_str = day.isoformat()
        day_acts = [a for a in activities if a.get('date') == day_str]
        types = sorted({a.get('type', 'unknown') for a in day_acts})
        total_mins = sum(a.get('duration_mins', 0) or 0 for a in day_acts)

        total_load = None
        entry = (daily_loads or {}).get(day_str)
        if isinstance(entry, dict):
            total_load = entry.get('total')
        elif isinstance(entry, (int, float)):
            total_load = entry

        grid[day_str] = {
            'day_of_week': day.strftime('%A'),
            'days_ago': offset,  # 0 = today, 1 = yesterday — authoritative
            'activity_count': len(day_acts),
            'types': types,
            'types_summary': '+'.join(types) if types else 'REST',
            'total_duration_mins': round(total_mins, 1),
            'total_load': round(total_load, 1) if total_load is not None else None,
            'is_rest': len(day_acts) == 0,
            'is_today': day == today,
        }
    return grid


def _count_decisions_due_review(active_decisions: list, today: date,
                                review_after_days: int = 7) -> int:
    """Count active decisions older than review_after_days (due a check-in)."""
    due = 0
    for d in active_decisions or []:
        d_date = d.get('date', '')
        try:
            if d_date and (today - date.fromisoformat(d_date)).days > review_after_days:
                due += 1
        except (ValueError, TypeError):
            pass
    return due


# Days past periodization.target_transition before the snapshot nudges the
# athlete with a phase_transition proposal (the data_quality flag fires
# immediately; the proposal waits out a grace window).
PHASE_OVERDUE_GRACE_DAYS = 7


def _season_lifecycle_proposals(training_config: dict, today: date) -> list[dict]:
    """Auto-generate season-lifecycle approvals (idempotent by event tag).

    1. RACE PASSED: an A/B-priority event whose date has passed with no
       race_review decision mentioning it → action_type='season_replan'.
       C/D/life_event priorities never trigger a replan.
    2. PHASE OVERDUE: periodization.target_transition passed more than
       PHASE_OVERDUE_GRACE_DAYS ago with no phase_transition decision since
       → action_type='phase_transition'.

    Proposals route through the normal approval machinery
    (propose_coaching_action → approve_proposal / reject_proposal) and carry
    an event_tag, so each real-world trigger only ever generates ONE
    proposal — open, approved and rejected proposals all block re-creation.
    Returns the list of freshly created proposals (usually empty).
    """
    created = []
    log = load_coaching_log()
    decisions = log.get('decisions', []) or []

    def _decision_text(d):
        return f"{d.get('decision') or ''} {d.get('rationale') or ''}".lower()

    for event in (training_config or {}).get('events', []) or []:
        if not isinstance(event, dict):
            continue
        if str(event.get('priority') or '').upper() not in ('A', 'B'):
            continue
        try:
            event_date = date.fromisoformat(event.get('date') or '')
        except (ValueError, TypeError):
            continue
        if event_date >= today:
            continue
        name = event.get('name') or f"Race on {event.get('date')}"
        reviewed = any(
            d.get('type') == 'race_review' and name.lower() in _decision_text(d)
            for d in decisions
        )
        if reviewed:
            continue
        days_ago = (today - event_date).days
        result = ensure_tagged_proposal(
            event_tag=f"season_replan:{_slugify(name)}:{event.get('date')}",
            action_type='season_replan',
            proposal=f"{name} completed — debrief and re-plan the season",
            rationale=(
                f"{name} ({event.get('priority')}-priority) was {days_ago} "
                f"day(s) ago and no race_review decision has been logged. "
                f"Debrief the race with the athlete (log_coaching_decision "
                f"with decision_type='race_review'), then re-plan the season "
                f"around the next target."),
            impact='major',
        )
        if result.get('created'):
            created.append(result)

    periodization = (training_config or {}).get('periodization', {}) or {}
    target_transition = periodization.get('target_transition')
    try:
        transition_date = (date.fromisoformat(target_transition)
                           if target_transition else None)
    except (ValueError, TypeError):
        transition_date = None
    if transition_date and (today - transition_date).days > PHASE_OVERDUE_GRACE_DAYS:
        transitioned_since = any(
            d.get('type') == 'phase_transition'
            and (d.get('date') or '') >= target_transition
            for d in decisions
        )
        if not transitioned_since:
            current_phase = periodization.get('current_phase') or 'unknown'
            days_overdue = (today - transition_date).days
            result = ensure_tagged_proposal(
                event_tag=f"phase_transition:{_slugify(current_phase)}:{target_transition}",
                action_type='phase_transition',
                proposal=(f"Phase transition overdue — '{current_phase}' was "
                          f"due to end {target_transition} "
                          f"({days_overdue} days ago)"),
                rationale=(
                    f"periodization.target_transition ({target_transition}) "
                    f"passed {days_overdue} days ago with no phase_transition "
                    f"decision since. Review the block with the athlete: "
                    f"update_phase() to move on, or push target_transition "
                    f"out with a logged rationale."),
                impact='major',
            )
            if result.get('created'):
                created.append(result)
    return created


def _activities_from_history(daily_loads: dict, start_iso: str, end_iso: str) -> list[dict]:
    """Reconstruct a parsed-activity list from persisted daily_loads.

    Fallback for when the Garmin activities fetch is unavailable — week_grid
    and planned-vs-actual then reflect locally persisted history instead of
    falsely showing every day as REST.
    """
    activities = []
    for day_iso, day_data in (daily_loads or {}).items():
        if not (start_iso <= day_iso <= end_iso) or not isinstance(day_data, dict):
            continue
        for act in day_data.get('activities', []) or []:
            entry = dict(act)
            entry.setdefault('date', day_iso)
            activities.append(entry)
    activities.sort(key=lambda a: a.get('date', ''))
    return activities


def _merge_sleep_nights(history_nights: list[dict], fetched: list[dict],
                        today: date, days: int = 7) -> list[dict]:
    """Last-N nights: persisted history overlaid with freshly fetched records.

    Freshly fetched records carry full detail and win on date collisions.
    Returns most-recent-first, capped at ``days`` nights.
    """
    cutoff = (today - timedelta(days=days - 1)).isoformat()
    by_date: dict[str, dict] = {}
    for rec in history_nights or []:
        d = rec.get('date')
        if d and d >= cutoff:
            by_date[d] = rec
    for rec in fetched or []:
        d = rec.get('date')
        if d and d >= cutoff:
            by_date[d] = rec
    return [by_date[d] for d in sorted(by_date, reverse=True)][:days]


def _build_sleep_gate(sleep_data: dict | None) -> dict:
    """Compact sleep-gate signal for the core payload.

    Sleep is a GATE for training decisions — the core snapshot always
    carries enough to apply it without the full per-night breakdown.
    """
    if not sleep_data or sleep_data.get('status') == 'no_data':
        return {'status': 'no_data'}
    nights = sleep_data.get('nights') or []
    last_night = nights[0] if nights else {}
    return {
        'avg_hours': sleep_data.get('avg_duration_hrs'),
        'deficit': sleep_data.get('status') in ('deficit', 'severe_deficit'),
        'status': sleep_data.get('status'),
        'acute_status': sleep_data.get('acute_status'),
        'last_night_score': last_night.get('score'),
        'last_night_hrs': last_night.get('duration_hrs'),
        'nights_analyzed': sleep_data.get('days_analyzed'),
    }


def _assemble_snapshot_payload(full: dict, include: set) -> dict:
    """Filter the internal full snapshot down to core + requested sections.

    Core ALWAYS carries: time context, flags, week_grid, slim fitness
    metrics (acwr_status + acwr_ewma reference + load hierarchy), acwr_warnings,
    injuries, plan_adherence, today/tomorrow plan, coaching memory, open
    planned-vs-actual anomalies, the sleep gate, and data_quality. Memory +
    curiosity + the sleep gate live in the DEFAULT payload by design — a
    bare metrics dashboard is explicitly not enough to coach with.
    """
    weekly_plan_full = full.get('weekly_plan') or {}
    plan_days = weekly_plan_full.get('days') or {}
    today_iso = full.get('snapshot_date')
    tomorrow_iso = None
    try:
        tomorrow_iso = (date.fromisoformat(today_iso) + timedelta(days=1)).isoformat()
    except (ValueError, TypeError):
        pass
    weekly_plan = {
        'week_start': weekly_plan_full.get('week_start'),
        'week_end': weekly_plan_full.get('week_end'),
        'has_plan': weekly_plan_full.get('has_plan'),
        'today': plan_days.get(today_iso),
        'tomorrow': plan_days.get(tomorrow_iso),
    }
    if 'plan' in include:
        weekly_plan['days'] = plan_days

    pva = full.get('planned_vs_actual')
    if isinstance(pva, dict) and 'plan' not in include:
        pva = {k: v for k, v in pva.items() if k != 'details'}

    memory = full.get('coaching_memory')
    if isinstance(memory, dict) and 'memory' not in include:
        memory = {k: v for k, v in memory.items() if k != 'adaptation_patterns'}

    out = {
        'current_time_context': full.get('current_time_context'),
        'snapshot_date': full.get('snapshot_date'),
        'day_of_week': full.get('day_of_week'),
        'sections': {
            'included': ['core'] + sorted(include),
            'available': list(SNAPSHOT_NAMED_SECTIONS) + ['full'],
        },
        'flags': full.get('flags', {}),
        'week_grid': full.get('week_grid'),
        'week_grid_today': full.get('week_grid_today'),
        'weekly_plan': weekly_plan,
        'plan_adherence': full.get('plan_adherence'),
        'planned_vs_actual': pva,
        'fitness_metrics': full.get('fitness_metrics'),
        'acwr_warnings': full.get('acwr_warnings'),
        'injuries': full.get('injuries'),
        'coaching_memory': memory,
        'sleep_gate': full.get('sleep_gate'),
        # Always present — {} means every quality check passed
        'data_quality': full.get('data_quality') or {},
    }

    if 'plan' in include:
        out['compliance'] = full.get('compliance')
    if 'activities' in include:
        out['activities_this_week'] = full.get('activities_this_week')
    if 'fitness' in include:
        out['volume_data'] = full.get('volume_data')
        out['trends'] = full.get('trends')
        out['intensity_distribution'] = full.get('intensity_distribution')
    if 'sleep' in include:
        out['sleep'] = full.get('sleep')
    if 'recovery' in include:
        out['recovery'] = full.get('recovery')
        out['readiness_baselines'] = full.get('readiness_baselines')
    if 'strength' in include:
        out['strength'] = full.get('strength')
    if 'goals' in include:
        out['goal_progress'] = full.get('goal_progress')
    if 'patterns' in include:
        out['adaptation_patterns'] = full.get('adaptation_patterns')
        out['activity_patterns'] = full.get('activity_patterns')
        out['compliance_diagnostics'] = full.get('compliance_diagnostics')
    if 'sport_priorities' in include:
        out['sport_priorities'] = full.get('sport_priorities')
    return out


def _build_snapshot_flags(snapshot: dict, today: date) -> dict:
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

    # Expired plan — the athlete is effectively uncoached. Loud, not silent.
    if pva.get('status') == 'plan_expired':
        flags['plan_expired'] = True
        flags['days_uncoached'] = pva.get('days_since_expiry')

    # Sleep deficit
    sleep = snapshot.get('sleep', {})
    if sleep.get('deficit_flag') or sleep.get('trend_direction') == 'declining':
        flags['sleep_deficit'] = True

    # Pending approvals
    memory = snapshot.get('coaching_memory', {})
    pending = memory.get('pending_approvals', [])
    if pending:
        flags['pending_approvals'] = len(pending)

    # Decisions due for review — prefer the summaries the memory section
    # carries (needs_review lifecycle aware); fall back to the age heuristic
    # when only active_decisions is available.
    due_summaries = memory.get('decisions_due_review')
    if isinstance(due_summaries, list):
        review_due = len(due_summaries)
    else:
        review_due = _count_decisions_due_review(
            memory.get('active_decisions', []), today
        )
    if review_due:
        flags['decisions_due_for_review'] = review_due

    # Compliance below 70%
    compliance = snapshot.get('compliance', {})
    rate = compliance.get('compliance_rate_pct')
    if rate is not None and rate < 70:
        flags['compliance_below_70'] = True

    return flags


def _analyze_sport_priorities(events: list, current_block: dict,
                              race_templates: dict, today: date) -> dict:
    """
    Analyze multi-sport priorities based on upcoming races.

    Returns recommended volume distribution across sports and identifies
    which sessions are shared (strength, mobility) vs sport-specific.
    """
    # Categorize events by sport type (race-type -> sport via taxonomy)
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
        sport = race_sport_for(sport_type) or 'other'

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
        sport = race_sport_for(sport_type) or sport_type
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

@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
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
        safety = check_safety_rules(activities, today=today)

        # Get upcoming events for context
        upcoming = get_upcoming_events(days_ahead=56, today=today)

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


def _compute_coaching_score(
    fitness_history: dict,
    athlete: dict,
    training_config: dict,
    coaching_log: dict,
    today: date,
) -> dict:
    """Compute the 4-component coaching score from persisted data only.

    Pure function — no Garmin calls, no disk I/O. Same output shape as
    get_coaching_score. Callers can pre-load the 4 inputs (e.g. after a
    snapshot has already refreshed fitness_history) to avoid redundant work.

    Components:
    - Progress (40%): CTL trajectory toward A-race
    - Health (30%): Active injuries + ACWR safety
    - Achievability (20%): Pillar compliance rate over the last 4 weeks,
      reconstructed from fitness_history.daily_loads[*].activities
    - Adaptation (10%): Athlete-response log richness
    """
    daily_loads = fitness_history.get('daily_loads', {}) or {}
    total_loads = _extract_total_loads(daily_loads) if daily_loads else {}
    fitness_data = calculate_fitness_metrics(total_loads, today) if total_loads else {}
    current_ctl = fitness_data.get('ctl', 0) if fitness_data else 0

    # 4-week CTL gain from persisted snapshots (v1 + v2)
    snapshots = fitness_history.get('snapshots', [])
    ctl_4wk_ago = None
    for snap in snapshots:
        try:
            snap_date = date.fromisoformat(snap['date'])
        except (KeyError, ValueError, TypeError):
            continue
        if (today - snap_date).days >= 28:
            if 'total' in snap:
                ctl_4wk_ago = snap['total'].get('ctl', 0)
            else:
                ctl_4wk_ago = snap.get('ctl', 0)
            break
    ctl_gain_4wk = current_ctl - (ctl_4wk_ago or current_ctl)

    # Progress (40%)
    events = training_config.get('events', [])
    a_race = next((e for e in events if e.get('priority') == 'A'), None)
    progress_score = 50
    progress_data = {
        'current_ctl': round(current_ctl, 1),
        'ctl_gain_4wk': round(ctl_gain_4wk, 1),
        'target_ctl': None,
        'days_remaining': None,
        'trajectory': 'unknown',
    }
    if a_race:
        race_type = a_race.get('type', 'default')
        target_ctl = CTL_TARGETS.get(race_type, CTL_TARGETS['default'])['ideal']
        progress_data['target_ctl'] = target_ctl

        race_sport = race_sport_for(race_type)
        if race_sport and daily_loads:
            sport_m = calculate_sport_fitness_metrics(daily_loads, race_sport, today)
            if sport_m.get('days_with_data', 0) > 0:
                current_ctl = sport_m['ctl']
                progress_data['current_ctl'] = round(current_ctl, 1)
                progress_data['ctl_source'] = f'{race_sport}_specific'

        try:
            race_dt = date.fromisoformat(a_race.get('date'))
            days_remaining = (race_dt - today).days
            progress_data['days_remaining'] = days_remaining
            if days_remaining > 0:
                required_gain = target_ctl - current_ctl
                weeks_remaining = days_remaining / 7
                if required_gain <= 0:
                    progress_score = 100
                    progress_data['trajectory'] = 'ahead'
                elif weeks_remaining > 0:
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

    # Health (30%)
    health_score = 90
    health_data = {
        'injuries_active': 0,
        'acwr_status': 'unknown',
        'acwr': None,
        'overtraining_risk': 'low',
    }
    relevant_injuries = [
        i for i in athlete.get('injury_history', [])
        if i.get('status') in ['active', 'improving']
    ]
    active_injuries = [i for i in relevant_injuries if i.get('status') == 'active']
    improving_injuries = [i for i in relevant_injuries if i.get('status') == 'improving']
    health_data['injuries_active'] = len(active_injuries)
    health_data['injuries_improving'] = len(improving_injuries)
    restricted = []
    for inj in relevant_injuries:
        restricted.extend(inj.get('restricted_activities', []))
    health_data['restricted_activities'] = sorted(set(restricted))
    health_score -= 20 * len(active_injuries)
    health_score -= 10 * len(improving_injuries)

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
    health_score = max(0, health_score)

    # Achievability (20%) — reconstruct last-28d activities from daily_loads
    start_iso = (today - timedelta(days=28)).isoformat()
    recent_activities = []
    for day_str, day_data in daily_loads.items():
        if day_str < start_iso:
            continue
        if isinstance(day_data, dict):
            recent_activities.extend(day_data.get('activities', []) or [])
    compliance = check_weekly_compliance(recent_activities)

    achievability_score = 70
    achievability_data = {
        'compliance_rate': None,
        'strength_compliant': compliance.get('strength', {}).get('compliant', True),
        'mobility_compliant': compliance.get('mobility', {}).get('compliant', True),
    }
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

    # Adaptation (10%)
    adaptation_score = 30
    adaptation_data = {
        'responses_logged': 0,
        'patterns_identified': 0,
        'positive_responses': 0,
        'negative_responses': 0,
    }
    responses = coaching_log.get('athlete_responses', [])
    adaptation_data['responses_logged'] = len(responses)
    patterns = set()
    positive_count = 0
    negative_count = 0
    for r in responses:
        if r.get('pattern'):
            patterns.add(r['pattern'])
        response_type = (r.get('response') or '').lower()
        if 'positive' in response_type or 'good' in response_type:
            positive_count += 1
        elif 'negative' in response_type or 'poor' in response_type:
            negative_count += 1
    adaptation_data['patterns_identified'] = len(patterns)
    adaptation_data['positive_responses'] = positive_count
    adaptation_data['negative_responses'] = negative_count
    if len(responses) >= 10:
        adaptation_score = 80
    elif len(responses) >= 5:
        adaptation_score = 65
    elif len(responses) >= 1:
        adaptation_score = 50

    overall_score = (
        progress_score * 0.4 +
        health_score * 0.3 +
        achievability_score * 0.2 +
        adaptation_score * 0.1
    )

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
    if achievability_data['compliance_rate'] is not None:
        if achievability_data['compliance_rate'] >= 80:
            feedback.append("High compliance suggests realistic plan")
        elif achievability_data['compliance_rate'] < 60:
            feedback.append("Low compliance - plan may be too ambitious")
    if adaptation_data['responses_logged'] < 5:
        feedback.append("Limited athlete response data - log more patterns")

    return {
        'overall_score': round(overall_score, 0),
        'trend': 'improving' if ctl_gain_4wk > 0 else 'declining' if ctl_gain_4wk < 0 else 'stable',
        'components': {
            'progress': {'score': progress_score, 'weight': '40%', 'data': progress_data},
            'health': {'score': health_score, 'weight': '30%', 'data': health_data},
            'achievability': {'score': achievability_score, 'weight': '20%', 'data': achievability_data},
            'adaptation': {'score': adaptation_score, 'weight': '10%', 'data': adaptation_data},
        },
        'feedback': feedback,
    }


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
def get_coaching_score() -> str:
    """
    Self-assessment: is the coaching working?

    Scores coaching effectiveness across 4 dimensions:
    - Progress (40%): CTL trajectory toward A-race goal
    - Health (30%): Injury status and ACWR safety
    - Achievability (20%): Compliance rate — is the plan realistic?
    - Adaptation (10%): Are athlete response patterns being logged?

    Reads from persisted files only (fitness_history.json, athlete.json,
    training_config.json, coaching_log.json) — no Garmin calls. Run the
    snapshot first if you want fresh activity data feeding the compliance
    calculation (snapshot auto-refreshes fitness_history).

    Returns:
        JSON with overall score, component breakdown, trend, and feedback.
    """
    try:
        return json.dumps(_compute_coaching_score(
            fitness_history=load_fitness_history(),
            athlete=load_athlete(),
            training_config=load_training_config(),
            coaching_log=load_coaching_log(),
            today=date.today(),
        ), indent=2)
    except Exception as e:
        logger.exception("get_coaching_score failed")
        return json.dumps({'error': str(e)})


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': True})
async def get_coaching_snapshot(ctx: Context, sections: list[str] | None = None,
                                force_refresh: bool = False) -> str:
    """
    MANDATORY FIRST CALL before any coaching recommendation.

    By default returns the CORE payload — everything needed to coach right
    now without the heavy detail: current_time_context, flags, week_grid,
    ACWR status (rolling 7d:28d primary, + legacy EWMA reference) and load
    hierarchy, active injuries,
    plan_adherence, today's + tomorrow's plan, coaching memory (recent
    decisions, pending approvals, decisions due review), open
    planned_vs_actual anomalies, a compact sleep_gate signal, and
    data_quality.

    Request named sections for drill-down detail, or ['full'] for everything:
    - plan: full week plan days, per-session comparison details, compliance
    - activities: full activity list (with per-activity HR-zone enrichment)
    - fitness: volume_data (CTL targeting), 4-week trends, intensity distribution
    - sleep: full per-night breakdown, 30-day trend, bedtime drift
    - recovery: readiness/HRV detail + personal baselines
    - strength: strength sync status + pending progressions
    - goals: race-prep / fun / aesthetics balance
    - patterns: adaptation patterns, activity patterns, 4-week compliance diagnostics
    - sport_priorities: multi-sport volume distribution
    - memory: adds learned adaptation-pattern list to coaching_memory

    Data ingestion (trailing activity window, sleep + readiness persistence)
    runs on EVERY call regardless of sections. Garmin fetches are cached for
    ~5 minutes; pass force_refresh=True to bypass the cache.

    The planned_vs_actual.anomalies array flags things that need attention:
    type mismatches, duration deltas, missing sessions, unplanned activities.
    Anomalies are PERSISTENT: each carries an id and an open/asked/resolved
    lifecycle in coaching memory. Investigate with the athlete, then record
    their answer via resolve_anomaly(id, explanation) — resolved anomalies
    stop surfacing; 'asked' ones keep surfacing with the explanation attached.

    Args:
        sections: None or ['core'] for the default payload; add any of
            plan/activities/fitness/sleep/recovery/strength/memory/goals/
            patterns/sport_priorities, or ['full'] for everything.
        force_refresh: Bypass the short-lived Garmin fetch cache.

    Returns:
        JSON with coaching context. Always check this first.
    """
    try:
        include, section_error = _resolve_snapshot_sections(sections)
        if section_error:
            return json.dumps({'error': section_error})

        now = datetime.now()
        today = now.date()
        current_time_context = build_current_time_context(now)

        # Garmin failures degrade individual sections instead of killing the
        # snapshot. AUTH_REQUIRED surfaces its actionable message in the
        # error envelope ALONGSIDE the locally derivable data.
        auth_error = None
        activities_error = None

        # 0. Activity ingestion. ALWAYS runs, regardless of sections — this
        # write side effect keeps fitness_history (and therefore CTL/ACWR)
        # alive. Re-ingests a trailing 3-day window (idempotent —
        # update_fitness_history overwrites by date), extending back to the
        # last actual ingest when ingestion has fallen behind.
        #
        # Staleness is tracked via the dedicated last_activity_ingest_date:
        # sleep/readiness persistence bumps last_updated on every snapshot,
        # which previously defeated this check and silently froze ingestion.
        history = load_fitness_history()
        last_ingest = history.get('last_activity_ingest_date')
        if not last_ingest:
            # Field absent (pre-fix histories): fall back to newest ingested day
            existing_loads = history.get('daily_loads', {})
            last_ingest = max(existing_loads.keys()) if existing_loads else None

        ingest_start = today - timedelta(days=3)
        if last_ingest:
            try:
                ingest_start = min(ingest_start, date.fromisoformat(last_ingest))
            except (ValueError, TypeError):
                pass
        else:
            ingest_start = today - timedelta(days=90)

        try:
            raw_refresh = _cached_garmin_fetch(
                ('activities_ingest', ingest_start.isoformat(), today.isoformat()),
                lambda: garmin_api_call(
                    lambda c: c.get_activities_by_date(
                        ingest_start.isoformat(), today.isoformat())
                ),
                force_refresh,
            )
            from ..parsers import parse_activities as _pa
            refreshed_activities = _pa(raw_refresh or [])
            history = update_fitness_history(refreshed_activities, today)
        except GarminAuthRequiredError as e:
            auth_error = str(e)
            logger.warning("Activity ingestion blocked: Garmin auth required")
        except Exception:
            logger.warning("Activity ingestion failed", exc_info=True)

        daily_loads = history.get('daily_loads', {})
        await ctx.report_progress(1, 10, "Fitness history loaded")

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

        # Guarded: a failed fetch degrades to locally persisted history (so
        # week_grid can't falsely show REST days) + a data_quality flag,
        # instead of aborting the whole snapshot.
        try:
            raw_activities = _cached_garmin_fetch(
                ('activities_range', fetch_start.isoformat(), today.isoformat()),
                lambda: garmin_api_call(
                    lambda c: c.get_activities_by_date(
                        fetch_start.isoformat(), today.isoformat())
                ),
                force_refresh,
            )
            all_fetched_activities = parse_activities(raw_activities)
        except GarminAuthRequiredError as e:
            auth_error = auth_error or str(e)
            activities_error = 'auth_required'
            all_fetched_activities = _activities_from_history(
                daily_loads, fetch_start.isoformat(), today.isoformat())
        except Exception as e:
            logger.warning("Activities fetch failed", exc_info=True)
            activities_error = str(e)
            all_fetched_activities = _activities_from_history(
                daily_loads, fetch_start.isoformat(), today.isoformat())
        await ctx.report_progress(3, 10, "Activities fetched")

        # Calendar week activities — for compliance, intensity distribution, etc.
        activities_this_week = [
            a for a in all_fetched_activities
            if a.get('date') and a['date'] >= monday_this_week.isoformat()
        ]

        # 3. Planned vs Actual comparison (uses full fetch range — the comparison
        # function filters by plan dates, so extra activities are harmless)
        #
        # When the plan has fully expired (ALL day entries in the past), the
        # comparison would flood the snapshot with false 'missing' anomalies.
        # Replace the flood with a single loud plan_expired signal instead.
        plan_last_day = None
        if current_plan and current_plan.get('days'):
            plan_day_dates = []
            for day_str in current_plan['days']:
                try:
                    plan_day_dates.append(date.fromisoformat(day_str))
                except (ValueError, TypeError):
                    continue
            if plan_day_dates:
                plan_last_day = max(plan_day_dates)
        plan_expired = plan_last_day is not None and plan_last_day < today
        days_since_expiry = (today - plan_last_day).days if plan_expired else 0

        sleep_history = history.get('sleep_history', [])
        if plan_expired:
            planned_vs_actual = {
                'status': 'plan_expired',
                'as_of': today.isoformat(),
                'days_since_expiry': days_since_expiry,
                'note': ('All plan days are in the past — anomaly comparison '
                         'skipped. The athlete is uncoached: build a fresh '
                         'week plan with them.'),
            }
        else:
            planned_vs_actual = _compare_planned_actual(
                current_plan, all_fetched_activities, today,
                daily_loads=daily_loads, sleep_history=sleep_history,
            )
            # Curiosity with memory: persist fresh detections (idempotent by
            # id) and surface ONLY open/asked anomalies, each carrying any
            # prior athlete_explanation. Resolved anomalies never resurface.
            # `today` is threaded so registration can enforce today-is-pending
            # and anchor summaries/days_ago (clock discipline).
            if isinstance(planned_vs_actual, dict) and 'anomalies' in planned_vs_actual:
                try:
                    planned_vs_actual['anomalies'] = register_detected_anomalies(
                        planned_vs_actual['anomalies'], today=today)
                except Exception:
                    logger.warning("Anomaly registry update failed", exc_info=True)

        # 4. Fitness metrics — overall + per-sport. ACWR at BOTH levels is the
        # rolling 7d:28d primary model (cutover 2026-06-10) — the hierarchy
        # never mixes models.
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
            overall_metrics = calculate_fitness_metrics(total_loads, today)

            # Per-sport metrics
            sport_fitness = {}
            for sport in ['cycling', 'running', 'strength']:
                sm = calculate_sport_fitness_metrics(daily_loads, sport, today)
                if sm.get('days_with_data', 0) > 0:
                    sport_fitness[sport] = {
                        'ctl': sm['ctl'], 'atl': sm['atl'],
                        'tsb': sm['tsb'], 'acwr': sm['acwr'],
                    }

            # Structured ACWR status (zone + safety boolean, no prose).
            # PRIMARY model: classic rolling 7d:28d (cutover 2026-06-10).
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
                # Legacy EWMA model — reference only since the 2026-06-10
                # cutover. Carries its own {value, zone, safe, note} block.
                'acwr_ewma': overall_metrics.get('acwr_ewma'),
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
        training_pillars = pillars_as_name_dict(athlete.get('training_pillars'))
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

        await ctx.report_progress(5, 10, "Fitness metrics calculated")

        # 6. Recovery status (today) + Sleep tracking
        try:
            readiness_data = _cached_garmin_fetch(
                ('readiness', today.isoformat()),
                lambda: garmin_api_call(
                    lambda c: c.get_training_readiness(today.isoformat())),
                force_refresh,
            )
            # Fetch dedicated HRV endpoint — readiness often returns null for hrv_status
            try:
                hrv_data = _cached_garmin_fetch(
                    ('hrv', today.isoformat()),
                    lambda: garmin_api_call(
                        lambda c: c.get_hrv_data(today.isoformat())),
                    force_refresh,
                )
            except Exception:
                logger.info("HRV data unavailable", exc_info=True)
                hrv_data = None
            recovery = _parse_readiness_for_snapshot(readiness_data, hrv_data=hrv_data)
            # Persist readiness for baseline tracking
            if recovery and recovery.get('status') != 'unavailable':
                readiness_rec = {
                    'date': today.isoformat(),
                    'score': recovery.get('score'),
                    'level': recovery.get('level'),
                    'hrv_status': recovery.get('hrv_status'),
                    'hrv_last_night_avg': recovery.get('hrv_last_night_avg'),
                    'body_battery': recovery.get('body_battery'),
                }
                history = persist_readiness_data(readiness_rec, history, today=today)
        except GarminAuthRequiredError as e:
            auth_error = auth_error or str(e)
            recovery = {'status': 'unavailable', 'note': str(e)}
        except Exception:
            logger.warning("Failed to fetch recovery data", exc_info=True)
            recovery = {'status': 'unavailable', 'note': 'Could not fetch readiness data'}

        # 6b. Sleep — ranged fetch: only nights missing from the persisted
        # sleep_history (persist_sleep_data keeps the full diary; retention is
        # config-driven), capped at the last 7 nights. Persistence runs on
        # EVERY call regardless of sections; the summary is computed from
        # persisted + fresh nights.
        known_sleep_dates = {r.get('date') for r in history.get('sleep_history', [])}
        fetched_sleep = []
        sleep_fetch_error = None
        fresh_sleep_need = None
        for offset in range(7):
            night_iso = (today - timedelta(days=offset)).isoformat()
            if night_iso in known_sleep_dates:
                continue
            try:
                payload = _cached_garmin_fetch(
                    ('sleep', night_iso),
                    lambda ds=night_iso: garmin_api_call(
                        lambda c, _ds=ds: c.get_sleep_data(_ds)),
                    force_refresh,
                )
            except GarminAuthRequiredError as e:
                auth_error = auth_error or str(e)
                break
            except Exception as e:
                logger.warning("Sleep fetch failed for %s", night_iso, exc_info=True)
                sleep_fetch_error = str(e)
                break
            record, need = parse_sleep_payload(payload, night_iso)
            if record:
                fetched_sleep.append(record)
            if offset == 0 and need:  # last night carries the personalized need
                fresh_sleep_need = dict(need, date=night_iso)

        if fetched_sleep:
            history = persist_sleep_data(fetched_sleep, history, today=today)
        if fresh_sleep_need:
            # Persisted so later same-day snapshots keep the personalized
            # target even though last night is no longer re-fetched.
            history['sleep_need'] = fresh_sleep_need

        stored_need = history.get('sleep_need') or None
        if stored_need and stored_need.get('date', '') < (today - timedelta(days=1)).isoformat():
            stored_need = None  # stale personalized need — use default target
        merged_nights = _merge_sleep_nights(
            history.get('sleep_history', []), fetched_sleep, today)
        sleep_data = summarize_sleep_records(merged_nights, sleep_need=stored_need)

        # Save history once (covers both readiness + sleep persistence)
        save_fitness_history(history)

        # 6c. Sleep trend (30-day from persisted data) + bedtime drift (14-day window)
        sleep_trend_30d = get_sleep_trend(history, days=30, today=today)
        bedtime_drift = detect_bedtime_drift(history.get('sleep_history', []))

        # 6d. Readiness baselines (personal norms)
        readiness_baselines = calculate_readiness_baselines(
            history.get('sleep_history', []),
            history.get('readiness_history', []),
            today,
        )

        await ctx.report_progress(7, 10, "Recovery and sleep analyzed")

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
            methodology.get('race_templates', {}),
            today,
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

        # 9. Intensity distribution (last 7 days). Per-activity HR-zone
        # enrichment is an N-call Garmin drill-down — only pay for it when
        # the activity detail is actually being returned (sections includes
        # 'activities' or 'full'); otherwise the distribution falls back to
        # avg-HR classification.
        if 'activities' in include and not activities_error:
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
            overall_trend = get_fitness_trend(28, today=today)
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
            race_sport = race_sport_for(race_type)

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
                current_weekly_tss=last_week_tss if last_week_tss > 0 else None,
                today=today,
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
            'current_time_context': current_time_context,
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

            'week_grid': _build_week_grid(all_fetched_activities, today, daily_loads),

            # The ISO date the grid (and its days_ago fields) anchors to
            'week_grid_today': today.isoformat(),

            'plan_adherence': _summarize_plan_adherence_by_pillar(
                current_plan, all_fetched_activities, today),

            'goal_progress': _summarize_goal_progress(
                all_fetched_activities, load_training_config(), today),

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
                'bedtime_drift': bedtime_drift if bedtime_drift.get('status') == 'ok' else None,
            } if sleep_data else {'status': 'no_data'},

            'sleep_gate': _build_sleep_gate(sleep_data),

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
        # Staleness based on actual activity ingestion (last_updated is bumped
        # by sleep/readiness persistence and can't signal frozen ingestion)
        last_ingest_check = history.get('last_activity_ingest_date')
        if not last_ingest_check:
            final_loads = history.get('daily_loads', {})
            last_ingest_check = max(final_loads.keys()) if final_loads else None
        if last_ingest_check and last_ingest_check < (today - timedelta(days=1)).isoformat():
            data_quality['fitness_history'] = 'stale'
        if plan_expired:
            data_quality['plan_stale'] = True
            data_quality['plan_days_since_expiry'] = days_since_expiry
        if activities_error:
            data_quality['activities_unavailable'] = (
                f'{activities_error} (week view rebuilt from locally '
                f'persisted fitness_history)'
            )
        if sleep_fetch_error:
            data_quality['sleep_fetch_error'] = sleep_fetch_error
        if auth_error:
            data_quality['garmin_auth'] = 'required'
        # Season-layer config validation (Phase 3): invalid block dates,
        # A-race in the past, no upcoming events, overdue phase transition.
        # Flags the coach must SURFACE to the athlete — never auto-fixed.
        try:
            data_quality.update(validate_season_config(training_config, today))
        except Exception:
            logger.warning("Season config validation failed", exc_info=True)
        if data_quality:
            snapshot['data_quality'] = data_quality

        # Season lifecycle: race-passed debriefs + overdue phase transitions
        # auto-propose ONCE through the normal approval machinery (idempotent
        # by event tag) — fresh proposals surface in coaching_memory
        # pending_approvals below; the athlete approves/rejects as usual.
        try:
            _season_lifecycle_proposals(training_config, today)
        except Exception:
            logger.warning("Season lifecycle proposal generation failed",
                           exc_info=True)

        # Coaching memory (continuity across sessions).
        # Decisions/responses are stored append-only (oldest first) — surface
        # the MOST RECENT entries, newest first, not the oldest.
        # Loading decisions runs the review lifecycle first: active decisions
        # past their review_date persist to 'needs_review' (idempotent), and
        # decisions_due_review carries their actual summaries, not a count.
        try:
            decision_log, _transitioned = auto_transition_due_decisions(today)
            coaching_ctx = get_coaching_context(today)
            active_decisions = sorted(
                coaching_ctx.get('active_decisions', []),
                key=lambda d: d.get('date') or '',
                reverse=True,
            )
            recent_responses = sorted(
                coaching_ctx.get('recent_responses', []),
                key=lambda r: r.get('date') or '',
                reverse=True,
            )
            snapshot['coaching_memory'] = {
                'active_decisions': active_decisions[:5],
                'pending_approvals': coaching_ctx.get('pending_approvals', []),
                'decisions_due_review': summarize_decisions_due_review(
                    decision_log.get('decisions', []), today)[:10],
                'adaptation_patterns': coaching_ctx.get('response_patterns', []),
                'recent_responses': recent_responses[:3],
            }
        except Exception:
            logger.warning("Failed to load coaching memory", exc_info=True)
            snapshot['coaching_memory'] = {'status': 'unavailable'}

        await ctx.report_progress(9, 10, "Building snapshot")

        # Snapshot flags — quick-scan summary for the LLM (built from the
        # FULL internal snapshot so core flags still see everything)
        flags = _build_snapshot_flags(snapshot, today)
        if flags:
            snapshot['flags'] = flags

        # Filter down to core + requested sections (no indent — the payload
        # is for an LLM, pretty-printing only burns tokens)
        payload = _assemble_snapshot_payload(snapshot, include)

        # Error envelope: Garmin trouble surfaces loudly, but ALONGSIDE the
        # locally derivable data (plan, injuries, memory, time context).
        if auth_error:
            payload = {'error': auth_error, **payload}
        elif activities_error:
            payload = {
                'error': (f'Garmin activities unavailable: {activities_error}. '
                          f'Snapshot degraded to locally persisted data.'),
                **payload,
            }
        return json.dumps(payload)

    except Exception as e:
        logger.exception("get_coaching_snapshot failed")
        return json.dumps({'error': str(e)})
