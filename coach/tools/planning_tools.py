"""Planning tools - weekly plan management, periodization, prescriptions, Garmin push."""

from ..mcp_app import mcp
from ..garmin_client import garmin_api_call, schedule_workout
from ..parsers import parse_activities, parse_training_readiness
from ..planner import (get_current_plan, save_weekly_plan,
                     create_empty_week_template,
                     load_athlete, load_coaching_log, save_coaching_log,
                     get_week_constraints as _get_week_constraints,
                     PLAN_RETENTION_DAYS)
from ..rules import (load_training_config, check_weekly_compliance,
                   pillars_as_name_dict, normalize_injury)
from ..fitness import load_fitness_history, calculate_fitness_metrics, _extract_total_loads
from ..schemas import WeeklyPlan as WeeklyPlanSchema
from ..taxonomy import workout_family_for, types_match
from ..config import DATA_DIR, TRAINING_CONFIG_FILE
from datetime import date, timedelta
from pydantic import ValidationError
import json
import logging

logger = logging.getLogger(__name__)

# Fat-finger guard: reject plan day keys further out than this.
PLAN_MAX_FUTURE_DAYS = 21


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


def _injury_gate_violations(plan_days: dict, injuries: list) -> list[dict]:
    """Find non-rest sessions whose type intersects an active/improving injury's
    restricted_activities.

    Returns a list of {date, session_type, injury, matched_restrictions} dicts —
    empty when the plan is safe.
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
    return violations


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


def _missing_purpose_sessions(plan_days: dict) -> list[dict]:
    """Non-rest sessions whose 'purpose' is missing or blank.

    This is the PURPOSE GATE (Phase 3): update_weekly_plan rejects the save
    when this list is non-empty, unless override_purpose_gate=True. Every
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

    Returns target volume, key sessions, intensity targets, pillar priorities,
    injury constraints, life events, and flexibility notes.
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
                       override_purpose_gate: bool = False) -> dict:
    """
    Saves a new or updated weekly training plan.

    Pass the plan as a structured dict via `plan` (preferred — validated
    against the typed schema, with errors naming the offending day/field).
    `plan_json` (a JSON string) is a DEPRECATED alias kept for older clients.
    Exactly one of the two must be provided.

    PLAN STRUCTURE (for plan)

        {
            'days': {
                'YYYY-MM-DD': {
                    'planned': {
                        'type': 'running',
                        'duration_mins': 45,
                        'intensity': 'easy',
                        'purpose': 'Aerobic base — Z2 consistency',
                        'description': 'Easy recovery run'
                    },
                    'notes': 'Focus on form'
                },
                ...
            },
            'rationale': 'Why this plan was generated'
        }

        Session fields: 'type' is REQUIRED (string). 'purpose' is REQUIRED
        for every non-rest session (see PURPOSE GATE below) — it explains
        WHY the session matters.

        'intensity' is a free string. The special value 'discretion' marks
        an athlete-discretion day: the coach grants the athlete the choice
        of effort instead of the plan lying about a prescribed intensity.
        Bound the choice with 'constraints', a list of strings:

            {'type': 'cycling', 'duration_mins': 60,
             'intensity': 'discretion',
             'constraints': ['Z2 only', 'no running'],
             'purpose': 'Unstructured fun — athlete picks the effort'}

        'planned' may also be a LIST of session dicts when the day has two
        or three distinct workouts (e.g. run + short strength bolt-on, long
        ride + upper-body block, gym day split into legs + UB). Each session
        in the list is pushed to Garmin as its own workout on that date and
        is counted independently in compliance and adherence reports:

            'planned': [
                {'type': 'running', 'duration_mins': 46, 'intensity': 'easy',
                 'description': 'Level 3 R8/W2 x 4 — protocol run'},
                {'type': 'strength', 'duration_mins': 15, 'intensity': 'moderate',
                 'description': 'Short set — calf raises, SL DL, hip retraction',
                 'exercises': [...]}
            ]

        Prefer the list form over cramming multiple workouts into one
        description string — the list form gets proper per-session tracking.

    STRUCTURED RUNNING SESSIONS

        For running sessions with intervals, repeats, run/walk protocols,
        threshold reps, fartlek, hill repeats, or distance-based segments —
        author a `structure` field. Without it, the run pushes as one
        timed block regardless of what the description prose says.

            {
                'type': 'running',
                'duration_mins': 30,          # fallback estimate
                'intensity': 'easy',          # default for phases without their own
                'description': 'L2 R4/W2 x 4',
                'structure': [
                    {'phase': 'warmup', 'duration_secs': 180, 'intensity': 'recovery',
                     'notes': '300m walk + 4 heel + 4 toe steps'},
                    {'phase': 'repeat', 'iterations': 4, 'steps': [
                        {'phase': 'interval', 'duration_secs': 240, 'intensity': 'easy',
                         'notes': 'Run 4 min'},
                        {'phase': 'recovery', 'duration_secs': 120, 'intensity': 'recovery',
                         'notes': 'Walk 2 min'}
                    ]},
                    {'phase': 'cooldown', 'duration_secs': 180, 'intensity': 'recovery',
                     'notes': '200-300m walk'}
                ]
            }

        Phase schema:
            phase: 'warmup' | 'interval' | 'recovery' | 'cooldown' | 'rest' | 'repeat'
            End condition (one of, priority order):
                distance_m: 300                 -> end at distance
                duration_secs: 240              -> end at time (or duration_mins)
                "open" as any duration value    -> lap-button advance
            Target (one of, priority order):
                pace: [slow_mps, fast_mps]      -> explicit pace band, metres/sec
                hr_target: [low, high]          -> explicit HR band, bpm
                cadence: [low, high]            -> steps per minute
                intensity: "easy"|"recovery"|"tempo"|"threshold"|"vo2"
                                                -> resolves to pace zone (HR fallback)
            notes: free-form, truncated to 50 chars on display
        Repeat phase:
            iterations: N
            steps: [list of nested phases — may include further repeats]

    PURPOSE GATE

        The save is REJECTED when any non-rest session lacks a non-empty
        'purpose' string (error code 'purpose_gate', listing the offending
        dates/sessions). Nested 'sessions' lists are checked at the leaf
        level; rest days are exempt. Only set override_purpose_gate=True
        with a logged rationale — a session you can't explain is a session
        that shouldn't be on the plan.

    INJURY GATE

        Plans are rejected when any non-rest session's type intersects an
        active/improving injury's restricted_activities (taxonomy-aware:
        'long_ride' is caught by a 'cycling' restriction; free-text
        restrictions match by substring). The error lists the offending
        dates/sessions. Only set override_injury_gate=True with the
        athlete's informed consent and a logged rationale.

        A plan failing BOTH gates reports both: the injury_gate error
        carries a 'purpose_gate' section with the missing-purpose sessions.

    DATE VALIDATION

        Plans whose day keys are ALL in the past are rejected ('plan is
        entirely historical — build a current week'), as is any plan with a
        day key more than 21 days in the future (fat-finger guard).

    LIFECYCLE

        The plan is saved as-supplied; internal fields like `pushed_workout_ids`
        are preserved server-side and do not need to be carried in the JSON.
        week_start/week_end are derived from the day keys when missing, and day
        entries older than 9 days are pruned to data/plan_history.json.

    Args:
        plan: Structured plan dict (see PLAN STRUCTURE above). Preferred.
        override_injury_gate: Bypass the injury restriction check (default False)
        plan_json: DEPRECATED — JSON string form of the plan. Use `plan`.
        override_purpose_gate: Bypass the missing-purpose rejection (default
            False). The bypass is logged and noted in the response.

    Returns:
        Confirmation, or a structured error naming the offending
        day/field/sessions ('validation_error', 'plan_dates', 'purpose_gate',
        'injury_gate').
    """
    try:
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
                     "(WHY it exists), or pass override_purpose_gate=True "
                     "with a logged rationale."),
        }
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

        purpose_gate_note = None
        if missing_purpose:  # only reachable with override_purpose_gate=True
            logger.warning(
                "Purpose gate OVERRIDDEN in update_weekly_plan: %s",
                missing_purpose,
            )
            purpose_gate_note = {
                'purpose_gate_overridden': True,
                'missing_purpose': missing_purpose,
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
        # warnings only cover days that actually remain in the plan. Non-empty
        # only when the purpose gate was overridden.
        purpose_warnings = _missing_purpose_sessions(plan.get('days', {}))
        result = {
            'status': 'success',
            'message': 'Weekly plan saved',
            'last_updated': today.isoformat(),
            'purpose_warnings': purpose_warnings,
        }
        if purpose_warnings:
            result['message'] = (
                f"Weekly plan saved with purpose gate OVERRIDDEN — "
                f"{len(purpose_warnings)} non-rest session(s) have no "
                "purpose. Every non-rest session should explain WHY it "
                "matters."
            )
        if purpose_gate_note:
            result['purpose_gate'] = purpose_gate_note
        if injury_gate_note:
            result['injury_gate'] = injury_gate_note
        return result
    except json.JSONDecodeError as e:
        logger.exception("update_weekly_plan failed: invalid JSON")
        return {'error': f'Invalid JSON: {str(e)}'}
    except Exception as e:
        logger.exception("update_weekly_plan failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': False, 'destructiveHint': True,
                       'idempotentHint': False, 'openWorldHint': True})
def push_plan_to_garmin(override_injury_gate: bool = False) -> dict:
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

    INJURY GATE: the push is rejected when any non-rest session's type
    intersects an active/improving injury's restricted_activities. Only set
    override_injury_gate=True with the athlete's informed consent.

    Args:
        override_injury_gate: Bypass the injury restriction check (default False)

    Returns:
        Structured summary with count of pushed workouts, dates, and any errors.
    """
    from ..workout_builder import build_workout, get_workout_type_name

    try:
        plan = get_current_plan()

        if not plan or 'days' not in plan:
            return {'error': 'No weekly plan found. Generate a plan first.'}

        # Injury write-gate: never push sessions that violate active/improving
        # injury restrictions unless explicitly overridden.
        injuries = load_athlete().get('injury_history', [])
        violations = _injury_gate_violations(plan.get('days', {}), injuries)
        injury_gate_note = None
        if violations:
            if not override_injury_gate:
                return {
                    'error': 'injury_gate',
                    'message': 'Plan contains sessions restricted by active/improving injuries — push blocked',
                    'violations': violations,
                    'hint': ('Fix the plan via update_weekly_plan, or pass '
                             'override_injury_gate=True with a logged rationale '
                             'after confirming with the athlete.'),
                }
            logger.warning(
                "Injury gate OVERRIDDEN in push_plan_to_garmin: %s", violations
            )
            injury_gate_note = {
                'injury_gate_overridden': True,
                'violations': violations,
            }

        # DUPLICATE PREVENTION: Delete previously pushed workouts by stored IDs
        previous_ids = plan.get('pushed_workout_ids', [])
        deleted_count = 0

        if previous_ids:
            # Delete only the specific workouts we pushed last time. Never
            # delete the entire Garmin workout library — that would wipe
            # manually-created workouts and any history outside this plan.
            existing_workouts = garmin_api_call(lambda c: c.get_workouts())
            existing_ids = {w.get('workoutId') for w in existing_workouts}
            for wid in previous_ids:
                if wid in existing_ids:
                    try:
                        garmin_api_call(
                            lambda c, wid=wid: c.client.delete(
                                'connectapi', f'/workout-service/workout/{wid}', api=True
                            )
                        )
                        deleted_count += 1
                    except Exception:
                        pass

        results = {
            'status': 'success',
            'duplicates_deleted': deleted_count,
            'pushed': [],
            'skipped': [],
            'errors': [],
        }
        if injury_gate_note:
            results['injury_gate'] = injury_gate_note

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
                        # Generate proper description for strength sub-sessions
                        if sub.get('type', '').lower() == 'strength' and 'description' not in sub:
                            focus = sub.get('focus', '')
                            if focus:
                                # Convert focus like "lower_posterior" to "Lower Posterior Strength"
                                focus_name = focus.replace('_', ' ').title()
                                sub['description'] = f"{focus_name} Strength"
                            else:
                                sub['description'] = "Strength Training"
                        # Generate proper description for rehab sub-sessions
                        elif sub.get('type', '').lower() == 'rehab' and 'description' not in sub:
                            sub['description'] = "Ankle Rehab"
                        # Copy parent description if sub doesn't have one (non-strength/rehab)
                        elif 'description' not in sub:
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
                        # Copy structure to indoor cycling sub-sessions (for technique/interval workouts)
                        if session.get('structure') and sub.get('type', '').lower() in ['indoor_cycling', 'wattbike', 'trainer']:
                            sub['structure'] = session['structure']
                            # Also copy FTP and power_targets if present
                            if session.get('ftp'):
                                sub['ftp'] = session['ftp']
                            if session.get('power_targets'):
                                sub['power_targets'] = session['power_targets']
                            if session.get('technique_goals'):
                                sub['technique_goals'] = session['technique_goals']
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
                        upload_result = garmin_api_call(lambda c, w=workout: c.upload_cycling_workout(w))
                        workout_name = workout.workoutName
                    elif workout_type == 'running':
                        upload_result = garmin_api_call(lambda c, w=workout: c.upload_running_workout(w))
                        workout_name = workout.workoutName
                    elif workout_type in ['yoga', 'strength', 'swimming', 'pilates', 'padel']:
                        # Yoga, strength, swimming, pilates, padel use generic upload with dict format
                        upload_result = garmin_api_call(lambda c, w=workout: c.upload_workout(w))
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
                    schedule_workout(workout_id, date_str)

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

        # Store pushed workout IDs for cleanup on next push. Accumulate
        # across pushes so partial pushes don't clobber earlier history:
        # carry forward prior IDs that weren't just deleted, then add the
        # newly-pushed ones.
        pushed_ids = [p['workout_id'] for p in results['pushed'] if 'workout_id' in p]
        if pushed_ids:
            prior_ids = set(plan.get('pushed_workout_ids', []))
            deleted_ids = set(previous_ids) if previous_ids else set()
            plan['pushed_workout_ids'] = list((prior_ids - deleted_ids) | set(pushed_ids))
            from ..planner import save_json_file
            save_json_file(DATA_DIR / 'weekly_plan.json', plan)

        # Build summary message
        pushed_count = len(results['pushed'])
        if pushed_count > 0:
            dates = [p['date'] for p in results['pushed']]
            types_pushed = set(p['type'] for p in results['pushed'])
            results['summary'] = f"Pushed {pushed_count} workout(s) to Garmin ({', '.join(types_pushed)}): {dates[0]} to {dates[-1]}"
        else:
            results['summary'] = "No workouts pushed (all sessions were rest days)"

        return results

    except Exception as e:
        logger.exception("push_plan_to_garmin failed")
        return {'error': str(e)}


@mcp.tool(annotations={'readOnlyHint': True, 'openWorldHint': False})
def get_week_constraints() -> dict:
    """Get constraints and requirements for building next week's plan.

    Returns blocked days, pillar requirements, phase-appropriate session
    guidelines with duration ranges and principles, key session types from
    race template, injury restrictions, and chronic compliance misses.

    The LLM assembles the actual plan using these constraints as guardrails.
    Call this before building or adjusting a weekly plan.

    Returns:
        Structured constraints dict for week planning.
    """
    try:
        athlete = load_athlete()
        injuries = athlete.get('injury_history', [])

        # Load compliance diagnostics if available (from coaching snapshot)
        compliance_diagnostics = None
        try:
            from .coaching_tools import _build_compliance_diagnostics
            training_pillars = pillars_as_name_dict(athlete.get('training_pillars'))
            if training_pillars:
                history = load_fitness_history()
                daily_loads = history.get('daily_loads', {})
                if daily_loads:
                    today = date.today()
                    weekly_activities_4wk = []
                    for week_offset in range(4):
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
        except Exception:
            logger.warning("Could not load compliance diagnostics", exc_info=True)

        constraints = _get_week_constraints(
            athlete=athlete,
            injuries=injuries,
            compliance_diagnostics=compliance_diagnostics,
        )

        return constraints

    except Exception as e:
        logger.exception("get_week_constraints failed")
        return {'error': str(e)}
