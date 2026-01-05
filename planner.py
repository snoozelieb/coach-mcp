"""
LLM Planning support - context building and plan management.

The LLM is the brain that generates plans. This module provides:
- Context assembly for LLM reasoning
- Plan persistence (read/write weekly_plan.json)
- Suggestion management (read/write suggestions.json)
"""
import json
from pathlib import Path
from datetime import date, timedelta
from typing import Any, Optional

DATA_DIR = Path(__file__).parent / "data"


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


def _get_a_race_requirements(
    upcoming_events: list[dict[str, Any]],
    training_config: dict[str, Any]
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

    # Get requirements for this race type
    race_requirements = training_config.get('race_requirements', {})
    requirements = race_requirements.get(race_type)

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
) -> dict[str, Any]:
    """
    Assemble full context for LLM planning decisions.

    This context gives the LLM everything it needs to:
    - Understand the athlete's capacity and history
    - Know the current training phase and targets
    - See what's been done vs what's required
    - Factor in recovery status for today's decisions
    - Consider any pending suggestions

    Args:
        athlete_profile: Baseline, PRs, constraints from athlete_profile.json
        training_config: Events, current block, pillars from training_config.json
        recent_activities: Last 14 days of parsed activities
        compliance_status: Current week's pillar compliance from rules.py
        today_recovery: Today's body battery, HRV, readiness
        pending_suggestions: Optional list of LLM's prior suggestions

    Returns:
        Complete context dict ready for LLM consumption
    """
    today = date.today()

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

    # Build the context
    context = {
        'today': today.isoformat(),
        'day_of_week': today.strftime('%A'),

        # Athlete capacity and history
        'athlete_profile': {
            'baseline': athlete_profile.get('baseline', {}),
            'personal_records': athlete_profile.get('personal_records', []),
            'constraints': athlete_profile.get('manual', {}).get('constraints', []),
            'injury_history': athlete_profile.get('manual', {}).get('injury_history', []),
        },

        # Current training phase
        'current_block': training_config.get('current_block', {}),
        'pillars': training_config.get('pillars', {}),
        'safety_constraints': training_config.get('constraints', {}),

        # Upcoming goals
        'upcoming_events': upcoming_events,
        'next_a_race': next(
            (e for e in upcoming_events if e.get('priority') == 'A'),
            None
        ),

        # A-race specific training requirements
        'a_race_requirements': _get_a_race_requirements(upcoming_events, training_config),

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

    Returns structure like:
    {
        'week_start': '2026-01-05',
        'days': {
            '2026-01-05': {'planned': None, 'actual': None, 'notes': ''},
            '2026-01-06': {'planned': None, 'actual': None, 'notes': ''},
            ...
        }
    }
    """
    today = date.today()
    days = {}

    for i in range(7):
        day = today + timedelta(days=i)
        days[day.isoformat()] = {
            'day_name': day.strftime('%A'),
            'planned': None,  # LLM fills this
            'actual': None,   # Filled by audit
            'status': 'pending',  # pending, completed, missed, modified
            'notes': '',
        }

    return {
        'week_start': today.isoformat(),
        'days': days,
        'generated_by': 'LLM',
        'last_updated': today.isoformat(),
    }
