"""
LLM Planning support - context building and plan management.

The LLM is the brain that generates plans. This module provides:
- Context assembly for LLM reasoning
- Plan persistence (read/write weekly_plan.json)
- Coaching-log access (decisions/approvals persist in coaching_log.json)
"""
import json
from datetime import date, timedelta
from typing import Any

from .config import (
    DATA_DIR,
    ATHLETE_FILE,
    ATHLETE_BASELINE_FILE,
    METHODOLOGY_FILE,
    COACHING_LOG_FILE,
    TRAINING_CONFIG_FILE,
    RACE_TEMPLATE_WINDOW_DAYS,
    RACE_TYPE_SPORT_MAP,
)
import logging

logger = logging.getLogger(__name__)


def load_json_file(filename: str) -> dict[str, Any]:
    """Load a JSON file from the data directory.

    Delegates to coach.storage: utf-8, cross-process locked, schema-validated
    (flag-only — validation problems are logged, data is always returned).
    DATA_DIR is read at call time so tests can monkeypatch planner.DATA_DIR.
    """
    from . import storage
    return storage.read_json(filename, data_dir=DATA_DIR)


def save_json_file(filename: str, data: dict[str, Any]) -> None:
    """Save data to a JSON file in the data directory.

    Delegates to coach.storage: atomic write (unique tempfile + os.replace),
    utf-8, cross-process locked, schema_version stamping + registered
    migrations on outgoing data (with one-time .v<N>.bak before the first
    migrating write).
    """
    from . import storage
    storage.write_json(filename, data, data_dir=DATA_DIR)


def load_athlete() -> dict[str, Any]:
    """
    Load athlete profile (WHO the athlete is).

    Returns merged data from:
    - athlete.json: personal info, life constraints, injury history, preferences
    - athlete_baseline.json: Garmin-derived capacity (auto-generated)
    """
    athlete = load_json_file(ATHLETE_FILE)
    baseline = load_json_file(ATHLETE_BASELINE_FILE)

    # Merge baseline into athlete data
    if baseline:
        athlete['baseline'] = baseline.get('baseline', {})
        athlete['personal_records'] = baseline.get('personal_records', [])
        athlete['baseline_last_refreshed'] = baseline.get('last_refreshed')

    return athlete


def load_methodology() -> dict[str, Any]:
    """
    Load training methodology (HOW to train).

    Returns:
    - pillars: weekly requirements (strength 2x, mobility 90min, etc)
    - safety_constraints: max consecutive hard days, rest after race, etc
    - race_templates: key sessions and phase guidance by race type
    """
    return load_json_file(METHODOLOGY_FILE)


def load_coaching_log() -> dict[str, Any]:
    """Load the coaching log with decisions and patterns."""
    return load_json_file(COACHING_LOG_FILE)


def save_coaching_log(log: dict[str, Any]) -> None:
    """Save the coaching log file.

    The date.today() here is a write-time audit stamp (metadata.last_updated),
    not date logic — allowlisted in tests/test_clock_discipline.py.
    """
    log.setdefault('metadata', {})['last_updated'] = date.today().isoformat()
    save_json_file(COACHING_LOG_FILE, log)


def get_coaching_context(today: date) -> dict[str, Any]:
    """
    Get coaching context for LLM continuity.

    Args:
        today: Current date, resolved at the tool boundary (clock discipline)

    Returns:
        - active_decisions: Decisions currently influencing planning
        - pending_approvals: Changes awaiting user approval
        - response_patterns: Identified athlete adaptation patterns
        - decisions_due_review: Decisions that should be reviewed
    """
    log = load_coaching_log()

    decisions = log.get('decisions', [])
    pending = log.get('pending_approvals', [])
    responses = log.get('athlete_responses', [])

    # Get active decisions
    active_decisions = [d for d in decisions if d.get('status') == 'active']

    # Find decisions due for review
    due_for_review = []
    for d in active_decisions:
        review_date = d.get('review_date')
        if review_date:
            try:
                if date.fromisoformat(review_date) <= today:
                    due_for_review.append(d['id'])
            except ValueError:
                pass

    # Filter out expired pending approvals
    active_pending = []
    for p in pending:
        expires = p.get('expires')
        if expires:
            try:
                if date.fromisoformat(expires) >= today:
                    active_pending.append(p)
            except ValueError:
                active_pending.append(p)
        else:
            active_pending.append(p)

    # Extract patterns from responses
    patterns = {}
    for r in responses:
        pattern = r.get('pattern')
        if pattern:
            if pattern not in patterns:
                patterns[pattern] = {'count': 0, 'examples': []}
            patterns[pattern]['count'] += 1
            if len(patterns[pattern]['examples']) < 2:
                patterns[pattern]['examples'].append(r.get('stimulus', ''))

    return {
        'active_decisions': active_decisions,
        'decisions_due_review': due_for_review,
        'pending_approvals': active_pending,
        'response_patterns': list(patterns.keys()),
        'pattern_details': patterns,
        'recent_responses': responses[-5:] if responses else []
    }


def validate_season_config(training_config: dict[str, Any],
                           today: date) -> dict[str, Any]:
    """Season-layer config validation → data_quality flags (flag, don't fix).

    Pure function over training_config.json content. Returns a dict of flags
    the snapshot merges into data_quality — the coach must SURFACE these to
    the athlete; nothing is ever auto-fixed:

    - block_dates_invalid: list of date-consistency problems
      (current_block.end_date before start_date; periodization
      target_transition before phase_start)
    - a_race_in_past: {name, date, days_ago} — the configured A race already
      happened; debrief it and re-plan the season
    - no_upcoming_events: True when no configured event is today or later
    - phase_overdue: {target_transition, days_overdue} when
      periodization.target_transition has passed
    """
    config = training_config or {}
    flags: dict[str, Any] = {}

    def _parse(value):
        try:
            return date.fromisoformat(value) if value else None
        except (ValueError, TypeError):
            return None

    block = config.get('current_block') or {}
    periodization = config.get('periodization') or {}

    problems = []
    block_start = _parse(block.get('start_date'))
    block_end = _parse(block.get('end_date'))
    if block_start and block_end and block_end < block_start:
        problems.append(
            f"current_block.end_date ({block.get('end_date')}) is before "
            f"start_date ({block.get('start_date')})")
    phase_start = _parse(periodization.get('phase_start'))
    transition = _parse(periodization.get('target_transition'))
    if phase_start and transition and transition < phase_start:
        problems.append(
            f"periodization.target_transition "
            f"({periodization.get('target_transition')}) is before "
            f"phase_start ({periodization.get('phase_start')})")
    if problems:
        flags['block_dates_invalid'] = problems

    has_upcoming = False
    past_a_races = []
    for event in config.get('events') or []:
        if not isinstance(event, dict):
            continue
        event_date = _parse(event.get('date'))
        if event_date is None:
            continue
        if event_date >= today:
            has_upcoming = True
        elif str(event.get('priority') or '').upper() == 'A':
            past_a_races.append((event_date, event))
    if past_a_races:
        race_date, race = max(past_a_races, key=lambda pair: pair[0])
        flags['a_race_in_past'] = {
            'name': race.get('name'),
            'date': race.get('date'),
            'days_ago': (today - race_date).days,
        }
    if not has_upcoming:
        flags['no_upcoming_events'] = True

    if transition and transition < today:
        flags['phase_overdue'] = {
            'target_transition': periodization.get('target_transition'),
            'days_overdue': (today - transition).days,
        }

    return flags


def _get_a_race_requirements(
    upcoming_events: list[dict[str, Any]],
    training_config: dict[str, Any],
    methodology: dict[str, Any]
) -> dict[str, Any] | None:
    """
    Get training requirements for the A-race based on its type.

    Returns race requirements with key sessions and phase guidance,
    or None if no A-race or requirements not defined.
    """
    # Find A-race
    a_race = next(
        (e for e in upcoming_events if e.get('priority') == 'A'),
        None
    )
    if not a_race:
        return None

    race_type = a_race.get('type')
    if not race_type:
        return None

    # Get requirements for this race type from methodology
    race_templates = methodology.get('race_templates', {})
    requirements = race_templates.get(race_type)

    if not requirements:
        return None

    # Get current phase for phase-specific guidance
    current_block = training_config.get('current_block', {})
    current_phase = current_block.get('phase', 'base')
    phase_guidance = requirements.get('phase_guidance', {})

    return {
        'race_name': a_race.get('name'),
        'race_type': race_type,
        'days_until': a_race.get('days_until'),
        'description': requirements.get('description'),
        'key_sessions': requirements.get('key_sessions', []),
        'current_phase': current_phase,
        'current_phase_guidance': phase_guidance.get(current_phase, ''),
        'all_phase_guidance': phase_guidance,
    }


def build_planning_context(
    athlete_profile: dict[str, Any],
    training_config: dict[str, Any],
    recent_activities: list[dict[str, Any]],
    compliance_status: dict[str, Any],
    today_recovery: dict[str, Any],
    pending_suggestions: list[dict[str, Any]] = None,
    methodology: dict[str, Any] = None,
    *,
    today: date,
) -> dict[str, Any]:
    """
    Assemble a context dict from already-loaded inputs.

    Used by tests to validate the shape of planning context. Production
    callers should use get_coaching_snapshot() instead — the standalone
    get_planning_context tool that wrapped this helper was removed in
    the Phase 2 rationalization.

    Args:
        athlete_profile: Athlete data from athlete.json + athlete_baseline.json
        training_config: Events, current block from training_config.json
        recent_activities: Last 14 days of parsed activities
        compliance_status: Current week's pillar compliance from rules.py
        today_recovery: Today's body battery, HRV, readiness
        pending_suggestions: Legacy kwarg, pass-through only (the suggestion
            workflow was consolidated into the unified proposal API)
        methodology: Pillars, constraints, race_templates from methodology.json
        today: Current date, resolved at the tool boundary (clock discipline)

    Returns:
        Complete context dict (same shape tests assert against)
    """
    # Load methodology if not provided
    if methodology is None:
        methodology = load_methodology()

    # Extract ALL upcoming events (for periodization context)
    events = training_config.get('events', [])
    upcoming_events = []
    for event in events:
        try:
            event_date = date.fromisoformat(event.get('date', ''))
            days_until = (event_date - today).days
            if days_until >= 0:  # Include all future events
                event_copy = event.copy()
                event_copy['days_until'] = days_until
                upcoming_events.append(event_copy)
        except ValueError:
            continue
    upcoming_events.sort(key=lambda e: e['days_until'])

    # Load current weekly plan for context
    current_plan = get_current_plan()

    # Get race templates for upcoming B/C races (within 8 weeks)
    race_templates = methodology.get('race_templates', {})
    relevant_race_templates = {}
    for event in upcoming_events:
        if event.get('days_until', 999) <= RACE_TEMPLATE_WINDOW_DAYS:
            race_type = event.get('type')
            priority = event.get('priority', 'C')
            if race_type and race_type in race_templates and priority in ['A', 'B', 'C']:
                if race_type not in relevant_race_templates:
                    relevant_race_templates[race_type] = {
                        'template': race_templates[race_type],
                        'races_using': []
                    }
                relevant_race_templates[race_type]['races_using'].append({
                    'name': event.get('name'),
                    'days_until': event.get('days_until'),
                    'priority': priority
                })

    # Build the context
    context = {
        'today': today.isoformat(),
        'day_of_week': today.strftime('%A'),

        # WHO - Athlete profile (personal + Garmin-derived)
        'athlete_profile': {
            'personal': athlete_profile.get('personal', {}),
            'life_constraints': athlete_profile.get('life_constraints', {}),
            'injury_history': athlete_profile.get('injury_history', []),
            'preferences': athlete_profile.get('preferences', {}),
            'coaching_notes': athlete_profile.get('coaching_notes', ''),
            'baseline': athlete_profile.get('baseline', {}),
            'personal_records': athlete_profile.get('personal_records', []),
        },

        # WHAT - Current training phase and goals
        'current_block': training_config.get('current_block', {}),

        # HOW - Training methodology
        'pillars': methodology.get('pillars', {}),
        'safety_constraints': methodology.get('safety_constraints', {}),

        # Upcoming goals
        'upcoming_events': upcoming_events,
        'next_a_race': next(
            (e for e in upcoming_events if e.get('priority') == 'A'),
            None
        ),

        # A-race specific training requirements
        'a_race_requirements': _get_a_race_requirements(
            upcoming_events, training_config, methodology
        ),

        # All relevant race templates (for B/C races within 8 weeks)
        'relevant_race_templates': relevant_race_templates,

        # Current weekly plan (for continuity)
        'current_weekly_plan': current_plan,

        # Recent history
        'recent_activities': recent_activities,
        'activities_last_7_days': [
            a for a in recent_activities
            if a.get('date') and (today - date.fromisoformat(a['date'])).days <= 7
        ],

        # Current compliance status
        'compliance': compliance_status,

        # Today's recovery status
        'recovery': today_recovery,

        # Pending LLM suggestions (if any)
        'pending_suggestions': pending_suggestions or [],

        # Coaching continuity (decisions, patterns, approvals)
        'coaching_context': get_coaching_context(today),
    }

    # Add active injuries with restrictions for easy reference
    injury_history = athlete_profile.get('injury_history', [])
    active_injuries = [
        injury for injury in injury_history
        if injury.get('status', 'active') == 'active'
    ]

    if active_injuries:
        # Collect all restricted activities from active injuries
        all_restricted = set()
        all_safe = set()
        for injury in active_injuries:
            all_restricted.update(injury.get('restricted_activities', []))
            all_safe.update(injury.get('safe_activities', []))

        context['active_injuries'] = {
            'count': len(active_injuries),
            'injuries': active_injuries,
            'restricted_activities': list(all_restricted),
            'safe_activities': list(all_safe),
            'warning': f"Athlete has {len(active_injuries)} active injury/injuries. Avoid: {', '.join(all_restricted) if all_restricted else 'see individual injuries'}",
        }

    return context


def get_current_plan() -> dict[str, Any]:
    """Load the current weekly plan."""
    return load_json_file('weekly_plan.json')


INTERNAL_PLAN_FIELDS = ('pushed_workout_ids',)

PLAN_HISTORY_FILE = 'plan_history.json'
PLAN_RETENTION_DAYS = 9  # Day entries older than this (before today) are archived


def _prune_and_archive_plan_days(plan: dict[str, Any], today: date) -> None:
    """Prune day entries older than PLAN_RETENTION_DAYS before today.

    Pruned days are archived by appending to data/plan_history.json so plan
    history is never silently lost. Mutates `plan` in place.
    """
    days = plan.get('days')
    if not isinstance(days, dict):
        return

    cutoff = (today - timedelta(days=PLAN_RETENTION_DAYS)).isoformat()
    pruned = {d: v for d, v in days.items() if d < cutoff}
    if not pruned:
        return

    plan['days'] = {d: v for d, v in days.items() if d >= cutoff}

    try:
        archive = load_json_file(PLAN_HISTORY_FILE) or {}
        entries = archive.get('archived_days', [])
        already_archived = {e.get('date') for e in entries if isinstance(e, dict)}
        for day_str in sorted(pruned):
            if day_str in already_archived:
                continue
            day_entry = pruned[day_str] if isinstance(pruned[day_str], dict) else {'value': pruned[day_str]}
            entries.append({'date': day_str, **day_entry})
        archive['archived_days'] = entries
        archive['last_archived'] = today.isoformat()
        save_json_file(PLAN_HISTORY_FILE, archive)
    except Exception:
        logger.warning("Failed to archive pruned plan days", exc_info=True)


def save_weekly_plan(plan: dict[str, Any], today: date | None = None) -> None:
    """Save the weekly plan.

    Preserves internal metadata fields (e.g. pushed_workout_ids) from the
    existing file when the caller hasn't supplied them. This stops the
    coach LLM's plan-edit calls from silently dropping push-tracking state.

    Also enforces plan lifecycle: day entries older than PLAN_RETENTION_DAYS
    are pruned (archived to plan_history.json), and week_start/week_end are
    derived from the day keys when missing.

    `today` should be threaded from the tool boundary. The internal
    date.today() fallback exists because scripts/daily_loop.py calls this
    bare — that one resolution line is allowlisted in
    tests/test_clock_discipline.py (storage boundary).
    """
    if today is None:
        today = date.today()
    existing = load_json_file('weekly_plan.json') or {}
    for field in INTERNAL_PLAN_FIELDS:
        if field not in plan and field in existing:
            plan[field] = existing[field]

    # Prune stale days first, then derive week bounds from what remains
    _prune_and_archive_plan_days(plan, today)

    days = plan.get('days')
    if isinstance(days, dict):
        valid_keys = []
        for k in days:
            try:
                date.fromisoformat(k)
                valid_keys.append(k)
            except (ValueError, TypeError):
                continue
        if valid_keys:
            if not plan.get('week_start'):
                plan['week_start'] = min(valid_keys)
            if not plan.get('week_end'):
                plan['week_end'] = max(valid_keys)

    plan['last_updated'] = today.isoformat()
    save_json_file('weekly_plan.json', plan)


def create_empty_week_template(today: date) -> dict[str, Any]:
    """
    Create an empty 7-day plan template starting from today.

    Args:
        today: Current date, resolved at the tool boundary (clock discipline)

    Returns a dict with:
        - week_start, week_end: ISO date strings
        - days: dict keyed by ISO date with day structures

    Day structure fields:
        - day_name: e.g., "Monday"
        - planned: session dict or None (see below)
        - actual: filled by audit after completion
        - status: "pending" | "completed" | "missed" | "modified"
        - notes: optional string

    Planned session fields:
        - type: e.g., "long_ride", "strength", "mobility", "double_session", "rest"
        - description: human-readable summary
        - duration_mins: total duration
        - intensity: "easy" | "moderate" | "hard" | "max_effort" (optional)
        - priority: "critical" | "high" | "medium" (optional)
        - purpose: REQUIRED - explains WHY this session matters
        - goal_category: "race_preparation" | "fun_activities" | "aesthetics"
        - phase_alignment: current training phase (optional)

    For double sessions, add:
        - sessions: list of {time, type, duration_mins, notes}

    For strength sessions, add:
        - exercises: list of {name, category, sets, reps, rest_secs}

    For test sessions (FTP, time trial), add:
        - protocol: list of {phase, duration_mins, notes}

    For swimming sessions, add:
        - target_distance_m: total target distance
        - pool_length_m: pool length (default 25)
        - structure: list of {phase, distance_m, stroke, pace, notes}
          phases: warmup, drills, main, cooldown
          Example: [
            {"phase": "warmup", "distance_m": 200, "stroke": "freestyle", "pace": "easy"},
            {"phase": "drills", "distance_m": 200, "notes": "Catch-up, fingertip drag"},
            {"phase": "main", "distance_m": 400, "notes": "4x100m steady, 15s rest"},
            {"phase": "cooldown", "distance_m": 100, "stroke": "easy choice"}
          ]
        Note: Check athlete.swimming profile for experience level and pace.

    For pilates/yoga sessions, add:
        - focus: primary focus area (core, flexibility, strength, full_body)
        - target_areas: list of body regions to emphasize
        - avoid: movements to skip (from injury considerations)
        - class_or_solo: "class" | "solo" | "video"
        Example: {
            "type": "pilates",
            "duration_mins": 45,
            "focus": "core",
            "target_areas": ["hip_flexors", "lower_back", "glutes"],
            "avoid": ["standing_balance"],
            "notes": "Post-ride recovery focus on hip mobility"
        }
        Note: Check athlete.pilates profile for experience and injury considerations.
    """
    days = {}

    for i in range(7):
        day = today + timedelta(days=i)
        days[day.isoformat()] = {
            'day_name': day.strftime('%A'),
            'planned': None,  # LLM fills with: type, description, duration_mins, intensity, purpose, goal_category
            'actual': None,   # Filled by audit
            'status': 'pending',  # pending, completed, missed, modified
            'notes': '',
        }

    return {
        'week_start': today.isoformat(),
        'week_end': (today + timedelta(days=6)).isoformat(),
        'days': days,
        'generated_by': 'LLM',
        'last_updated': today.isoformat(),
    }


def get_week_constraints(
    athlete: dict = None,
    training_config: dict = None,
    methodology: dict = None,
    injuries: list = None,
    compliance_diagnostics: dict = None,
) -> dict:
    """Return constraints and requirements for the LLM to build a week.

    Assembles structured reference data from athlete profile, training config,
    and methodology. The LLM uses this to construct the actual plan.

    All parameters are optional — loads from files if not provided.
    """
    if athlete is None:
        athlete = load_athlete()
    if training_config is None:
        tc_path = DATA_DIR / TRAINING_CONFIG_FILE
        training_config = json.loads(tc_path.read_text()) if tc_path.exists() else {}
    if methodology is None:
        methodology = load_methodology()
    if injuries is None:
        injuries = []

    constraints = {}

    # Blocked days from life constraints
    life_constraints = athlete.get('life_constraints', {})
    blocked = life_constraints.get('blocked_days', [])
    if blocked:
        constraints['blocked_days'] = blocked

    # Available training days
    available_days = life_constraints.get('available_days')
    if available_days:
        constraints['available_days'] = available_days

    # Pillar requirements
    from .rules import pillars_as_name_dict, pillar_target_minutes
    pillars = pillars_as_name_dict(athlete.get('training_pillars'))
    if pillars:
        pillar_reqs = {}
        for name, config in pillars.items():
            req = {'types': config.get('types', [])}
            target_type = config.get('target_type', 'sessions')
            if target_type == 'sessions':
                req['min_sessions'] = config.get('target_sessions_per_week', 0)
            elif target_type == 'hours':
                req['min_mins'] = round(config.get('target_hours_per_week', 0) * 60)
            elif target_type == 'minutes':
                req['min_mins'] = pillar_target_minutes(config)
            pillar_reqs[name] = req
        constraints['pillar_requirements'] = pillar_reqs

    # Current phase
    current_block = training_config.get('current_block', {})
    phase = current_block.get('phase', 'base')
    constraints['phase'] = phase

    # A-race info and key sessions from race template
    events = training_config.get('events', [])
    a_race = next((e for e in events if e.get('priority') == 'A'), None)
    if a_race:
        race_type = a_race.get('type', 'default')
        template = methodology.get('race_templates', {}).get(race_type, {})
        key_sessions = template.get('key_sessions', [])
        phase_guidance = template.get('phase_guidance', {}).get(phase, '')

        constraints['a_race'] = {
            'name': a_race.get('name'),
            'type': race_type,
            'date': a_race.get('date'),
            'sport': RACE_TYPE_SPORT_MAP.get(race_type),
        }
        if key_sessions:
            constraints['key_session_types'] = [
                s.get('type') for s in key_sessions if s.get('priority') in ('critical', 'high')
            ]
        if phase_guidance:
            constraints['phase_guidance'] = phase_guidance

    # Session guidelines (phase-appropriate parameters)
    session_guidelines = methodology.get('session_guidelines', {})
    if session_guidelines:
        phase_guidelines = {}
        for session_type, phases in session_guidelines.items():
            if session_type.startswith('_'):
                continue
            guideline = phases.get(phase)
            if guideline:
                phase_guidelines[session_type] = guideline
        if phase_guidelines:
            constraints['session_guidelines'] = phase_guidelines

    # Active injuries / restrictions — normalize records first: real injury
    # records use 'type'/'restricted_activities', not 'name'/'restrictions'.
    if injuries:
        from .rules import normalize_injury
        active = [
            normalize_injury(i) for i in injuries
            if isinstance(i, dict) and i.get('status') in ('active', 'improving')
        ]
        if active:
            constraints['injury_restrictions'] = [
                {
                    'name': i['name'],
                    'status': i['status'],
                    'severity': i.get('severity'),
                    'restricted_activities': i['restricted_activities'],
                    # Legacy alias kept for older consumers
                    'restrictions': i['restricted_activities'],
                }
                for i in active
            ]

    # Chronic compliance misses (from diagnostics)
    if compliance_diagnostics and compliance_diagnostics.get('per_pillar'):
        chronic = [
            name for name, data in compliance_diagnostics['per_pillar'].items()
            if data.get('chronic_miss')
        ]
        if chronic:
            constraints['chronic_misses'] = chronic

    return constraints
