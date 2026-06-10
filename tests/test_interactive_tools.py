"""Tests for tools/interactive_tools.py — data-first brief + check-in payloads.

These two tools replaced the dead sampling/elicitation paths: they gather and
shape data for the LLM coach to render. The tests seed the sandbox DATA_DIR
and assert the structured payloads — grounding (time context), today's plan
lookup, fitness metrics, injury filtering, and the question set contract.
"""
import json
from datetime import date, timedelta

from coach.tools.interactive_tools import (
    generate_smart_brief,
    interactive_check_in,
)

TIME_PERIODS = ('early_morning', 'morning', 'afternoon', 'evening', 'night')

TODAY_SESSION = {'type': 'cycling', 'duration_mins': 90,
                 'purpose': 'Z2 aerobic base', 'intensity': 'easy'}


def _seed_brief_env(data_dir):
    today = date.today()

    (data_dir / 'athlete.json').write_text(json.dumps({
        'personal': {'name': 'Test Athlete', 'age': 36},
        'injury_history': [
            {'location': 'shin', 'status': 'active', 'date': '2026-05-20'},
            {'location': 'knee', 'status': 'improving', 'date': '2026-04-01'},
            {'location': 'ankle', 'status': 'resolved', 'date': '2025-11-01'},
        ],
    }))

    (data_dir / 'weekly_plan.json').write_text(json.dumps({
        'week_start': today.isoformat(),
        'week_end': (today + timedelta(days=6)).isoformat(),
        'days': {today.isoformat(): {'planned': dict(TODAY_SESSION)}},
    }))

    daily_loads = {
        (today - timedelta(days=i)).isoformat(): {
            'total': 50.0, 'by_sport': {'cycling': 50.0}, 'activities': []}
        for i in range(14)
    }
    (data_dir / 'fitness_history.json').write_text(json.dumps({
        'schema_version': 2, 'daily_loads': daily_loads, 'snapshots': [],
        'sleep_history': [], 'readiness_history': [],
        'last_updated': today.isoformat(),
    }))

    (data_dir / 'training_config.json').write_text(json.dumps({
        'events': [
            {'name': 'Near Race', 'priority': 'B',
             'date': (today + timedelta(days=10)).isoformat()},
            {'name': 'A Race', 'priority': 'A',
             'date': (today + timedelta(days=40)).isoformat()},
            {'name': 'Past Race', 'priority': 'C',
             'date': (today - timedelta(days=10)).isoformat()},
        ],
    }))

    (data_dir / 'coaching_log.json').write_text(json.dumps({
        'decisions': [
            {'id': 'd1', 'type': 'load_adjustment',
             'decision': 'hold volume', 'status': 'active'},
            {'id': 'd2', 'type': 'phase_transition',
             'decision': 'base to build', 'status': 'completed'},
            {'id': 'd3', 'type': 'recovery_protocol',
             'decision': 'extra rest day', 'status': 'active'},
        ],
        'athlete_responses': [],
        'pending_approvals': [],
    }))
    return today


class TestGenerateSmartBrief:
    def test_full_payload_grounded_in_seeded_data(self, sandbox_data_dir):
        today = _seed_brief_env(sandbox_data_dir)

        result = json.loads(generate_smart_brief())

        assert 'error' not in result
        # Time grounding
        ctx = result['current_time_context']
        assert ctx['date'] == today.isoformat()
        assert result['time_period'] in TIME_PERIODS
        assert result['day'] == today.strftime('%A')
        # Framing carries the name + the time so the greeting can't drift
        assert 'Test Athlete' in result['framing']
        assert result['time_period'] in result['framing']
        # Today's plan resolved from weekly_plan.json
        assert result['today_plan'] == TODAY_SESSION
        # Fitness from the seeded daily loads
        fitness = result['fitness']
        assert isinstance(fitness['ctl'], (int, float))
        assert isinstance(fitness['acwr'], (int, float))
        # Only active/improving injuries surface
        assert [(i['location'], i['status'])
                for i in result['active_injuries']] == [
            ('shin', 'active'), ('knee', 'improving')]
        # Upcoming events: soonest first, capped at 2, past events dropped
        assert [e['name'] for e in result['upcoming_events']] == [
            'Near Race', 'A Race']
        assert result['upcoming_events'][0]['days_until'] == 10
        # Active decisions only
        assert [d['summary'] for d in result['recent_decisions']] == [
            'hold volume', 'extra rest day']

    def test_empty_data_dir_degrades_gracefully(self, sandbox_data_dir):
        """No data files at all: the brief still renders with defaults
        instead of erroring — a fresh install can ask for a brief."""
        result = json.loads(generate_smart_brief())

        assert 'error' not in result
        assert result['athlete_name'] == 'Athlete'
        assert result['today_plan'] is None
        assert result['fitness'] is None
        assert result['active_injuries'] == []
        assert result['upcoming_events'] == []
        assert result['recent_decisions'] == []

    def test_no_session_today_returns_none_plan(self, sandbox_data_dir):
        today = date.today()
        (sandbox_data_dir / 'weekly_plan.json').write_text(json.dumps({
            'week_start': today.isoformat(),
            'week_end': (today + timedelta(days=6)).isoformat(),
            'days': {(today + timedelta(days=1)).isoformat():
                     {'planned': {'type': 'rest'}}},
        }))

        result = json.loads(generate_smart_brief())

        assert result['today_plan'] is None


class TestInteractiveCheckIn:
    def test_question_set_contract(self, sandbox_data_dir):
        today = _seed_brief_env(sandbox_data_dir)

        result = json.loads(interactive_check_in())

        assert 'error' not in result
        assert result['current_time_context']['date'] == today.isoformat()
        assert result['today_planned'] == TODAY_SESSION

        questions = {q['id']: q for q in result['questions']}
        assert set(questions) == {'feeling', 'sleep', 'niggles'}
        # feeling + sleep are option questions; niggles is free text
        assert len(questions['feeling']['options']) >= 4
        assert len(questions['sleep']['options']) >= 4
        assert questions['niggles']['type'] == 'free_text'
        # The coaching note tells the LLM to ask conversationally and carries
        # the current time period
        assert result['current_time_context']['time_period'] in (
            result['coaching_note'])

    def test_works_without_plan(self, sandbox_data_dir):
        result = json.loads(interactive_check_in())

        assert 'error' not in result
        assert result['today_planned'] is None
        assert len(result['questions']) == 3
