"""
Tests for workout_builder.py module.

Tests helper functions, workout type detection, and workout building
for cycling, running, strength, yoga, swimming, and padel sessions.
"""
import pytest
from unittest.mock import patch

from garminconnect.workout import CyclingWorkout, RunningWorkout, ExecutableStep

from workout_builder import (
    is_indoor_cycling,
    is_simple_outdoor_ride,
    get_workout_type_name,
    build_workout,
    build_cycling_workout,
    build_running_workout,
    build_strength_workout,
    build_yoga_workout,
    build_pilates_workout,
    build_swimming_workout,
    build_padel_workout,
    build_ftp_test_workout,
    build_structured_indoor_workout,
    get_hr_target_for_intensity,
    get_power_target_for_intensity,
    get_pace_target_for_intensity,
    STEP_WARMUP,
    STEP_COOLDOWN,
    STEP_INTERVAL,
    STEP_REST,
    STEP_REPEAT,
    STEP_RECOVERY,
    TARGET_HR,
    TARGET_POWER,
    TARGET_PACE,
    TARGET_NONE,
    TARGET_CADENCE,
    END_TIME,
    END_REPS,
    END_LAP_BUTTON,
    CYCLING_SPORT,
    RUNNING_SPORT,
    STRENGTH_SPORT,
    YOGA_SPORT,
    PILATES_SPORT,
    SWIMMING_SPORT,
    PADEL_SPORT,
    DEFAULT_HR_ZONES,
    GARMIN_CATEGORY_MAP,
    VALID_WORKOUT_CATEGORIES,
)


# ─── is_indoor_cycling ─────────────────────────────────────────────

class TestIsIndoorCycling:
    def test_wattbike_type_is_indoor(self):
        assert is_indoor_cycling({"type": "wattbike"}) is True

    def test_trainer_type_is_indoor(self):
        assert is_indoor_cycling({"type": "trainer"}) is True

    def test_indoor_cycling_type_is_indoor(self):
        assert is_indoor_cycling({"type": "indoor_cycling"}) is True

    def test_ftp_test_type_is_indoor(self):
        assert is_indoor_cycling({"type": "ftp_test"}) is True

    def test_type_containing_indoor_is_indoor(self):
        assert is_indoor_cycling({"type": "some_indoor_session"}) is True

    def test_type_containing_wattbike_is_indoor(self):
        assert is_indoor_cycling({"type": "wattbike_technique"}) is True

    def test_type_containing_trainer_is_indoor(self):
        assert is_indoor_cycling({"type": "turbo_trainer_session"}) is True

    def test_explicit_indoor_flag_is_indoor(self):
        assert is_indoor_cycling({"type": "cycling", "indoor": True}) is True

    def test_outdoor_ride_not_indoor(self):
        assert is_indoor_cycling({"type": "long_ride"}) is False

    def test_easy_ride_not_indoor(self):
        assert is_indoor_cycling({"type": "easy_ride"}) is False

    def test_mtb_not_indoor(self):
        assert is_indoor_cycling({"type": "mtb"}) is False

    def test_road_ride_not_indoor(self):
        assert is_indoor_cycling({"type": "road_ride"}) is False

    def test_cycling_without_indoor_flag(self):
        assert is_indoor_cycling({"type": "cycling"}) is False

    def test_indoor_flag_false_not_indoor(self):
        assert is_indoor_cycling({"type": "cycling", "indoor": False}) is False

    def test_empty_type_not_indoor(self):
        assert is_indoor_cycling({"type": ""}) is False

    def test_missing_type_not_indoor(self):
        assert is_indoor_cycling({}) is False

    def test_case_insensitive(self):
        assert is_indoor_cycling({"type": "WATTBIKE"}) is True
        assert is_indoor_cycling({"type": "Indoor_Cycling"}) is True


# ─── is_simple_outdoor_ride ─────────────────────────────────────────

class TestIsSimpleOutdoorRide:
    def test_simple_outdoor_ride_skips_warmup(self):
        session = {"type": "long_ride", "duration_mins": 120, "intensity": "easy"}
        assert is_simple_outdoor_ride(session) is True

    def test_easy_ride_skips_warmup(self):
        session = {"type": "easy_ride", "duration_mins": 60, "intensity": "easy"}
        assert is_simple_outdoor_ride(session) is True

    def test_mtb_ride_skips_warmup(self):
        session = {"type": "mtb", "duration_mins": 90}
        assert is_simple_outdoor_ride(session) is True

    def test_indoor_ride_keeps_warmup(self):
        session = {"type": "wattbike", "duration_mins": 60, "intensity": "easy"}
        assert is_simple_outdoor_ride(session) is False

    def test_indoor_cycling_keeps_warmup(self):
        session = {"type": "indoor_cycling", "duration_mins": 60}
        assert is_simple_outdoor_ride(session) is False

    def test_structured_outdoor_ride_keeps_warmup(self):
        session = {
            "type": "road_ride",
            "duration_mins": 90,
            "structure": [
                {"phase": "warmup", "duration_mins": 10},
                {"phase": "interval", "duration_mins": 5},
            ],
        }
        assert is_simple_outdoor_ride(session) is False

    def test_outdoor_ride_with_empty_structure_skips_warmup(self):
        session = {"type": "easy_ride", "duration_mins": 60, "structure": []}
        # Empty list is falsy so structure check passes
        assert is_simple_outdoor_ride(session) is True


# ─── get_workout_type_name ──────────────────────────────────────────

class TestGetWorkoutTypeName:
    def test_cycling_types(self):
        for t in ["long_ride", "easy_ride", "cycling", "mtb", "road_ride", "indoor_cycling"]:
            assert get_workout_type_name({"type": t}) == "cycling"

    def test_ride_substring_match(self):
        assert get_workout_type_name({"type": "tempo_ride"}) == "cycling"

    def test_running_types(self):
        for t in ["run", "long_run", "easy_run", "running", "trail_run", "interval_run"]:
            assert get_workout_type_name({"type": t}) == "running"

    def test_run_substring_match(self):
        assert get_workout_type_name({"type": "morning_run"}) == "running"

    def test_swimming_types(self):
        for t in ["swim", "swimming", "pool"]:
            assert get_workout_type_name({"type": t}) == "swimming"

    def test_yoga_types(self):
        for t in ["yoga", "mobility", "stretching"]:
            assert get_workout_type_name({"type": t}) == "yoga"

    def test_pilates_types(self):
        for t in ["pilates", "rehab", "rehabilitation"]:
            assert get_workout_type_name({"type": t}) == "pilates"

    def test_strength_types(self):
        for t in ["strength", "strength_training", "gym", "weights"]:
            assert get_workout_type_name({"type": t}) == "strength"

    def test_padel_types(self):
        for t in ["padel", "paddelball", "paddle"]:
            assert get_workout_type_name({"type": t}) == "padel"

    def test_skip_types(self):
        for t in ["rest", "rest_or_easy"]:
            assert get_workout_type_name({"type": t}) == "skipped"

    def test_unknown_type(self):
        assert get_workout_type_name({"type": "basketball"}) == "unknown"

    def test_empty_type(self):
        assert get_workout_type_name({"type": ""}) == "unknown"

    def test_missing_type(self):
        assert get_workout_type_name({}) == "unknown"


# ─── build_workout dispatch ─────────────────────────────────────────

class TestBuildWorkoutDispatch:
    """Test that build_workout dispatches to the correct builder."""

    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_returns_cycling_workout_for_ride(self, mock_hr):
        session = {"type": "easy_ride", "duration_mins": 60, "intensity": "easy"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, CyclingWorkout)

    @patch("workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_returns_running_workout_for_run(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, RunningWorkout)

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_returns_dict_for_strength(self, mock_baseline, mock_library):
        session = {"type": "strength", "duration_mins": 45}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, dict)
        assert result["sportType"] == STRENGTH_SPORT

    def test_returns_dict_for_yoga(self):
        session = {"type": "yoga", "duration_mins": 30}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, dict)
        assert result["sportType"] == YOGA_SPORT

    def test_returns_dict_for_pilates(self):
        session = {"type": "pilates", "duration_mins": 20}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, dict)
        assert result["sportType"] == PILATES_SPORT

    def test_returns_dict_for_swimming(self):
        session = {"type": "swim", "duration_mins": 30}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, dict)
        assert result["sportType"] == SWIMMING_SPORT

    def test_returns_dict_for_padel(self):
        session = {"type": "padel", "duration_mins": 90}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, dict)
        assert result["sportType"] == PADEL_SPORT

    def test_returns_none_for_rest(self):
        session = {"type": "rest", "duration_mins": 0}
        result = build_workout(session, "2025-01-01")
        assert result is None

    def test_returns_none_for_rest_or_easy(self):
        session = {"type": "rest_or_easy", "duration_mins": 0}
        result = build_workout(session, "2025-01-01")
        assert result is None

    def test_returns_none_for_zero_duration(self):
        session = {"type": "easy_run", "duration_mins": 0}
        result = build_workout(session, "2025-01-01")
        assert result is None

    def test_returns_none_for_unknown_type(self):
        session = {"type": "basketball", "duration_mins": 60}
        result = build_workout(session, "2025-01-01")
        assert result is None

    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_ride_substring_dispatches_to_cycling(self, mock_hr):
        session = {"type": "tempo_ride", "duration_mins": 60, "intensity": "tempo"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, CyclingWorkout)

    @patch("workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_run_substring_dispatches_to_running(self, mock_hr, mock_pace):
        session = {"type": "morning_run", "duration_mins": 30, "intensity": "easy"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, RunningWorkout)

    @patch("workout_builder.get_power_target_for_intensity", return_value=None)
    @patch("workout_builder.get_hr_target_for_intensity", return_value=None)
    def test_ftp_test_dispatches_to_ftp_builder(self, mock_hr, mock_power):
        session = {"type": "ftp_test", "duration_mins": 50, "description": "FTP Test"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, CyclingWorkout)
        assert "FTP" in result.workoutName or "ftp" in result.workoutName.lower()


# ─── build_cycling_workout ──────────────────────────────────────────

class TestBuildCyclingWorkout:
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_outdoor_ride_has_hr_target(self, mock_hr):
        session = {"type": "easy_ride", "duration_mins": 60, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")

        assert isinstance(result, CyclingWorkout)
        assert "(Outdoor)" in result.workoutName
        assert result.estimatedDurationInSecs == 3600

    @patch("workout_builder.get_power_target_for_intensity", return_value=(130, 160))
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_indoor_ride_has_power_target(self, mock_hr, mock_power):
        session = {"type": "wattbike", "duration_mins": 60, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")

        assert isinstance(result, CyclingWorkout)
        assert "(Indoor)" in result.workoutName
        # Indoor rides have warmup, so check steps
        steps = result.workoutSegments[0].workoutSteps
        assert len(steps) == 3  # warmup + main + cooldown
        # Main step should have power target
        main_step = steps[1]
        assert main_step.targetType == TARGET_POWER
        assert main_step.targetValueOne == 130
        assert main_step.targetValueTwo == 160

    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_simple_outdoor_ride_has_no_warmup(self, mock_hr):
        """Simple outdoor rides skip warmup/cooldown sections."""
        session = {"type": "easy_ride", "duration_mins": 90, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        # Simple outdoor ride: single interval step, no warmup/cooldown
        assert len(steps) == 1
        assert steps[0].stepType == STEP_INTERVAL

    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_outdoor_ride_single_step_full_duration(self, mock_hr):
        session = {"type": "long_ride", "duration_mins": 120, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        # Single interval step for the full duration
        assert len(steps) == 1
        assert steps[0].endConditionValue == 120 * 60

    @patch("workout_builder.get_power_target_for_intensity", return_value=(150, 180))
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_indoor_ride_has_warmup_main_cooldown(self, mock_hr, mock_power):
        session = {"type": "indoor_cycling", "duration_mins": 60, "intensity": "tempo"}
        result = build_cycling_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        assert len(steps) == 3
        assert steps[0].stepType == STEP_WARMUP
        assert steps[1].stepType == STEP_INTERVAL
        assert steps[2].stepType == STEP_COOLDOWN

    @patch("workout_builder.get_hr_target_for_intensity", return_value=None)
    def test_no_zones_uses_no_target(self, mock_hr):
        session = {"type": "easy_ride", "duration_mins": 60, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        for step in steps:
            assert step.targetType == TARGET_NONE

    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_description_already_tagged_not_double_tagged(self, mock_hr):
        session = {
            "type": "easy_ride",
            "duration_mins": 60,
            "intensity": "easy",
            "description": "Easy Spin (Outdoor)",
        }
        result = build_cycling_workout(session, "2025-01-01")
        # Should not have double tags like "(Outdoor) (Outdoor)"
        assert result.workoutName.count("(Outdoor)") == 1

    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_cycling_sport_type(self, mock_hr):
        session = {"type": "easy_ride", "duration_mins": 60, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")
        assert result.workoutSegments[0].sportType == CYCLING_SPORT

    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_outdoor_ride_has_description_from_notes(self, mock_hr):
        """Outdoor cycling workout should include notes as description."""
        session = {
            "type": "easy_ride",
            "duration_mins": 90,
            "intensity": "easy",
            "notes": "Z2 aerobic ride. HR 124-145 ONLY",
        }
        result = build_cycling_workout(session, "2025-01-01")
        assert result.description == "Z2 aerobic ride. HR 124-145 ONLY"

    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_outdoor_ride_falls_back_to_purpose(self, mock_hr):
        """Falls back to purpose when no notes."""
        session = {
            "type": "easy_ride",
            "duration_mins": 90,
            "intensity": "easy",
            "purpose": "Build aerobic base",
        }
        result = build_cycling_workout(session, "2025-01-01")
        assert result.description == "Build aerobic base"

    @patch("workout_builder.get_hr_target_for_intensity", return_value=(124, 145))
    def test_outdoor_ride_step_has_hr_description(self, mock_hr):
        """Outdoor ride main step should show HR range in description."""
        session = {"type": "easy_ride", "duration_mins": 90, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")
        main_step = result.workoutSegments[0].workoutSteps[0]
        assert main_step.description == "HR 124-145 bpm"

    @patch("workout_builder.get_power_target_for_intensity", return_value=(130, 160))
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_indoor_ride_step_has_no_hr_description(self, mock_hr, mock_power):
        """Indoor ride steps should NOT have HR description."""
        session = {"type": "wattbike", "duration_mins": 60, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")
        main_step = result.workoutSegments[0].workoutSteps[1]
        assert not hasattr(main_step, "description") or main_step.description is None


# ─── build_running_workout ──────────────────────────────────────────

class TestBuildRunningWorkout:
    @patch("workout_builder.get_pace_target_for_intensity", return_value=(2.5, 3.0))
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_uses_pace_when_available(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")

        assert isinstance(result, RunningWorkout)
        steps = result.workoutSegments[0].workoutSteps
        # Main step should use pace target
        main_step = steps[1]
        assert main_step.targetType == TARGET_PACE

    @patch("workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_falls_back_to_hr_when_no_pace(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        main_step = steps[1]
        assert main_step.targetType == TARGET_HR
        assert main_step.targetValueOne == 120
        assert main_step.targetValueTwo == 140

    @patch("workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("workout_builder.get_hr_target_for_intensity", return_value=None)
    def test_no_targets_when_no_zones(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        for step in steps:
            assert step.targetType in (TARGET_NONE, TARGET_HR)  # warmup/cooldown may differ
            # Specifically the main step
        assert steps[1].targetType == TARGET_NONE

    @patch("workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_has_warmup_main_cooldown(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 60, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        assert len(steps) == 3
        assert steps[0].stepType == STEP_WARMUP
        assert steps[1].stepType == STEP_INTERVAL
        assert steps[2].stepType == STEP_COOLDOWN

    @patch("workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_estimated_duration_matches_session(self, mock_hr, mock_pace):
        session = {"type": "long_run", "duration_mins": 90, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")
        assert result.estimatedDurationInSecs == 90 * 60

    @patch("workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_running_sport_type(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")
        assert result.workoutSegments[0].sportType == RUNNING_SPORT

    @patch("workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_workout_name_from_description(self, mock_hr, mock_pace):
        session = {
            "type": "easy_run",
            "duration_mins": 45,
            "intensity": "easy",
            "description": "Morning easy run",
        }
        result = build_running_workout(session, "2025-01-01")
        assert result.workoutName == "Morning easy run"


# ─── build_strength_workout ─────────────────────────────────────────

class TestBuildStrengthWorkout:
    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_simple_timed_workout_no_exercises(self, mock_baseline, mock_library):
        session = {"type": "strength", "duration_mins": 45}
        result = build_strength_workout(session, "2025-01-01")

        assert result["sportType"] == STRENGTH_SPORT
        assert result["estimatedDurationInSecs"] == 45 * 60
        steps = result["workoutSegments"][0]["workoutSteps"]
        assert len(steps) == 1
        assert steps[0]["stepType"] == STEP_WARMUP

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_workout_with_exercises_has_warmup(self, mock_baseline, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        # First step: warmup
        assert steps[0]["stepType"] == STEP_WARMUP
        assert steps[0]["endConditionValue"] == 300.0  # 5 min warmup

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_workout_has_rest_after_warmup(self, mock_baseline, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        # Second step: rest (lap button after warmup)
        assert steps[1]["stepType"] == STEP_REST
        assert steps[1]["endCondition"] == END_LAP_BUTTON

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_exercise_creates_repeat_group(self, mock_baseline, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 4, "reps": 8},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        # Steps: warmup, rest, repeat_group
        assert len(steps) == 3
        repeat_group = steps[2]
        assert repeat_group["type"] == "RepeatGroupDTO"
        assert repeat_group["numberOfIterations"] == 4

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_repeat_group_contains_exercise_and_rest(self, mock_baseline, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BENCH_PRESS", "category": "BENCH_PRESS", "sets": 3, "reps": 12},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        repeat_group = result["workoutSegments"][0]["workoutSteps"][2]
        inner_steps = repeat_group["workoutSteps"]

        assert len(inner_steps) == 2
        # Exercise step
        assert inner_steps[0]["stepType"] == STEP_INTERVAL
        assert inner_steps[0]["endCondition"] == END_REPS
        assert inner_steps[0]["endConditionValue"] == 12.0
        assert inner_steps[0]["exerciseName"] == "BENCH_PRESS"
        assert inner_steps[0]["category"] == "BENCH_PRESS"
        # Rest step
        assert inner_steps[1]["stepType"] == STEP_REST
        assert inner_steps[1]["endCondition"] == END_LAP_BUTTON

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_multiple_exercises_create_multiple_groups(self, mock_baseline, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 60,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10},
                {"name": "BENCH_PRESS", "category": "BENCH_PRESS", "sets": 3, "reps": 12},
                {"name": "DEADLIFT", "category": "DEADLIFT", "sets": 3, "reps": 8},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        # warmup + rest + 3 repeat groups
        assert len(steps) == 5
        assert result["exercise_count"] == 3

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_explicit_weight_included(self, mock_baseline, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10, "weight_kg": 60},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        repeat_group = result["workoutSegments"][0]["workoutSteps"][2]
        exercise_step = repeat_group["workoutSteps"][0]

        assert exercise_step["weightValue"] == 60.0
        assert exercise_step["weightUnit"]["unitKey"] == "kilogram"

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={
        "bench_press": {"current": {"weight_kg": 40}}
    })
    def test_baseline_weight_used_when_no_explicit_weight(self, mock_baseline, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BENCH_PRESS", "category": "BENCH_PRESS", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        repeat_group = result["workoutSegments"][0]["workoutSteps"][2]
        exercise_step = repeat_group["workoutSteps"][0]

        assert exercise_step["weightValue"] == 40.0

    @patch("workout_builder.load_exercise_db", return_value={
        "BARBELL_SQUAT": {"category": "SQUAT"}
    })
    @patch("workout_builder.load_exercise_library", return_value={
        "barbell squat": {"garmin_note": "Drive through heels, chest up"}
    })
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_exercise_library_note_included(self, mock_baseline, mock_library, mock_db):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        repeat_group = result["workoutSegments"][0]["workoutSteps"][2]
        exercise_step = repeat_group["workoutSteps"][0]

        assert exercise_step["description"] == "Drive through heels, chest up"

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_garmin_category_mapping(self, mock_baseline, mock_library):
        """Non-standard categories should be mapped to valid Garmin categories."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "HIP_THRUST", "category": "GLUTE", "sets": 3, "reps": 12},
                {"name": "CALF_RAISE", "category": "CALF", "sets": 3, "reps": 15},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        # GLUTE -> SQUAT
        ex1 = steps[2]["workoutSteps"][0]
        assert ex1["category"] == "SQUAT"

        # CALF -> SQUAT
        ex2 = steps[3]["workoutSteps"][0]
        assert ex2["category"] == "SQUAT"

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_remapped_category_clears_exercise_name(self, mock_baseline, mock_library):
        """When category is remapped, exerciseName must be cleared (doesn't exist in new category)."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "FACE_PULL", "category": "SUSPENSION", "sets": 3, "reps": 15},
                {"name": "LYING_LEG_CURL", "category": "HAMSTRING_CURL", "sets": 3, "reps": 12},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        # SUSPENSION -> ROW, but exerciseName cleared (FACE_PULL not under ROW)
        face_pull = steps[2]["workoutSteps"][0]
        assert face_pull["category"] == "ROW"
        assert face_pull["exerciseName"] == ""
        assert "Face Pull" in face_pull["description"]

        # HAMSTRING_CURL -> DEADLIFT, exerciseName cleared
        leg_curl = steps[3]["workoutSteps"][0]
        assert leg_curl["category"] == "DEADLIFT"
        assert leg_curl["exerciseName"] == ""
        assert "Lying Leg Curl" in leg_curl["description"]

    @patch("workout_builder.load_exercise_db", return_value={
        "BARBELL_SQUAT": {"category": "SQUAT"}
    })
    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_valid_category_keeps_exercise_name(self, mock_baseline, mock_library, mock_db):
        """Valid categories keep exerciseName intact when DB confirms the pair."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        ex = result["workoutSegments"][0]["workoutSteps"][2]["workoutSteps"][0]
        assert ex["category"] == "SQUAT"
        assert ex["exerciseName"] == "BARBELL_SQUAT"

    @patch("workout_builder.load_exercise_db", return_value={
        "SQUAT": {"category": "SUSPENSION"}
    })
    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_db_mismatch_clears_exercise_name(self, mock_baseline, mock_library, mock_db):
        """When DB says exercise is under different category, exerciseName is cleared."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "SQUAT", "category": "SQUAT", "sets": 4, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        ex = result["workoutSegments"][0]["workoutSteps"][2]["workoutSteps"][0]
        assert ex["category"] == "SQUAT"
        assert ex["exerciseName"] == ""
        assert "Squat" in ex["description"]

    @patch("workout_builder.load_exercise_library", return_value={})
    @patch("workout_builder.load_strength_baseline", return_value={})
    def test_unknown_category_falls_back_to_other(self, mock_baseline, mock_library):
        """Completely unknown category (not in map or valid set) falls back to OTHER."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "WEIRD_EXERCISE", "category": "MADE_UP_CATEGORY", "sets": 2, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        ex = result["workoutSegments"][0]["workoutSteps"][2]["workoutSteps"][0]
        assert ex["category"] == "OTHER"
        assert ex["exerciseName"] == ""


# ─── build_yoga_workout ─────────────────────────────────────────────

class TestBuildYogaWorkout:
    def test_basic_yoga_workout(self):
        session = {"type": "yoga", "duration_mins": 30}
        result = build_yoga_workout(session, "2025-01-01")

        assert result["sportType"] == YOGA_SPORT
        assert result["estimatedDurationInSecs"] == 30 * 60
        steps = result["workoutSegments"][0]["workoutSteps"]
        assert len(steps) == 1
        assert steps[0]["stepType"] == STEP_INTERVAL
        assert steps[0]["endConditionValue"] == 30 * 60

    def test_mobility_uses_yoga_sport(self):
        session = {"type": "mobility", "duration_mins": 20}
        result = build_yoga_workout(session, "2025-01-01")
        assert result["sportType"] == YOGA_SPORT

    def test_workout_name_from_description(self):
        session = {"type": "yoga", "duration_mins": 45, "description": "Evening Yoga Flow"}
        result = build_yoga_workout(session, "2025-01-01")
        assert result["workoutName"] == "Evening Yoga Flow"


# ─── build_pilates_workout ──────────────────────────────────────────

class TestBuildPilatesWorkout:
    def test_basic_pilates_workout(self):
        session = {"type": "pilates", "duration_mins": 20}
        result = build_pilates_workout(session, "2025-01-01")

        assert result["sportType"] == PILATES_SPORT
        assert result["estimatedDurationInSecs"] == 20 * 60

    def test_rehab_type_gets_ankle_rehab_name(self):
        session = {"type": "rehab", "duration_mins": 15}
        result = build_pilates_workout(session, "2025-01-01")
        assert result["workoutName"] == "Ankle Rehab"

    def test_custom_description_used(self):
        session = {"type": "pilates", "duration_mins": 20, "description": "Core Stability"}
        result = build_pilates_workout(session, "2025-01-01")
        assert result["workoutName"] == "Core Stability"

    def test_rehab_notes_in_workout_description(self):
        """Rehab session notes should appear in workout description for Garmin."""
        session = {
            "type": "rehab",
            "duration_mins": 15,
            "notes": "ANKLE UNLOCK: Toe curls 3x15, Calf raises 3x12, Band dorsiflexion 3x10",
        }
        result = build_pilates_workout(session, "2025-01-01")
        assert result["description"] == session["notes"]

    def test_rehab_notes_in_step_description(self):
        """Step-level description should have truncated notes."""
        session = {
            "type": "rehab",
            "duration_mins": 15,
            "notes": "ANKLE UNLOCK: Toe curls 3x15, Calf raises 3x12, Band dorsiflexion 3x10",
        }
        result = build_pilates_workout(session, "2025-01-01")
        step = result["workoutSegments"][0]["workoutSteps"][0]
        assert "description" in step
        assert len(step["description"]) <= 50

    def test_no_notes_no_description(self):
        """Sessions without notes should have empty description."""
        session = {"type": "pilates", "duration_mins": 20}
        result = build_pilates_workout(session, "2025-01-01")
        assert result["description"] == ""
        step = result["workoutSegments"][0]["workoutSteps"][0]
        assert "description" not in step

    def test_long_notes_truncated_in_description(self):
        """Workout description truncated to 255 chars for Garmin API."""
        long_notes = "A" * 300
        session = {"type": "rehab", "duration_mins": 15, "notes": long_notes}
        result = build_pilates_workout(session, "2025-01-01")
        assert len(result["description"]) == 255

    def test_structured_rehab_creates_repeat_groups(self):
        """Exercises list creates RepeatGroupDTOs for each exercise."""
        session = {
            "type": "rehab",
            "duration_mins": 15,
            "description": "Ankle Rehab",
            "exercises": [
                {"name": "Toe Curls", "sets": 3, "reps": 15},
                {"name": "Calf Raises", "sets": 3, "duration_secs": 30},
                {"name": "Band Dorsiflexion", "sets": 3, "reps": 10},
            ],
        }
        result = build_pilates_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        assert len(steps) == 3
        for step in steps:
            assert step["type"] == "RepeatGroupDTO"
            assert step["stepType"] == STEP_REPEAT
            assert step["numberOfIterations"] == 3
            assert len(step["workoutSteps"]) == 2  # exercise + rest

    def test_rep_based_exercise_uses_end_reps(self):
        """Exercise with reps uses END_REPS end condition."""
        session = {
            "type": "rehab",
            "exercises": [{"name": "Toe Curls", "sets": 3, "reps": 15}],
        }
        result = build_pilates_workout(session, "2025-01-01")
        exercise_step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]

        assert exercise_step["endCondition"] == END_REPS
        assert exercise_step["endConditionValue"] == 15.0

    def test_timed_exercise_uses_end_time(self):
        """Exercise with duration_secs uses END_TIME end condition."""
        session = {
            "type": "rehab",
            "exercises": [{"name": "Calf Raises", "sets": 3, "duration_secs": 30}],
        }
        result = build_pilates_workout(session, "2025-01-01")
        exercise_step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]

        assert exercise_step["endCondition"] == END_TIME
        assert exercise_step["endConditionValue"] == 30.0

    def test_no_reps_no_duration_uses_lap_button(self):
        """Exercise without reps or duration_secs falls back to END_LAP_BUTTON."""
        session = {
            "type": "rehab",
            "exercises": [{"name": "Stretch", "sets": 2}],
        }
        result = build_pilates_workout(session, "2025-01-01")
        exercise_step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]

        assert exercise_step["endCondition"] == END_LAP_BUTTON
        assert "endConditionValue" not in exercise_step

    def test_exercise_notes_in_step_description(self):
        """Exercise notes (form cues) appear as step description on watch, prefixed with name."""
        session = {
            "type": "rehab",
            "exercises": [
                {"name": "Toe Curls", "sets": 3, "reps": 15, "notes": "Scrunch towel with toes"},
            ],
        }
        result = build_pilates_workout(session, "2025-01-01")
        exercise_step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]

        assert exercise_step["description"] == "Toe Curls: Scrunch towel with toes"

    def test_rehab_exercise_name_always_in_description(self):
        """Step description starts with readable exercise name even without notes."""
        session = {
            "type": "rehab",
            "exercises": [
                {"name": "Single_Leg_Balance", "sets": 3, "reps": 1},
            ],
        }
        result = build_pilates_workout(session, "2025-01-01")
        exercise_step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]

        assert exercise_step["description"] == "Single Leg Balance"

    def test_rehab_description_truncated_to_50(self):
        """Long exercise name + notes still truncated to 50 chars."""
        session = {
            "type": "rehab",
            "exercises": [
                {
                    "name": "Peroneal_Self_Massage",
                    "sets": 1,
                    "duration_secs": 180,
                    "notes": "3min. Thumb pressure along lateral lower leg from knee to ankle",
                },
            ],
        }
        result = build_pilates_workout(session, "2025-01-01")
        exercise_step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]

        assert len(exercise_step["description"]) <= 50
        assert exercise_step["description"].startswith("Peroneal Self Massage:")

    def test_exercise_count_in_output(self):
        """Structured rehab workout includes exercise_count field."""
        session = {
            "type": "rehab",
            "exercises": [
                {"name": "Toe Curls", "sets": 3, "reps": 15},
                {"name": "Calf Raises", "sets": 3, "duration_secs": 30},
            ],
        }
        result = build_pilates_workout(session, "2025-01-01")

        assert result["exercise_count"] == 2

    def test_no_exercises_falls_back_to_simple_timer(self):
        """Session without exercises list uses simple timed fallback."""
        session = {"type": "rehab", "duration_mins": 15}
        result = build_pilates_workout(session, "2025-01-01")

        steps = result["workoutSegments"][0]["workoutSteps"]
        assert len(steps) == 1
        assert steps[0]["type"] == "ExecutableStepDTO"
        assert steps[0]["endCondition"] == END_TIME
        assert "exercise_count" not in result


# ─── build_swimming_workout ─────────────────────────────────────────

class TestBuildSwimmingWorkout:
    def test_simple_timed_swimming(self):
        session = {"type": "swim", "duration_mins": 30}
        result = build_swimming_workout(session, "2025-01-01")

        assert result["sportType"] == SWIMMING_SPORT
        assert result["estimatedDurationInSecs"] == 30 * 60
        assert result["poolLength"] == 25.0
        assert result["poolLengthUnit"]["unitKey"] == "meter"

    def test_structured_swimming_uses_distance(self):
        session = {
            "type": "swim",
            "duration_mins": 45,
            "structure": [
                {"phase": "warmup", "distance_m": 200},
                {"phase": "main", "distance_m": 800},
                {"phase": "cooldown", "distance_m": 200},
            ],
        }
        result = build_swimming_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        assert len(steps) == 3
        assert steps[0]["stepType"] == STEP_WARMUP
        assert steps[0]["endConditionValue"] == 200.0
        assert steps[1]["stepType"] == STEP_INTERVAL
        assert steps[2]["stepType"] == STEP_COOLDOWN

    def test_custom_pool_length(self):
        session = {"type": "swim", "duration_mins": 30, "pool_length_m": 50.0}
        result = build_swimming_workout(session, "2025-01-01")
        assert result["poolLength"] == 50.0


# ─── build_padel_workout ────────────────────────────────────────────

class TestBuildPadelWorkout:
    def test_basic_padel_workout(self):
        session = {"type": "padel", "duration_mins": 90}
        result = build_padel_workout(session, "2025-01-01")

        assert result["sportType"] == PADEL_SPORT
        assert result["estimatedDurationInSecs"] == 90 * 60
        assert result["workoutName"] == "Padel Session"

    def test_custom_description(self):
        session = {"type": "padel", "duration_mins": 90, "description": "Wednesday Padel Match"}
        result = build_padel_workout(session, "2025-01-01")
        assert result["workoutName"] == "Wednesday Padel Match"


# ─── build_ftp_test_workout ─────────────────────────────────────────

class TestBuildFtpTestWorkout:
    def test_default_protocol_phases(self):
        session = {"type": "ftp_test", "duration_mins": 50, "description": "FTP Test"}
        result = build_ftp_test_workout(session, "2025-01-01")

        assert isinstance(result, CyclingWorkout)
        assert "(Indoor)" in result.workoutName
        steps = result.workoutSegments[0].workoutSteps
        # Default protocol: warmup + 3 blowout/recovery pairs + main recovery + test + cooldown = 9 steps
        assert len(steps) == 9

    def test_ftp_test_uses_cadence_targets(self):
        session = {"type": "ftp_test", "duration_mins": 50, "description": "FTP Test"}
        result = build_ftp_test_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        # Warmup step should have cadence targets
        warmup = steps[0]
        assert warmup.targetType == TARGET_CADENCE
        assert warmup.targetValueOne == 90
        assert warmup.targetValueTwo == 100

    def test_ftp_test_with_custom_protocol(self):
        session = {
            "type": "ftp_test",
            "duration_mins": 30,
            "description": "Short FTP",
            "protocol": [
                {"phase": "warmup", "duration_mins": 10, "cadence_min": 85, "cadence_max": 95},
                {"phase": "test", "duration_mins": 20, "cadence_min": 90, "cadence_max": 100},
            ],
        }
        result = build_ftp_test_workout(session, "2025-01-01")
        steps = result.workoutSegments[0].workoutSteps
        assert len(steps) == 2
        assert result.estimatedDurationInSecs == 30 * 60

    def test_ftp_test_cycling_sport(self):
        session = {"type": "ftp_test", "duration_mins": 50, "description": "FTP Test"}
        result = build_ftp_test_workout(session, "2025-01-01")
        assert result.workoutSegments[0].sportType == CYCLING_SPORT


# ─── build_structured_indoor_workout ────────────────────────────────

class TestBuildStructuredIndoorWorkout:
    def test_power_watts_targets(self):
        structure = [
            {"phase": "warmup", "duration_mins": 10, "power_watts": [100, 130]},
            {"phase": "interval", "duration_mins": 20, "power_watts": [200, 240]},
            {"phase": "cooldown", "duration_mins": 5, "power_watts": [90, 110]},
        ]
        session = {"type": "wattbike", "duration_mins": 35, "structure": structure}
        result = build_structured_indoor_workout(
            session, "Sweet Spot (Indoor)", 35 * 60, structure
        )

        assert isinstance(result, CyclingWorkout)
        steps = result.workoutSegments[0].workoutSteps
        assert len(steps) == 3

        # Warmup with power target
        assert steps[0].stepType == STEP_WARMUP
        assert steps[0].targetType == TARGET_POWER
        assert steps[0].targetValueOne == 100
        assert steps[0].targetValueTwo == 130

        # Interval with power
        assert steps[1].stepType == STEP_INTERVAL
        assert steps[1].targetType == TARGET_POWER
        assert steps[1].targetValueOne == 200
        assert steps[1].targetValueTwo == 240

    @patch("workout_builder.get_athlete_power_zones", return_value={
        "z4_threshold": [220, 260]
    })
    def test_power_pct_calculated_from_ftp(self, mock_zones):
        structure = [
            {"phase": "interval", "duration_mins": 20, "power_pct": 90},
        ]
        session = {
            "type": "wattbike",
            "duration_mins": 20,
            "ftp": 250,
            "structure": structure,
        }
        result = build_structured_indoor_workout(
            session, "Threshold (Indoor)", 20 * 60, structure
        )

        steps = result.workoutSegments[0].workoutSteps
        step = steps[0]
        assert step.targetType == TARGET_POWER
        # 90% of 250 = 225, range is +/- 5%: 212-237
        assert step.targetValueOne == int(250 * 0.85)
        assert step.targetValueTwo == int(250 * 0.95)

    def test_cadence_focused_phase_uses_cadence_target(self):
        structure = [
            {"phase": "single_leg_drills", "duration_mins": 10, "cadence": [80, 90], "power_watts": [100, 130]},
        ]
        session = {"type": "wattbike", "duration_mins": 10, "structure": structure}
        result = build_structured_indoor_workout(
            session, "Technique (Indoor)", 10 * 60, structure
        )

        step = result.workoutSegments[0].workoutSteps[0]
        # Cadence-focused phases prioritize cadence over power
        assert step.targetType == TARGET_CADENCE
        assert step.targetValueOne == 80
        assert step.targetValueTwo == 90

    def test_fallback_to_cadence_when_no_power(self):
        structure = [
            {"phase": "warmup", "duration_mins": 10, "cadence": [85, 95]},
        ]
        session = {"type": "wattbike", "duration_mins": 10, "structure": structure}
        result = build_structured_indoor_workout(
            session, "Warmup (Indoor)", 10 * 60, structure
        )

        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_CADENCE

    def test_no_target_when_nothing_specified(self):
        structure = [
            {"phase": "warmup", "duration_mins": 10},
        ]
        session = {"type": "wattbike", "duration_mins": 10, "structure": structure}
        result = build_structured_indoor_workout(
            session, "Easy (Indoor)", 10 * 60, structure
        )

        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_NONE

    def test_notes_added_as_description(self):
        structure = [
            {"phase": "interval", "duration_mins": 5, "notes": "Hold steady power!"},
        ]
        session = {"type": "wattbike", "duration_mins": 5, "structure": structure}
        result = build_structured_indoor_workout(
            session, "Intervals (Indoor)", 5 * 60, structure
        )

        step = result.workoutSegments[0].workoutSteps[0]
        assert step.description == "Hold steady power!"

    def test_calculated_duration_from_structure(self):
        structure = [
            {"phase": "warmup", "duration_mins": 10},
            {"phase": "interval", "duration_mins": 20},
            {"phase": "cooldown", "duration_mins": 5},
        ]
        session = {"type": "wattbike", "duration_mins": 35, "structure": structure}
        result = build_structured_indoor_workout(
            session, "Workout (Indoor)", 0, structure
        )

        # Total from structure: 10+20+5 = 35 min = 2100 sec
        assert result.estimatedDurationInSecs == 2100


# ─── get_hr_target_for_intensity ────────────────────────────────────

class TestGetHrTargetForIntensity:
    @patch("workout_builder.get_athlete_hr_zones", return_value=None)
    def test_uses_default_zones_when_none(self, mock_zones):
        result = get_hr_target_for_intensity("easy")
        assert result == (DEFAULT_HR_ZONES["z2_aerobic"][0], DEFAULT_HR_ZONES["z2_aerobic"][1])

    @patch("workout_builder.get_athlete_hr_zones", return_value={
        "z2_aerobic": [115, 145],
    })
    def test_uses_athlete_zones(self, mock_zones):
        result = get_hr_target_for_intensity("easy")
        assert result == (115, 145)

    @patch("workout_builder.get_athlete_hr_zones", return_value=None)
    def test_recovery_maps_to_z1(self, mock_zones):
        result = get_hr_target_for_intensity("recovery")
        assert result == (DEFAULT_HR_ZONES["z1_recovery"][0], DEFAULT_HR_ZONES["z1_recovery"][1])

    @patch("workout_builder.get_athlete_hr_zones", return_value=None)
    def test_threshold_maps_to_z4(self, mock_zones):
        result = get_hr_target_for_intensity("threshold")
        assert result == (DEFAULT_HR_ZONES["z4_threshold"][0], DEFAULT_HR_ZONES["z4_threshold"][1])

    @patch("workout_builder.get_athlete_hr_zones", return_value=None)
    def test_unknown_intensity_defaults_to_z2(self, mock_zones):
        result = get_hr_target_for_intensity("unknown_intensity")
        assert result == (DEFAULT_HR_ZONES["z2_aerobic"][0], DEFAULT_HR_ZONES["z2_aerobic"][1])


# ─── GARMIN_CATEGORY_MAP ────────────────────────────────────────────

class TestGarminCategoryMap:
    def test_glute_maps_to_squat(self):
        assert GARMIN_CATEGORY_MAP["GLUTE"] == "SQUAT"

    def test_hamstring_maps_to_deadlift(self):
        assert GARMIN_CATEGORY_MAP["HAMSTRING"] == "DEADLIFT"

    def test_chest_maps_to_bench_press(self):
        assert GARMIN_CATEGORY_MAP["CHEST"] == "BENCH_PRESS"

    def test_back_maps_to_row(self):
        assert GARMIN_CATEGORY_MAP["BACK"] == "ROW"

    def test_abs_maps_to_core(self):
        assert GARMIN_CATEGORY_MAP["ABS"] == "CORE"

    def test_calf_maps_to_squat(self):
        assert GARMIN_CATEGORY_MAP["CALF"] == "SQUAT"

    def test_suspension_maps_to_row(self):
        """SUSPENSION (face pulls, TRX) must map to ROW for workout API."""
        assert GARMIN_CATEGORY_MAP["SUSPENSION"] == "ROW"

    def test_hamstring_curl_maps_to_deadlift(self):
        """HAMSTRING_CURL (lying leg curl) must map to DEADLIFT for workout API."""
        assert GARMIN_CATEGORY_MAP["HAMSTRING_CURL"] == "DEADLIFT"

    def test_leg_curl_maps_to_deadlift(self):
        assert GARMIN_CATEGORY_MAP["LEG_CURL"] == "DEADLIFT"

    def test_plank_maps_to_core(self):
        assert GARMIN_CATEGORY_MAP["PLANK"] == "CORE"

    def test_push_up_maps_to_bench_press(self):
        assert GARMIN_CATEGORY_MAP["PUSH_UP"] == "BENCH_PRESS"

    def test_all_exercise_db_categories_mapped(self):
        """Every non-valid exercise DB category should be in the map."""
        valid_workout_categories = {
            "BENCH_PRESS", "ROW", "PULL_UP", "LATERAL_RAISE", "CURL",
            "TRICEPS_EXTENSION", "CORE", "DEADLIFT", "SQUAT",
            "SHOULDER_PRESS", "LUNGE", "CARDIO", "OTHER",
        }
        # Categories from exercises.json that generate_strength_workout might use
        exercise_db_categories = {
            "SUSPENSION", "HAMSTRING_CURL", "LEG_CURL", "CALF_RAISE",
            "HIP_RAISE", "HIP_STABILITY", "HIP_SWING", "HYPEREXTENSION",
            "LEG_RAISE", "PLANK", "CRUNCH", "SIT_UP", "CHOP",
            "FLYE", "PUSH_UP", "SHRUG", "SHOULDER_STABILITY",
            "OLYMPIC_LIFT", "PLYO",
        }
        for cat in exercise_db_categories:
            assert cat in GARMIN_CATEGORY_MAP, f"{cat} missing from GARMIN_CATEGORY_MAP"
            assert GARMIN_CATEGORY_MAP[cat] in valid_workout_categories, (
                f"{cat} maps to {GARMIN_CATEGORY_MAP[cat]} which is not a valid workout category"
            )
