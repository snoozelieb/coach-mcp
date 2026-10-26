"""SNAPSHOT FIRST as code: prescriptive writes refuse without a fresh snapshot.

The autouse conftest fixture marks a snapshot as taken for every test; these
tests reset it to exercise the gate. The gated tool list is pinned so adding
or removing a gate is a deliberate change.
"""
import os
from datetime import date, timedelta

import coach.session_state as session_state
from coach.tools.planning_tools import update_weekly_plan, training_program, update_phase
from coach.tools.injury_tools import update_injury_status
import json

TODAY = date.today()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()
PLAN = {'days': {TOMORROW: {'planned': {'type': 'running', 'purpose': 'aerobic base',
                                        'duration_mins': 30}}}}


def test_gated_tool_list_is_pinned():
    assert session_state.GATED_TOOLS == (
        'update_weekly_plan', 'training_program', 'push_plan_to_garmin',
        'update_phase', 'update_injury_status')


def test_plan_save_refused_without_a_snapshot(sandbox_data_dir):
    session_state.reset()
    result = update_weekly_plan(PLAN)
    assert result['error'] == 'snapshot_required'
    assert 'get_coaching_snapshot' in result['hint']
    assert not (sandbox_data_dir / 'weekly_plan.json').exists()


def test_plan_save_allowed_after_a_snapshot_today(sandbox_data_dir):
    session_state.mark_snapshot_taken(TODAY)
    result = update_weekly_plan(PLAN)
    assert result.get('status') == 'success', result


def test_yesterdays_snapshot_is_stale():
    session_state.mark_snapshot_taken(TODAY - timedelta(days=1))
    result = update_weekly_plan(PLAN)
    assert result['error'] == 'snapshot_stale'
    assert result['last_snapshot']['date'] == (TODAY - timedelta(days=1)).isoformat()


def test_old_snapshot_from_today_is_stale(monkeypatch):
    session_state.mark_snapshot_taken(TODAY)
    session_state._last['monotonic'] -= 3 * 3600  # three hours ago
    result = update_weekly_plan(PLAN)
    assert result['error'] == 'snapshot_stale'
    monkeypatch.setenv('COACH_SNAPSHOT_MAX_AGE_MIN', '600')
    assert update_weekly_plan(PLAN).get('status') == 'success'


def test_kill_switch_disables_the_gate(monkeypatch):
    session_state.reset()
    monkeypatch.setenv('COACH_SNAPSHOT_GATE', '0')
    assert update_weekly_plan(PLAN).get('status') == 'success'


def test_program_set_and_phase_are_gated_but_program_get_is_not():
    session_state.reset()
    assert training_program(action='get').get('error') != 'snapshot_required'
    assert training_program(action='set', program={})['error'] == 'snapshot_required'
    assert update_phase('build')['error'] == 'snapshot_required'


def test_injury_status_transition_is_gated_but_notes_are_not(sandbox_data_dir):
    (sandbox_data_dir / 'athlete.json').write_text(json.dumps({
        'injury_history': [{'date': '2026-05-20', 'type': 'knee', 'status': 'active',
                            'restricted_activities': ['running']}]}), encoding='utf-8')
    session_state.reset()
    refused = json.loads(update_injury_status('2026-05-20', new_status='improving'))
    assert refused['error'] == 'snapshot_required'
    noted = json.loads(update_injury_status('2026-05-20', notes='walked pain-free'))
    assert noted['status'] == 'success'


def test_state_reports_age_and_degraded_flag():
    session_state.mark_snapshot_taken(TODAY, degraded=True)
    state = session_state.snapshot_state()
    assert state['date'] == TODAY.isoformat()
    assert state['degraded'] is True
    assert state['age_minutes'] < 1
    assert os.environ.get('COACH_SNAPSHOT_GATE', '1') != '0'
