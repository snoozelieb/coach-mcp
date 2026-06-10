"""Tests for the Phase 2 typed plan input + structured planning-tool output.

Covers:
- update_weekly_plan accepts a typed dict via `plan` (validated through
  schemas.WeeklyPlan; errors name the offending day/field)
- `plan_json` stays as a deprecated alias (exactly one of the two required)
- `purpose_warnings` emitted for non-rest sessions missing a purpose (warn-only)
- the Phase 0 injury write-gate still fires through the typed path
- all seven planning tools return structured dicts (not JSON strings)
"""
import json
from datetime import date, timedelta

import pytest

import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_mod
import coach.parsers as parsers_mod
import coach.workout_builder as workout_builder
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod

from coach.schemas import Session, WeeklyPlan
from coach.tools.planning_tools import (
    get_periodization_status,
    get_weekly_prescription,
    update_phase,
    get_weekly_plan,
    update_weekly_plan,
    push_plan_to_garmin,
    get_week_constraints,
    _missing_purpose_sessions,
)

TODAY = date.today()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()
DAY_AFTER = (TODAY + timedelta(days=2)).isoformat()


# ---------------------------------------------------------------------------
# Environment helpers (same pattern as tests/test_phase0_fixes.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def data_env(data_dir, monkeypatch):
    """Redirect DATA_DIR in every module that does file I/O to a tmp dir."""
    for mod in (planner, rules, fitness_mod, parsers_mod, workout_builder,
                coaching_mod, planning_mod):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    return data_dir


def _write(data_dir, filename, payload):
    (data_dir / filename).write_text(json.dumps(payload), encoding='utf-8')


def _session(**overrides):
    base = {'type': 'cycling', 'duration_mins': 60, 'intensity': 'easy',
            'purpose': 'Aerobic base'}
    base.update(overrides)
    return base


class FakeGarminClient:
    """Minimal fake for the Garmin methods get_weekly_prescription uses."""

    def get_training_readiness(self, d):
        return []

    def get_activities_by_date(self, start, end):
        return []


ACTIVE_RUN_INJURY = {
    'date': '2026-06-01', 'type': 'shin', 'body_region': 'shin',
    'status': 'active', 'severity': 'moderate',
    'restricted_activities': ['running'],
    'safe_activities': ['cycling'],
}


# ---------------------------------------------------------------------------
# Schema refinements
# ---------------------------------------------------------------------------

class TestSessionSchema:
    def test_type_is_required(self):
        with pytest.raises(Exception):
            Session.model_validate({'duration_mins': 30})

    def test_discretion_intensity_with_constraints_validates(self):
        s = Session.model_validate({
            'type': 'cycling', 'intensity': 'discretion',
            'constraints': ['Z2 only', 'no running'],
            'purpose': 'Unstructured fun',
        })
        assert s.intensity == 'discretion'
        assert s.constraints == ['Z2 only', 'no running']

    def test_weekly_plan_shape_unchanged(self):
        plan = WeeklyPlan.model_validate({
            'days': {TOMORROW: {'planned': {'type': 'running'}}},
            'rationale': 'why',
        })
        assert plan.rationale == 'why'
        assert TOMORROW in plan.days


# ---------------------------------------------------------------------------
# Typed plan input
# ---------------------------------------------------------------------------

class TestTypedPlanInput:
    def test_typed_plan_accepted_and_saved(self, data_env):
        plan = {'days': {TOMORROW: {'planned': _session()}},
                'rationale': 'easy week'}

        result = update_weekly_plan(plan)

        assert isinstance(result, dict)
        assert result['status'] == 'success'
        assert result['purpose_warnings'] == []
        saved = json.loads((data_env / 'weekly_plan.json').read_text())
        assert saved['days'][TOMORROW]['planned']['type'] == 'cycling'
        assert saved['rationale'] == 'easy week'

    def test_bad_day_key_rejected(self, data_env):
        plan = {'days': {'next tuesday': {'planned': _session()}}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'validation_error'
        assert any(p['field'] == 'days' and 'next tuesday' in p['message']
                   for p in result['problems'])
        assert not (data_env / 'weekly_plan.json').exists()

    def test_missing_session_type_rejected_with_named_field(self, data_env):
        plan = {'days': {TOMORROW: {'planned': {'duration_mins': 30}}}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'validation_error'
        problem = next(p for p in result['problems']
                       if p['day'] == TOMORROW and p['field'] == 'planned.type')
        assert 'required' in problem['message'].lower()
        assert not (data_env / 'weekly_plan.json').exists()

    def test_non_string_session_type_rejected(self, data_env):
        plan = {'days': {TOMORROW: {'planned': _session(type=123)}}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'validation_error'
        assert any(p['day'] == TOMORROW and p['field'] == 'planned.type'
                   for p in result['problems'])

    def test_list_form_error_names_session_index(self, data_env):
        plan = {'days': {TOMORROW: {'planned': [
            _session(type='running', purpose='long effort'),
            {'duration_mins': 20},  # second session missing type
        ]}}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'validation_error'
        assert any(p['day'] == TOMORROW and p['field'] == 'planned.1.type'
                   for p in result['problems'])

    def test_structural_errors_still_pre_empt_validation(self, data_env):
        assert 'days' in update_weekly_plan({'week_start': TOMORROW})['error']
        assert 'days' in update_weekly_plan({'days': 'not a dict'})['error']
        assert 'object' in update_weekly_plan([1, 2, 3])['error']


# ---------------------------------------------------------------------------
# plan_json deprecated alias + exactly-one enforcement
# ---------------------------------------------------------------------------

class TestPlanJsonAlias:
    def test_plan_json_alias_still_works(self, data_env):
        plan = {'days': {TOMORROW: {'planned': _session()}}}

        result = update_weekly_plan(plan_json=json.dumps(plan))

        assert result['status'] == 'success'
        assert (data_env / 'weekly_plan.json').exists()

    def test_legacy_positional_json_string_tolerated(self, data_env):
        """Pre-typed clients passed the JSON string positionally."""
        plan = {'days': {TOMORROW: {'planned': _session()}}}

        result = update_weekly_plan(json.dumps(plan))

        assert result['status'] == 'success'

    def test_plan_json_goes_through_same_validation(self, data_env):
        plan = {'days': {TOMORROW: {'planned': {'duration_mins': 30}}}}

        result = update_weekly_plan(plan_json=json.dumps(plan))

        assert result['error'] == 'validation_error'

    def test_both_given_is_an_error(self, data_env):
        plan = {'days': {TOMORROW: {'planned': _session()}}}

        result = update_weekly_plan(plan, plan_json=json.dumps(plan))

        assert 'error' in result
        assert 'not both' in result['error']
        assert not (data_env / 'weekly_plan.json').exists()

    def test_none_given_is_an_error(self, data_env):
        result = update_weekly_plan()

        assert 'error' in result
        assert 'No plan provided' in result['error']

    def test_invalid_json_string_is_an_error(self, data_env):
        result = update_weekly_plan(plan_json='{not valid json')

        assert 'error' in result
        assert 'Invalid JSON' in result['error']


# ---------------------------------------------------------------------------
# purpose_warnings (warn-only; Phase 3 makes it a gate)
# ---------------------------------------------------------------------------

class TestPurposeWarnings:
    def test_non_rest_sessions_missing_purpose_are_listed(self, data_env):
        plan = {'days': {
            TOMORROW: {'planned': _session(purpose=None, name='No-why ride')},
            DAY_AFTER: {'planned': {'type': 'rest'}},
        }}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success', "warn-only — must still save"
        assert result['purpose_warnings'] == [{
            'date': TOMORROW, 'type': 'cycling', 'name': 'No-why ride',
            'warning': 'missing purpose',
        }]
        assert 'purpose' in result['message']

    def test_blank_purpose_counts_as_missing(self, data_env):
        plan = {'days': {TOMORROW: {'planned': _session(purpose='   ')}}}

        result = update_weekly_plan(plan)

        assert len(result['purpose_warnings']) == 1

    def test_sessions_with_purpose_not_warned(self, data_env):
        plan = {'days': {TOMORROW: {'planned': [
            _session(type='running', purpose='Tempo stimulus'),
            _session(type='strength', purpose='Strength pillar 1/2'),
        ]}}}

        result = update_weekly_plan(plan)

        assert result['purpose_warnings'] == []

    def test_rest_aliases_never_warned(self):
        days = {
            TOMORROW: {'planned': {'type': 'rest_or_easy'}},
            DAY_AFTER: {'planned': {'type': 'off'}},
        }
        assert _missing_purpose_sessions(days) == []

    def test_nested_sessions_checked_at_leaf_level(self):
        days = {TOMORROW: {'planned': {
            'type': 'double_session',
            'sessions': [
                {'type': 'running', 'duration_mins': 30},          # no purpose
                {'type': 'strength', 'purpose': 'Posterior chain'},
            ],
        }}}

        warnings = _missing_purpose_sessions(days)

        # The leaf running session is warned; the container wrapper is not.
        assert [w['type'] for w in warnings] == ['running']


# ---------------------------------------------------------------------------
# Injury write-gate still fires through the typed path
# ---------------------------------------------------------------------------

class TestInjuryGateTypedPath:
    def test_typed_plan_rejected_by_injury_gate(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })
        plan = {'days': {TOMORROW: {
            'planned': _session(type='trail_running', purpose='long effort'),
        }}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'injury_gate'
        assert result['violations'][0]['date'] == TOMORROW
        assert result['violations'][0]['session_type'] == 'trail_running'
        assert not (data_env / 'weekly_plan.json').exists()

    def test_typed_plan_override_saves_with_warning(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })
        plan = {'days': {TOMORROW: {
            'planned': _session(type='running', purpose='gated restart'),
        }}}

        result = update_weekly_plan(plan, override_injury_gate=True)

        assert result['status'] == 'success'
        assert result['injury_gate']['injury_gate_overridden'] is True


# ---------------------------------------------------------------------------
# Structured dict output from all seven planning tools
# ---------------------------------------------------------------------------

class TestStructuredReturns:
    """Every planning tool returns a dict (FastMCP emits real output schemas),
    never a json.dumps string."""

    def test_get_periodization_status_returns_dict(self, data_env):
        _write(data_env, 'training_config.json', {'current_block': {'phase': 'base'}})

        result = get_periodization_status()

        assert isinstance(result, dict)
        assert result['current_phase']['name'] == 'base'

    def test_get_weekly_prescription_returns_dict(self, data_env, monkeypatch):
        _write(data_env, 'training_config.json', {
            'current_block': {'phase': 'base', 'weekly_volume_target_hrs': 6.0},
        })
        _write(data_env, 'athlete.json', {'personal': {}})
        monkeypatch.setattr(planning_mod, 'garmin_api_call',
                            lambda fn: fn(FakeGarminClient()))

        result = get_weekly_prescription()

        assert isinstance(result, dict)
        assert 'error' not in result
        assert result['volume']['target_hrs'] == 6.0

    def test_update_phase_returns_dict(self, data_env):
        _write(data_env, 'training_config.json', {
            'periodization': {'current_phase': 'base', 'phases': {}},
            'current_block': {'phase': 'base'},
        })

        result = update_phase('build', notes='ready')

        assert isinstance(result, dict)
        assert result['status'] == 'success'
        assert result['transition'] == {
            'from': 'base', 'to': 'build', 'date': TODAY.isoformat(),
        }

    def test_update_phase_invalid_phase_returns_dict(self, data_env):
        result = update_phase('sprinting')

        assert isinstance(result, dict)
        assert 'Invalid phase' in result['error']

    def test_get_weekly_plan_returns_dict(self, data_env):
        result = get_weekly_plan()

        assert isinstance(result, dict)
        assert 'error' not in result
        assert 'days' in result  # empty template on clean state

    def test_update_weekly_plan_returns_dict(self, data_env):
        result = update_weekly_plan({'days': {TOMORROW: {'planned': _session()}}})

        assert isinstance(result, dict)
        assert result['status'] == 'success'

    def test_push_plan_to_garmin_returns_dict(self, data_env, monkeypatch):
        # No plan on disk: structured error dict, not a string
        result = push_plan_to_garmin()

        assert isinstance(result, dict)
        assert 'plan' in result['error'].lower()

    def test_get_week_constraints_returns_dict(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {},
            'life_constraints': {'blocked_days': ['Wednesday']},
        })

        result = get_week_constraints()

        assert isinstance(result, dict)
        assert 'error' not in result
        assert result['blocked_days'] == ['Wednesday']
