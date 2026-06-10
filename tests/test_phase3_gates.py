"""Phase 3.1 — hardened write-side coaching gates in update_weekly_plan.

Covers:
- PURPOSE GATE: non-rest sessions without a non-empty purpose REJECT the save
  (error 'purpose_gate'); override_purpose_gate=True bypasses with a logged
  warning + response note; rest days exempt; nested sessions checked at the
  leaf level
- INJURY GATE upgrade: taxonomy-aware matching (plan aliases like 'long_ride'
  caught by a 'cycling' restriction, 'running' vs 'trail_running') with a
  substring fallback for free-text restrictions ('no running', 'no high-impact')
- PLAN DATE VALIDATION: entirely-historical plans rejected; any day key more
  than 21 days in the future rejected (fat-finger guard); today-anchored
- COMPOSITION: a plan failing purpose AND injury reports both in one error
- push_plan_to_garmin has NO purpose gate (plans are gated at save)
"""
import json
import logging
from datetime import date, timedelta

import pytest

import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_mod
import coach.parsers as parsers_mod
import coach.workout_builder as workout_builder
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod

from coach.tools.planning_tools import (
    update_weekly_plan,
    push_plan_to_garmin,
    _activity_matches_restriction,
    _plan_date_error,
    PLAN_MAX_FUTURE_DAYS,
)

TODAY = date.today()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()
DAY_AFTER = (TODAY + timedelta(days=2)).isoformat()


# ---------------------------------------------------------------------------
# Environment helpers (same pattern as tests/test_typed_plan.py)
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


ACTIVE_RUN_INJURY = {
    'date': '2026-06-01', 'type': 'shin', 'body_region': 'shin',
    'status': 'active', 'severity': 'moderate',
    'restricted_activities': ['running'],
    'safe_activities': ['cycling'],
}

ACTIVE_CYCLING_INJURY = {
    'date': '2026-06-01', 'type': 'knee', 'body_region': 'knee',
    'status': 'active', 'severity': 'moderate',
    'restricted_activities': ['cycling'],
    'safe_activities': ['swimming'],
}

FREE_TEXT_INJURY = {
    'date': '2026-06-01', 'type': 'stress reaction', 'body_region': 'tibia',
    'status': 'improving', 'severity': 'moderate',
    'restricted_activities': ['no high-impact', 'no running'],
    'safe_activities': ['cycling'],
}


# ---------------------------------------------------------------------------
# PURPOSE GATE
# ---------------------------------------------------------------------------

class TestPurposeGate:
    def test_missing_purpose_rejects_save(self, data_env):
        plan = {'days': {TOMORROW: {
            'planned': _session(purpose=None, name='No-why ride'),
        }}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'purpose_gate'
        assert result['missing_purpose'] == [{
            'date': TOMORROW, 'type': 'cycling', 'name': 'No-why ride',
            'warning': 'missing purpose',
        }]
        assert 'override_purpose_gate' in result['hint']
        assert not (data_env / 'weekly_plan.json').exists(), \
            "plan saved despite purpose gate"

    def test_blank_purpose_rejects_save(self, data_env):
        plan = {'days': {TOMORROW: {'planned': _session(purpose='   ')}}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'purpose_gate'
        assert len(result['missing_purpose']) == 1

    def test_override_saves_with_logged_note(self, data_env, caplog):
        plan = {'days': {TOMORROW: {'planned': _session(purpose=None)}}}

        with caplog.at_level(logging.WARNING,
                             logger='coach.tools.planning_tools'):
            result = update_weekly_plan(plan, override_purpose_gate=True)

        assert result['status'] == 'success'
        assert result['purpose_gate']['purpose_gate_overridden'] is True
        assert len(result['purpose_gate']['missing_purpose']) == 1
        assert len(result['purpose_warnings']) == 1
        assert 'OVERRIDDEN' in result['message']
        assert 'Purpose gate OVERRIDDEN' in caplog.text
        assert (data_env / 'weekly_plan.json').exists()

    def test_rest_sessions_exempt(self, data_env):
        plan = {'days': {
            TOMORROW: {'planned': {'type': 'rest'}},
            DAY_AFTER: {'planned': {'type': 'rest_or_easy'}},
            (TODAY + timedelta(days=3)).isoformat(): {'planned': {'type': 'off'}},
            (TODAY + timedelta(days=4)).isoformat(): {'planned': None},
        }}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'
        assert result['purpose_warnings'] == []

    def test_nested_sessions_gated_at_leaf_level(self, data_env):
        plan = {'days': {TOMORROW: {'planned': {
            'type': 'double_session',
            'sessions': [
                {'type': 'running', 'duration_mins': 30},  # no purpose
                {'type': 'strength', 'purpose': 'Posterior chain'},
            ],
        }}}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'purpose_gate'
        # Only the leaf running session is flagged — not the wrapper, not
        # the strength session that has a purpose.
        assert [m['type'] for m in result['missing_purpose']] == ['running']

    def test_all_purposes_present_saves_clean(self, data_env):
        plan = {'days': {TOMORROW: {'planned': [
            _session(type='running', purpose='Tempo stimulus'),
            _session(type='strength', purpose='Strength pillar 1/2'),
        ]}}}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'
        assert result['purpose_warnings'] == []
        assert 'purpose_gate' not in result


# ---------------------------------------------------------------------------
# INJURY GATE — taxonomy matching + free-text fallback
# ---------------------------------------------------------------------------

class TestInjuryGateTaxonomy:
    def test_plan_alias_caught_by_canonical_restriction(self, data_env):
        """'long_ride' is a cycling alias the old substring check missed."""
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_CYCLING_INJURY],
        })
        plan = {'days': {TOMORROW: {
            'planned': _session(type='long_ride', purpose='Endurance block'),
        }}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'injury_gate'
        assert result['violations'][0]['session_type'] == 'long_ride'
        assert result['violations'][0]['matched_restrictions'] == ['cycling']
        assert not (data_env / 'weekly_plan.json').exists()

    def test_running_family_aliases_caught(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })
        plan = {'days': {TOMORROW: {
            'planned': _session(type='trail_running', purpose='Long effort'),
        }}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'injury_gate'
        assert result['violations'][0]['session_type'] == 'trail_running'

    def test_free_text_restriction_caught_by_substring_fallback(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [FREE_TEXT_INJURY],
        })
        plan = {'days': {TOMORROW: {
            'planned': _session(type='running', purpose='Protocol run'),
        }}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'injury_gate'
        assert 'no running' in result['violations'][0]['matched_restrictions']

    def test_unrelated_type_passes_gate(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })
        plan = {'days': {TOMORROW: {
            'planned': _session(type='wattbike', purpose='Z2 spin'),
        }}}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'
        assert 'injury_gate' not in result

    def test_matcher_taxonomy_and_fallback_semantics(self):
        # Taxonomy pass: aliases across spellings
        assert _activity_matches_restriction('long_ride', 'cycling')
        assert _activity_matches_restriction('wattbike', 'cycling')
        assert _activity_matches_restriction('trail_running', 'running')
        assert _activity_matches_restriction('running', 'trail_running')
        # Substring fallback: free-text restrictions
        assert _activity_matches_restriction('running', 'no running')
        assert _activity_matches_restriction('run', 'no running')
        assert _activity_matches_restriction('high_impact', 'no high-impact')
        # Non-matches
        assert not _activity_matches_restriction('strength', 'running')
        assert not _activity_matches_restriction('cycling', 'no high-impact')
        assert not _activity_matches_restriction('', 'running')
        assert not _activity_matches_restriction('cycling', '')


# ---------------------------------------------------------------------------
# PLAN DATE VALIDATION
# ---------------------------------------------------------------------------

class TestPlanDateValidation:
    def test_entirely_historical_plan_rejected(self, data_env):
        two_ago = (TODAY - timedelta(days=2)).isoformat()
        plan = {'days': {
            two_ago: {'planned': _session()},
            YESTERDAY: {'planned': _session()},
        }}

        result = update_weekly_plan(plan)

        assert result['error'] == 'plan_dates'
        assert 'entirely historical' in result['message']
        assert not (data_env / 'weekly_plan.json').exists()

    def test_today_counts_as_current(self, data_env):
        plan = {'days': {TODAY.isoformat(): {'planned': _session()}}}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'

    def test_past_days_allowed_when_plan_reaches_present(self, data_env):
        plan = {'days': {
            YESTERDAY: {'planned': _session()},
            TOMORROW: {'planned': _session()},
        }}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'

    def test_far_future_day_rejected(self, data_env):
        too_far = (TODAY + timedelta(days=PLAN_MAX_FUTURE_DAYS + 1)).isoformat()
        plan = {'days': {
            TOMORROW: {'planned': _session()},
            too_far: {'planned': _session()},
        }}

        result = update_weekly_plan(plan)

        assert result['error'] == 'plan_dates'
        assert result['offending_days'] == [too_far]
        assert not (data_env / 'weekly_plan.json').exists()

    def test_horizon_boundary_day_allowed(self, data_env):
        at_horizon = (TODAY + timedelta(days=PLAN_MAX_FUTURE_DAYS)).isoformat()
        plan = {'days': {at_horizon: {'planned': _session()}}}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'

    def test_helper_ignores_empty_days(self):
        assert _plan_date_error({}, TODAY) is None
        assert _plan_date_error(None, TODAY) is None


# ---------------------------------------------------------------------------
# GATE COMPOSITION
# ---------------------------------------------------------------------------

class TestGateComposition:
    def _injured_env(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_RUN_INJURY],
        })

    def test_failing_both_gates_reports_both(self, data_env):
        self._injured_env(data_env)
        plan = {'days': {
            TOMORROW: {'planned': {'type': 'trail_running',
                                   'duration_mins': 45}},  # injury + no purpose
            DAY_AFTER: {'planned': _session(purpose=None)},  # no purpose only
        }}

        result = update_weekly_plan(plan)

        assert result['error'] == 'injury_gate'
        assert result['violations'][0]['session_type'] == 'trail_running'
        missing_types = [m['type'] for m in
                         result['purpose_gate']['missing_purpose']]
        assert sorted(missing_types) == ['cycling', 'trail_running']
        assert 'purpose gate' in result['message']
        assert not (data_env / 'weekly_plan.json').exists()

    def test_injury_override_alone_still_blocks_on_purpose(self, data_env):
        self._injured_env(data_env)
        plan = {'days': {TOMORROW: {
            'planned': {'type': 'running', 'duration_mins': 30},
        }}}

        result = update_weekly_plan(plan, override_injury_gate=True)

        assert result['error'] == 'purpose_gate'
        assert not (data_env / 'weekly_plan.json').exists()

    def test_purpose_override_alone_still_blocks_on_injury(self, data_env):
        self._injured_env(data_env)
        plan = {'days': {TOMORROW: {
            'planned': {'type': 'running', 'duration_mins': 30},
        }}}

        result = update_weekly_plan(plan, override_purpose_gate=True)

        assert result['error'] == 'injury_gate'
        assert 'purpose_gate' not in result  # purpose was overridden
        assert not (data_env / 'weekly_plan.json').exists()

    def test_both_overrides_save_with_both_notes(self, data_env, caplog):
        self._injured_env(data_env)
        plan = {'days': {TOMORROW: {
            'planned': {'type': 'running', 'duration_mins': 30},
        }}}

        with caplog.at_level(logging.WARNING,
                             logger='coach.tools.planning_tools'):
            result = update_weekly_plan(plan, override_injury_gate=True,
                                        override_purpose_gate=True)

        assert result['status'] == 'success'
        assert result['injury_gate']['injury_gate_overridden'] is True
        assert result['purpose_gate']['purpose_gate_overridden'] is True
        assert 'Injury gate OVERRIDDEN' in caplog.text
        assert 'Purpose gate OVERRIDDEN' in caplog.text
        assert (data_env / 'weekly_plan.json').exists()

    def test_date_gate_pre_empts_coaching_gates(self, data_env):
        self._injured_env(data_env)
        plan = {'days': {YESTERDAY: {
            'planned': {'type': 'running', 'duration_mins': 30},
        }}}

        result = update_weekly_plan(plan)

        assert result['error'] == 'plan_dates'


# ---------------------------------------------------------------------------
# push_plan_to_garmin: injury-gated, but NO purpose gate (gated at save)
# ---------------------------------------------------------------------------

class TestPushHasNoPurposeGate:
    def test_push_proceeds_without_purposes(self, data_env, monkeypatch):
        _write(data_env, 'athlete.json', {'personal': {}, 'injury_history': []})
        _write(data_env, 'weekly_plan.json', {'days': {TOMORROW: {
            'planned': {'type': 'running', 'duration_mins': 30,
                        'description': 'Easy run'},  # no purpose
        }}})

        def _fail(*args, **kwargs):
            raise Exception("Garmin unavailable")
        monkeypatch.setattr(planning_mod, 'garmin_api_call', _fail)

        result = push_plan_to_garmin()

        # Got past every gate to the (failing) upload stage — no purpose gate.
        assert result.get('error') != 'purpose_gate'
        assert len(result['errors']) == 1

    def test_push_still_injury_gated_via_taxonomy(self, data_env):
        _write(data_env, 'athlete.json', {
            'personal': {}, 'injury_history': [ACTIVE_CYCLING_INJURY],
        })
        _write(data_env, 'weekly_plan.json', {'days': {TOMORROW: {
            'planned': {'type': 'long_ride', 'duration_mins': 120,
                        'purpose': 'Endurance block'},
        }}})

        result = push_plan_to_garmin()

        assert result['error'] == 'injury_gate'
        assert result['violations'][0]['session_type'] == 'long_ride'
