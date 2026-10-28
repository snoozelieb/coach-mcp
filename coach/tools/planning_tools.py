"""Planning tools - weekly plan management, periodization, prescriptions, Garmin push."""

from ..mcp_app import mcp
from ..garmin_client import garmin_api_call, schedule_workout
from ..parsers import parse_activities, parse_training_readiness
from ..session_state import require_snapshot
from ..planner import (get_current_plan, save_weekly_plan, merge_plan_fields,
                     append_decision,
                     create_empty_week_template,
                     load_athlete, load_coaching_log, save_coaching_log,
                     load_json_file, save_json_file,
                     get_week_constraints as _get_week_constraints,
                     PLAN_RETENTION_DAYS)
from ..rules import (load_training_config, check_weekly_compliance,
                   pillars_as_name_dict, normalize_injury)
from ..fitness import (load_fitness_history, calculate_fitness_metrics,
                     _extract_total_loads, derive_sport_capacity,
                     demonstrated_max_mins)
from ..schemas import (WeeklyPlan as WeeklyPlanSchema,
                     TrainingProgram as TrainingProgramSchema)
from ..taxonomy import workout_family_for, types_match, capacity_bucket_for, CAPACITY_BUCKETS, is_known_type
from ..config import DATA_DIR, TRAINING_CONFIG_FILE, TRAINING_PROGRAM_FILE
from ..authoring import contract_document, exercise_matches_restriction
from ..workout_builder import build_workout_report, get_workout_type_name
from datetime import date, timedelta
from pydantic import ValidationError
import copy
import hashlib
import json
import logging
import secrets
import threading
import time

logger = logging.getLogger(__name__)

# Fat-finger guard: reject plan day keys further out than this.
PLAN_MAX_FUTURE_DAYS = 21

# ---------------------------------------------------------------------------
# Grounding thresholds (VERIFY BEFORE PRESCRIBING)
# ---------------------------------------------------------------------------
# Weekly-plan grounding is WARN-only: taper, rehab, and bone-load protocols
# legitimately sit below demonstrated capacity — the warning asks whether the
# regression is deliberate instead of blocking the save. The training_program
# capacity-anchor gate (below) is the HARD enforcement: a multi-week program
# must state the demonstrated capacity it anchors to, and the server verifies
# the claim against actual history.
GROUNDING_REGRESSION_FACTOR = 0.6    # planned week max < 60% of demonstrated
GROUNDING_SPIKE_FACTOR = 1.3         # planned week max > 130% of demonstrated
GROUNDING_MIN_REFERENCE_MINS = 20.0  # ignore tiny references (noise floor)
GROUNDING_MIN_UNKNOWN_MINS = 30.0    # no-history warning floor
ANCHOR_TOLERANCE = 0.25              # claimed vs server-derived capacity
PROGRAM_MAX_WEEKS = 52
PROGRAM_MAX_FUTURE_START_DAYS = 90
PROGRAM_HISTORY_KEEP = 10
PROGRAM_WEEKLY_INCREASE_WARN_PCT = 25.0


def _norm_type(value) -> str:
    """Lowercase + underscore normalization (matches taxonomy lookups)."""
    return str(value or '').strip().lower().replace(' ', '_').replace('-', '_')


def _activity_matches_restriction(session_type: str, restriction: str) -> bool:
    """True when a planned session type violates an injury restriction.

    Two passes:
    1. Taxonomy: types_match() catches alias spellings a substring check
       misses — 'long_ride' vs restricted 'cycling', 'run' vs 'trail_running'.
    2. Substring fallback (normalized) so free-text restrictions still catch —
       'no running' matches 'running', 'no high-impact' matches 'high_impact'.
    """
    s = _norm_type(session_type)
    r = _norm_type(restriction)
    if not s or not r:
        return False
    if types_match(s, r):
        return True
    return s in r or r in s


def _iter_plan_sessions(day_data: dict):
    """Yield every session dict for a plan day, including nested 'sessions' lists."""
    planned = day_data.get('planned') if isinstance(day_data, dict) else None
    if not planned:
        return
    sessions = planned if isinstance(planned, list) else [planned]
    for session in sessions:
        if not isinstance(session, dict):
            continue
        yield session
        nested = session.get('sessions')
        if isinstance(nested, list):
            for sub in nested:
                if isinstance(sub, dict):
                    yield sub


def _iter_session_exercises(session: dict):
    """(name, category) for every exercise in a session, circuits included."""
    for item in session.get('exercises') or []:
        if not isinstance(item, dict):
            continue
        members = item.get('exercises') if isinstance(item.get('exercises'), list) else [item]
        for ex in members:
            if isinstance(ex, dict):
                yield _norm_type(ex.get('name')), _norm_type(ex.get('category'))


def _exercise_restriction_hits(session: dict, restrictions: list) -> list[dict]:
    """Movement-level matches: every content word of an active injury's
    restriction ('no jumping' -> jump; 'overhead pressing' -> overhead, press)
    found among the tokens of an exercise's Garmin key or category
    (coach.authoring.exercise_matches_restriction: stemmed tokens, Garmin
    synonyms such as PLYO for 'jumping', qualifiers like 'heavy' dropped so
    the gate fails safe). Sport words ('running', 'cycling') are judged on
    the session type, not on exercise names, so a 'cycling' restriction never
    trips on a core movement that happens to carry the word."""
    movement_restrictions = [r for r in restrictions if not is_known_type(r)]
    if not movement_restrictions:
        return []
    hits = []
    for name, category in _iter_session_exercises(session):
        matched = [r for r in movement_restrictions
                   if exercise_matches_restriction(name, category, r)]
        if matched:
            hits.append({'exercise': name.upper(), 'matched_restrictions': matched})
    return hits


def _injury_gate_violations(plan_days: dict, injuries: list) -> list[dict]:
    """Find non-rest sessions whose type intersects an active/improving injury's
    restricted_activities, and strength exercises whose Garmin key or category
    carries a restricted movement word.

    Types are checked on every session, nested lists included. Exercises are
    checked on the sessions exactly as the push expands them
    (_expand_push_sessions: a container's exercises land on its nested
    strength session), so a 'rest'-typed container that carries a gym list
    cannot smuggle a restricted movement past the gate.

    Returns a list of {date, session_type, injury, injury_status,
    matched_restrictions[, exercise]} dicts — empty when the plan is safe.
    """
    gating = [
        normalize_injury(i) for i in (injuries or [])
        if isinstance(i, dict) and i.get('status') in ('active', 'improving')
    ]
    gating = [i for i in gating if i['restricted_activities']]
    if not gating:
        return []

    violations = []
    for day_str, day_data in (plan_days or {}).items():
        for session in _iter_plan_sessions(day_data):
            session_type = str(session.get('type', '')).lower()
            if (not session_type or 'rest' in session_type
                    or workout_family_for(session_type) == 'rest'):
                continue
            for injury in gating:
                matched = [
                    r for r in injury['restricted_activities']
                    if _activity_matches_restriction(session_type, r)
                ]
                if matched:
                    violations.append({
                        'date': day_str,
                        'session_type': session.get('type'),
                        'injury': injury['name'],
                        'injury_status': injury['status'],
                        'matched_restrictions': matched,
                    })
        if not isinstance(day_data, dict):
            continue
        for session in _expand_push_sessions(copy.deepcopy(day_data)):
            for injury in gating:
                for hit in _exercise_restriction_hits(session, injury['restricted_activities']):
                    violations.append({
                        'date': day_str,
                        'session_type': session.get('type'),
                        'exercise': hit['exercise'],
                        'injury': injury['name'],
                        'injury_status': injury['status'],
                        'matched_restrictions': hit['matched_restrictions'],
                    })
    return violations


def _planned_sport_maxima(plan_days: dict) -> dict[str, dict]:
    """Longest planned session per capacity bucket.

    Returns {bucket: {'planned_max_mins', 'date', 'session_type'}} for every
    duration-prescribed sport in the plan (taxonomy.capacity_bucket_for);
    rest days and sessions without a numeric duration are skipped.
    """
    maxima: dict[str, dict] = {}
    for day_str, day_data in (plan_days or {}).items():
        for session in _iter_plan_sessions(day_data):
            session_type = str(session.get('type', '')).lower()
            if (not session_type or 'rest' in session_type
                    or workout_family_for(session_type) == 'rest'):
                continue
            try:
                mins = float(session.get('duration_mins') or 0)
            except (TypeError, ValueError):
                continue
            if mins <= 0:
                continue
            bucket = capacity_bucket_for(session_type)
            if bucket is None:
                continue
            if (bucket not in maxima
                    or mins > maxima[bucket]['planned_max_mins']):
                maxima[bucket] = {
                    'planned_max_mins': mins,
                    'date': day_str,
                    'session_type': session.get('type'),
                }
    return maxima


def _grounding_warnings(plan_days: dict, today: date) -> list[dict]:
    """VERIFY BEFORE PRESCRIBING: plan vs demonstrated capacity, per sport.

    Compares the plan's longest session in each sport against the athlete's
    demonstrated recent capacity (derive_sport_capacity over persisted
    daily_loads). Warn-only — a failure here never blocks a plan save, and
    any internal error degrades to an empty list.
    """
    try:
        history = load_fitness_history()
        capacity = derive_sport_capacity(history.get('daily_loads', {}), today)
    except Exception:
        logger.warning("Grounding check skipped: capacity derivation failed",
                       exc_info=True)
        return []

    by_sport = capacity.get('by_sport', {})
    window_days = capacity.get('window_days', 90)
    warnings = []
    for bucket, planned in sorted(_planned_sport_maxima(plan_days).items()):
        planned_max = planned['planned_max_mins']
        demonstrated, window = demonstrated_max_mins(by_sport.get(bucket))

        if demonstrated is None:
            if planned_max >= GROUNDING_MIN_UNKNOWN_MINS:
                warnings.append({
                    'type': 'no_demonstrated_history',
                    'sport': bucket,
                    'planned_max_mins': planned_max,
                    'message': (
                        f"No completed {bucket} session in the last "
                        f"{window_days} days, but the plan's longest {bucket} "
                        f"session is {planned_max:.0f} min "
                        f"({planned['date']}). Return-to-sport progressions "
                        f"start small — verify with the athlete."),
                })
            continue
        if demonstrated < GROUNDING_MIN_REFERENCE_MINS:
            continue

        window_label = ('last 28 days' if window == '28d'
                        else f'last {window_days} days')
        if planned_max < demonstrated * GROUNDING_REGRESSION_FACTOR:
            warnings.append({
                'type': 'capacity_regression',
                'sport': bucket,
                'planned_max_mins': planned_max,
                'demonstrated_max_mins': demonstrated,
                'message': (
                    f"This plan's longest {bucket} session is "
                    f"{planned_max:.0f} min but the athlete demonstrated "
                    f"{demonstrated:.0f} min in the {window_label}. If the "
                    f"regression is deliberate (taper, rehab, bone-load "
                    f"protocol), say so in the plan rationale; otherwise "
                    f"re-anchor to demonstrated capacity "
                    f"(snapshot.capacity)."),
            })
        elif planned_max > demonstrated * GROUNDING_SPIKE_FACTOR:
            warnings.append({
                'type': 'session_spike',
                'sport': bucket,
                'planned_max_mins': planned_max,
                'demonstrated_max_mins': demonstrated,
                'message': (
                    f"This plan's longest {bucket} session is "
                    f"{planned_max:.0f} min vs {demonstrated:.0f} min "
                    f"demonstrated in the {window_label}. Large "
                    f"single-session jumps drive injury risk — progress "
                    f"~10-15% per week from demonstrated capacity."),
            })
    return warnings


# Union-branch markers pydantic inserts into error locs for
# PlanDay.planned's Union[list[Session], Session, None] — stripped when
# naming the offending field, and used to drop bare shape-mismatch noise
# when a more specific field-level error exists for the same day.
_UNION_LOC_MARKERS = frozenset({
    'Session', 'list[Session]', 'PlanDay',
    'SessionExercise', 'list[SessionExercise]',
})


def _format_plan_validation_errors(exc: ValidationError) -> list[dict]:
    """Convert a pydantic ValidationError into problems naming day + field.

    Each problem is {'day': 'YYYY-MM-DD' | None, 'field': 'planned.type' | ...,
    'message': str}. Union branches (single Session vs list[Session]) each
    report their own miss — when a day has field-level errors, the bare
    shape-mismatch errors from the other branch are dropped as noise.
    """
    raw = []
    for err in exc.errors():
        loc = err.get('loc', ())
        day = None
        field_loc = loc
        if len(loc) >= 2 and loc[0] == 'days':
            day = str(loc[1])
            field_loc = loc[2:]
        parts = [str(p) for p in field_loc if str(p) not in _UNION_LOC_MARKERS]
        # An error terminating AT a union marker is a branch-shape mismatch,
        # not a field-level problem.
        specific = bool(loc) and str(loc[-1]) not in _UNION_LOC_MARKERS
        raw.append({
            'day': day,
            'field': '.'.join(parts) or None,
            'message': err.get('msg', 'invalid value'),
            '_specific': specific,
        })

    days_with_specific = {p['day'] for p in raw if p['_specific']}
    problems = []
    seen = set()
    for p in raw:
        if not p['_specific'] and p['day'] in days_with_specific:
            continue
        key = (p['day'], p['field'], p['message'])
        if key in seen:
            continue
        seen.add(key)
        problems.append({'day': p['day'], 'field': p['field'], 'message': p['message']})
    return problems


def _authoring_problems(plan_days: dict) -> tuple[list[dict], list[dict]]:
    """(errors, warnings) from rendering every pushable session exactly as
    push_plan_to_garmin will — same expansion of legacy nested days, same
    renderer — so the save gate and the push gate cannot disagree.

    Each entry: {date, session (type/name), field, message}. Sessions Garmin
    has no sport for (skipped at push) are not errors here.
    """
    errors, warnings = [], []
    for day_str, day_data in sorted((plan_days or {}).items()):
        if not isinstance(day_data, dict) or not day_data.get('planned'):
            continue
        for session in _expand_push_sessions(copy.deepcopy(day_data)):
            report = build_workout_report(session, day_str)
            if report['workout_type'] in ('skipped', 'unknown'):
                continue
            label = session.get('name') or session.get('description') or session.get('type')
            for problem in report['errors']:
                errors.append({'date': day_str, 'session': label, **problem})
            for problem in report['warnings']:
                warnings.append({'date': day_str, 'session': label, **problem})
    return errors, warnings


def _missing_purpose_sessions(plan_days: dict) -> list[dict]:
    """Non-rest sessions whose 'purpose' is missing or blank.

    This is the PURPOSE GATE (Phase 3): update_weekly_plan rejects the save
    when this list is non-empty; there is no bypass (override_purpose_gate is
    deprecated and ignored). Every
    non-rest session must say WHY it exists. Wrapper sessions that just hold
    a nested 'sessions' list are skipped — their leaf sessions are checked
    instead.
    """
    warnings = []
    for day_str, day_data in sorted((plan_days or {}).items()):
        for session in _iter_plan_sessions(day_data):
            if isinstance(session.get('sessions'), list):
                continue  # container for sub-sessions, not a session itself
            session_type = str(session.get('type', '')).lower()
            if (not session_type or 'rest' in session_type
                    or workout_family_for(session_type) == 'rest'):
                continue
            purpose = session.get('purpose')
            if not (isinstance(purpose, str) and purpose.strip()):
                warnings.append({
                    'date': day_str,
                    'type': session.get('type'),
                    'name': session.get('name'),
                    'warning': 'missing purpose',
                })
    return warnings


def _plan_date_error(plan_days: dict, today: date) -> dict | None:
    """Today-anchored plan date validation. Returns an error dict or None.

    Rejects plans whose days are ALL in the past (stale plan re-saved as
    current) and plans containing any day key more than PLAN_MAX_FUTURE_DAYS
    days in the future (fat-finger guard). Day keys are already ISO-validated
    by the schema; unparseable keys are ignored here.
    """
    parsed = {}
    for key in (plan_days or {}):
        try:
            parsed[key] = date.fromisoformat(key)
        except (ValueError, TypeError):
            continue
    if not parsed:
        return None

    if all(d < today for d in parsed.values()):
        return {
            'error': 'plan_dates',
            'message': 'plan is entirely historical — build a current week',
            'plan_days': sorted(parsed),
            'today': today.isoformat(),
        }

    horizon = today + timedelta(days=PLAN_MAX_FUTURE_DAYS)
    too_far = sorted(k for k, d in parsed.items() if d > horizon)
    if too_far:
        return {
            'error': 'plan_dates',
            'message': (f'plan contains day(s) more than {PLAN_MAX_FUTURE_DAYS} '
                        'days in the future — check the dates'),
            'offending_days': too_far,
            'latest_allowed': horizon.isoformat(),
        }
    return None


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
def get_periodization_status() -> dict:
    """
    Where are we in the season? Current phase, progress, and what comes next.

    Returns current phase (base/build/peak/taper), week within phase, days
    until A-race, remaining phases, fitness trajectory, and phase-specific
    guidance (key sessions, intensity targets, volume trend).

    The current phase determines training priorities: base = volume over
    intensity, build = race-specific intensity, peak = race simulation,
    taper = sharp volume reduction. Use this to ensure weekly plans align
    with the season plan.
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

        # Get fitness status if available. daily_loads is the v2 dict format —
        # flatten to {date: total} before calculating metrics.
        fitness_metrics = None
        fitness_metrics_unavailable = False
        try:
            history = load_fitness_history()
            daily_loads = history.get('daily_loads', {})
            if daily_loads:
                fitness_metrics = calculate_fitness_metrics(
                    _extract_total_loads(daily_loads), today)
        except Exception:
            logger.warning("Could not calculate fitness metrics for periodization status",
                           exc_info=True)
            fitness_metrics_unavailable = True

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
        elif fitness_metrics_unavailable:
            result['fitness_metrics_unavailable'] = True

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

        return result

    except Exception as e:
        logger.exception("get_periodization_status failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': True})
def get_weekly_prescription() -> dict:
    """
    What should this week look like? Volume, intensity, and key sessions.

    Combines phase demands, fitness status (CTL/ACWR/TSB), recovery readiness,
    and pillar compliance into a weekly prescription. Volume is auto-adjusted:
    reduced if ACWR > 1.3 (injury risk), increased if ACWR < 0.8 (undertrained).

    The prescription is a starting point — adapt it through conversation with
    the athlete based on how they're feeling, life events, and injuries.

    The volume target sizes the WEEK's load. Individual session durations
    anchor to `recent_capacity` (demonstrated per-sport capacity from
    completed sessions) — never to a template number.

    Returns target volume, key sessions, recent_capacity, intensity targets,
    pillar priorities, injury constraints, life events, and flexibility notes.
    """
    try:
        today = date.today()

        # Load all context
        config = load_training_config()
        athlete = load_athlete()
        periodization = config.get('periodization', {})
        current_block = config.get('current_block', {})

        # Get current phase info
        current_phase = periodization.get('current_phase', current_block.get('phase', 'base'))
        phases = periodization.get('phases', {})
        phase_info = phases.get(current_phase, {})

        # Get fitness metrics. daily_loads is the v2 dict format — flatten to
        # {date: total} first, otherwise the ACWR volume adjustment never fires.
        fitness_metrics = None
        fitness_metrics_unavailable = False
        daily_loads = {}
        try:
            history = load_fitness_history()
            daily_loads = history.get('daily_loads', {})
            if daily_loads:
                fitness_metrics = calculate_fitness_metrics(
                    _extract_total_loads(daily_loads), today)
        except Exception:
            logger.warning("Could not calculate fitness metrics for weekly prescription",
                           exc_info=True)
            fitness_metrics_unavailable = True

        # Demonstrated per-sport capacity — the volume target below sizes the
        # WEEK's load; individual session durations must anchor here.
        recent_capacity = None
        try:
            if daily_loads:
                recent_capacity = derive_sport_capacity(daily_loads, today)
        except Exception:
            logger.warning("Could not derive capacity for weekly prescription",
                           exc_info=True)

        # Get today's readiness
        readiness_data = garmin_api_call(lambda c: c.get_training_readiness(today.isoformat()))
        readiness = parse_training_readiness(readiness_data)

        # Get recent compliance
        start_7_days = today - timedelta(days=7)
        raw_activities = garmin_api_call(
            lambda c: c.get_activities_by_date(
                start_7_days.isoformat(),
                today.isoformat()
            )
        )
        recent_activities = parse_activities(raw_activities)
        compliance = check_weekly_compliance(recent_activities)

        # Calculate prescribed volume
        base_volume_hrs = current_block.get('weekly_volume_target_hrs', 6.0)

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

        # Check for active injuries (normalized — tolerates old/new record shapes)
        injury_constraints = []
        injuries = athlete.get('injury_history', [])
        for injury in injuries:
            if isinstance(injury, dict) and injury.get('status') == 'active':
                norm = normalize_injury(injury)
                injury_constraints.append({
                    'injury': norm['type'],
                    'restricted': norm['restricted_activities'],
                    'safe': norm['safe_activities'],
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
                'note': ("These sessions drive adaptation - prioritize them. "
                         "Size their durations from recent_capacity below, "
                         "never from a template."),
            },
            'recent_capacity': {
                'by_sport': (recent_capacity or {}).get('by_sport', {}),
                'window_days': (recent_capacity or {}).get('window_days'),
                'note': ("Demonstrated capacity from completed sessions "
                         "(VERIFY BEFORE PRESCRIBING). The volume target "
                         "above sizes the week's LOAD; every individual "
                         "session duration anchors HERE — e.g. a long run "
                         "progresses 10-15%/week from the demonstrated "
                         "longest recent run, wherever that is."),
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
        elif fitness_metrics_unavailable:
            prescription['fitness_metrics_unavailable'] = True

        return prescription

    except Exception as e:
        logger.exception("get_weekly_prescription failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': False, 'openWorldHint': False})
def update_phase(new_phase: str, notes: str = None) -> dict:
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
        return {
            'error': f"Invalid phase '{new_phase}'. Valid phases: {valid_phases}"
        }

    try:
        today = date.today()
        gate = require_snapshot('update_phase', today)
        if gate:
            return gate
        state: dict = {}

        def _apply(config: dict) -> None:
            state['old_phase'] = config.get('periodization', {}).get('current_phase', 'unknown')
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
            state['phase_info'] = phases.get(new_phase.lower(), {})
            if state['phase_info']:
                config['current_block']['focus'] = state['phase_info'].get('key_sessions', [])
                config['current_block']['notes'] = state['phase_info'].get('focus', f'{new_phase} phase')

        # Locked read-modify-write (atomic, UTF-8, migrated): the file is
        # shared with other server processes; the bare open()/dump it
        # replaces skipped all of that.
        from ..storage import update_json
        update_json(TRAINING_CONFIG_FILE, _apply, data_dir=DATA_DIR)
        old_phase = state['old_phase']
        phase_info = state['phase_info']

        # Log the decision
        try:
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

        return {
            'status': 'success',
            'transition': {
                'from': old_phase,
                'to': new_phase.lower(),
                'date': today.isoformat(),
            },
            'phase_info': phase_info if phase_info else 'Custom phase - no template',
            'notes': notes,
        }

    except Exception as e:
        logger.exception("update_phase failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
def get_weekly_plan() -> dict:
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
            plan = create_empty_week_template(date.today())  # tool boundary
        return plan
    except Exception as e:
        logger.exception("get_weekly_plan failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': False})
def update_weekly_plan(plan: dict | None = None,
                       override_injury_gate: bool = False,
                       plan_json: str | None = None,
                       override_purpose_gate: bool = False,
                       athlete_statement: str | None = None,
                       override_rationale: str | None = None) -> dict:
    """
    Saves a new or updated weekly training plan — the rolling 7-day plan,
    at most 21 days ahead. Every save runs five code-enforced checks.

    GATES AND ERROR CODES

    - snapshot_required / snapshot_stale: SNAPSHOT FIRST — a
      get_coaching_snapshot() from this session, today, under 2 hours old
      must precede the save.
    - purpose_gate: REJECTED when any non-rest session lacks a non-empty
      'purpose' (why this session, this day, this athlete). Nested
      'sessions' lists are checked at the leaf; rest days are exempt.
      override_purpose_gate is DEPRECATED and ignored — a session you cannot
      explain does not belong on the plan; an athlete-choice day uses
      intensity 'discretion' and still states its purpose.
    - injury_gate: REJECTED when a non-rest session's type intersects an
      active/improving injury's restricted_activities (taxonomy-aware:
      'long_ride' is caught by 'cycling'; free text matches by substring)
      or a strength exercise's key/category carries a restricted movement
      word ('jump', 'overhead', 'shoulder_press'); the violation names it.
      override_injury_gate=True needs athlete_statement (the athlete's own
      words) and override_rationale, logged as a 'gate_override' decision;
      without them: 'override_requires_confirmation'. A plan failing both
      gates reports both (injury_gate carries a 'purpose_gate' section).
    - plan_dates: REJECTED when every day is in the past or any day is
      more than 21 days out.
    - authoring_error: REJECTED when a session's 'structure' (runs, rides)
      or 'exercises' (strength) breaks the session contract — an unknown
      key, a phase without an end condition, an exercise without sets or
      reps/duration_secs, an intensity outside the vocabulary. The error
      names the field and the allowed keys; nothing is defaulted.
    - grounding_warnings (VERIFY BEFORE PRESCRIBING; save succeeds): the
      plan's longest session per sport against DEMONSTRATED capacity
      (snapshot.capacity): capacity_regression (< 60 % of the recent max —
      say why in the rationale), session_spike (> 130 %),
      no_demonstrated_history (30+ min in a sport with no completed
      session in the window). Address each before pushing.
    - authoring_warnings (save succeeds): what the watch will not show as
      written (off-catalogue name, cue over 50 chars, no load stated while
      the baseline has one, no warm-up before loaded compounds).
    - validation_error: the dict failed the typed schema (ISO day keys;
      every session needs a string 'type').

    PLAN STRUCTURE (plan)

        {'days': {'YYYY-MM-DD': {'planned': {...} or [{...}, ...],
                                 'notes': '...'}, ...},
         'rationale': 'Why this plan, for this athlete'}

        Session: 'type' (REQUIRED), 'purpose' (REQUIRED unless rest),
        'duration_mins', 'intensity', 'name' (Garmin workout name, 40
        chars), 'description', 'structure' (runs, rides), 'exercises'
        (strength), 'warmup_mins'/'cooldown_mins'. 'discretion' as the
        intensity marks an athlete-choice day; its 'constraints' list goes
        on the watch.
        'planned' may be a LIST when a day has two or three workouts; each
        is pushed and counted on its own.

    SESSION CONTRACT (preview_workout renders it)

        The renderer draws what the session says and defaults nothing.
        'structure': phases {phase: warmup|interval|recovery|
        cooldown|rest; ONE of duration_secs|duration_mins|distance_m, or
        "open" for a lap-button step; ONE target: intensity recovery|easy|
        tempo|sweet_spot|threshold|vo2|max_effort, hr_target [lo,hi],
        pace_sec_per_km [slow,fast], power_watts [lo,hi], power_pct,
        cadence [lo,hi]; notes} and repeat blocks {phase: 'repeat',
        iterations, steps}. 'exercises': {name (Garmin key), category,
        sets, reps or duration_secs, each_side, weight_kg, rest_secs,
        notes} or circuits {rounds, exercises, rest_secs}. Without
        structure a session is one block (runs/indoor rides: warm-up, main,
        cool-down). preview_workout() returns the full contract.

    LIFECYCLE: saved as supplied (pushed_workout_ids preserved); days older
    than 9 days are pruned to plan_history.json.

    Args:
        plan: the plan dict (preferred). plan_json: DEPRECATED JSON string.
        override_injury_gate + athlete_statement + override_rationale:
        informed consent for a restricted session. override_purpose_gate:
        DEPRECATED, ignored.

    Returns:
        {'status', 'purpose_warnings', 'grounding_warnings',
        'authoring_warnings'} or a structured error ('validation_error',
        'plan_dates', 'purpose_gate', 'injury_gate', 'authoring_error').
    """
    try:
        gate = require_snapshot('update_weekly_plan', date.today())
        if gate:
            return gate
        purpose_override_requested = bool(override_purpose_gate)
        if purpose_override_requested:
            logger.warning("override_purpose_gate is deprecated and ignored")
        override_purpose_gate = False
        statement = (athlete_statement or '').strip()
        why = (override_rationale or '').strip()
        if override_injury_gate and not (statement and why):
            return {
                'error': 'override_requires_confirmation',
                'message': ("override_injury_gate needs the athlete's own words "
                            "(athlete_statement) and override_rationale — nothing "
                            "was saved."),
                'hint': ("Quote the athlete's agreement in athlete_statement and give "
                         "the evidence in override_rationale; both are logged as a "
                         "'gate_override' decision."),
            }

        # Tolerate legacy positional calls that pass the JSON string as the
        # first argument (pre-typed clients).
        if isinstance(plan, str) and plan_json is None:
            plan, plan_json = None, plan

        # Exactly one of plan / plan_json must be provided.
        if plan is not None and plan_json is not None:
            return {'error': "Provide either 'plan' (structured dict) or "
                             "'plan_json' (deprecated JSON string), not both"}
        if plan is None and plan_json is None:
            return {'error': "No plan provided — pass 'plan' as a structured "
                             "dict (preferred) or 'plan_json' as a JSON string "
                             "(deprecated)"}
        if plan_json is not None:
            plan = json.loads(plan_json)

        # Validate plan structure
        if not isinstance(plan, dict):
            return {'error': 'Plan must be a JSON object, not ' + type(plan).__name__}
        if 'days' not in plan:
            return {'error': "Plan must contain a 'days' key"}
        if not isinstance(plan['days'], dict):
            return {'error': "'days' must be a dict keyed by YYYY-MM-DD date strings"}

        # Typed schema validation: day keys must be ISO dates, every session
        # needs at least a string 'type'. The raw dict (not a model dump) is
        # what gets saved — validation gates, it never rewrites.
        try:
            WeeklyPlanSchema.model_validate(plan)
        except ValidationError as exc:
            problems = _format_plan_validation_errors(exc)
            logger.warning("update_weekly_plan rejected by schema validation: %s",
                           problems)
            return {
                'error': 'validation_error',
                'message': 'Plan failed schema validation',
                'problems': problems,
                'hint': ("Day keys must be YYYY-MM-DD ISO dates and every "
                         "planned session needs at least a string 'type'."),
            }

        # Date sanity gate: an entirely-historical plan or a fat-fingered
        # future date is a structural problem — reject before the coaching
        # gates run.
        today = date.today()
        date_error = _plan_date_error(plan['days'], today)
        if date_error:
            return date_error

        # Coaching gates run on the days that will survive the save's
        # pruning (entries older than PLAN_RETENTION_DAYS get archived) —
        # a gate shouldn't reject on a day about to be pruned anyway.
        prune_cutoff = (today - timedelta(days=PLAN_RETENTION_DAYS)).isoformat()
        gated_days = {d: v for d, v in plan['days'].items()
                      if not isinstance(d, str) or d >= prune_cutoff}

        # Purpose gate: every non-rest session must say WHY it exists.
        missing_purpose = _missing_purpose_sessions(gated_days)
        # Injury write-gate: never save sessions that violate active/improving
        # injury restrictions unless explicitly overridden.
        injuries = load_athlete().get('injury_history', [])
        violations = _injury_gate_violations(gated_days, injuries)

        purpose_blocked = bool(missing_purpose) and not override_purpose_gate
        injury_blocked = bool(violations) and not override_injury_gate

        purpose_gate_error = {
            'missing_purpose': missing_purpose,
            'hint': ("Add a non-empty 'purpose' string to each listed session "
                     "(WHY it exists). override_purpose_gate is deprecated and "
                     "no longer bypasses this gate; an athlete-choice day uses "
                     "intensity 'discretion' and still states its purpose."),
        }
        if purpose_override_requested:
            purpose_gate_error['override_purpose_gate'] = (
                'deprecated — ignored, the purpose gate cannot be bypassed')
        if injury_blocked:
            error = {
                'error': 'injury_gate',
                'message': 'Plan contains sessions restricted by active/improving injuries',
                'violations': violations,
                'hint': ('Adjust the offending sessions, or pass '
                         'override_injury_gate=True with a logged rationale '
                         'after confirming with the athlete.'),
            }
            if purpose_blocked:
                # Gates compose: report BOTH failures in one round-trip.
                error['message'] += (
                    '; the plan also fails the purpose gate '
                    f'({len(missing_purpose)} session(s) without a purpose)'
                )
                error['purpose_gate'] = purpose_gate_error
            return error
        if purpose_blocked:
            return {
                'error': 'purpose_gate',
                'message': (f'{len(missing_purpose)} non-rest session(s) have '
                            'no purpose — every non-rest session must explain '
                            'WHY it exists'),
                **purpose_gate_error,
            }

        # Session contract: what the watch will show must be what the plan
        # says. Unknown keys, missing end conditions, exercises without
        # sets/reps — rejected with the allowed keys, never defaulted.
        authoring_errors, authoring_warnings = _authoring_problems(gated_days)
        if authoring_errors:
            return {
                'error': 'authoring_error',
                'message': (f"{len({(p['date'], p['session']) for p in authoring_errors})} "
                            "session(s) do not satisfy the session contract — nothing was saved"),
                'problems': authoring_errors,
                'hint': ("Fix the named fields. preview_workout(session) shows the rendering "
                         "and preview_workout() returns the full contract."),
            }

        injury_gate_note = None
        if violations:  # only reachable with override_injury_gate=True
            logger.warning(
                "Injury gate OVERRIDDEN in update_weekly_plan: %s", violations
            )
            injury_gate_note = {
                'injury_gate_overridden': True,
                'violations': violations,
            }

        save_weekly_plan(plan, today=today)
        # Computed AFTER save: save_weekly_plan prunes stale days in place, so
        # warnings only cover days that actually remain in the plan. Always
        # empty now that the purpose gate has no bypass; kept in the response
        # for older clients.
        purpose_warnings = _missing_purpose_sessions(plan.get('days', {}))
        # VERIFY BEFORE PRESCRIBING: longest planned session per sport vs
        # the athlete's demonstrated capacity. Warn-only by design.
        grounding_warnings = _grounding_warnings(plan.get('days', {}), today)
        result = {
            'status': 'success',
            'message': 'Weekly plan saved',
            'last_updated': today.isoformat(),
            'purpose_warnings': purpose_warnings,
            'grounding_warnings': grounding_warnings,
            'authoring_warnings': authoring_warnings,
        }
        if authoring_warnings:
            result['message'] += (
                f" — {len(authoring_warnings)} authoring warning(s): the watch will not "
                "show these sessions exactly as written; see authoring_warnings "
                "(preview_workout shows the rendering)."
            )
        if purpose_warnings:
            result['message'] = (
                f"Weekly plan saved with purpose gate OVERRIDDEN — "
                f"{len(purpose_warnings)} non-rest session(s) have no "
                "purpose. Every non-rest session should explain WHY it "
                "matters."
            )
        if grounding_warnings:
            result['message'] += (
                f" — {len(grounding_warnings)} grounding warning(s): the "
                "plan's session durations disagree with demonstrated "
                "capacity. Address every grounding_warnings entry with the "
                "athlete before pushing to Garmin."
            )
        if injury_gate_note:
            result['injury_gate'] = injury_gate_note
            # Consent trail: the athlete's words and the coach's reasoning
            decision = append_decision(
                'gate_override',
                f"Weekly plan saved despite injury restrictions on "
                f"{len(violations)} session(s)",
                why, athlete_statement=statement,
                extra={'tool': 'update_weekly_plan', 'violations': violations},
                today=today)
            result['decision_logged'] = decision.get('id')
        return result
    except json.JSONDecodeError as e:
        logger.exception("update_weekly_plan failed: invalid JSON")
        return {'error': f'Invalid JSON: {str(e)}'}
    except Exception as e:
        logger.exception("update_weekly_plan failed")
        return {'error': str(e)}


# ---------------------------------------------------------------------------
# Push consent: preview -> single-use confirm token -> write
# ---------------------------------------------------------------------------

PUSH_TOKEN_TTL_SECS = 600
# token -> (plan digest, issued at, the rendered manifest the athlete saw)
_push_tokens: dict[str, tuple[str, float, dict]] = {}
_push_token_lock = threading.Lock()


def _push_digest(plan: dict, previous_ids: list, override: bool) -> str:
    """Hash of what the push depends on: the planned sessions per day, the
    ids that will be deleted, and the override flag. Audit verdicts
    (status/actual) and athlete claims are excluded on purpose, so the 06:30
    audit does not invalidate a 06:25 preview."""
    days = plan.get('days') or {}
    material = {
        'days': {d: (v.get('planned') if isinstance(v, dict) else v)
                 for d, v in sorted(days.items())},
        'previous_ids': sorted(str(i) for i in previous_ids),
        'override_injury_gate': bool(override),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode('utf-8')).hexdigest()[:16]


def _issue_push_token(digest: str, manifest: dict) -> str:
    """Mint a single-use token bound to the plan digest AND to the rendered
    manifest the preview showed: the confirm step uploads these payloads,
    not a re-render (a profile or catalogue edit between the two calls can
    no longer change what the athlete approved)."""
    now = time.monotonic()
    with _push_token_lock:
        for token, (_digest, issued, _manifest) in list(_push_tokens.items()):
            if now - issued > PUSH_TOKEN_TTL_SECS:
                _push_tokens.pop(token, None)
        token = secrets.token_hex(8)
        _push_tokens[token] = (digest, now, manifest)
    return token


def _consume_push_token(token: str, digest: str) -> tuple[dict | None, dict | None]:
    """Pop the token (single use) and validate it.

    Returns (error, manifest): error is None when the push may proceed, and
    manifest is the rendered manifest from the preview."""
    with _push_token_lock:
        entry = _push_tokens.pop(str(token), None)
    hint = 'Call push_plan_to_garmin() without a token for a fresh preview.'
    if entry is None:
        return ({'error': 'confirm_token_invalid',
                 'message': ('Unknown or already used confirm_token — tokens are '
                             'single-use and live 10 minutes. Nothing was pushed.'),
                 'hint': hint}, None)
    stored_digest, issued, manifest = entry
    if time.monotonic() - issued > PUSH_TOKEN_TTL_SECS:
        return ({'error': 'confirm_token_expired',
                 'message': 'The preview is older than 10 minutes. Nothing was pushed.',
                 'hint': hint}, None)
    if stored_digest != digest:
        return ({'error': 'confirm_token_mismatch',
                 'message': ('The plan\'s sessions or its pushed workouts changed since '
                             'the preview. Nothing was pushed.'),
                 'hint': hint}, None)
    return None, manifest


def _expand_push_sessions(day_data: dict) -> list[dict]:
    """Flatten a day's planned session(s), expanding nested 'sessions' lists."""
    planned = day_data.get('planned')
    if not planned:
        return []
    sessions = planned if isinstance(planned, list) else [planned]
    expanded = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        if isinstance(session.get('sessions'), list):
            parent_exercises = session.get('exercises', [])
            parent_intensity = session.get('intensity')  # inherited only when the parent states one
            for sub in session.get('sessions', []):
                if not isinstance(sub, dict):
                    continue
                if sub.get('time') == 'optional':
                    continue  # pool_sauna-style optional extras are never pushed
                sub_type = str(sub.get('type', '')).lower()
                if sub_type == 'strength' and 'description' not in sub:
                    focus = sub.get('focus', '')
                    sub['description'] = (f"{focus.replace('_', ' ').title()} Strength"
                                          if focus else "Strength Training")
                elif sub_type == 'rehab' and 'description' not in sub:
                    sub['description'] = sub.get('purpose') or "Rehab"
                elif 'description' not in sub:
                    sub['description'] = sub.get('notes', session.get('description', ''))
                if 'intensity' not in sub and parent_intensity is not None:
                    sub['intensity'] = parent_intensity
                if sub_type == 'strength' and parent_exercises:
                    sub['exercises'] = parent_exercises
                if 'protocol' in sub or 'protocol' in session:
                    sub['protocol'] = sub.get('protocol', session.get('protocol', []))
                if session.get('structure') and sub_type in ['indoor_cycling', 'wattbike', 'trainer']:
                    sub['structure'] = session['structure']
                    for key in ('ftp', 'power_targets', 'technique_goals'):
                        if session.get(key):
                            sub[key] = session[key]
                expanded.append(sub)
        else:
            expanded.append(session)
    return expanded


def _build_push_manifest(plan: dict) -> tuple[list[dict], list[dict], list[dict]]:
    """(to_push, skipped, unrenderable).

    Every session is rendered here, so the preview shows the step tree the
    watch will get and the confirm step uploads exactly that. to_push
    entries carry the built workout; unrenderable entries carry the
    contract errors (they are never uploaded).
    """
    to_push, skipped, unrenderable = [], [], []
    for date_str, day_data in sorted((plan.get('days') or {}).items()):
        if not isinstance(day_data, dict) or not day_data.get('planned'):
            skipped.append({'date': date_str, 'reason': 'rest day'})
            continue
        for session in _expand_push_sessions(day_data):
            workout_type = get_workout_type_name(session)
            if workout_type == 'skipped':
                skipped.append({'date': date_str, 'type': session.get('type'),
                                'reason': 'rest day - not pushed'})
                continue
            if workout_type == 'unknown':
                skipped.append({'date': date_str, 'type': session.get('type'),
                                'reason': 'unknown workout type'})
                continue
            label = session.get('name') or session.get('description') or session.get('type')
            report = build_workout_report(session, date_str)
            if not report['ok']:
                unrenderable.append({'date': date_str, 'type': session.get('type'),
                                     'name': label, 'errors': report['errors'],
                                     'warnings': report['warnings']})
                continue
            if report['workout'] is None:
                skipped.append({'date': date_str, 'type': session.get('type'),
                                'reason': 'nothing to render: no duration_mins, structure or exercises'})
                continue
            to_push.append({
                'date': date_str,
                'type': session.get('type'),
                'workout_type': workout_type,
                'name': report['name'] or label,
                'duration_mins': session.get('duration_mins'),
                'estimated_mins': report['estimated_mins'],
                'steps': report['steps'],
                'warnings': report['warnings'],
                'session': session,
                'workout': report['workout'],
            })
    return to_push, skipped, unrenderable


@mcp.tool(meta={"anthropic/alwaysLoad": True, "anthropic/requiresUserInteraction": True},
          annotations={'readOnlyHint': False, 'destructiveHint': True,
                       'idempotentHint': False, 'openWorldHint': True})
def push_plan_to_garmin(override_injury_gate: bool = False,
                        confirm_token: str | None = None,
                        athlete_statement: str | None = None,
                        override_rationale: str | None = None) -> dict:
    """
    Push the current 7-day plan to Garmin Connect in TWO steps: a preview,
    then a confirm with the single-use token the preview returned.

    STEP 1 — PREVIEW (no confirm_token): nothing is written. Returns the
    sessions that will be pushed per date, each with the rendered step tree
    ('steps': what the watch will show, line by line) and authoring
    warnings, the sessions that cannot be rendered ('unrenderable', with
    the contract errors — fix them with update_weekly_plan, they are never
    uploaded), the previously pushed workouts that will be DELETED first
    (duplicate prevention, checked against the Garmin library),
    grounding_warnings, injury violations, and a confirm_token valid for 10
    minutes. Show the athlete the steps; this tool carries
    anthropic/requiresUserInteraction — the athlete says yes before step 2.

    STEP 2 — CONFIRM (confirm_token from step 1): deletes the listed
    workouts, uploads exactly the payloads rendered for the preview (running,
    cycling, strength, yoga, pilates, swimming, padel; rest days skipped —
    a profile or catalogue change after the preview cannot alter what the
    athlete approved), schedules each to its date and records the new
    workout ids on the plan. 'status' is 'partial' when any session of the
    plan is not on the watch: an upload or scheduling failure, or an
    unrenderable session (all listed under errors). A token is rejected
    when it is unknown or already used ('confirm_token_invalid'), older than
    10 minutes ('confirm_token_expired'), or when the plan's sessions or its
    pushed ids changed since the preview ('confirm_token_mismatch') — ask for
    a fresh preview. Audit verdicts and athlete claims do not invalidate it.

    GATES: snapshot_required / snapshot_stale (SNAPSHOT FIRST, both steps).
    injury_gate: the push is rejected when a non-rest session's type
    intersects an active/improving injury's restricted_activities;
    override_injury_gate=True needs athlete_statement (the athlete's own
    words) and override_rationale, logged as a 'gate_override' decision on
    confirm ('override_requires_confirmation' without them).

    BOOKKEEPING: a workout that failed to delete stays tracked (no silent
    duplicates); an uploaded workout whose scheduling failed stays tracked
    and is reported under errors; pushed_workout_ids is updated whenever
    anything was deleted or uploaded.

    Args:
        override_injury_gate: Bypass the injury restriction check (default False)
        confirm_token: The token from the preview — omit for a preview
        athlete_statement: The athlete's own words agreeing to an override
        override_rationale: Why the coach agrees, citing the evidence

    Returns:
        Preview: {'status': 'preview', 'sessions' (each with 'steps' and
        'warnings'), 'unrenderable', 'skipped', 'workouts_to_delete',
        'grounding_warnings', 'injury_violations', 'confirm_token',
        'expires_in_minutes', 'next'}.
        Confirm: {'status': 'success' | 'partial', 'duplicates_deleted',
        'pushed', 'skipped', 'errors', 'summary'}, or a structured error.
    """
    try:
        today = date.today()
        gate = require_snapshot('push_plan_to_garmin', today)
        if gate:
            return gate
        statement = (athlete_statement or '').strip()
        why = (override_rationale or '').strip()
        if override_injury_gate and not (statement and why):
            return {
                'error': 'override_requires_confirmation',
                'message': ("override_injury_gate needs the athlete's own words "
                            "(athlete_statement) and override_rationale — nothing "
                            "was pushed."),
                'hint': ("Quote the athlete's agreement in athlete_statement and give "
                         "the evidence in override_rationale."),
            }

        plan = get_current_plan()
        if not plan or 'days' not in plan:
            return {'error': 'No weekly plan found. Generate a plan first.'}

        # Injury write-gate: never push sessions that violate active/improving
        # injury restrictions unless explicitly overridden with consent.
        injuries = load_athlete().get('injury_history', [])
        violations = _injury_gate_violations(plan.get('days', {}), injuries)
        if violations and not override_injury_gate:
            return {
                'error': 'injury_gate',
                'message': 'Plan contains sessions restricted by active/improving injuries — push blocked',
                'violations': violations,
                'hint': ('Fix the plan via update_weekly_plan, or pass '
                         'override_injury_gate=True with athlete_statement and '
                         'override_rationale after confirming with the athlete.'),
            }

        previous_ids = list(plan.get('pushed_workout_ids', []) or [])
        digest = _push_digest(plan, previous_ids, override_injury_gate)

        # ---------------- STEP 1: preview (read-only) ----------------
        if confirm_token is None:
            to_push, skipped, unrenderable = _build_push_manifest(plan)
            manifest = {'to_push': to_push, 'skipped': skipped, 'unrenderable': unrenderable}
            library_note = None
            if previous_ids:
                try:
                    existing = garmin_api_call(lambda c: c.get_workouts())
                    existing_ids = {w.get('workoutId') for w in (existing or [])}
                    to_delete = [wid for wid in previous_ids if wid in existing_ids]
                except Exception as e:
                    logger.warning("Preview: could not read the Garmin workout library",
                                   exc_info=True)
                    to_delete = previous_ids
                    library_note = f'library check unavailable ({e}); all tracked ids listed'
            else:
                to_delete = []
            preview = {
                'status': 'preview',
                'sessions': [{k: v for k, v in entry.items() if k not in ('session', 'workout')}
                             for entry in to_push],
                'unrenderable': unrenderable,
                'skipped': skipped,
                'workouts_to_delete': to_delete,
                'grounding_warnings': _grounding_warnings(plan.get('days', {}), today),
                'injury_violations': violations,
                'confirm_token': _issue_push_token(digest, manifest),
                'expires_in_minutes': PUSH_TOKEN_TTL_SECS // 60,
                'next': ('Show the athlete each session\'s steps and the deletions. When they '
                         'agree, call push_plan_to_garmin(confirm_token=...) with the '
                         'same override arguments. Nothing has been written yet.'),
            }
            if unrenderable:
                preview['next'] = (
                    f"{len(unrenderable)} session(s) cannot be rendered and will NOT be "
                    "pushed (see unrenderable): fix them with update_weekly_plan first, "
                    "then preview again. " + preview['next'])
            if library_note:
                preview['library_check'] = library_note
            return preview

        # ---------------- STEP 2: confirm ----------------
        # Uploads the manifest rendered at preview time — the step trees the
        # athlete approved — never a fresh render.
        # The library read comes first: a transient Garmin failure must not
        # consume the token the athlete's approval is bound to.
        existing_workouts = None
        if previous_ids:
            try:
                existing_workouts = garmin_api_call(lambda c: c.get_workouts())
            except Exception as e:
                logger.warning("Confirm: could not read the Garmin workout library", exc_info=True)
                return {
                    'error': 'garmin_library_unavailable',
                    'message': (f"Could not read the Garmin workout library ({e}); nothing was "
                                "pushed and the confirm_token is still valid."),
                    'hint': 'Retry the confirm with the same token, or preview again.',
                }
        token_error, manifest = _consume_push_token(confirm_token, digest)
        if token_error:
            return token_error
        to_push, skipped, unrenderable = manifest['to_push'], manifest['skipped'], manifest['unrenderable']

        injury_gate_note = None
        if violations:
            logger.warning("Injury gate OVERRIDDEN in push_plan_to_garmin: %s", violations)
            injury_gate_note = {'injury_gate_overridden': True, 'violations': violations}

        # DUPLICATE PREVENTION: delete only the specific workouts we pushed
        # last time and that still exist. Never delete the entire Garmin
        # workout library.
        actually_deleted: list = []
        delete_failures: list = []
        gone_externally: list = []
        if previous_ids:
            existing_ids = {w.get('workoutId') for w in (existing_workouts or [])}
            for wid in previous_ids:
                if wid not in existing_ids:
                    gone_externally.append(wid)
                    continue
                try:
                    garmin_api_call(
                        lambda c, wid=wid: c.client.delete(
                            'connectapi', f'/workout-service/workout/{wid}', api=True
                        )
                    )
                    actually_deleted.append(wid)
                except Exception as e:
                    logger.warning("Could not delete workout %s: %s", wid, e)
                    delete_failures.append(wid)

        results = {
            'status': 'success',
            'duplicates_deleted': len(actually_deleted),
            'pushed': [],
            'skipped': list(skipped),
            'errors': [{'date': u['date'], 'type': u['type'], 'name': u['name'],
                        'error': 'authoring_error — not pushed', 'problems': u['errors']}
                       for u in unrenderable],
        }
        if delete_failures:
            results['delete_failures'] = delete_failures
        if injury_gate_note:
            results['injury_gate'] = injury_gate_note

        uploaded_ids: list = []
        for entry in to_push:
            date_str, workout_type = entry['date'], entry['workout_type']
            workout = entry['workout']  # rendered by the manifest: the previewed steps are what ships
            try:
                if workout_type == 'cycling':
                    upload_result = garmin_api_call(lambda c, w=workout: c.upload_cycling_workout(w))
                    workout_name = workout.workoutName
                elif workout_type == 'running':
                    upload_result = garmin_api_call(lambda c, w=workout: c.upload_running_workout(w))
                    workout_name = workout.workoutName
                elif workout_type in ['yoga', 'strength', 'swimming', 'pilates', 'padel']:
                    upload_result = garmin_api_call(lambda c, w=workout: c.upload_workout(w))
                    workout_name = workout.get('workoutName', 'Workout')
                else:
                    results['skipped'].append({'date': date_str, 'type': workout_type,
                                               'reason': 'upload method not implemented'})
                    continue
                workout_id = (upload_result or {}).get('workoutId')
                if not workout_id:
                    results['errors'].append({'date': date_str, 'type': workout_type,
                                              'error': 'No workout ID returned from upload'})
                    continue
                uploaded_ids.append(workout_id)  # tracked from this moment on
            except Exception as e:
                results['errors'].append({'date': date_str, 'type': workout_type, 'error': str(e)})
                continue

            try:
                schedule_workout(workout_id, date_str)
            except Exception as e:
                # Uploaded but unscheduled: keep the id so the next push
                # cleans it up, and say so.
                results['errors'].append({'date': date_str, 'type': workout_type,
                                          'workout_id': workout_id,
                                          'error': f'uploaded but scheduling failed: {e}'})
                continue

            result_entry = {'date': date_str, 'type': workout_type,
                            'workout_id': workout_id, 'name': workout_name}
            if workout_type == 'strength' and isinstance(workout, dict):
                result_entry['exercise_count'] = workout.get('exercise_count', 0)
            results['pushed'].append(result_entry)

        # Tracked ids: what is still on Garmin from before, plus everything
        # uploaded now (scheduled or not). Merge only this field so a plan
        # saved during the push is not overwritten.
        if uploaded_ids or actually_deleted or gone_externally:
            tracked = ((set(previous_ids) - set(actually_deleted) - set(gone_externally))
                       | set(uploaded_ids))
            plan['pushed_workout_ids'] = sorted(tracked, key=str)
            merge_plan_fields({'pushed_workout_ids': plan['pushed_workout_ids']})

        if injury_gate_note:
            decision = append_decision(
                'gate_override',
                f"Plan pushed to Garmin despite injury restrictions on "
                f"{len(violations)} session(s)",
                why, athlete_statement=statement,
                extra={'tool': 'push_plan_to_garmin', 'violations': violations},
                today=today)
            results['decision_logged'] = decision.get('id')

        if results['errors']:
            results['status'] = 'partial'  # something the athlete approved did not land
        pushed_count = len(results['pushed'])
        if pushed_count > 0:
            dates = [p['date'] for p in results['pushed']]
            types_pushed = sorted({p['type'] for p in results['pushed']})
            results['summary'] = (f"Pushed {pushed_count} workout(s) to Garmin "
                                  f"({', '.join(types_pushed)}): {dates[0]} to {dates[-1]}")
        elif to_push:
            results['summary'] = (f"No workouts pushed: {len(to_push)} upload(s) failed "
                                  "(see errors)")
        else:
            results['summary'] = "No workouts pushed (nothing to push: rest days or skipped sessions)"
        if unrenderable:
            results['summary'] += (f"; {len(unrenderable)} session(s) NOT pushed — contract "
                                   "errors listed under errors")
        return results

    except Exception as e:
        logger.exception("push_plan_to_garmin failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
def preview_workout(session: dict | None = None, include_payload: bool = False) -> dict:
    """
    Render ONE plan session the way push_plan_to_garmin will, without
    saving or pushing anything: the step tree the watch will show, the
    contract errors (nothing is defaulted — a missing end condition, an
    exercise without sets, an unknown key are errors that name the allowed
    keys) and the warnings (an off-catalogue exercise name the watch will
    strip, a cue over 50 characters, an intensity outside the zone
    vocabulary). Iterate here until the rendering matches the brief, then
    save with update_weekly_plan.

    Call with no session to get the session contract: the intensity
    vocabulary, the phase and exercise keys and their JSON schemas.

    Args:
        session: one session dict exactly as it would appear under a plan
            day's 'planned' (type, duration_mins, intensity, description,
            structure / exercises, ...). Omit for the contract.
        include_payload: also return the Garmin workout payload (large).

    Returns:
        {'ok', 'errors', 'warnings', 'workout_type', 'name', 'sport',
        'estimated_mins', 'steps'} — or {'contract': markdown} when no
        session was given.
    """
    try:
        if session is None or session == {}:
            return {'contract': contract_document(),
                    'hint': 'Pass a session dict to see how it renders.'}
        if not isinstance(session, dict):
            return {'error': 'session must be an object (one planned session)'}
        report = build_workout_report(session, 'preview')
        result = {k: v for k, v in report.items() if k != 'workout'}
        if include_payload and report['workout'] is not None:
            workout = report['workout']
            result['garmin_payload'] = workout.to_dict() if hasattr(workout, 'to_dict') else workout
        if not report['ok']:
            result['hint'] = ('Fix the fields named in errors; preview_workout() with no '
                              'session returns the contract and allowed keys.')
        return result
    except Exception as e:
        logger.exception("preview_workout failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
def get_week_constraints() -> dict:
    """Get constraints and requirements for building next week's plan.

    Returns blocked days, pillar requirements, phase-appropriate session
    guidelines with duration ranges and principles, key session types from
    race template, injury restrictions, chronic compliance misses, and
    `recent_capacity` — the athlete's demonstrated per-sport capacity.

    The session-guideline duration_ranges are GENERIC templates: cross-check
    every duration against recent_capacity before prescribing (VERIFY BEFORE
    PRESCRIBING). The LLM assembles the actual plan using these constraints
    as guardrails. Call this before building or adjusting a weekly plan.

    Returns:
        Structured constraints dict for week planning.
    """
    try:
        today = date.today()
        athlete = load_athlete()
        injuries = athlete.get('injury_history', [])

        # Load compliance diagnostics if available (from coaching snapshot)
        compliance_diagnostics = None
        try:
            from .coaching_tools import _compliance_diagnostics_from_history
            training_pillars = pillars_as_name_dict(athlete.get('training_pillars'))
            if training_pillars:
                history = load_fitness_history()
                daily_loads = history.get('daily_loads', {})
                if daily_loads:
                    compliance_diagnostics = _compliance_diagnostics_from_history(
                        daily_loads, training_pillars, today
                    )
        except Exception:
            logger.warning("Could not load compliance diagnostics", exc_info=True)

        constraints = _get_week_constraints(
            athlete=athlete,
            injuries=injuries,
            compliance_diagnostics=compliance_diagnostics,
        )

        # Demonstrated capacity sits BESIDE the template duration_ranges so
        # a generic range can never be quoted without the athlete's actual
        # numbers in view (VERIFY BEFORE PRESCRIBING).
        try:
            history = load_fitness_history()
            daily_loads = history.get('daily_loads', {})
            if daily_loads:
                cap = derive_sport_capacity(daily_loads, today)
                constraints['recent_capacity'] = {
                    'by_sport': cap.get('by_sport', {}),
                    'window_days': cap.get('window_days'),
                    'note': ('Session-guideline duration_ranges are generic '
                             'templates. Anchor every prescribed duration to '
                             'this demonstrated capacity instead.'),
                }
        except Exception:
            logger.warning("Could not attach capacity to week constraints",
                           exc_info=True)

        return constraints

    except Exception as e:
        logger.exception("get_week_constraints failed")
        return {'error': str(e)}


def _program_injury_violations(sport: str, injuries: list) -> list[dict]:
    """Active/improving injury restrictions that the program's sport hits."""
    violations = []
    for injury in (injuries or []):
        if not isinstance(injury, dict):
            continue
        if injury.get('status') not in ('active', 'improving'):
            continue
        norm = normalize_injury(injury)
        matched = [r for r in norm['restricted_activities']
                   if _activity_matches_restriction(sport, r)]
        if matched:
            violations.append({
                'sport': sport,
                'injury': norm['name'],
                'injury_status': norm['status'],
                'matched_restrictions': matched,
            })
    return violations


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': False,
                       'idempotentHint': True, 'openWorldHint': False})
def training_program(action: str = 'get', program: dict | None = None,
                     override_injury_gate: bool = False,
                     athlete_statement: str | None = None,
                     override_rationale: str | None = None) -> dict:
    """
    Multi-week training program store — the macro plan the rolling 7-day
    weekly plan executes. The weekly plan holds at most 21 days; anything
    longer (a race build, a return-to-run block) lives HERE so the server
    can gate it instead of it existing only as chat prose.

    ACTIONS

        action='get'  -> the stored program + which week the athlete is in.
        action='set'  -> validate and save `program` (overwrites; the prior
                         program is archived into the file's history).

    PROGRAM STRUCTURE (for action='set')

        {
            'name': '10k build — Cape Town',
            'sport': 'running',                # must be duration-prescribed:
                                               # cycling/running/strength/swimming
            'start_date': 'YYYY-MM-DD',
            'goal_event': {'name': '...', 'date': 'YYYY-MM-DD'},   # optional
            'weeks': [                          # 1..52 entries, week 1 first
                {'week': 1, 'focus': 'consolidation',
                 'long_session_mins': 45, 'volume_target_hrs': 4.0,
                 'notes': '...'},
                ...
            ],
            'capacity_anchor': {                # REQUIRED — see gate below
                'sport': 'running',
                'demonstrated_longest_session_mins': 45,
                'source': 'snapshot.capacity',
                'rationale_if_below': '...'     # required when week 1 sits
            },                                  # far below capacity
            'review_checkpoints': [{'date': '...', 'note': 'physio'}],
            'rationale': 'Why this program, for this athlete'
        }

    CAPACITY ANCHOR GATE (VERIFY BEFORE PRESCRIBING — hard, code-enforced)

        The program must STATE the athlete's demonstrated capacity and the
        server VERIFIES the claim against actual history:
        - capacity_anchor missing -> schema rejection.
        - claimed demonstrated_longest_session_mins deviating > 25% from the
          server-derived value (longest completed session, recent window)
          -> REJECTED with error 'capacity_anchor_mismatch' naming both
          numbers. Re-read snapshot.capacity — never assert a starting
          capacity the history contradicts.
        - week 1's long_session_mins < 60% of demonstrated capacity without
          capacity_anchor.rationale_if_below -> REJECTED with error
          'capacity_anchor'. A deliberate regression (taper, rehab,
          bone-load protocol) is legitimate — but it must be stated, not
          silently anchored to a template.

    OTHER GATES

        - sport must be duration-prescribed (cycling/running/strength/
          swimming — games and mobility aren't duration-progressed).
        - INJURY: a sport restricted by an active/improving injury rejects
          the save; override_injury_gate=True needs athlete_statement and
          override_rationale (logged as a 'gate_override' decision; without
          them: 'override_requires_confirmation'). Mirrors update_weekly_plan.
        - SNAPSHOT FIRST: action='set' needs a snapshot from this session
          today ('snapshot_required' / 'snapshot_stale').
        - DATES (error 'program_dates'): start_date more than 90 days out, or a program entirely in
          the past, is rejected.
        - Week-over-week long_session_mins increases > 25% come back as
          warnings (progression_rate) — address them, don't ignore them.

    The weekly plan remains the execution surface: each week, derive that
    week's sessions from the program and save them via update_weekly_plan
    (its own purpose/injury/grounding checks still run).

    Args:
        action: 'get' (default) or 'set'.
        program: The program dict (required for action='set').
        override_injury_gate: Bypass the injury restriction check (default
            False) — needs athlete_statement and override_rationale.
        athlete_statement: The athlete's own words agreeing to the override.
        override_rationale: Why the coach agrees, citing the evidence.

    Returns:
        action='get': the stored program with current-week context.
        action='set': confirmation with program summary and warnings, or a
        structured error ('validation_error', 'program_dates', 'sport',
        'injury_gate', 'capacity_anchor_mismatch', 'capacity_anchor').
    """
    try:
        today = date.today()
        action = str(action or 'get').strip().lower()
        if action != 'get':
            gate = require_snapshot('training_program', today)
            if gate:
                return gate
            statement = (athlete_statement or '').strip()
            why = (override_rationale or '').strip()
            if override_injury_gate and not (statement and why):
                return {
                    'error': 'override_requires_confirmation',
                    'message': ("override_injury_gate needs the athlete's own words "
                                "(athlete_statement) and override_rationale — nothing "
                                "was saved."),
                    'hint': ("Quote the athlete's agreement in athlete_statement and "
                             "give the evidence in override_rationale."),
                }

        if action == 'get':
            doc = load_json_file(TRAINING_PROGRAM_FILE) or {}
            stored = doc.get('program')
            if not isinstance(stored, dict) or not stored.get('weeks'):
                return {
                    'status': 'no_program',
                    'hint': ("No multi-week program stored. Build one with "
                             "training_program(action='set', program=...) — "
                             "anchor it to snapshot.capacity (see the "
                             "race_program prompt)."),
                }
            result = {'status': 'ok', 'program': stored}
            try:
                start = date.fromisoformat(stored['start_date'])
                total_weeks = len(stored['weeks'])
                week_no = ((today - start).days // 7) + 1
                result['program_status'] = (
                    'upcoming' if today < start
                    else 'complete' if week_no > total_weeks
                    else 'active')
                result['current_week'] = (
                    week_no if 1 <= week_no <= total_weeks else None)
                result['total_weeks'] = total_weeks
            except (ValueError, TypeError, KeyError):
                pass
            return result

        if action != 'set':
            return {'error': f"Unknown action '{action}'. Valid actions: "
                             "'get', 'set'"}

        if not isinstance(program, dict):
            return {'error': "action='set' requires 'program' as a structured "
                             "dict (see PROGRAM STRUCTURE in the docstring)"}

        # Typed schema gate — required fields: name, sport, start_date,
        # weeks, capacity_anchor (with sport + demonstrated mins).
        try:
            TrainingProgramSchema.model_validate(program)
        except ValidationError as exc:
            problems = [
                {'field': '.'.join(str(p) for p in e.get('loc', ())),
                 'message': e.get('msg')}
                for e in exc.errors()
            ]
            logger.warning("training_program rejected by schema: %s", problems)
            return {
                'error': 'validation_error',
                'message': 'Program failed schema validation',
                'problems': problems,
                'hint': ("Required: name, sport, start_date (YYYY-MM-DD), "
                         "weeks (list), capacity_anchor {sport, "
                         "demonstrated_longest_session_mins}."),
            }

        weeks = program.get('weeks') or []
        if not weeks or len(weeks) > PROGRAM_MAX_WEEKS:
            return {'error': 'validation_error',
                    'message': f'weeks must contain 1-{PROGRAM_MAX_WEEKS} '
                               f'entries (got {len(weeks)})'}

        # Date sanity
        try:
            start = date.fromisoformat(str(program.get('start_date')))
        except (ValueError, TypeError):
            return {'error': 'program_dates',
                    'message': f"start_date {program.get('start_date')!r} is "
                               "not a YYYY-MM-DD date"}
        if start > today + timedelta(days=PROGRAM_MAX_FUTURE_START_DAYS):
            return {'error': 'program_dates',
                    'message': f'start_date {start.isoformat()} is more than '
                               f'{PROGRAM_MAX_FUTURE_START_DAYS} days in the '
                               'future (fat-finger guard)'}
        if start + timedelta(days=len(weeks) * 7) < today:
            return {'error': 'program_dates',
                    'message': 'program is entirely in the past — build a '
                               'current program'}

        # Sport gate: only duration-prescribed sports can carry a
        # duration-laddered program.
        sport = str(program.get('sport', ''))
        bucket = capacity_bucket_for(sport)
        if bucket is None:
            return {'error': 'sport',
                    'message': f"'{sport}' is not a duration-prescribed "
                               f"sport. Valid capacity buckets: "
                               f"{', '.join(CAPACITY_BUCKETS)}"}

        # Injury gate (mirrors update_weekly_plan)
        injuries = load_athlete().get('injury_history', [])
        violations = _program_injury_violations(sport, injuries)
        if violations and not override_injury_gate:
            return {
                'error': 'injury_gate',
                'message': ('Program sport is restricted by an '
                            'active/improving injury'),
                'violations': violations,
                'hint': ('Change the program sport, or pass '
                         'override_injury_gate=True with a logged rationale '
                         'after confirming with the athlete.'),
            }

        warnings = []
        notes = []
        if violations:  # only reachable with override_injury_gate=True
            logger.warning("Injury gate OVERRIDDEN in training_program: %s",
                           violations)
            notes.append({'injury_gate_overridden': True,
                          'violations': violations})

        # ------------------------------------------------------------------
        # CAPACITY ANCHOR GATE — the coach-fiction check. The program claims
        # a demonstrated capacity; the server recomputes it from history and
        # rejects claims the data contradicts.
        # ------------------------------------------------------------------
        anchor = program.get('capacity_anchor') or {}
        anchor_bucket = capacity_bucket_for(anchor.get('sport'))
        if anchor_bucket != bucket:
            return {'error': 'capacity_anchor',
                    'message': f"capacity_anchor.sport "
                               f"({anchor.get('sport')!r}) must match the "
                               f"program sport ({sport!r})"}
        try:
            claimed = float(anchor.get('demonstrated_longest_session_mins'))
        except (TypeError, ValueError):
            return {'error': 'capacity_anchor',
                    'message': 'capacity_anchor.demonstrated_longest_'
                               'session_mins must be a number'}

        server_mins = None
        window_days = None
        try:
            history = load_fitness_history()
            capacity = derive_sport_capacity(
                history.get('daily_loads', {}), today)
            window_days = capacity.get('window_days')
            server_mins, _window = demonstrated_max_mins(
                capacity.get('by_sport', {}).get(bucket))
        except Exception:
            logger.warning("Capacity derivation failed for program gate",
                           exc_info=True)

        if server_mins and server_mins >= GROUNDING_MIN_REFERENCE_MINS:
            if abs(claimed - server_mins) > server_mins * ANCHOR_TOLERANCE:
                return {
                    'error': 'capacity_anchor_mismatch',
                    'message': (
                        f'VERIFY BEFORE PRESCRIBING: the program claims a '
                        f'demonstrated longest {bucket} session of '
                        f'{claimed:.0f} min, but the athlete\'s history '
                        f'shows {server_mins:.0f} min (last {window_days} '
                        f'days). Never assert a starting capacity the data '
                        f'contradicts.'),
                    'claimed_mins': claimed,
                    'demonstrated_mins': server_mins,
                    'hint': ('Re-read snapshot.capacity (or query_metrics('
                             "kind='sport_history')) and anchor the program "
                             'to the demonstrated number.'),
                }
            demonstrated = server_mins
        else:
            demonstrated = claimed
            warnings.append({
                'type': 'no_demonstrated_history',
                'sport': bucket,
                'message': (f'No verifiable {bucket} history in the local '
                            f'capacity window — the anchor '
                            f'({claimed:.0f} min) is taken on trust. '
                            f'Confirm it with the athlete.'),
            })

        # Week-1 anchor check: a program starting far below demonstrated
        # capacity must SAY WHY (deliberate regression), or re-anchor.
        week1_mins = None
        for wk in weeks:
            if isinstance(wk, dict) and wk.get('long_session_mins'):
                try:
                    week1_mins = float(wk['long_session_mins'])
                except (TypeError, ValueError):
                    week1_mins = None
                break
        if (week1_mins is not None
                and demonstrated >= GROUNDING_MIN_REFERENCE_MINS):
            rationale_below = str(anchor.get('rationale_if_below') or '').strip()
            if week1_mins < demonstrated * GROUNDING_REGRESSION_FACTOR:
                if not rationale_below:
                    return {
                        'error': 'capacity_anchor',
                        'message': (
                            f'Week 1\'s long session ({week1_mins:.0f} min) '
                            f'is far below the demonstrated capacity of '
                            f'{demonstrated:.0f} min. A deliberate '
                            f'regression (taper, rehab, bone-load protocol) '
                            f'is legitimate — state it in capacity_anchor.'
                            f'rationale_if_below. Otherwise anchor week 1 '
                            f'at demonstrated capacity.'),
                        'week1_mins': week1_mins,
                        'demonstrated_mins': demonstrated,
                    }
                notes.append({
                    'deliberate_regression': True,
                    'week1_mins': week1_mins,
                    'demonstrated_mins': demonstrated,
                    'rationale': rationale_below,
                })
            elif week1_mins > demonstrated * GROUNDING_SPIKE_FACTOR:
                warnings.append({
                    'type': 'aggressive_start',
                    'sport': bucket,
                    'week1_mins': week1_mins,
                    'demonstrated_mins': demonstrated,
                    'message': (f'Week 1\'s long session ({week1_mins:.0f} '
                                f'min) exceeds demonstrated capacity '
                                f'({demonstrated:.0f} min) by more than '
                                f'30% — injury-risk jump.'),
                })

        # Progression-rate warnings: >25%/week long-session jumps
        prev_mins = None
        for wk in weeks:
            if not isinstance(wk, dict):
                continue
            try:
                mins = float(wk.get('long_session_mins'))
            except (TypeError, ValueError):
                prev_mins = None
                continue
            if prev_mins and mins > prev_mins * (
                    1 + PROGRAM_WEEKLY_INCREASE_WARN_PCT / 100):
                warnings.append({
                    'type': 'progression_rate',
                    'week': wk.get('week'),
                    'from_mins': prev_mins,
                    'to_mins': mins,
                    'message': (f'Week {wk.get("week")}: long session jumps '
                                f'{prev_mins:.0f} -> {mins:.0f} min '
                                f'(> {PROGRAM_WEEKLY_INCREASE_WARN_PCT:.0f}%'
                                f'/week).'),
                })
            prev_mins = mins

        # Save (archive any prior program into the file's history)
        doc = load_json_file(TRAINING_PROGRAM_FILE) or {}
        history_list = doc.get('history', []) or []
        prior = doc.get('program')
        if isinstance(prior, dict) and prior.get('weeks'):
            history_list.append({'archived': today.isoformat(),
                                 'program': prior})
        record = dict(program)
        record.setdefault('schema_version', 1)
        record.setdefault('status', 'active')
        record.setdefault('created', today.isoformat())
        record['last_updated'] = today.isoformat()
        if violations:  # overridden with consent — leave the trail
            append_decision(
                'gate_override',
                f"Program '{program.get('name', 'unnamed')}' saved despite injury "
                f"restrictions on {sport}",
                why, athlete_statement=statement,
                extra={'tool': 'training_program', 'violations': violations},
                today=today)
        save_json_file(TRAINING_PROGRAM_FILE, {
            'schema_version': 1,
            'program': record,
            'history': history_list[-PROGRAM_HISTORY_KEEP:],
        })

        ladder = [wk.get('long_session_mins') for wk in weeks
                  if isinstance(wk, dict)]
        result = {
            'status': 'success',
            'message': (f"Program '{record.get('name')}' saved "
                        f"({len(weeks)} weeks, {bucket})"),
            'program_summary': {
                'name': record.get('name'),
                'sport': bucket,
                'start_date': record.get('start_date'),
                'total_weeks': len(weeks),
                'long_session_ladder_mins': ladder,
                'goal_event': record.get('goal_event'),
                'capacity_anchor': {
                    'demonstrated_mins': demonstrated,
                    'verified_against_history': bool(server_mins),
                },
            },
            'warnings': warnings,
            'next_step': ('Derive week 1 into the rolling plan via '
                          'update_weekly_plan (purpose/injury/grounding '
                          'checks still run there).'),
        }
        if notes:
            result['notes'] = notes
        return result

    except Exception as e:
        logger.exception("training_program failed")
        return {'error': str(e)}
