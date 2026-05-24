"""
LLM Planning support - context building and plan management.

The LLM is the brain that generates plans. This module provides:
- Context assembly for LLM reasoning
- Plan persistence (read/write weekly_plan.json)
- Suggestion management (read/write suggestions.json)
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
    """Load a JSON file from the data directory."""
    filepath = DATA_DIR / filename
    if not filepath.exists():
        return {}
    with open(filepath) as f:
        return json.load(f)


def save_json_file(filename: str, data: dict[str, Any]) -> None:
    """Save data to a JSON file in the data directory (atomic write)."""
    DATA_DIR.mkdir(exist_ok=True)
    filepath = DATA_DIR / filename
    tmp_path = filepath.with_suffix('.tmp')
    with open(tmp_path, 'w') as f:
        json.dump(data, f, indent=2)
    tmp_path.replace(filepath)


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
    """Save the coaching log file."""
    log.setdefault('metadata', {})['last_updated'] = date.today().isoformat()
    save_json_file(COACHING_LOG_FILE, log)


def get_coaching_context() -> dict[str, Any]:
    """
    Get coaching context for LLM continuity.

    Returns:
        - active_decisions: Decisions currently influencing planning
        - pending_approvals: Changes awaiting user approval
        - response_patterns: Identified athlete adaptation patterns
        - decisions_due_review: Decisions that should be reviewed
    """
    log = load_coaching_log()
    today = date.today()

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

    Returns:
        Complete context dict (same shape tests assert against)
    """
    today = date.today()

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
        'coaching_context': get_coaching_context(),
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


def save_weekly_plan(plan: dict[str, Any]) -> None:
    """Save the weekly plan.

    Preserves internal metadata fields (e.g. pushed_workout_ids) from the
    existing file when the caller hasn't supplied them. This stops the
    coach LLM's plan-edit calls from silently dropping push-tracking state.
    """
    existing = load_json_file('weekly_plan.json') or {}
    for field in INTERNAL_PLAN_FIELDS:
        if field not in plan and field in existing:
            plan[field] = existing[field]
    plan['last_updated'] = date.today().isoformat()
    save_json_file('weekly_plan.json', plan)


def create_empty_week_template() -> dict[str, Any]:
    """
    Create an empty 7-day plan template starting from today.

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
    today = date.today()
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
    from .rules import pillars_as_name_dict
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
                req['min_mins'] = config.get('target_minutes_per_week', 0)
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

    # Active injuries / restrictions
    if injuries:
        active = [i for i in injuries if i.get('status') in ('active', 'improving')]
        if active:
            constraints['injury_restrictions'] = [
                {
                    'name': i.get('name', 'unknown'),
                    'severity': i.get('severity'),
                    'restrictions': i.get('restrictions', []),
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
