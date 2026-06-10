"""End-to-end tests for the typed plan -> Garmin push pipeline (Phase 4).

Drives the REAL flow with seeded data files and the canonical
FakeGarminClient: update_weekly_plan (typed dict, purposes on every non-rest
session) then push_plan_to_garmin, asserting:

- workout payload structure per sport: cycling (HR-targeted single block),
  running (structured warmup + repeat block + cooldown with pace targets
  derived from the athlete's threshold pace), strength (warmup + per-exercise
  RepeatGroupDTO with reps/weights), yoga (timed block)
- every pushed workout is scheduled to its plan date
- re-push deletes the previously pushed workout ids and persists the new ones
- the injury hard gate blocks the push, and override_injury_gate=True pushes
  with a logged override note
- rest days and unknown workout types are reported in 'skipped', never errors
"""
import json
from datetime import date, timedelta

import pytest

import coach.garmin_client as garmin_client_mod
import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_mod
import coach.parsers as parsers_mod
import coach.workout_builder as workout_builder
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod
import coach.tools.strength_tools as strength_mod

from coach.tools.planning_tools import push_plan_to_garmin, update_weekly_plan
from conftest import FakeGarminClient, patch_garmin_everywhere

TODAY = date.today()

D0 = TODAY.isoformat()
D1 = (TODAY + timedelta(days=1)).isoformat()
D2 = (TODAY + timedelta(days=2)).isoformat()
D3 = (TODAY + timedelta(days=3)).isoformat()
D4 = (TODAY + timedelta(days=4)).isoformat()
D5 = (TODAY + timedelta(days=5)).isoformat()

# Athlete zone config the builders read — targets are deterministic.
HR_ZONES = {
    'z1_recovery': [95, 117],
    'z2_aerobic': [118, 139],
    'z3_tempo': [140, 155],
    'z4_threshold': [156, 171],
    'z5_max': [172, 188],
}
THRESHOLD_PACE = 330  # 5:30/km -> z2_easy [379, 409] sec/km

RUN_STRUCTURE = [
    {'phase': 'warmup', 'duration_secs': 180, 'intensity': 'recovery',
     'notes': '300m walk + drills'},
    {'phase': 'repeat', 'iterations': 4, 'steps': [
        {'phase': 'interval', 'duration_secs': 240, 'intensity': 'easy',
         'notes': 'Run 4 min'},
        {'phase': 'recovery', 'duration_secs': 120, 'intensity': 'recovery',
         'notes': 'Walk 2 min'},
    ]},
    {'phase': 'cooldown', 'duration_secs': 180, 'intensity': 'recovery',
     'notes': '200m walk'},
]

STRENGTH_EXERCISES = [
    {'name': 'GOBLET_SQUAT', 'category': 'SQUAT', 'sets': 3, 'reps': 10,
     'weight_kg': 24, 'rest_secs': 60},
    {'name': 'BENCH_PRESS', 'category': 'BENCH_PRESS', 'sets': 3, 'reps': 8,
     'weight_kg': 40, 'rest_secs': 90},
]


def _full_plan():
    """A realistic week: ride, structured run, gym, yoga, rest, unknown."""
    return {
        'days': {
            D0: {'planned': {
                'type': 'cycling', 'duration_mins': 90, 'intensity': 'easy',
                'description': 'Endurance ride',
                'purpose': 'Z2 aerobic base toward the gravel race'}},
            D1: {'planned': {
                'type': 'running', 'duration_mins': 30, 'intensity': 'easy',
                'description': 'L2 R4/W2 x 4', 'structure': RUN_STRUCTURE,
                'purpose': 'Run-walk protocol — tendon reload'}},
            D2: {'planned': {
                'type': 'strength', 'duration_mins': 45,
                'description': 'Lower body strength',
                'exercises': STRENGTH_EXERCISES,
                'purpose': 'Posterior chain for climbing power'}},
            D3: {'planned': {
                'type': 'yoga', 'duration_mins': 30,
                'description': 'Hip mobility flow',
                'purpose': 'Hip mobility for the bike position'}},
            D4: {'planned': {'type': 'rest'}},
            D5: {'planned': {
                'type': 'juggling', 'duration_mins': 20,
                'purpose': 'Coordination fun — not a Garmin sport'}},
        },
        'rationale': 'e2e push test plan',
    }


@pytest.fixture
def data_env(data_dir, monkeypatch):
    """Redirect DATA_DIR in every module that does file I/O to a tmp dir."""
    for mod in (planner, rules, fitness_mod, parsers_mod, workout_builder,
                coaching_mod, planning_mod, strength_mod):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    return data_dir


def _write(data_dir, filename, payload):
    (data_dir / filename).write_text(json.dumps(payload), encoding='utf-8')


def _seed_athlete(data_dir, injuries=None):
    _write(data_dir, 'athlete.json', {
        'personal': {
            'name': 'Test Athlete', 'age': 36, 'weight_kg': 70,
            'max_hr': 188, 'resting_hr': 52,
            'hr_zones': HR_ZONES,
            'threshold_pace_sec_per_km': THRESHOLD_PACE,
        },
        'injury_history': injuries or [],
        'life_constraints': {}, 'preferences': {},
    })


@pytest.fixture
def push_env(data_env, monkeypatch):
    """Healthy athlete + saved typed plan + FakeGarminClient everywhere."""
    _seed_athlete(data_env)
    save = update_weekly_plan(plan=_full_plan())
    assert save['status'] == 'success', save

    client = FakeGarminClient()
    patch_garmin_everywhere(monkeypatch, client)
    return data_env, client


def _read_plan(data_dir):
    return json.loads(
        (data_dir / 'weekly_plan.json').read_text(encoding='utf-8'))


def _uploads_by_kind(client):
    return {kind: workout for kind, workout in client.uploaded}


# ---------------------------------------------------------------------------
# Typed save (the e2e entry point)
# ---------------------------------------------------------------------------

class TestTypedPlanSave:
    def test_typed_plan_saves_and_derives_week_bounds(self, data_env):
        _seed_athlete(data_env)

        result = update_weekly_plan(plan=_full_plan())

        assert result['status'] == 'success'
        assert result['purpose_warnings'] == []
        on_disk = _read_plan(data_env)
        assert set(on_disk['days']) == {D0, D1, D2, D3, D4, D5}
        assert on_disk['week_start'] == D0
        assert on_disk['week_end'] == D5

    def test_missing_purpose_rejects_the_save(self, data_env):
        _seed_athlete(data_env)
        plan = _full_plan()
        del plan['days'][D0]['planned']['purpose']

        result = update_weekly_plan(plan=plan)

        assert result['error'] == 'purpose_gate'
        assert any(m['date'] == D0 for m in result['missing_purpose'])
        assert not (data_env / 'weekly_plan.json').exists(), \
            "a gated plan must not be saved"

    def test_session_without_type_fails_schema_validation(self, data_env):
        _seed_athlete(data_env)
        plan = _full_plan()
        del plan['days'][D1]['planned']['type']

        result = update_weekly_plan(plan=plan)

        assert result['error'] == 'validation_error'
        assert any(p.get('day') == D1 for p in result['problems'])


# ---------------------------------------------------------------------------
# First push: payloads, scheduling, skip reporting, persisted ids
# ---------------------------------------------------------------------------

class TestPushHappyPath:
    def test_push_summary_and_persisted_ids(self, push_env):
        data_env, client = push_env

        result = push_plan_to_garmin()

        assert result['status'] == 'success'
        assert result['errors'] == []
        assert result['duplicates_deleted'] == 0
        assert [(p['date'], p['type']) for p in result['pushed']] == [
            (D0, 'cycling'), (D1, 'running'), (D2, 'strength'), (D3, 'yoga')]
        pushed_ids = [p['workout_id'] for p in result['pushed']]
        assert len(set(pushed_ids)) == 4
        # New ids persisted for next-push cleanup
        assert sorted(_read_plan(data_env)['pushed_workout_ids']) == sorted(pushed_ids)

    def test_each_workout_scheduled_on_its_plan_date(self, push_env):
        _, client = push_env

        result = push_plan_to_garmin()

        scheduled = dict(client.scheduled)  # {workout_id: date}
        assert len(scheduled) == 4
        for pushed in result['pushed']:
            assert scheduled[pushed['workout_id']] == pushed['date']

    def test_rest_and_unknown_types_are_skipped_not_errors(self, push_env):
        _, client = push_env

        result = push_plan_to_garmin()

        skipped = {s['date']: s['reason'] for s in result['skipped']}
        assert 'rest day' in skipped[D4]
        assert skipped[D5] == 'unknown workout type'
        assert result['errors'] == []
        # Nothing was uploaded for the skipped days
        assert len(client.uploaded) == 4

    def test_cycling_payload_single_z2_block_with_hr_target(self, push_env):
        _, client = push_env
        push_plan_to_garmin()

        workout = _uploads_by_kind(client)['cycling']
        assert '(Outdoor)' in workout.workoutName
        segment = workout.workoutSegments[0]
        assert segment.sportType['sportTypeKey'] == 'cycling'
        # Simple outdoor ride: ONE main block (no warmup/cooldown noise)
        steps = segment.workoutSteps
        assert len(steps) == 1
        step = steps[0]
        assert step.stepType['stepTypeKey'] == 'interval'
        assert step.endCondition['conditionTypeKey'] == 'time'
        assert step.endConditionValue == 90 * 60
        # The prescription reaches the wrist: z2 HR band from the profile
        assert step.targetType['workoutTargetTypeKey'] == 'heart.rate.zone'
        assert (step.targetValueOne, step.targetValueTwo) == tuple(
            HR_ZONES['z2_aerobic'])
        assert workout.estimatedDurationInSecs == 90 * 60

    def test_running_payload_repeat_block_with_pace_targets(self, push_env):
        _, client = push_env
        push_plan_to_garmin()

        workout = _uploads_by_kind(client)['running']
        segment = workout.workoutSegments[0]
        assert segment.sportType['sportTypeKey'] == 'running'
        warmup, repeat, cooldown = segment.workoutSteps

        assert warmup.stepType['stepTypeKey'] == 'warmup'
        assert warmup.endConditionValue == 180
        assert cooldown.stepType['stepTypeKey'] == 'cooldown'
        assert cooldown.endConditionValue == 180

        assert repeat.stepType['stepTypeKey'] == 'repeat'
        assert repeat.numberOfIterations == 4
        assert repeat.endCondition['conditionTypeKey'] == 'iterations'
        interval, recovery = repeat.workoutSteps
        assert interval.stepType['stepTypeKey'] == 'interval'
        assert interval.endConditionValue == 240
        assert recovery.stepType['stepTypeKey'] == 'recovery'
        assert recovery.endConditionValue == 120

        # 'easy' resolves to the pace band derived from threshold pace
        # (z2_easy = [379, 409] sec/km -> slow/fast m/s)
        assert interval.targetType['workoutTargetTypeKey'] == 'pace.zone'
        assert interval.targetValueOne == pytest.approx(1000 / 409, rel=1e-3)
        assert interval.targetValueTwo == pytest.approx(1000 / 379, rel=1e-3)

        # Estimated duration: 180 + 4*(240+120) + 180 = 1800s
        assert workout.estimatedDurationInSecs == 1800

    def test_strength_payload_exercises_sets_reps_weights(self, push_env):
        _, client = push_env
        result = push_plan_to_garmin()

        # strength shares the generic upload path with yoga — find by sport
        workout = [w for kind, w in client.uploaded
                   if kind == 'generic'
                   and w['sportType']['sportTypeKey'] == 'strength_training'][0]
        assert workout['exercise_count'] == 2
        strength_entry = [p for p in result['pushed'] if p['type'] == 'strength'][0]
        assert strength_entry['exercise_count'] == 2

        steps = workout['workoutSegments'][0]['workoutSteps']
        # Built-in warmup block + lap-button rest, then one repeat per exercise
        assert steps[0]['stepType']['stepTypeKey'] == 'warmup'
        assert steps[0]['endConditionValue'] == 300.0
        assert steps[1]['stepType']['stepTypeKey'] == 'rest'
        assert steps[1]['endCondition']['conditionTypeKey'] == 'lap.button'

        repeats = steps[2:]
        assert len(repeats) == 2
        for repeat, spec in zip(repeats, STRENGTH_EXERCISES):
            assert repeat['type'] == 'RepeatGroupDTO'
            assert repeat['numberOfIterations'] == spec['sets']
            exercise_step, rest_step = repeat['workoutSteps']
            assert exercise_step['category'] == spec['category']
            assert exercise_step['endCondition']['conditionTypeKey'] == 'reps'
            assert exercise_step['endConditionValue'] == float(spec['reps'])
            assert exercise_step['weightValue'] == float(spec['weight_kg'])
            assert exercise_step['weightUnit']['unitKey'] == 'kilogram'
            assert rest_step['endCondition']['conditionTypeKey'] == 'lap.button'

    def test_yoga_payload_timed_block(self, push_env):
        _, client = push_env
        push_plan_to_garmin()

        # yoga shares the generic upload path with strength — find it by sport
        yoga = [w for kind, w in client.uploaded
                if kind == 'generic'
                and w['sportType']['sportTypeKey'] == 'yoga'][0]
        steps = yoga['workoutSegments'][0]['workoutSteps']
        assert len(steps) == 1
        assert steps[0]['endConditionValue'] == 30 * 60.0
        assert yoga['estimatedDurationInSecs'] == 30 * 60

    def test_strength_and_yoga_both_use_generic_upload(self, push_env):
        _, client = push_env
        push_plan_to_garmin()

        assert client.call_counts['upload_cycling_workout'] == 1
        assert client.call_counts['upload_running_workout'] == 1
        assert client.call_counts['upload_workout'] == 2  # strength + yoga


# ---------------------------------------------------------------------------
# Re-push: duplicate prevention
# ---------------------------------------------------------------------------

class TestRePush:
    def test_repush_deletes_prior_workouts_and_replaces_ids(self, push_env):
        data_env, client = push_env

        first = push_plan_to_garmin()
        first_ids = [p['workout_id'] for p in first['pushed']]

        second = push_plan_to_garmin()

        assert second['duplicates_deleted'] == len(first_ids)
        assert sorted(client.deleted_workout_ids) == sorted(first_ids)
        second_ids = [p['workout_id'] for p in second['pushed']]
        assert not set(first_ids) & set(second_ids)
        # Only the live ids remain persisted — deleted ones are dropped
        assert sorted(_read_plan(data_env)['pushed_workout_ids']) == sorted(second_ids)

    def test_repush_checks_library_before_deleting(self, push_env):
        _, client = push_env
        first = push_plan_to_garmin()
        first_ids = [p['workout_id'] for p in first['pushed']]

        # Athlete manually deleted one workout on Garmin Connect meanwhile
        manually_removed = first_ids[0]
        client.workout_library = [
            w for w in client.workout_library
            if w['workoutId'] != manually_removed
        ]

        second = push_plan_to_garmin()

        assert second['duplicates_deleted'] == len(first_ids) - 1
        assert manually_removed not in client.deleted_workout_ids


# ---------------------------------------------------------------------------
# Injury hard gate on push
# ---------------------------------------------------------------------------

RUNNING_INJURY = {
    'date': (TODAY - timedelta(days=10)).isoformat(),
    'type': 'achilles tendinopathy', 'body_region': 'ankle',
    'status': 'active', 'severity': 'moderate',
    'restricted_activities': ['running', 'jumping'],
    'safe_activities': ['cycling', 'swimming'],
}


class TestInjuryGate:
    @pytest.fixture
    def injured_env(self, data_env, monkeypatch):
        """Plan saved while healthy, injury recorded afterwards."""
        _seed_athlete(data_env)
        assert update_weekly_plan(plan=_full_plan())['status'] == 'success'
        _seed_athlete(data_env, injuries=[RUNNING_INJURY])

        client = FakeGarminClient()
        patch_garmin_everywhere(monkeypatch, client)
        return data_env, client

    def test_push_blocked_by_active_running_restriction(self, injured_env):
        _, client = injured_env

        result = push_plan_to_garmin()

        assert result['error'] == 'injury_gate'
        violations = result['violations']
        assert violations[0]['date'] == D1
        assert violations[0]['session_type'] == 'running'
        assert violations[0]['injury_status'] == 'active'
        assert 'running' in violations[0]['matched_restrictions']
        # Hard gate: NOTHING was uploaded or scheduled
        assert client.uploaded == []
        assert client.scheduled == []

    def test_override_pushes_with_logged_note(self, injured_env):
        data_env, client = injured_env

        result = push_plan_to_garmin(override_injury_gate=True)

        assert result['status'] == 'success'
        assert result['injury_gate']['injury_gate_overridden'] is True
        assert result['injury_gate']['violations']
        # The restricted run went out (athlete gave informed consent)
        assert [p['type'] for p in result['pushed']] == [
            'cycling', 'running', 'strength', 'yoga']
        assert len(client.scheduled) == 4
