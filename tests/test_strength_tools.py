"""Tests for tools/strength_tools.py — progression logic and state transitions."""
import json
import pytest

import planner
from config import PROGRESSION_INCREMENT_KG, MIN_SETS_FOR_PROGRESSION
from tools.strength_tools import (
    _get_canonical_exercise_group,
    _calculate_progression,
    get_strength_baseline,
    approve_progression,
    set_exercise_preference,
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
