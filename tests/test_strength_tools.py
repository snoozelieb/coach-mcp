"""Tests for tools/strength_tools.py — progression logic and state transitions."""
import json
from datetime import date

import pytest

import coach.planner as planner
from coach.config import PROGRESSION_INCREMENT_KG, MIN_SETS_FOR_PROGRESSION
from coach.tools.strength_tools import (
    _get_canonical_exercise_group,
    _calculate_progression,
    sync_strength_session,
    get_strength_baseline,
    approve_progression,
    set_exercise_preference,
    generate_strength_workout,
    add_exercise,
)
from conftest import (
    FakeGarminClient,
    make_garmin_activity,
    patch_garmin_everywhere,
)


@pytest.fixture
def strength_dir(data_dir, monkeypatch):
    """Redirect DATA_DIR and seed athlete.json with strength baseline."""
    monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
    athlete = {
        'personal': {'name': 'Test'},
        'strength_baseline': {
            'exercises': {
                'bench_press': {
                    'canonical_name': 'BENCH_PRESS',
                    'preferred_variation': 'DUMBBELL_BENCH_PRESS',
                    'current': {'weight_kg': 10, 'reps': 12, 'sets': 3, 'last_performed': '2026-01-10'},
                    'history': [],
                    'progression': None,
                },
                'squat': {
                    'canonical_name': 'SQUAT',
                    'preferred_variation': 'GOBLET_SQUAT',
                    'current': {'weight_kg': 20, 'reps': 12, 'sets': 3, 'last_performed': '2026-01-10'},
                    'history': [],
                    'progression': {
                        'suggested_weight_kg': 22.5,
                        'suggested_reps': 12,
                        'rationale': 'Completed 3x12 @ 20kg',
                        'status': 'pending',
                    },
                },
            },
            'equivalence_groups': {},
        },
    }
    (data_dir / 'athlete.json').write_text(json.dumps(athlete))
    return data_dir


# ---------------------------------------------------------------------------
# _get_canonical_exercise_group
# ---------------------------------------------------------------------------

class TestGetCanonicalExerciseGroup:
    def test_exercise_found_in_default_group(self):
        result = _get_canonical_exercise_group('DUMBBELL_BENCH_PRESS', 'UNKNOWN_CAT', {})
        assert result == 'BENCH_PRESS'

    def test_custom_groups_override(self):
        custom = {'CUSTOM_GROUP': ['CUSTOM_EXERCISE_A', 'CUSTOM_EXERCISE_B']}
        result = _get_canonical_exercise_group('CUSTOM_EXERCISE_A', 'UNKNOWN', custom)
        assert result == 'CUSTOM_GROUP'


# ---------------------------------------------------------------------------
# _calculate_progression
# ---------------------------------------------------------------------------

class TestCalculateProgression:
    def test_progression_when_target_met(self):
        result = _calculate_progression(
            current_weight=10.0, target_reps=12, actual_reps=12, actual_sets=3,
        )
        assert result is not None
        assert result['suggested_weight_kg'] == 10.0 + PROGRESSION_INCREMENT_KG
        assert result['status'] == 'pending'

    def test_no_progression_insufficient_reps(self):
        result = _calculate_progression(
            current_weight=10.0, target_reps=12, actual_reps=8, actual_sets=3,
        )
        assert result is None

    def test_no_progression_insufficient_sets(self):
        result = _calculate_progression(
            current_weight=10.0, target_reps=12, actual_reps=12, actual_sets=2,
        )
        assert result is None

    def test_exceeding_reps_still_progresses(self):
        result = _calculate_progression(
            current_weight=10.0, target_reps=12, actual_reps=15, actual_sets=3,
        )
        assert result is not None
        assert result['suggested_weight_kg'] == 10.0 + PROGRESSION_INCREMENT_KG
        # Target reps must stay at the original target, not drift to actual
        assert result['suggested_reps'] == 12


# ---------------------------------------------------------------------------
# get_strength_baseline — only test meaningful aggregation
# ---------------------------------------------------------------------------

class TestGetStrengthBaseline:
    def test_pending_progressions_listed(self, strength_dir):
        result = json.loads(get_strength_baseline())

        assert len(result['pending_progressions']) == 1
        assert result['pending_progressions'][0]['exercise'] == 'squat'


# ---------------------------------------------------------------------------
# approve_progression — state transition
# ---------------------------------------------------------------------------

class TestApproveProgression:
    def test_approves_and_updates_file(self, strength_dir):
        result = json.loads(approve_progression('squat'))

        assert result['status'] == 'success'
        assert result['old_weight_kg'] == 20
        assert result['new_weight_kg'] == 22.5

        # Verify persistence — weight AND progression status
        athlete = json.loads((strength_dir / 'athlete.json').read_text())
        squat = athlete['strength_baseline']['exercises']['squat']
        assert squat['current']['weight_kg'] == 22.5
        assert squat['progression']['status'] == 'approved'

        # Progression must no longer show as pending in baseline view
        baseline_result = json.loads(get_strength_baseline())
        assert len(baseline_result['pending_progressions']) == 0

    def test_no_pending_progression(self, strength_dir):
        result = json.loads(approve_progression('bench_press'))

        assert result['status'] == 'error'
        assert 'No pending progression' in result['message']


# ---------------------------------------------------------------------------
# set_exercise_preference — validation
# ---------------------------------------------------------------------------

class TestSetExercisePreference:
    def test_valid_preference(self, strength_dir):
        result = json.loads(set_exercise_preference('BENCH_PRESS', 'BARBELL_BENCH_PRESS'))

        assert result['status'] == 'success'
        assert result['preferred_variation'] == 'BARBELL_BENCH_PRESS'

    def test_invalid_group(self, strength_dir):
        result = json.loads(set_exercise_preference('NONEXISTENT_GROUP', 'BARBELL_BENCH_PRESS'))

        assert result['status'] == 'error'
        assert 'Unknown exercise group' in result['message']

    def test_invalid_variation(self, strength_dir):
        result = json.loads(set_exercise_preference('BENCH_PRESS', 'INVALID_EXERCISE'))

        assert result['status'] == 'error'
        assert 'not in the BENCH_PRESS group' in result['message']


# ---------------------------------------------------------------------------
# sync_strength_session — the Garmin → baseline pipeline
# ---------------------------------------------------------------------------

def _seed_strength_athlete(data_dir, squat_kg=20):
    """athlete.json with a squat baseline at squat_kg in the sandbox dir."""
    athlete = {
        'personal': {'name': 'Test Athlete'},
        'strength_baseline': {
            'exercises': {
                'squat': {
                    'canonical_name': 'SQUAT',
                    'preferred_variation': 'GOBLET_SQUAT',
                    'current': {'weight_kg': squat_kg, 'reps': 12, 'sets': 3,
                                'last_performed': '2026-01-10'},
                    'history': [],
                    'progression': None,
                },
            },
            'equivalence_groups': {},
        },
    }
    (data_dir / 'athlete.json').write_text(json.dumps(athlete))
    return athlete


def _exercise_sets_payload(activity_id, *, sets=1, reps=8, weight_grams=60000.0,
                           name='BARBELL_BACK_SQUAT', category='SQUAT'):
    """get_activity_exercise_sets() shape with N ACTIVE sets + a REST set."""
    active = [{
        'exercises': [{'category': category, 'name': name,
                       'probability': 100.0}],
        'duration': 52.0,
        'repetitionCount': reps,
        'weight': weight_grams,
        'setType': 'ACTIVE',
        'startTime': '2026-01-15T17:05:00.0',
    } for _ in range(sets)]
    rest = [{'exercises': [], 'duration': 90.0, 'repetitionCount': None,
             'weight': None, 'setType': 'REST',
             'startTime': '2026-01-15T17:06:00.0'}]
    return {'activityId': activity_id, 'exerciseSets': active + rest}


class TestSyncStrengthSession:
    def test_syncs_most_recent_strength_session_and_flags_pr(
            self, sandbox_data_dir, monkeypatch):
        """Happy path: finds yesterday's strength session, maps the exercise
        to its canonical group, updates the baseline, and flags the PR."""
        _seed_strength_athlete(sandbox_data_dir, squat_kg=20)
        client = FakeGarminClient(overrides={
            'get_activity_exercise_sets': lambda aid: _exercise_sets_payload(
                aid, sets=1, reps=8, weight_grams=60000.0),
        })
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(sync_strength_session())

        assert result['status'] == 'success'
        assert result['exercises_synced'] == 1
        assert result['updates'][0]['exercise'] == 'squat'
        assert result['updates'][0]['current']['weight_kg'] == 60.0
        # 60kg beats the 20kg baseline → PR
        assert result['prs'] == [{'exercise': 'squat', 'previous_kg': 20,
                                  'new_kg': 60.0, 'improvement_kg': 40.0}]
        # 1 set of 8 — no progression suggested
        assert result['progression_suggestions'] == []

        # Persisted to athlete.json (sandbox)
        athlete = json.loads((sandbox_data_dir / 'athlete.json').read_text())
        squat = athlete['strength_baseline']['exercises']['squat']
        assert squat['current']['weight_kg'] == 60.0
        assert squat['preferred_variation'] == 'BARBELL_BACK_SQUAT'
        assert len(squat['history']) == 1
        assert athlete['strength_baseline']['last_synced'] == date.today().isoformat()

    def test_target_met_suggests_progression(self, sandbox_data_dir, monkeypatch):
        """3x12 at the working weight → pending +increment suggestion."""
        _seed_strength_athlete(sandbox_data_dir, squat_kg=20)
        client = FakeGarminClient(overrides={
            'get_activity_exercise_sets': lambda aid: _exercise_sets_payload(
                aid, sets=MIN_SETS_FOR_PROGRESSION, reps=12,
                weight_grams=22500.0),
        })
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(sync_strength_session())

        assert result['status'] == 'success'
        suggestion = result['progression_suggestions'][0]
        assert suggestion['exercise'] == 'squat'
        assert suggestion['current_kg'] == 22.5
        assert suggestion['suggested_kg'] == 22.5 + PROGRESSION_INCREMENT_KG

        athlete = json.loads((sandbox_data_dir / 'athlete.json').read_text())
        progression = athlete['strength_baseline']['exercises']['squat']['progression']
        assert progression['status'] == 'pending'

    def test_no_strength_sessions_in_week(self, sandbox_data_dir, monkeypatch):
        _seed_strength_athlete(sandbox_data_dir)
        client = FakeGarminClient(activities=[
            make_garmin_activity(date.today(), 'cycling'),
        ])
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(sync_strength_session())

        assert result['status'] == 'no_activity'

    def test_exercise_sets_fetch_failure_is_clean_error(
            self, sandbox_data_dir, monkeypatch):
        _seed_strength_athlete(sandbox_data_dir)
        client = FakeGarminClient(overrides={
            'get_activity_exercise_sets': Exception('endpoint down'),
        })
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(sync_strength_session('12345'))

        assert result['status'] == 'error'
        assert 'Could not fetch exercise sets' in result['message']

    def test_no_exercise_set_data(self, sandbox_data_dir, monkeypatch):
        _seed_strength_athlete(sandbox_data_dir)
        client = FakeGarminClient(overrides={
            'get_activity_exercise_sets': lambda aid: {},
        })
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(sync_strength_session('12345'))

        assert result['status'] == 'no_data'


# ---------------------------------------------------------------------------
# generate_strength_workout — context-aware selection
# ---------------------------------------------------------------------------

EXERCISE_LIBRARY = {
    'metadata': {'exercise_count': 6},
    'exercises': {
        'BENCH_PRESS': {'category': 'BENCH_PRESS', 'garmin_category': 'BENCH_PRESS',
                        'muscles': ['chest', 'triceps'],
                        'primary_muscles': ['chest']},
        'BENT_OVER_ROW': {'category': 'ROW', 'garmin_category': 'ROW',
                          'muscles': ['lats', 'biceps'],
                          'primary_muscles': ['lats']},
        'BICEP_CURL': {'category': 'CURL', 'garmin_category': 'CURL',
                       'muscles': ['biceps'], 'primary_muscles': ['biceps']},
        'SQUAT': {'category': 'SQUAT', 'garmin_category': 'SQUAT',
                  'muscles': ['quadriceps', 'glutes'],
                  'primary_muscles': ['quadriceps']},
        'PLANK': {'category': 'PLANK', 'garmin_category': 'PLANK',
                  'muscles': ['core'], 'primary_muscles': ['core']},
        'CALF_RAISE': {'category': 'CALF_RAISE', 'garmin_category': 'CALF_RAISE',
                       'muscles': ['calves'], 'primary_muscles': ['calves']},
    },
    'injury_mappings': {'ankle': ['CALF_RAISE']},
}


def _seed_workout_env(data_dir, injury_history=None):
    (data_dir / 'exercises.json').write_text(json.dumps(EXERCISE_LIBRARY))
    (data_dir / 'athlete.json').write_text(json.dumps({
        'personal': {'name': 'Test Athlete'},
        'injury_history': injury_history or [],
    }))


class TestGenerateStrengthWorkout:
    def test_upper_body_happy_path(self, sandbox_data_dir, monkeypatch):
        _seed_workout_env(sandbox_data_dir)
        patch_garmin_everywhere(monkeypatch, FakeGarminClient(activities=[]))

        result = json.loads(generate_strength_workout(focus='upper_body'))

        names = [ex['name'] for ex in result['exercises']]
        # One pick per template group: push, pull, accessory
        assert names == ['BENCH_PRESS', 'BENT_OVER_ROW', 'BICEP_CURL']
        assert result['focus'] == 'upper_body'
        assert result['auto_adjustments'] == ['No adjustments needed']
        assert result['estimated_duration_mins'] > 0
        assert all(ex['sets'] == 3 and ex['reps'] == 10
                   for ex in result['exercises'])

    def test_long_ride_switches_lower_body_to_upper(
            self, sandbox_data_dir, monkeypatch):
        """A >60min ride today reduces leg volume and flips a lower_body
        request to upper_body."""
        _seed_workout_env(sandbox_data_dir)
        client = FakeGarminClient(activities=[
            make_garmin_activity(date.today(), 'cycling', duration_secs=2 * 3600),
        ])
        patch_garmin_everywhere(monkeypatch, client)

        result = json.loads(generate_strength_workout(focus='lower_body'))

        assert result['focus'] == 'upper_body'
        assert result['original_focus'] == 'lower_body'
        assert any('cycle' in adj.lower() for adj in result['auto_adjustments'])

    def test_injury_history_adds_prehab(self, sandbox_data_dir, monkeypatch):
        """Resolved ankle injury → CALF_RAISE prehab from injury_mappings."""
        _seed_workout_env(sandbox_data_dir, injury_history=[
            {'type': 'ankle sprain', 'status': 'resolved', 'date': '2025-06-01'},
        ])
        patch_garmin_everywhere(monkeypatch, FakeGarminClient(activities=[]))

        result = json.loads(generate_strength_workout(focus='upper_body'))

        prehab_names = [ex['name'] for ex in result['prehab_exercises']]
        assert 'CALF_RAISE' in prehab_names
        assert 'ankle' in result['prehab_exercises'][0]['reason']

    def test_active_ankle_injury_avoids_calves(self, sandbox_data_dir, monkeypatch):
        """An ACTIVE ankle injury must exclude calf work entirely — including
        the prehab exercise that targets the avoided muscle."""
        _seed_workout_env(sandbox_data_dir, injury_history=[
            {'type': 'ankle sprain', 'status': 'active', 'date': '2026-05-30'},
        ])
        patch_garmin_everywhere(monkeypatch, FakeGarminClient(activities=[]))

        result = json.loads(generate_strength_workout(focus='full_body'))

        all_names = ([ex['name'] for ex in result['exercises']]
                     + [ex['name'] for ex in result['prehab_exercises']])
        assert 'CALF_RAISE' not in all_names
        assert 'calves' in result.get('avoid_muscles', [])

    def test_missing_library_is_clean_error(self, sandbox_data_dir, monkeypatch):
        (sandbox_data_dir / 'athlete.json').write_text(json.dumps(
            {'personal': {}, 'injury_history': []}))
        patch_garmin_everywhere(monkeypatch, FakeGarminClient(activities=[]))

        result = json.loads(generate_strength_workout())

        assert 'Exercise library not found' in result['error']


# ---------------------------------------------------------------------------
# add_exercise — custom library entries
# ---------------------------------------------------------------------------

class TestAddExercise:
    def test_adds_custom_exercise_and_persists(self, sandbox_data_dir):
        (sandbox_data_dir / 'exercises.json').write_text(
            json.dumps(EXERCISE_LIBRARY))

        result = json.loads(add_exercise(
            name='nordic curl',
            category='HAMSTRING_CURL',
            primary_muscles='hamstrings, glutes',
            secondary_muscles='calves',
            injury_prevention='hamstring',
        ))

        assert result['status'] == 'success'
        entry = result['exercise']
        assert entry['custom'] is True
        assert entry['primary_muscles'] == ['hamstrings', 'glutes']
        assert entry['secondary_muscles'] == ['calves']
        assert entry['injury_prevention'] == ['hamstring']

        library = json.loads((sandbox_data_dir / 'exercises.json').read_text())
        # Name normalized to UPPER_SNAKE and tracked as custom
        assert 'NORDIC_CURL' in library['exercises']
        assert 'NORDIC_CURL' in library['custom_exercises']
        assert library['metadata']['exercise_count'] == len(library['exercises'])

    def test_duplicate_rejected(self, sandbox_data_dir):
        (sandbox_data_dir / 'exercises.json').write_text(
            json.dumps(EXERCISE_LIBRARY))

        result = json.loads(add_exercise(
            name='BENCH_PRESS', category='BENCH_PRESS',
            primary_muscles='chest',
        ))

        assert 'already exists' in result['error']

    def test_missing_library_is_clean_error(self, sandbox_data_dir):
        result = json.loads(add_exercise(
            name='NORDIC_CURL', category='HAMSTRING_CURL',
            primary_muscles='hamstrings',
        ))

        assert 'Exercise library not found' in result['error']
