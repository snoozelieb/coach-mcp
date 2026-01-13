"""
LLM Planning support - context building and plan management.

The LLM is the brain that generates plans. This module provides:
- Context assembly for LLM reasoning
- Plan persistence (read/write weekly_plan.json)
- Suggestion management (read/write suggestions.json)
"""
import json
from datetime import date, timedelta
from typing import Any, Optional

from config import (
    DATA_DIR,
    ATHLETE_FILE,
    ATHLETE_BASELINE_FILE,
    METHODOLOGY_FILE,
    COACHING_LOG_FILE,
    RACE_TEMPLATE_WINDOW_DAYS,
)


def load_json_file(filename: str) -> dict[str, Any]:
    """Load a JSON file from the data directory."""
    filepath = DATA_DIR / filename
    if not filepath.exists():
        return {}
    with open(filepath) as f:
        return json.load(f)


def save_json_file(filename: str, data: dict[str, Any]) -> None:
    """Save data to a JSON file in the data directory."""
    DATA_DIR.mkdir(exist_ok=True)
    filepath = DATA_DIR / filename
    with open(filepath, 'w') as f:
        json.dump(data, f, indent=2)


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
    Assemble full context for LLM planning decisions.

    This context gives the LLM everything it needs to:
    - Understand WHO the athlete is (personal, life constraints, preferences)
    - Know WHAT they're training for (events, current phase)
    - Understand HOW to train (pillars, safety rules, race templates)
    - See what's been done vs what's required
    - Factor in recovery status for today's decisions
    - Consider any pending suggestions

    Args:
        athlete_profile: Athlete data from athlete.json + athlete_baseline.json
        training_config: Events, current block from training_config.json
        recent_activities: Last 14 days of parsed activities
        compliance_status: Current week's pillar compliance from rules.py
        today_recovery: Today's body battery, HRV, readiness
        pending_suggestions: Optional list of LLM's prior suggestions
        methodology: Pillars, constraints, race_templates from methodology.json

    Returns:
        Complete context dict ready for LLM consumption
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


def save_weekly_plan(plan: dict[str, Any]) -> None:
    """Save the weekly plan."""
    plan['last_updated'] = date.today().isoformat()
    save_json_file('weekly_plan.json', plan)


def get_pending_suggestions() -> list[dict[str, Any]]:
    """Load pending suggestions from the LLM."""
    data = load_json_file('suggestions.json')
    return data.get('pending', [])


def save_suggestion(suggestion: dict[str, Any]) -> str:
    """
    Add a new suggestion from the LLM.

    Returns the suggestion ID.
    """
    data = load_json_file('suggestions.json')
    if 'pending' not in data:
        data['pending'] = []
    if 'history' not in data:
        data['history'] = []

    # Generate ID
    suggestion_id = f"sug_{date.today().isoformat()}_{len(data['pending']) + 1}"
    suggestion['id'] = suggestion_id
    suggestion['created'] = date.today().isoformat()
    suggestion['status'] = 'pending'

    data['pending'].append(suggestion)
    save_json_file('suggestions.json', data)

    return suggestion_id


def approve_suggestion(suggestion_id: str) -> Optional[dict[str, Any]]:
    """
    Approve a suggestion and move it to history.

    Returns the approved suggestion or None if not found.
    """
    data = load_json_file('suggestions.json')
    pending = data.get('pending', [])
    history = data.get('history', [])

    # Find the suggestion
    for i, s in enumerate(pending):
        if s.get('id') == suggestion_id:
            suggestion = pending.pop(i)
            suggestion['status'] = 'approved'
            suggestion['resolved'] = date.today().isoformat()
            history.append(suggestion)

            data['pending'] = pending
            data['history'] = history
            save_json_file('suggestions.json', data)

            return suggestion

    return None


def reject_suggestion(suggestion_id: str, reason: str = None) -> Optional[dict[str, Any]]:
    """
    Reject a suggestion and move it to history.

    Returns the rejected suggestion or None if not found.
    """
    data = load_json_file('suggestions.json')
    pending = data.get('pending', [])
    history = data.get('history', [])

    # Find the suggestion
    for i, s in enumerate(pending):
        if s.get('id') == suggestion_id:
            suggestion = pending.pop(i)
            suggestion['status'] = 'rejected'
            suggestion['resolved'] = date.today().isoformat()
            if reason:
                suggestion['rejection_reason'] = reason
            history.append(suggestion)

            data['pending'] = pending
            data['history'] = history
            save_json_file('suggestions.json', data)

            return suggestion

    return None


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
