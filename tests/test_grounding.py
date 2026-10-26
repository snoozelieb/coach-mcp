"""VERIFY BEFORE PRESCRIBING: the grounding layer.

Covers the surfaces added when the generic-10K-program incident showed that
prompt-only personalization fails (same lesson as the purpose/injury gates):

- fitness.capacity_by_sport / derive_sport_capacity / demonstrated_max_mins —
  demonstrated per-sport capacity from completed sessions
- update_weekly_plan grounding_warnings (capacity_regression / session_spike /
  no_demonstrated_history) — warn-only by design
- the training_program tool: schema gate, sport gate, date gates, injury
  gate, and the HARD capacity-anchor gate (claimed capacity verified against
  actual history; unexplained week-1 regressions rejected)
- query_metrics(kind='sport_history') — per-sport session history, local only
- get_activities_range activity_type filter
- get_weekly_prescription / get_week_constraints recent_capacity blocks
- refresh_athlete_baseline per-sport capacity (baseline.by_sport)
"""
import json
from datetime import date, timedelta
from unittest.mock import Mock, patch

import pytest

import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_lib
import coach.parsers as parsers_mod
import coach.workout_builder as workout_builder
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod
import coach.tools.fitness_tools as fitness_tools_mod

from coach.fitness import (
    capacity_by_sport,
    derive_sport_capacity,
    demonstrated_max_mins,
)
from coach.taxonomy import capacity_bucket_for
from coach.tools.planning_tools import (
    update_weekly_plan,
    training_program,
    get_weekly_prescription,
    get_week_constraints,
    _grounding_warnings,
)
from coach.tools.fitness_tools import query_metrics
from coach.tools.data_tools import get_activities_range
from conftest import SAMPLE_PR_DATA, make_garmin_activity

TODAY = date.today()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()
DAY_AFTER = (TODAY + timedelta(days=2)).isoformat()


# ---------------------------------------------------------------------------
# Environment helpers (same pattern as tests/test_typed_plan.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def data_env(data_dir, monkeypatch):
    """Redirect DATA_DIR in every module that does file I/O to a tmp dir."""
    for mod in (planner, rules, fitness_lib, parsers_mod, workout_builder,
                coaching_mod, planning_mod, fitness_tools_mod):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    return data_dir


def _write(data_dir, filename, payload):
    (data_dir / filename).write_text(json.dumps(payload), encoding='utf-8')


def _seed_history(data_dir, sessions):
    """Seed fitness_history.json daily_loads.

    sessions: list of (days_ago, type, sport, mins) or
    (days_ago, type, sport, mins, extra_dict) tuples.
    """
    daily = {}
    for entry in sessions:
        days_ago, act_type, sport, mins = entry[:4]
        extra = entry[4] if len(entry) > 4 else {}
        d = (TODAY - timedelta(days=days_ago)).isoformat()
        day = daily.setdefault(
            d, {'total': 0.0, 'by_sport': {}, 'activities': []})
        day['total'] += float(mins)
        day['by_sport'][sport] = day['by_sport'].get(sport, 0) + float(mins)
        act = {'id': len(day['activities']) + 1, 'type': act_type,
               'sport': sport, 'duration_mins': mins, 'load': float(mins),
               'date': d}
        act.update(extra)
        day['activities'].append(act)
    _write(data_dir, 'fitness_history.json', {
        'schema_version': 2,
        'daily_loads': daily,
        'snapshots': [],
        'sleep_history': [],
        'last_updated': TODAY.isoformat(),
        'last_activity_ingest_date': TODAY.isoformat(),
    })


# The incident shape: a runner whose demonstrated longest recent run is
# ~45 minutes (7 km at 6:32/km that same morning).
RUN_HISTORY = [
    (1, 'running', 'running', 45.2,
     {'distance_km': 7.0, 'avg_pace_min_km': 6.53}),
    (3, 'running', 'running', 40.0, {'distance_km': 6.1}),
    (6, 'running', 'running', 42.0),
    (10, 'running', 'running', 38.0),
    (20, 'running', 'running', 44.0),
    (40, 'trail_running', 'running', 55.0),   # outside 28d, inside 90d
]


def _purposeful(session_type, mins, **overrides):
    base = {'type': session_type, 'duration_mins': mins,
            'intensity': 'easy', 'purpose': 'Aerobic base'}
    base.update(overrides)
    return base


def _program(**overrides):
    base = {
        'name': '10k build',
        'sport': 'running',
        'start_date': TODAY.isoformat(),
        'weeks': [
            {'week': 1, 'focus': 'consolidation', 'long_session_mins': 45},
            {'week': 2, 'focus': 'duration', 'long_session_mins': 50},
        ],
        'capacity_anchor': {
            'sport': 'running',
            'demonstrated_longest_session_mins': 45.2,
            'source': 'snapshot.capacity',
        },
        'rationale': 'anchored to the athlete, not a template',
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# capacity_by_sport / derive_sport_capacity
# ---------------------------------------------------------------------------

class TestCapacityDerivation:
    def test_capacity_buckets(self):
        assert capacity_bucket_for('running') == 'running'
        assert capacity_bucket_for('long_run') == 'running'
        assert capacity_bucket_for('trail_running') == 'running'
        assert capacity_bucket_for('long_ride') == 'cycling'
        assert capacity_bucket_for('strength') == 'strength'
        assert capacity_bucket_for('lap_swimming') == 'swimming'
        # Games and mobility are not duration-progressed
        assert capacity_bucket_for('padel') is None
        assert capacity_bucket_for('ultimate_disc') is None
        assert capacity_bucket_for('yoga') is None

    def test_longest_typical_and_last_session(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        cap = derive_sport_capacity(
            json.loads((data_env / 'fitness_history.json').read_text())
            ['daily_loads'], TODAY)

        running = cap['by_sport']['running']
        assert running['longest_session_28d']['duration_mins'] == 45.2
        assert running['longest_session_28d']['distance_km'] == 7.0
        # The 55-min trail run is outside 28d but inside the 90d window
        assert running['longest_session_window']['duration_mins'] == 55.0
        assert running['typical_session_mins_28d'] == 42.0   # median of 5
        assert running['sessions_28d'] == 5
        assert running['last_session']['date'] == (
            TODAY - timedelta(days=1)).isoformat()
        assert running['last_session']['avg_pace_min_km'] == 6.53
        assert len(running['weekly_mins_4wk']) == 4

    def test_window_filtering_and_empty(self):
        acts = [{'date': (TODAY - timedelta(days=200)).isoformat(),
                 'type': 'running', 'duration_mins': 60}]
        assert capacity_by_sport(acts, TODAY) == {}
        assert capacity_by_sport([], TODAY) == {}
        assert capacity_by_sport(None, TODAY) == {}

    def test_zero_duration_and_unbucketed_sports_skipped(self):
        acts = [
            {'date': TODAY.isoformat(), 'type': 'running',
             'duration_mins': 0},
            {'date': TODAY.isoformat(), 'type': 'padel',
             'duration_mins': 90},
        ]
        assert capacity_by_sport(acts, TODAY) == {}

    def test_demonstrated_max_prefers_recent(self):
        entry = {
            'longest_session_28d': {'duration_mins': 45.0},
            'longest_session_window': {'duration_mins': 90.0},
        }
        assert demonstrated_max_mins(entry) == (45.0, '28d')
        entry['longest_session_28d'] = None
        assert demonstrated_max_mins(entry) == (90.0, 'window')
        assert demonstrated_max_mins(None) == (None, None)
        assert demonstrated_max_mins({}) == (None, None)


# ---------------------------------------------------------------------------
# update_weekly_plan grounding warnings (warn-only)
# ---------------------------------------------------------------------------

class TestPlanGroundingWarnings:
    def test_regression_flagged_but_save_succeeds(self, data_env):
        """The incident: a 20-min 'long run' for a demonstrated 45-min
        runner must be flagged — and still save (warn-only)."""
        _seed_history(data_env, RUN_HISTORY)
        plan = {'days': {TOMORROW: {'planned': _purposeful('long_run', 20)}}}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'
        warnings = result['grounding_warnings']
        assert len(warnings) == 1
        w = warnings[0]
        assert w['type'] == 'capacity_regression'
        assert w['sport'] == 'running'
        assert w['planned_max_mins'] == 20
        assert w['demonstrated_max_mins'] == 45.2
        assert 'taper' in w['message']
        assert 'grounding' in result['message']

    def test_spike_flagged(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        plan = {'days': {TOMORROW: {'planned': _purposeful('running', 65)}}}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'
        assert [w['type'] for w in result['grounding_warnings']] == \
            ['session_spike']

    def test_anchored_plan_is_clean(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        plan = {'days': {
            TOMORROW: {'planned': _purposeful('long_run', 50)},
            DAY_AFTER: {'planned': _purposeful('recovery_run', 25)},
        }}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'
        assert result['grounding_warnings'] == []
        assert result['message'] == 'Weekly plan saved'

    def test_no_history_sport_flagged(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        plan = {'days': {TOMORROW: {'planned': _purposeful('swim', 40)}}}

        result = update_weekly_plan(plan)

        warnings = result['grounding_warnings']
        assert [w['type'] for w in warnings] == ['no_demonstrated_history']
        assert warnings[0]['sport'] == 'swimming'

    def test_games_rest_and_short_sessions_ignored(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        plan = {'days': {
            TOMORROW: {'planned': [
                _purposeful('running', 45),
                _purposeful('strength', 20),   # under the 30-min floor
            ]},
            DAY_AFTER: {'planned': {'type': 'rest'}},
            (TODAY + timedelta(days=3)).isoformat():
                {'planned': _purposeful('padel', 90)},   # no capacity bucket
        }}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'
        assert result['grounding_warnings'] == []

    def test_grounding_never_blocks_on_internal_failure(self, data_env):
        """A corrupt history degrades to zero warnings, not a failed save."""
        (data_env / 'fitness_history.json').write_text('{not json',
                                                       encoding='utf-8')
        plan = {'days': {TOMORROW: {'planned': _purposeful('running', 45)}}}

        result = update_weekly_plan(plan)

        assert result['status'] == 'success'
        assert result['grounding_warnings'] == []

    def test_helper_handles_non_numeric_durations(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        days = {TOMORROW: {'planned': {'type': 'running',
                                       'duration_mins': 'forty-five',
                                       'purpose': 'p'}}}
        assert _grounding_warnings(days, TODAY) == []


# ---------------------------------------------------------------------------
# training_program: the hard capacity-anchor gate
# ---------------------------------------------------------------------------

class TestTrainingProgramTool:
    def test_get_without_program(self, data_env):
        result = training_program(action='get')
        assert result['status'] == 'no_program'
        assert 'snapshot.capacity' in result['hint']

    def test_valid_program_saves_and_verifies_anchor(self, data_env):
        _seed_history(data_env, RUN_HISTORY)

        result = training_program(action='set', program=_program())

        assert result['status'] == 'success'
        summary = result['program_summary']
        assert summary['sport'] == 'running'
        assert summary['total_weeks'] == 2
        assert summary['long_session_ladder_mins'] == [45, 50]
        assert summary['capacity_anchor']['verified_against_history'] is True
        assert result['warnings'] == []

        saved = json.loads(
            (data_env / 'training_program.json').read_text())
        assert saved['program']['name'] == '10k build'
        assert saved['program']['status'] == 'active'
        assert saved['history'] == []

        fetched = training_program(action='get')
        assert fetched['status'] == 'ok'
        assert fetched['program_status'] == 'active'
        assert fetched['current_week'] == 1
        assert fetched['total_weeks'] == 2

    def test_anchor_mismatch_rejected(self, data_env):
        """Coach fiction, code-caught: claiming a 20-min capacity for a
        demonstrated 45-min runner rejects the program."""
        _seed_history(data_env, RUN_HISTORY)
        program = _program()
        program['capacity_anchor']['demonstrated_longest_session_mins'] = 20
        program['weeks'][0]['long_session_mins'] = 20

        result = training_program(action='set', program=program)

        assert result['error'] == 'capacity_anchor_mismatch'
        assert result['claimed_mins'] == 20
        assert result['demonstrated_mins'] == 45.2
        assert 'VERIFY BEFORE PRESCRIBING' in result['message']
        assert not (data_env / 'training_program.json').exists()

    def test_unexplained_week1_regression_rejected(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        program = _program()
        program['weeks'][0]['long_session_mins'] = 20  # anchor stays honest

        result = training_program(action='set', program=program)

        assert result['error'] == 'capacity_anchor'
        assert 'rationale_if_below' in result['message']

    def test_deliberate_regression_with_rationale_saves(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        program = _program()
        program['weeks'][0]['long_session_mins'] = 20
        program['capacity_anchor']['rationale_if_below'] = (
            'Fibula bone-stress rebuild: bone loading, not aerobic fitness, '
            'is the limiter — deliberate sub-capacity restart.')

        result = training_program(action='set', program=program)

        assert result['status'] == 'success'
        assert any(n.get('deliberate_regression') for n in result['notes'])

    def test_aggressive_start_warns(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        program = _program()
        program['weeks'][0]['long_session_mins'] = 65

        result = training_program(action='set', program=program)

        assert result['status'] == 'success'
        assert 'aggressive_start' in [w['type'] for w in result['warnings']]

    def test_progression_rate_warning(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        program = _program(weeks=[
            {'week': 1, 'long_session_mins': 45},
            {'week': 2, 'long_session_mins': 60},   # +33%/week
        ])

        result = training_program(action='set', program=program)

        assert result['status'] == 'success'
        assert 'progression_rate' in [w['type'] for w in result['warnings']]

    def test_unverifiable_anchor_taken_on_trust_with_warning(self, data_env):
        # No fitness history at all — the anchor can't be checked
        result = training_program(action='set', program=_program())

        assert result['status'] == 'success'
        assert 'no_demonstrated_history' in [
            w['type'] for w in result['warnings']]
        assert result['program_summary']['capacity_anchor'][
            'verified_against_history'] is False

    def test_injury_gate(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        _write(data_env, 'athlete.json', {'injury_history': [{
            'date': (TODAY - timedelta(days=10)).isoformat(),
            'type': 'fibula bone stress', 'status': 'active',
            'restricted_activities': ['running'],
        }]})

        blocked = training_program(action='set', program=_program())
        assert blocked['error'] == 'injury_gate'
        assert blocked['violations'][0]['matched_restrictions'] == ['running']

        overridden = training_program(action='set', program=_program(),
                                      override_injury_gate=True,
                                      athlete_statement='I understand the risk and want to continue', override_rationale='test override with consent')
        assert overridden['status'] == 'success'
        assert any(n.get('injury_gate_overridden')
                   for n in overridden['notes'])

    def test_sport_must_be_duration_prescribed(self, data_env):
        result = training_program(action='set',
                                  program=_program(sport='padel'))
        assert result['error'] == 'sport'
        assert 'running' in result['message']

    def test_missing_anchor_fails_schema(self, data_env):
        program = _program()
        del program['capacity_anchor']
        result = training_program(action='set', program=program)
        assert result['error'] == 'validation_error'
        assert any('capacity_anchor' in p['field']
                   for p in result['problems'])

    def test_date_gates(self, data_env):
        far_future = _program(
            start_date=(TODAY + timedelta(days=120)).isoformat())
        assert training_program(action='set',
                                program=far_future)['error'] == \
            'program_dates'

        all_past = _program(
            start_date=(TODAY - timedelta(days=100)).isoformat())
        assert training_program(action='set',
                                program=all_past)['error'] == 'program_dates'

    def test_overwrite_archives_prior_program(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        assert training_program(action='set',
                                program=_program())['status'] == 'success'
        assert training_program(
            action='set',
            program=_program(name='10k build v2'))['status'] == 'success'

        saved = json.loads(
            (data_env / 'training_program.json').read_text())
        assert saved['program']['name'] == '10k build v2'
        assert len(saved['history']) == 1
        assert saved['history'][0]['program']['name'] == '10k build'

    def test_unknown_action(self, data_env):
        result = training_program(action='delete')
        assert 'error' in result
        assert "'get'" in result['error'] and "'set'" in result['error']

    def test_set_requires_program_dict(self, data_env):
        result = training_program(action='set')
        assert 'error' in result
        assert 'program' in result['error']


# ---------------------------------------------------------------------------
# query_metrics(kind='sport_history')
# ---------------------------------------------------------------------------

class TestSportHistory:
    def test_summary_without_sport(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        result = query_metrics(kind='sport_history', days=90)

        assert result['window_days'] == 90
        assert 'running' in result['by_sport']
        assert 'VERIFY BEFORE PRESCRIBING' in result['note']

    def test_per_sport_sessions_with_alias(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        result = query_metrics(kind='sport_history', sport='run', days=90)

        assert result['sport'] == 'running'
        assert result['sessions_found'] == 6
        assert result['capacity']['longest_session_28d'][
            'duration_mins'] == 45.2
        # Most recent first, carrying distance/pace where persisted
        first = result['sessions'][0]
        assert first['date'] == (TODAY - timedelta(days=1)).isoformat()
        assert first['distance_km'] == 7.0
        assert first['avg_pace_min_km'] == 6.53
        assert 'get_activities_range' in result['note']

    def test_unbucketed_sport_errors(self, data_env):
        _seed_history(data_env, RUN_HISTORY)
        result = query_metrics(kind='sport_history', sport='padel')
        assert 'error' in result
        assert 'running' in result['error']

    def test_no_data(self, data_env):
        result = query_metrics(kind='sport_history')
        assert result['status'] == 'no_data'

    def test_unknown_kind_lists_sport_history(self, data_env):
        result = query_metrics(kind='bogus')
        assert 'sport_history' in result['error']


# ---------------------------------------------------------------------------
# get_activities_range activity_type filter
# ---------------------------------------------------------------------------

class TestActivitiesRangeFilter:
    @patch('coach.tools.data_tools.garmin_api_call')
    def test_taxonomy_aware_filter(self, mock_api):
        mock_api.return_value = [
            make_garmin_activity(TODAY, 'running', duration_secs=2700,
                                 distance_m=7000),
            make_garmin_activity(TODAY, 'trail_running', duration_secs=3300,
                                 distance_m=8000),
            make_garmin_activity(TODAY, 'cycling', duration_secs=5400),
        ]
        start = (TODAY - timedelta(days=7)).isoformat()

        unfiltered = json.loads(get_activities_range(start))
        assert len(unfiltered) == 3

        runs = json.loads(get_activities_range(start, activity_type='run'))
        assert [a['type'] for a in runs] == ['running', 'trail_running']


# ---------------------------------------------------------------------------
# recent_capacity beside the template surfaces
# ---------------------------------------------------------------------------

class _MinimalGarmin:
    def get_training_readiness(self, d):
        return []

    def get_activities_by_date(self, start, end):
        return []


class TestCapacityBesideTemplates:
    def test_prescription_carries_recent_capacity(self, data_env, monkeypatch):
        _seed_history(data_env, RUN_HISTORY)
        monkeypatch.setattr(planning_mod, 'garmin_api_call',
                            lambda fn, *a, **kw: fn(_MinimalGarmin(), *a, **kw))

        result = get_weekly_prescription()

        cap = result['recent_capacity']
        assert cap['by_sport']['running']['longest_session_28d'][
            'duration_mins'] == 45.2
        assert 'VERIFY BEFORE PRESCRIBING' in cap['note']

    def test_week_constraints_carry_recent_capacity(self, data_env):
        _seed_history(data_env, RUN_HISTORY)

        result = get_week_constraints()

        assert 'error' not in result
        cap = result['recent_capacity']
        assert cap['by_sport']['running']['longest_session_28d'][
            'duration_mins'] == 45.2


# ---------------------------------------------------------------------------
# refresh_athlete_baseline per-sport capacity
# ---------------------------------------------------------------------------

class TestBaselineBySport:
    @pytest.mark.asyncio
    @patch('coach.tools.fitness_tools.garmin_api_call')
    async def test_baseline_records_per_sport_capacity(
            self, mock_api, data_env, mock_ctx):
        client = Mock()
        client.get_activities_by_date.return_value = [
            make_garmin_activity(TODAY - timedelta(days=5), 'running',
                                 duration_secs=2700, distance_m=7000),
            make_garmin_activity(TODAY - timedelta(days=12), 'cycling',
                                 duration_secs=5400),
        ]
        client.get_personal_record.return_value = SAMPLE_PR_DATA
        client.get_full_name.side_effect = Exception('offline')
        client.get_user_profile.side_effect = Exception('offline')
        client.get_body_composition.side_effect = Exception('offline')
        client.client.connectapi.side_effect = Exception('offline')
        mock_api.side_effect = lambda fn, *a, **kw: fn(client, *a, **kw)

        result = json.loads(
            await fitness_tools_mod.refresh_athlete_baseline(mock_ctx))
        assert result['status'] == 'success'

        profile = json.loads(
            (data_env / 'athlete_baseline.json').read_text())
        by_sport = profile['baseline']['by_sport']
        assert by_sport['running']['longest_session_28d'][
            'duration_mins'] == 45.0
        assert by_sport['cycling']['longest_session_28d'][
            'duration_mins'] == 90.0
