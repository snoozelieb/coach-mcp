"""Second review round of the session-contract batch (2026-10-28).

Red team: a rest-typed container carried a gym list past the injury gate
(N1); text, negative and boolean minutes rendered garbage with a success
status (N2); restriction words matched by substring (N3); W/kg values passed
as watts (N4); a library read failure at confirm swallowed the token (N5);
summary wording (N6), skip reason wording (N7), session-level 'none' (N9).
Intent guardian: 'no jumping' and 'plyometrics' never reached BOX_JUMP or
PLYO (N1); generic HR zones landed silently (N2); a numeric rep scheme in a
run description only warned (N3); the authoring agent was a denylist (N4).
"""
import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

import coach.authoring as authoring
import coach.planner as planner
import coach.workout_builder as wb
import coach.tools.injury_tools as injury_mod
import coach.tools.planning_tools as planning_mod
from coach.tools.planning_tools import push_plan_to_garmin, update_weekly_plan
# Fixtures shared with the e2e push suite; pytest registers them under their
# own names, the aliases only keep the module namespace clear of the
# parameter names.
from test_e2e_push import D0, D1, _seed_athlete
from test_e2e_push import data_env as _e2e_data_env, push_env as _e2e_push_env  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
TODAY = date.today()


@pytest.fixture
def data_env(_e2e_data_env):  # noqa: F811 — the parameter requests the imported fixture
    return _e2e_data_env


@pytest.fixture
def push_env(_e2e_push_env):  # noqa: F811
    return _e2e_push_env


def _report(session, day=None):
    return wb.build_workout_report(session, (day or TODAY).isoformat())


# ---------------------------------------------------------------------------
# Minutes are numbers (red team N2)
# ---------------------------------------------------------------------------

PLAIN_SESSIONS = [
    {'type': 'yoga', 'purpose': 'x'},
    {'type': 'pilates', 'purpose': 'x'},
    {'type': 'padel', 'purpose': 'x'},
    {'type': 'strength', 'purpose': 'x'},
    {'type': 'cycling', 'intensity': 'easy', 'purpose': 'x'},
    {'type': 'running', 'intensity': 'easy', 'purpose': 'x'},
    {'type': 'swimming', 'purpose': 'x'},
]


@pytest.mark.parametrize('base', PLAIN_SESSIONS, ids=lambda s: s['type'])
@pytest.mark.parametrize('bad', ['30', -30, True, 0], ids=['text', 'negative', 'flag', 'zero'])
def test_minutes_must_be_a_positive_number_in_every_lane(base, bad):
    session = {**base, 'duration_mins': bad}
    report = _report(session)
    assert report['ok'] is False
    assert any(p['field'] == 'duration_mins' for p in report['errors']), report['errors']
    assert report['workout'] is None


def test_warmup_minutes_must_be_numbers_too():
    report = _report({'type': 'running', 'duration_mins': 40, 'intensity': 'easy',
                      'purpose': 'x', 'warmup_mins': '5'})
    assert any(p['field'] == 'warmup_mins' for p in report['errors'])


def test_numeric_minutes_still_render():
    report = _report({'type': 'yoga', 'duration_mins': 30.5, 'purpose': 'x'})
    assert report['ok'] and report['steps'] == ['1. Interval 30:30']


# ---------------------------------------------------------------------------
# Watts, 'none', prose (red team N4, N9; guardian N3)
# ---------------------------------------------------------------------------

def test_power_watts_band_under_30_w_is_rejected():
    report = _report({'type': 'wattbike', 'duration_mins': 30, 'purpose': 'x',
                      'structure': [{'phase': 'interval', 'duration_mins': 10, 'power_watts': [2.5, 2.8]}]})
    assert report['ok'] is False
    assert any('watts' in p['message'] for p in report['errors']), report['errors']


def test_session_level_none_is_untargeted_without_a_warning():
    report = _report({'type': 'running', 'duration_mins': 40, 'intensity': 'none', 'purpose': 'x'})
    assert report['ok']
    assert not [w for w in report['warnings'] if w['field'] == 'intensity'], report['warnings']
    assert all('@' not in line for line in report['steps'])


def test_rep_scheme_in_a_run_description_without_structure_is_an_error():
    report = _report({'type': 'running', 'duration_mins': 40, 'intensity': 'easy', 'purpose': 'x',
                      'description': '5 x 3 min at threshold, 90 s jog'})
    assert report['ok'] is False
    assert any(p['field'] == 'structure' and '5 x 3 min' in p['message'] for p in report['errors'])


def test_interval_words_warn_but_a_negation_does_not():
    warned = _report({'type': 'running', 'duration_mins': 40, 'intensity': 'easy', 'purpose': 'x',
                      'description': 'Fartlek by feel, strides at the end'})
    assert warned['ok'] and any(w['field'] == 'structure' for w in warned['warnings'])
    quiet = _report({'type': 'running', 'duration_mins': 40, 'intensity': 'easy', 'purpose': 'x',
                     'description': 'Easy run. No intervals today.'})
    assert quiet['ok'] and not [w for w in quiet['warnings'] if w['field'] == 'structure']


# ---------------------------------------------------------------------------
# Catalogue corrections for names cached under SUSPENSION (one category per name)
# ---------------------------------------------------------------------------

CACHE = {'PULL_UP': {'category': 'SUSPENSION'}, 'FACE_PULL': {'category': 'SUSPENSION'},
         'SQUAT_JUMP': {'category': 'SUSPENSION'}, 'SIDE_LUNGE': {'category': 'SUSPENSION'},
         'TRX_ROW': {'category': 'SUSPENSION'}}


@pytest.mark.parametrize('name, stated, expected_category, keeps_name', [
    ('PULL_UP', 'PULL_UP', 'PULL_UP', True),
    ('PULL_UP', None, 'PULL_UP', True),
    ('FACE_PULL', None, 'ROW', True),
    ('SQUAT_JUMP', None, 'PLYO', True),
    ('SIDE_LUNGE', 'LUNGE', 'LUNGE', True),
    ('TRX_ROW', None, 'SUSPENSION', True),
])
def test_names_the_cache_files_under_suspension_keep_their_category(name, stated, expected_category, keeps_name):
    spec = authoring.CircuitExercise(name=name, category=stated, reps=10)
    category, sent_name, warning = authoring.resolve_exercise_category(spec, CACHE)
    assert category == expected_category
    assert (sent_name == name) is keeps_name
    assert warning is None


# ---------------------------------------------------------------------------
# Restriction words reach the exercise keys Garmin uses (red N3, guardian N1)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('name, category, restriction, hit', [
    ('BOX_JUMP', 'PLYO', 'no jumping', True),
    ('BURPEE', 'PLYO', 'no jumping', True),
    ('BURPEE', 'PLYO', 'plyometrics', True),
    ('SKATER_HOPS', 'PLYO', 'hopping', True),
    ('SHOULDER_PRESS', 'SHOULDER_PRESS', 'overhead pressing', True),
    ('OVERHEAD_PRESS', 'SHOULDER_PRESS', 'overhead', True),
    ('LEG_PRESS', 'SQUAT', 'overhead pressing', False),
    ('BENT_OVER_ROW', 'ROW', 'rows', True),
    ('CROWN_CRUNCH', 'CRUNCH', 'row', False),
    ('KNEELING_CRUNCH', 'CRUNCH', 'knee', False),
    ('RUSSIAN_TWIST', 'CORE', 'twisting', True),
    ('GOBLET_SQUAT', 'SQUAT', 'no heavy squats', True),   # qualifiers drop: the gate fails safe
    ('GOBLET_SQUAT', 'SQUAT', 'deep squats', True),
    ('CYCLING_RUSSIAN_TWIST', 'CORE', 'cycling', False),  # a sport word is judged on the type
    ('JUMP_ROPE', 'CARDIO', 'high-impact', True),
])
def test_exercise_matches_restriction(name, category, restriction, hit):
    if restriction == 'cycling':
        assert authoring.exercise_matches_restriction(name, category, restriction) is True
        session = {'type': 'strength', 'exercises': [{'name': name, 'category': category, 'sets': 3, 'reps': 8}]}
        assert planning_mod._exercise_restriction_hits(session, [restriction]) == []
        return
    assert authoring.exercise_matches_restriction(name, category, restriction) is hit


def test_rest_typed_container_cannot_smuggle_a_gym_list_past_the_gate(data_env):
    """Red team N1: the gate walks the same expansion as the push."""
    _seed_athlete(data_env, injuries=[{
        'name': 'ankle sprain', 'date': (TODAY - timedelta(days=10)).isoformat(),
        'status': 'active', 'restricted_activities': ['no jumping']}])
    plan = {'days': {D1: {'planned': {
        'type': 'rest', 'purpose': 'gym day',
        'exercises': [{'name': 'BOX_JUMP', 'category': 'PLYO', 'sets': 3, 'reps': 8}],
        'sessions': [{'type': 'strength', 'duration_mins': 30, 'purpose': 'legs'}],
    }}}}
    result = update_weekly_plan(plan=plan)
    assert result.get('error') == 'injury_gate', result
    exercises = [v.get('exercise') for v in result['violations']]
    assert 'BOX_JUMP' in exercises


# ---------------------------------------------------------------------------
# The push (red team N5, N6, N7)
# ---------------------------------------------------------------------------

def _push_everything(client):
    preview = push_plan_to_garmin()
    assert preview['status'] == 'preview'
    confirmed = push_plan_to_garmin(confirm_token=preview['confirm_token'])
    assert confirmed['status'] == 'success', confirmed
    return confirmed


def test_library_read_failure_at_confirm_keeps_the_token(push_env):
    data_env, client = push_env
    _push_everything(client)                       # tracked ids exist now
    preview = push_plan_to_garmin()
    client.overrides['get_workouts'] = RuntimeError('library down')
    failed = push_plan_to_garmin(confirm_token=preview['confirm_token'])
    assert failed['error'] == 'garmin_library_unavailable'
    assert not [c for c in client.calls if c[0].startswith('upload')][len(client.uploaded):]
    del client.overrides['get_workouts']
    retried = push_plan_to_garmin(confirm_token=preview['confirm_token'])
    assert retried['status'] == 'success', retried


def test_summary_says_uploads_failed_when_nothing_landed(push_env):
    data_env, client = push_env
    boom = RuntimeError('upload refused')
    client.overrides.update({'upload_cycling_workout': boom, 'upload_running_workout': boom,
                             'upload_workout': boom})
    preview = push_plan_to_garmin()
    result = push_plan_to_garmin(confirm_token=preview['confirm_token'])
    assert result['status'] == 'partial'
    assert result['summary'].startswith('No workouts pushed:') and 'upload(s) failed' in result['summary']
    assert result['pushed'] == []


def test_placeholder_skip_reason_matches_the_save_warning(push_env):
    data_env, client = push_env
    planner.merge_plan_days({D0: {'planned': {'type': 'running', 'purpose': 'placeholder'}}})
    preview = push_plan_to_garmin()
    reasons = [s['reason'] for s in preview['skipped'] if s.get('date') == D0]
    assert reasons and reasons[0].startswith('nothing to render')


# ---------------------------------------------------------------------------
# Generic HR zones are never silent; the purpose rides along (guardian N2, opportunity)
# ---------------------------------------------------------------------------

def _profile(sandbox, with_zones: bool):
    personal = {'name': 'T', 'max_hr': 185}
    if with_zones:
        personal['hr_zones'] = {'z1_recovery': [0, 125], 'z2_aerobic': [125, 140], 'z3_tempo': [140, 155],
                                'z4_threshold': [155, 168], 'z5_max': [168, 185]}
    (sandbox / 'athlete.json').write_text(json.dumps({'personal': personal, 'injury_history': []}),
                                          encoding='utf-8')


def test_generic_hr_zones_are_flagged(sandbox_data_dir):
    session = {'type': 'cycling', 'duration_mins': 60, 'intensity': 'easy', 'purpose': 'base'}
    _profile(sandbox_data_dir, with_zones=False)
    report = _report(session)
    assert report['ok'] and any('@ HR ' in line for line in report['steps'])
    assert any('no HR zones' in w['message'] for w in report['warnings']), report['warnings']
    _profile(sandbox_data_dir, with_zones=True)
    report = _report(session)
    assert report['steps'][0].startswith('1. Interval 1:00:00 @ HR 125-140 bpm')
    assert not any('no HR zones' in w['message'] for w in report['warnings'])


def test_running_and_strength_workouts_carry_the_purpose(sandbox_data_dir):
    _profile(sandbox_data_dir, with_zones=True)
    run = _report({'type': 'running', 'duration_mins': 40, 'intensity': 'easy', 'purpose': 'Tendon reload'})
    assert run['workout'].description == 'Tendon reload'
    gym = _report({'type': 'strength', 'duration_mins': 30, 'purpose': 'Posterior chain',
                   'exercises': [{'name': 'GOBLET_SQUAT', 'category': 'SQUAT', 'sets': 3, 'reps': 10,
                                  'weight_kg': 20}]})
    assert gym['workout']['description'] == 'Posterior chain'


# ---------------------------------------------------------------------------
# update_injury_status teaches the phrasing the exercise gate can see
# ---------------------------------------------------------------------------

def test_update_injury_status_reports_restriction_coverage(sandbox_data_dir):
    injury_date = (TODAY - timedelta(days=5)).isoformat()
    (sandbox_data_dir / 'athlete.json').write_text(json.dumps({
        'personal': {'name': 'T'},
        'injury_history': [{'name': 'ankle sprain', 'date': injury_date, 'status': 'active',
                            'restricted_activities': ['running']}],
    }), encoding='utf-8')
    shutil.copyfile(ROOT / 'evals' / 'authoring' / 'exercises.json', sandbox_data_dir / 'exercises.json')
    raw = injury_mod.update_injury_status(injury_date=injury_date,
                                          restricted_activities=['running', 'no jumping', 'hopscotch'])
    result = json.loads(raw)
    assert result['status'] == 'success', result
    coverage = result['restriction_coverage']
    assert 'running' not in coverage                     # a sport word gates session types
    assert coverage['no jumping']['catalogue_keys'] >= 2  # BOX_JUMP, SQUAT_JUMP, ...
    assert coverage['hopscotch']['catalogue_keys'] == 0
    assert 'hopscotch' in result['hint']
