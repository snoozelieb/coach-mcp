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

    def test_prose_sets_without_exercises_is_an_error(self):
        """Regression: '2x20/side' in the description landed as a 15-minute timer."""
        (message,) = errors_of({"type": "strength", "duration_mins": 15,
                                "description": "Evening: banded ankle eversion 2x20/side"})
        assert "ONE timed block" in message and "exercises" in message

    def test_long_cue_warns_about_the_watch_limit(self):
        report = validate_session({"type": "strength", "warmup_mins": 5, "exercises": [
            {"name": "GOBLET_SQUAT", "sets": 3, "reps": 10, "notes": "x" * 80}]}, CATALOGUE)
        (message,) = [w["message"] for w in report.warnings if w["field"] == "exercises[0]"]
        assert "80 characters" in message

    def test_catalogue_category_mismatch_warns_that_garmin_strips_the_name(self):
        (message,) = warnings_of({"type": "strength", "exercises": [
            {"name": "PLANK", "category": "CORE", "sets": 3, "duration_secs": 30}]})
        assert "SUSPENSION" in message and "strip" in message

    def test_catalogue_corrections_apply_to_exact_or_misfiled_names_only(self):
        """Red-team S3: a substring correction used to override 31 valid cached
        categories silently (SIDE_LYING_LEG_RAISE: HIP_STABILITY -> LEG_RAISE)."""
        catalogue = {"SIDE_LYING_LEG_RAISE": {"category": "HIP_STABILITY"},
                     "SIDE_PLANK": {"category": "SUSPENSION"},
                     "LEG_RAISE": {"category": "LEG_RAISE"}}
        report = validate_session({"type": "strength", "warmup_mins": 5, "exercises": [
            {"name": "SIDE_LYING_LEG_RAISE", "sets": 2, "reps": 12},
            {"name": "SIDE_PLANK", "sets": 2, "duration_secs": 30},
            {"name": "LEG_RAISE", "sets": 2, "reps": 12}]}, catalogue)
        assert report.ok and report.warnings == []
        resolved = [authoring.resolve_exercise_category(ex, catalogue)[:2] for ex in report.exercises]
        assert resolved == [("HIP_STABILITY", "SIDE_LYING_LEG_RAISE"),  # cache trusted
                            ("PLANK", "SIDE_PLANK"),                    # SUSPENSION mis-filing corrected
                            ("LEG_RAISE", "LEG_RAISE")]

    def test_catalogue_correction_keeps_the_plank_name(self):
        """The cached catalogue lists PLANK under SUSPENSION; Garmin also has
        PLANK/PLANK. Stating PLANK keeps the name, no warning."""
        report = validate_session({"type": "strength", "exercises": [
            {"name": "PLANK", "category": "PLANK", "sets": 3, "duration_secs": 30}]}, CATALOGUE)
        assert report.ok and report.warnings == []

    def test_skip_warmup_is_retired(self):
        report = validate_session({"type": "strength", "skip_warmup": True, "warmup_mins": 5,
                                   "exercises": [{"name": "GOBLET_SQUAT", "sets": 3, "reps": 10}]}, CATALOGUE)
        (message,) = [w["message"] for w in report.warnings if w["field"] == "skip_warmup"]
        assert "warmup_mins" in message

    def test_loaded_compound_without_a_warm_up_warns(self):
        """The implicit 5-minute block is gone; the coach is told when nothing replaced it."""
        report = validate_session({"type": "strength", "exercises": [
            {"name": "BARBELL_BACK_SQUAT", "category": "SQUAT", "sets": 5, "reps": 5, "weight_kg": 80}]})
        (message,) = [w["message"] for w in report.warnings if w["field"] == "warmup_mins"]
        assert "SQUAT" in message and "warmup_mins" in message
        quiet = validate_session({"type": "strength", "warmup_mins": 8, "exercises": [
            {"name": "BARBELL_BACK_SQUAT", "category": "SQUAT", "sets": 5, "reps": 5, "weight_kg": 80}]})
        assert not [w for w in quiet.warnings if w["field"] == "warmup_mins"]
        led_by_warmup = validate_session({"type": "strength", "exercises": [
            {"name": "BIKE_WARM_UP", "category": "WARM_UP", "sets": 1, "duration_secs": 300},
            {"name": "BARBELL_BACK_SQUAT", "category": "SQUAT", "sets": 5, "reps": 5, "weight_kg": 80}]})
        assert not [w for w in led_by_warmup.warnings if w["field"] == "warmup_mins"]

    def test_no_load_while_the_baseline_records_one_warns(self):
        """The old builder silently attached the baseline weight; now the coach is told."""
        baseline = {"squat": {"preferred_variation": "GOBLET_SQUAT",
                              "current": {"weight_kg": 24, "reps": 10, "sets": 3}},
                    "row": {"current_weight_kg": 42.5, "preferred_variation": "INDOOR_ROW"}}
        report = validate_session({"type": "strength", "warmup_mins": 5, "exercises": [
            {"name": "GOBLET_SQUAT", "sets": 3, "reps": 10},
            {"name": "ONE_ARM_BENT_OVER_ROW", "sets": 3, "reps": 10},
            {"name": "PLANK", "category": "PLANK", "sets": 3, "duration_secs": 30}]},
            CATALOGUE, strength_baseline=baseline)
        messages = {w["field"]: w["message"] for w in report.warnings}
        assert "24 kg for squat (GOBLET_SQUAT)" in messages["exercises[0]"]
        assert "42.5 kg for row" in messages["exercises[1]"]
        assert "exercises[2]" not in messages
        assert "weightValue" not in str(report.exercises)  # a warning, never a value

    def test_warmup_and_cooldown_mins_must_be_numbers_on_runs_too(self):
        (message,) = errors_of({"type": "running", "duration_mins": 45, "intensity": "easy", "cooldown_mins": "x"})
        assert "cooldown_mins must be a number" in message
        assert validate_session({"type": "running", "duration_mins": 45, "intensity": "easy",
                                 "warmup_mins": 10, "cooldown_mins": 5}).ok

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

    def test_interval_prose_without_structure_is_caught_on_runs_and_rides(self):
        """Regression: '5 x 3 min at vo2' in a description rendered as one easy block,
        silently. A numeric rep scheme is now an error (as for strength); interval
        words alone are a warning; a negated mention is neither."""
        for session in (
            {"type": "running", "duration_mins": 45, "intensity": "easy",
             "description": "5 x 3 min at vo2 with 2 min jog"},
            {"type": "wattbike", "duration_mins": 60, "intensity": "threshold", "description": "4x4 threshold"},
        ):
            (message,) = [e["message"] for e in validate_session(session).errors if e["field"] == "structure"]
            assert "no 'structure'" in message and "ONE block" in message, session
        (message,) = [w["message"] for w in validate_session(
            {"type": "running", "duration_mins": 40, "intensity": "easy", "notes": "6 strides at the end"}
        ).warnings if w["field"] == "structure"]
        assert "no 'structure'" in message and "ONE block" in message
        assert warnings_of({"type": "running", "duration_mins": 45, "intensity": "easy",
                            "description": "Easy run, chat pace"}) == []
        assert warnings_of({"type": "running", "duration_mins": 45, "intensity": "easy",
                            "description": "Easy run. No intervals today."}) == []

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

    def test_plain_run_without_intensity_warns(self):
        """Red-team S5: the old renderer defaulted to easy; now the main block is
        untargeted and the coach is told."""
        (message,) = [w["message"] for w in validate_session({"type": "running", "duration_mins": 40}).warnings
                      if w["field"] == "intensity"]
        assert "no intensity stated" in message and "discretion" in message
        assert not [w for w in validate_session({"type": "running", "duration_mins": 40, "structure": [
            {"phase": "interval", "duration_mins": 40, "intensity": "easy"}]}).warnings if w["field"] == "intensity"]

    @pytest.mark.parametrize("phase, needle", [
        ({"phase": "interval", "duration_mins": 5, "pace": [330, 315]}, "pace values must be 0.5-10 m/s"),
        ({"phase": "interval", "duration_mins": 5, "pace_sec_per_km": [3.0, 3.2]}, "pace_sec_per_km values must be 120-1200"),
        ({"phase": "interval", "duration_mins": 5, "power_pct": 1.0}, "power_pct values must be 20-200"),
        ({"phase": "interval", "duration_mins": 5, "hr_target": [15, 300]}, "hr_target values must be 30-230"),
        ({"phase": "interval", "distance_m": 1.0}, "distance_m must be at least 20 m"),
        ({"phase": "interval", "duration_mins": 5, "power_watts": 220}, "power_watts must be a [low, high] pair"),
        ({"phase": "interval", "duration_mins": 5, "steps": []}, "unknown key 'steps'"),
    ])
    def test_unit_mistakes_are_teaching_errors(self, phase, needle):
        """Red-team S2/M2: seconds under the m/s key, a fraction for a percentage,
        a scalar for a pair — each names the unit or the shape."""
        (message,) = errors_of({"type": "running", "structure": [phase]})
        assert needle in message, message

    def test_intensity_none_is_an_explicit_no_target(self):
        report = validate_session({"type": "wattbike", "intensity": "threshold", "structure": [
            {"phase": "interval", "duration_mins": 5, "intensity": "none", "notes": "technique, no target"}]})
        assert report.ok and report.warnings == []
        assert report.structure[0].intensity == "none"

    def test_ftp_test_lane_is_checked(self):
        """Red-team S8/C2: a structure on an ftp_test is ignored by the renderer —
        say so; a typo in the protocol names the allowed keys."""
        (message,) = errors_of({"type": "ftp_test", "duration_mins": 60,
                                "structure": [{"phase": "interval", "duration_mins": 20}]})
        assert "ignores 'structure'" in message
        messages = errors_of({"type": "ftp_test", "duration_mins": 60,
                              "protocol": [{"phase": "warmup", "duration_min": 15}]})
        assert any("duration_mins is required" in m for m in messages)
        assert any("unknown key 'duration_min'" in m and "cadence_min" in m for m in messages)
        assert validate_session({"type": "ftp_test", "duration_mins": 60}).ok

    def test_swimming_and_rehab_numbers_must_be_numbers(self):
        (message,) = errors_of({"type": "swim", "duration_mins": 30, "pool_length_m": "25m",
                                "structure": [{"phase": "main", "distance_m": 400}]})
        assert "pool_length_m must be a number" in message
        (message,) = errors_of({"type": "rehab", "duration_mins": 15,
                                "exercises": [{"name": "Band walk", "sets": 2, "reps": "10-12"}]})
        assert "reps must be a whole number" in message
        report = validate_session({"type": "rehab", "duration_mins": 15,
                                   "exercises": [{"name": "Band walk", "set": 2, "rep": 10}]})
        assert report.ok and "unknown key(s) 'set', 'rep'" in report.warnings[0]["message"]

    def test_duration_mins_must_be_numeric(self):
        (message,) = errors_of({"type": "running", "duration_mins": "forty", "intensity": "easy"})
        assert "duration_mins must be a number" in message


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
                 'notes': 'No jump. Push off the left foot'},
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
            '   3.1. SIDE_LUNGE_JUMP_OFF 8 reps — "each side: No jump. Push off the left foot"',
            '   3.2. Rest lap button',
            '4. 3 x',
            '   4.1. ONE_ARM_BENT_OVER_ROW 10 reps',   # no weight invented
            '   4.2. Rest 1:00',
            '5. 2 x',
            '   5.1. [Banded Exercises] 20 reps — "Banded Ankle Eversion each side"',
            '   5.2. Rest lap button',
        ]
        warnings = {w['field']: w['message'] for w in result['warnings']}
        assert 'custom catalogue entry' in warnings['exercises[2]']
        assert 'exercises[0]' not in warnings and 'exercises[1]' not in warnings
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

    def test_library_cue_on_the_watch_is_flagged(self, data_env):
        (data_env / 'exercise_library.json').write_text(json.dumps({
            'goblet squat': {'garmin_note': 'Elbows inside knees, chest tall'}}), encoding='utf-8')
        result = preview_workout({'type': 'strength', 'duration_mins': 20, 'warmup_mins': 3,
                                  'exercises': [{'name': 'GOBLET_SQUAT', 'sets': 3, 'reps': 10, 'weight_kg': 20}]})
        assert result['steps'][2:] == ['3. 3 x', '   3.1. GOBLET_SQUAT 10 reps @ 20 kg — "Elbows inside knees, chest tall"',
                                       '   3.2. Rest lap button']
        (message,) = [w['message'] for w in result['warnings'] if w['field'] == 'exercises[0]']
        assert 'exercise-library cue' in message and 'Elbows inside knees' in message

    def test_estimate_far_from_the_planned_duration_warns(self, data_env):
        result = preview_workout({'type': 'strength', 'duration_mins': 60, 'warmup_mins': 5,
                                  'exercises': [{'name': 'GOBLET_SQUAT', 'sets': 2, 'reps': 8, 'weight_kg': 20,
                                                 'rest_secs': 30}]})
        (message,) = [w['message'] for w in result['warnings'] if w['field'] == 'duration_mins']
        assert message.startswith('planned 60 min but the exercises estimate to about')

    def test_discretion_day_is_untargeted_and_carries_its_constraints(self, data_env):
        ride = preview_workout({'type': 'cycling', 'duration_mins': 90, 'intensity': 'discretion',
                                'constraints': ['Z2 only', 'no climbs'], 'description': 'Your ride'})
        assert ride['steps'] == ['1. Interval 1:30:00 — "Your call: Z2 only; no climbs"']
        run = preview_workout({'type': 'running', 'duration_mins': 40, 'intensity': 'discretion',
                               'constraints': ['flat', 'stop if the ankle talks']})
        assert run['steps'] == ['1. Warm-up 4:00', '2. Interval 32:00 — "Your call: flat; stop if the ankle talks"',
                                '3. Cool-down 4:00']
        assert ride['warnings'] == [] and run['warnings'] == []

    def test_plain_run_honours_explicit_warmup_and_cooldown(self, data_env):
        result = preview_workout({'type': 'running', 'duration_mins': 50, 'intensity': 'easy',
                                  'warmup_mins': 12, 'cooldown_mins': 8})
        assert result['steps'][0].startswith('1. Warm-up 12:00 @ pace')
        assert result['steps'][1].startswith('2. Interval 30:00 @ pace')
        assert result['steps'][2].startswith('3. Cool-down 8:00 @ pace')

    def test_unknown_type_and_bad_input(self, data_env):
        assert 'no Garmin workout family' in preview_workout({'type': 'juggling', 'duration_mins': 10})['errors'][0]['message']
        assert 'error' in preview_workout(['not', 'a', 'dict'])


class TestSaveGate:
    def test_nested_legacy_day_is_gated_the_way_the_push_expands_it(self, data_env):
        """Red-team S4: the parent's exercises are injected into the strength
        sub-session only at push; the save must see the same expansion."""
        plan = {'days': {TOMORROW: {'planned': {
            'type': 'double_session', 'purpose': 'container', 'duration_mins': 60,
            'exercises': [{'name': 'GOBLET_SQUAT', 'category': 'SQUAT'}],  # no sets
            'sessions': [{'type': 'strength', 'duration_mins': 30, 'purpose': 'gym', 'focus': 'legs'},
                         {'type': 'yoga', 'duration_mins': 20, 'purpose': 'hips'}]}}}}

        result = update_weekly_plan(plan=plan)

        assert result['error'] == 'authoring_error'
        assert any('sets is required' in p['message'] for p in result['problems'])

    def test_power_pct_without_ftp_is_rejected_at_save_not_at_push(self, data_env):
        athlete = json.loads((data_env / 'athlete.json').read_text(encoding='utf-8'))
        del athlete['personal']['ftp']
        (data_env / 'athlete.json').write_text(json.dumps(athlete), encoding='utf-8')
        plan = {'days': {TOMORROW: {'planned': {
            'type': 'wattbike', 'duration_mins': 30, 'intensity': 'threshold', 'purpose': 'x',
            'structure': [{'phase': 'interval', 'duration_mins': 10, 'power_pct': [95, 100]}]}}}}
        result = update_weekly_plan(plan=plan)
        assert result['error'] == 'authoring_error'
        assert 'FTP' in result['problems'][0]['message']

    def test_restricted_movement_in_an_exercise_trips_the_injury_gate(self, data_env):
        athlete = json.loads((data_env / 'athlete.json').read_text(encoding='utf-8'))
        athlete['injury_history'] = [{'type': 'shoulder impingement', 'status': 'active',
                                      'date': (TODAY - timedelta(days=5)).isoformat(),
                                      'restricted_activities': ['overhead pressing', 'jumping']}]
        (data_env / 'athlete.json').write_text(json.dumps(athlete), encoding='utf-8')
        plan = {'days': {TOMORROW: {'planned': {
            'type': 'strength', 'duration_mins': 30, 'purpose': 'legs', 'warmup_mins': 5,
            'exercises': [{'name': 'GOBLET_SQUAT', 'sets': 3, 'reps': 10, 'weight_kg': 20},
                          {'name': 'SIDE_LUNGE_JUMP_OFF', 'sets': 2, 'reps': 8},
                          {'rounds': 2, 'exercises': [{'name': 'OVERHEAD_PRESS', 'category': 'SHOULDER_PRESS', 'reps': 8}]}]}}}}

        result = update_weekly_plan(plan=plan)

        assert result['error'] == 'injury_gate'
        hits = {v['exercise']: v['matched_restrictions'] for v in result['violations'] if 'exercise' in v}
        assert hits == {'SIDE_LUNGE_JUMP_OFF': ['jumping'], 'OVERHEAD_PRESS': ['overhead pressing']}
        assert not (data_env / 'weekly_plan.json').exists()

    def test_sport_restriction_words_do_not_trip_on_exercise_names(self, data_env):
        """A 'cycling' restriction gates ride sessions, not a core movement that
        carries the word in its Garmin key."""
        athlete = json.loads((data_env / 'athlete.json').read_text(encoding='utf-8'))
        athlete['injury_history'] = [{'type': 'saddle sore', 'status': 'active',
                                      'date': (TODAY - timedelta(days=2)).isoformat(),
                                      'restricted_activities': ['cycling']}]
        (data_env / 'athlete.json').write_text(json.dumps(athlete), encoding='utf-8')
        plan = {'days': {TOMORROW: {'planned': {
            'type': 'strength', 'duration_mins': 20, 'purpose': 'core',
            'exercises': [{'name': 'CYCLING_RUSSIAN_TWIST', 'category': 'CORE', 'sets': 3, 'reps': 20}]}}}}
        assert update_weekly_plan(plan=plan)['status'] == 'success'

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
