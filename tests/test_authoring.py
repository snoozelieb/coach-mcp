"""The session authoring contract (coach/authoring.py) and the preview.

The renderer draws what the plan says and defaults nothing; the validator
turns every way a session used to degrade silently into a teaching error
or a warning. The cases here are the silent degradations found on the
athlete's own pushed workouts (docs review, 2026-10): each one now errors
with the allowed keys or renders faithfully.
"""
import json
from datetime import date, timedelta

import pytest

import coach.authoring as authoring
import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_mod
import coach.parsers as parsers_mod
import coach.workout_builder as wb
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod

from coach.authoring import (
    INTENSITY_ZONES, VALID_WORKOUT_CATEGORIES, contract_document,
    normalise_exercise_name, normalise_intensity, validate_session,
)
from coach.tools.planning_tools import preview_workout, update_weekly_plan

TODAY = date.today()
TOMORROW = (TODAY + timedelta(days=1)).isoformat()

CATALOGUE = {
    "GOBLET_SQUAT": {"category": "SQUAT"},
    "PLANK": {"category": "SUSPENSION"},
    "ONE_ARM_BENT_OVER_ROW": {"category": "ROW"},
    "SIDE_LUNGE_JUMP_OFF": {"category": "LUNGE"},
    "BAND_PULL_APART": {"category": "CUSTOM", "custom": True},
    "BANDED_ANKLE_EVERSION": {"category": "BANDED_EXERCISES", "custom": True},
    "LYING_LEG_CURL": {"category": "HAMSTRING_CURL"},
    "DEAD_BUG": {"category": "HIP_STABILITY"},
}


def errors_of(session, catalogue=CATALOGUE):
    return [p["message"] for p in validate_session(session, catalogue).errors]


def warnings_of(session, catalogue=CATALOGUE):
    return [p["message"] for p in validate_session(session, catalogue).warnings]


# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------

class TestVocabulary:
    def test_one_intensity_table_for_every_sport(self):
        for word, zones in INTENSITY_ZONES.items():
            assert len(zones) == 3, word
        assert "vo2" in INTENSITY_ZONES  # the documented word that used to map to easy

    @pytest.mark.parametrize("raw, canonical", [
        ("easy", "easy"), ("Easy", "easy"), ("z2", "easy"), ("Zone2", "easy"),
        ("endurance", "easy"), ("z4", "threshold"), ("hard", "threshold"),
        ("VO2max", "vo2"), ("vo2", "vo2"), ("z5", "vo2"), ("sweet spot", "sweet_spot"),
        ("sprint", "max_effort"), ("moderate", None), ("discretion", None), ("", None), (None, None),
    ])
    def test_intensity_aliases(self, raw, canonical):
        assert normalise_intensity(raw) == canonical

    @pytest.mark.parametrize("raw, key", [
        ("Bulgarian split squat", "BULGARIAN_SPLIT_SQUAT"),
        ("goblet-squat", "GOBLET_SQUAT"), ("  Plank ", "PLANK"), ("push up / knees", "PUSH_UP_KNEES"),
    ])
    def test_exercise_names_become_garmin_keys(self, raw, key):
        assert normalise_exercise_name(raw) == key

    def test_catalogue_categories_the_api_rejects_map_to_accepted_ones(self):
        for cat, mapped in authoring.CATALOGUE_CATEGORY_TO_WORKOUT.items():
            assert cat not in VALID_WORKOUT_CATEGORIES
            assert mapped == "" or mapped in VALID_WORKOUT_CATEGORIES


# ---------------------------------------------------------------------------
# The silent degradations, now errors or faithful renderings
# ---------------------------------------------------------------------------

class TestStrengthContract:
    def test_missing_sets_is_required_not_three(self):
        assert any("sets is required" in m for m in errors_of(
            {"type": "strength", "exercises": [{"name": "COPENHAGEN_PLANK", "category": "PLANK", "reps": 10}]}))

    def test_reps_or_duration_exactly_one(self):
        both = errors_of({"type": "strength", "exercises": [
            {"name": "PLANK", "sets": 3, "reps": 10, "duration_secs": 30}]})
        neither = errors_of({"type": "strength", "exercises": [{"name": "PLANK", "sets": 3}]})
        assert any("exactly one of reps" in m for m in both)
        assert any("exactly one of reps" in m for m in neither)

    def test_unknown_key_names_the_allowed_keys(self):
        messages = errors_of({"type": "strength", "exercises": [
            {"name": "GOBLET_SQUAT", "sets": 3, "reps": 10, "weight": 20}]})
        assert messages == ["unknown key 'weight'; allowed keys: name, category, reps, duration_secs, "
                            "each_side, weight_kg, notes, sets, rest_secs"]

    def test_muscle_group_category_suggests_garmin_categories(self):
        (message,) = errors_of({"type": "strength", "exercises": [
            {"name": "HIP_THRUST", "category": "glutes", "sets": 3, "reps": 12}]})
        assert "muscle group" in message and "HIP_RAISE" in message

    def test_unknown_category_lists_the_allowed_set(self):
        (message,) = errors_of({"type": "strength", "exercises": [
            {"name": "X", "category": "MADE_UP", "sets": 3, "reps": 12}]})
        assert "MADE_UP" in message and "BANDED_EXERCISES" in message

    def test_off_catalogue_name_requires_a_category(self):
        (message,) = errors_of({"type": "strength", "exercises": [{"name": "MYSTERY", "sets": 3, "reps": 10}]})
        assert "not in the Garmin exercise catalogue" in message and "category" in message

    def test_custom_catalogue_entry_without_category_requires_one(self):
        (message,) = errors_of({"type": "strength", "exercises": [{"name": "BAND_PULL_APART", "sets": 3, "reps": 15}]})
        assert "CUSTOM" in message and "category" in message

    def test_no_catalogue_on_this_install_requires_a_category(self):
        (message,) = errors_of({"type": "strength", "exercises": [{"name": "GOBLET_SQUAT", "sets": 3, "reps": 10}]},
                               catalogue=None)
        assert "no exercise catalogue" in message

    def test_reps_range_goes_in_notes(self):
        (message,) = errors_of({"type": "strength", "exercises": [{"name": "GOBLET_SQUAT", "sets": 3, "reps": "8-10"}]})
        assert "8-10" in message and "notes" in message

    def test_sets_in_a_circuit_exercise_is_an_error(self):
        (message,) = errors_of({"type": "strength", "exercises": [
            {"rounds": 2, "exercises": [{"name": "GOBLET_SQUAT", "sets": 3, "reps": 10}]}]})
        assert "unknown key 'sets'" in message and "rest_secs" not in message.split("allowed keys")[1]

    def test_prose_sets_without_exercises_warns(self):
        (message,) = warnings_of({"type": "strength", "duration_mins": 15,
                                  "description": "Evening: banded ankle eversion 2x20/side"})
        assert "ONE timed block" in message

    def test_long_cue_warns_about_the_watch_limit(self):
        (message,) = warnings_of({"type": "strength", "exercises": [
            {"name": "GOBLET_SQUAT", "sets": 3, "reps": 10, "notes": "x" * 80}]})
        assert "80 characters" in message

    def test_catalogue_category_mismatch_warns_that_garmin_strips_the_name(self):
        (message,) = warnings_of({"type": "strength", "exercises": [
            {"name": "PLANK", "category": "CORE", "sets": 3, "duration_secs": 30}]})
        assert "SUSPENSION" in message and "strip" in message

    def test_catalogue_correction_keeps_the_plank_name(self):
        """The cached catalogue lists PLANK under SUSPENSION; Garmin also has
        PLANK/PLANK. Stating PLANK keeps the name, no warning."""
        report = validate_session({"type": "strength", "exercises": [
            {"name": "PLANK", "category": "PLANK", "sets": 3, "duration_secs": 30}]}, CATALOGUE)
        assert report.ok and report.warnings == []

    def test_skip_warmup_is_retired(self):
        (message,) = warnings_of({"type": "strength", "skip_warmup": True,
                                  "exercises": [{"name": "GOBLET_SQUAT", "sets": 3, "reps": 10}]})
        assert "warmup_mins" in message

    def test_warmup_mins_must_be_a_number(self):
        (message,) = errors_of({"type": "strength", "warmup_mins": "five",
                                "exercises": [{"name": "GOBLET_SQUAT", "sets": 3, "reps": 10}]})
        assert "warmup_mins must be a number" in message

    def test_a_complete_session_has_no_errors(self):
        report = validate_session({"type": "strength", "warmup_mins": 5, "exercises": [
            {"name": "Goblet squat", "sets": 3, "reps": 10, "weight_kg": 24, "rest_secs": 60, "notes": "Knees out"},
            {"name": "PLANK", "sets": 3, "duration_secs": 45},
            {"rounds": 3, "rest_secs": 60, "exercises": [
                {"name": "DEAD_BUG", "reps": 8, "each_side": True},
                {"name": "COPENHAGEN_PLANK", "category": "PLANK", "duration_secs": 20, "each_side": True}]},
        ]}, CATALOGUE)
        assert report.ok
        assert [type(i).__name__ for i in report.exercises] == ["Exercise", "Exercise", "Circuit"]
        assert report.exercises[0].name == "GOBLET_SQUAT"


class TestPhaseContract:
    def test_typo_key_names_the_phase_keys(self):
        (message,) = errors_of({"type": "running", "structure": [{"phase": "interval", "duration": 10}]})
        assert message.startswith("unknown key 'duration'; allowed keys: phase, duration_secs, duration_mins")

    def test_no_end_condition_is_an_error_not_a_lap_button(self):
        (message,) = errors_of({"type": "running", "structure": [{"phase": "interval", "intensity": "easy"}]})
        assert "exactly one end condition" in message

    def test_two_end_conditions_is_an_error(self):
        (message,) = errors_of({"type": "running", "structure": [
            {"phase": "interval", "duration_mins": 5, "distance_m": 1000}]})
        assert "exactly one end condition" in message

    def test_intensity_outside_the_vocabulary_is_an_error(self):
        (message,) = errors_of({"type": "running", "structure": [
            {"phase": "interval", "duration_mins": 10, "intensity": "moderate"}]})
        assert "'moderate' is not in the vocabulary" in message and "vo2" in message

    def test_zone_aliases_and_vo2_are_accepted(self):
        report = validate_session({"type": "running", "structure": [
            {"phase": "interval", "duration_mins": 10, "intensity": "z4"},
            {"phase": "interval", "duration_mins": 3, "intensity": "VO2max"}]})
        assert report.ok
        assert [p.intensity for p in report.structure] == ["threshold", "vo2"]

    def test_two_numeric_targets_is_an_error(self):
        (message,) = errors_of({"type": "running", "structure": [
            {"phase": "interval", "duration_mins": 5, "pace_sec_per_km": [330, 315], "hr_target": [150, 160]}]})
        assert "ONE target" in message

    def test_intensity_may_label_a_numeric_target(self):
        report = validate_session({"type": "running", "structure": [
            {"phase": "interval", "duration_mins": 5, "pace_sec_per_km": [330, 315], "intensity": "threshold"}]})
        assert report.ok and report.structure[0].target_key() == "pace_sec_per_km"

    def test_phase_word_outside_the_set_is_an_error(self):
        (message,) = errors_of({"type": "running", "structure": [{"phase": "sprint", "duration_mins": 1}]})
        assert "'sprint' is not one of warmup, interval, recovery, cooldown, rest" in message

    def test_phase_aliases(self):
        report = validate_session({"type": "running", "structure": [
            {"phase": "Warm-up", "duration_mins": 10}, {"phase": "walk", "duration_mins": 2},
            {"phase": "work", "duration_mins": 1}]})
        assert [p.phase for p in report.structure] == ["warmup", "recovery", "interval"]

    def test_repeat_block_errors_point_at_the_nested_step(self):
        report = validate_session({"type": "running", "structure": [
            {"phase": "repeat", "iterations": 3, "steps": [{"phase": "interval", "duration_secs": 60, "intensty": "easy"}]}]})
        assert report.errors[0]["field"] == "structure[0].steps[0]"
        assert report.errors[0]["message"].startswith("unknown key 'intensty'")

    def test_repeat_without_steps(self):
        messages = errors_of({"type": "running", "structure": [{"phase": "repeat", "iterations": 3, "step": []}]})
        assert any("steps is required" in m for m in messages)
        assert any("unknown key 'step'; allowed keys: phase, iterations, steps, notes" in m for m in messages)

    def test_negative_or_non_numeric_durations(self):
        assert "greater than 0" in errors_of({"type": "running", "structure": [{"phase": "interval", "duration_mins": -2}]})[0]
        assert 'must be a number or "open"' in errors_of({"type": "running", "structure": [{"phase": "interval", "duration_mins": "ten"}]})[0]

    def test_open_and_fractional_end_conditions(self):
        report = validate_session({"type": "cycling", "structure": [
            {"phase": "warmup", "duration_mins": "open"},
            {"phase": "interval", "duration_mins": 0.5, "power_watts": [300, 320]},
            {"phase": "interval", "duration_secs": 30, "cadence": [100, 110]},
            {"phase": "interval", "distance_m": 5000, "hr_target": [150, 160]}]})
        assert report.ok
        assert [p.end_condition() for p in report.structure] == [
            ("open", None), ("time", 30), ("time", 30.0), ("distance", 5000.0)]

    def test_pace_on_a_ride_is_an_error(self):
        (message,) = errors_of({"type": "cycling", "structure": [
            {"phase": "interval", "duration_mins": 5, "pace_sec_per_km": [300, 290]}]})
        assert "pace targets are for running" in message

    def test_untargeted_interval_warns_only_without_a_session_intensity(self):
        bare = {"type": "running", "structure": [{"phase": "interval", "duration_mins": 10}]}
        assert any("no target" in m for m in warnings_of(bare))
        assert warnings_of({**bare, "intensity": "easy"}) == []
        assert warnings_of({"type": "running", "structure": [{"phase": "warmup", "duration_mins": 10}]}) == []

    def test_session_intensity_outside_the_vocabulary_warns(self):
        (message,) = warnings_of({"type": "cycling", "duration_mins": 60, "intensity": "moderate"})
        assert "'moderate' is not in the zone vocabulary" in message
        assert warnings_of({"type": "cycling", "duration_mins": 60, "intensity": "discretion"}) == []

    def test_structure_on_a_yoga_session_warns(self):
        (message,) = warnings_of({"type": "yoga", "duration_mins": 30, "structure": [{"phase": "interval", "duration_mins": 30}]})
        assert "ignores it" in message

    def test_rest_days_and_plain_sessions_pass(self):
        assert validate_session({"type": "rest"}).ok
        assert validate_session({"type": "padel", "duration_mins": 90, "intensity": "discretion"}).ok
        assert validate_session({"type": "running", "duration_mins": 45, "intensity": "easy"}).ok


# ---------------------------------------------------------------------------
# The contract document
# ---------------------------------------------------------------------------

class TestContractDocument:
    def test_full_contract_carries_vocabulary_schemas_and_limits(self):
        text = contract_document()
        for needle in ("sweet_spot", "duration_secs", "pace_sec_per_km", "power_pct", "each_side",
                       "rounds", "warmup_mins", "BANDED_EXERCISES", "preview_workout"):
            assert needle in text, needle
        assert text.count("```json") == 3

    def test_sport_narrowing(self):
        assert "rounds" not in contract_document("running")
        assert "pace_sec_per_km" not in contract_document("strength")
        assert "rounds" in contract_document("strength")

    def test_schemas_are_generated_from_the_models(self):
        phase_schema = json.loads(contract_document("running").split("```json\n")[1].split("```")[0])
        assert set(phase_schema["properties"]) == set(authoring.Phase.model_fields)
        assert phase_schema.get("additionalProperties") is False


# ---------------------------------------------------------------------------
# The preview (rendered step tree) and the tool
# ---------------------------------------------------------------------------

@pytest.fixture
def data_env(data_dir, monkeypatch):
    for mod in (planner, rules, fitness_mod, parsers_mod, wb, coaching_mod, planning_mod):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    (data_dir / 'athlete.json').write_text(json.dumps({
        'personal': {'hr_zones': {'z1_recovery': [95, 117], 'z2_aerobic': [118, 139],
                                  'z3_tempo': [140, 155], 'z4_threshold': [156, 171], 'z5_max': [172, 188]},
                     'power_zones': {'z1_recovery': [0, 118], 'z2_endurance': [120, 162],
                                     'z4_threshold': [196, 226], 'z5_vo2max': [228, 259]},
                     'ftp': 216, 'threshold_pace_sec_per_km': 330},
        'injury_history': [], 'life_constraints': {}, 'preferences': {},
    }), encoding='utf-8')
    (data_dir / 'exercises.json').write_text(json.dumps({'exercises': CATALOGUE}), encoding='utf-8')
    return data_dir


class TestPreviewWorkoutTool:
    def test_no_session_returns_the_contract(self, data_env):
        result = preview_workout()
        assert result['contract'].startswith('# Session authoring contract')

    def test_strength_session_renders_the_watch_view(self, data_env):
        result = preview_workout({
            'type': 'strength', 'duration_mins': 40, 'warmup_mins': 5, 'name': 'Rehab A',
            'exercises': [
                {'name': 'SIDE_LUNGE_JUMP_OFF', 'sets': 2, 'reps': 8, 'each_side': True,
                 'notes': 'PUSH OFF LEFT, finish through toe. No jump'},
                {'name': 'ONE_ARM_BENT_OVER_ROW', 'sets': 3, 'reps': 10, 'rest_secs': 60},
                {'name': 'BANDED_ANKLE_EVERSION', 'category': 'BANDED_EXERCISES', 'sets': 2, 'reps': 20,
                 'each_side': True},
            ]})
        assert result['ok'] and result['errors'] == []
        assert result['name'] == 'Rehab A' and result['sport'] == 'strength_training'
        assert result['steps'] == [
            '1. [Warm Up] 5:00',
            '2. Rest lap button',
            '3. 2 x',
            '   3.1. SIDE_LUNGE_JUMP_OFF 8 reps — "each side: PUSH OFF LEFT, finish through toe. No…"',
            '   3.2. Rest lap button',
            '4. 3 x',
            '   4.1. ONE_ARM_BENT_OVER_ROW 10 reps',   # no weight invented
            '   4.2. Rest 1:00',
            '5. 2 x',
            '   5.1. [Banded Exercises] 20 reps — "Banded Ankle Eversion each side"',
            '   5.2. Rest lap button',
        ]
        assert [w['field'] for w in result['warnings']] == ['exercises[2]']  # custom catalogue entry
        assert 'workout' not in result

    def test_errors_come_back_with_a_hint_not_a_rendering(self, data_env):
        result = preview_workout({'type': 'strength', 'duration_mins': 30,
                                  'exercises': [{'name': 'COPENHAGEN_PLANK'}]})
        assert result['ok'] is False
        assert result['steps'] == []
        assert any('sets is required' in e['message'] for e in result['errors'])
        assert 'preview_workout()' in result['hint']

    def test_ride_with_repeats_and_power_pct(self, data_env):
        result = preview_workout({
            'type': 'wattbike', 'duration_mins': 50, 'intensity': 'threshold', 'description': '4x4',
            'structure': [
                {'phase': 'warmup', 'duration_mins': 10, 'intensity': 'easy'},
                {'phase': 'repeat', 'iterations': 4, 'steps': [
                    {'phase': 'interval', 'duration_mins': 4, 'power_pct': [100, 105], 'notes': 'Hold it'},
                    {'phase': 'recovery', 'duration_mins': 0.5, 'power_watts': [100, 120]}]},
                {'phase': 'cooldown', 'duration_mins': 'open', 'intensity': 'recovery'}]},
            include_payload=True)
        assert result['ok']
        assert result['name'] == '4x4 (Indoor)'
        assert result['steps'] == [
            '1. Warm-up 10:00 @ 120-162 W',
            '2. 4 x',
            '   2.1. Interval 4:00 @ 216-226 W — "Hold it"',
            '   2.2. Recovery 0:30 @ 100-120 W',
            '3. Cool-down lap button @ 0-118 W',
        ]
        assert result['estimated_mins'] == 28
        payload = result['garmin_payload']
        assert payload['workoutSegments'][0]['workoutSteps'][1]['numberOfIterations'] == 4

    def test_run_with_pace_and_distance(self, data_env):
        result = preview_workout({
            'type': 'running', 'duration_mins': 40, 'intensity': 'easy',
            'structure': [
                {'phase': 'warmup', 'duration_mins': 10},
                {'phase': 'interval', 'distance_m': 1000, 'pace_sec_per_km': [330, 315]},
                {'phase': 'interval', 'duration_mins': 3, 'intensity': 'vo2'}]})
        assert result['ok']
        assert result['steps'][0].startswith('1. Warm-up 10:00 @ pace')  # inherits the session's easy
        assert result['steps'][1] == '2. Interval 1 km @ pace 5:30/km to 5:15/km'
        assert result['steps'][2] == '3. Interval 3:00 @ pace 5:13/km to 4:40/km'  # vo2 pace zone, not easy

    def test_unknown_type_and_bad_input(self, data_env):
        assert 'no Garmin workout family' in preview_workout({'type': 'juggling', 'duration_mins': 10})['errors'][0]['message']
        assert 'error' in preview_workout(['not', 'a', 'dict'])


class TestSaveGate:
    def test_contract_violation_rejects_the_save(self, data_env):
        plan = {'days': {TOMORROW: {'planned': {
            'type': 'strength', 'duration_mins': 30, 'purpose': 'rehab',
            'exercises': [{'name': 'COPENHAGEN_PLANK', 'category': 'PLANK'}]}}}}

        result = update_weekly_plan(plan=plan)

        assert result['error'] == 'authoring_error'
        problem = result['problems'][0]
        assert problem['date'] == TOMORROW and problem['field'] == 'exercises[0]'
        assert 'sets is required' in problem['message']
        assert 'preview_workout' in result['hint']
        assert not (data_env / 'weekly_plan.json').exists()

    def test_warnings_ride_along_with_a_successful_save(self, data_env):
        plan = {'days': {TOMORROW: {'planned': {
            'type': 'strength', 'duration_mins': 30, 'purpose': 'rehab',
            'exercises': [{'name': 'MYSTERY_MOVE', 'category': 'SQUAT', 'sets': 3, 'reps': 10}]}}}}

        result = update_weekly_plan(plan=plan)

        assert result['status'] == 'success'
        assert result['authoring_warnings'][0]['date'] == TOMORROW
        assert 'not in the Garmin exercise catalogue' in result['authoring_warnings'][0]['message']
        assert 'authoring warning' in result['message']

    def test_plain_sessions_save_without_warnings(self, data_env):
        plan = {'days': {TOMORROW: {'planned': [
            {'type': 'cycling', 'duration_mins': 60, 'intensity': 'easy', 'purpose': 'base'},
            {'type': 'rest'}]}}}
        result = update_weekly_plan(plan=plan)
        assert result['status'] == 'success'
        assert result['authoring_warnings'] == []
