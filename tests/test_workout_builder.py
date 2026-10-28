"""
Tests for workout_builder.py module.

Tests helper functions, workout type detection, and workout building
for cycling, running, strength, yoga, swimming, and padel sessions.
"""
import pytest
from unittest.mock import patch

from garminconnect.workout import CyclingWorkout, RunningWorkout, ExecutableStep, RepeatGroup

from coach.workout_builder import (
    is_indoor_cycling,
    is_simple_outdoor_ride,
    build_workout,
    build_cycling_workout,
    build_running_workout,
    build_structured_running_workout,
    build_strength_workout,
    build_yoga_workout,
    build_pilates_workout,
    build_swimming_workout,
    build_padel_workout,
    build_ftp_test_workout,
    build_structured_cycling_workout,
    get_hr_target_for_intensity,
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
    END_DISTANCE,
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
    CATALOGUE_CATEGORY_TO_WORKOUT,
    VALID_WORKOUT_CATEGORIES,
    WorkoutAuthoringError,
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


# ─── build_workout dispatch ─────────────────────────────────────────

class TestBuildWorkoutDispatch:
    """Test that build_workout dispatches to the correct builder."""

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_returns_cycling_workout_for_ride(self, mock_hr):
        session = {"type": "easy_ride", "duration_mins": 60, "intensity": "easy"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, CyclingWorkout)

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_returns_running_workout_for_run(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, RunningWorkout)

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_returns_dict_for_strength(self, mock_library):
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

    def test_zero_duration_is_a_contract_error_not_a_placeholder(self):
        session = {"type": "easy_run", "duration_mins": 0}
        with pytest.raises(WorkoutAuthoringError) as exc:
            build_workout(session, "2025-01-01")
        assert "more than 0 minutes" in str(exc.value)

    def test_returns_none_for_a_placeholder_without_duration(self):
        session = {"type": "easy_run", "purpose": "placeholder"}
        assert build_workout(session, "2025-01-01") is None

    def test_returns_none_for_unknown_type(self):
        session = {"type": "basketball", "duration_mins": 60}
        result = build_workout(session, "2025-01-01")
        assert result is None

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_ride_substring_dispatches_to_cycling(self, mock_hr):
        session = {"type": "tempo_ride", "duration_mins": 60, "intensity": "tempo"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, CyclingWorkout)

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_run_substring_dispatches_to_running(self, mock_hr, mock_pace):
        session = {"type": "morning_run", "duration_mins": 30, "intensity": "easy"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, RunningWorkout)

    @patch("coach.workout_builder.get_power_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=None)
    def test_ftp_test_dispatches_to_ftp_builder(self, mock_hr, mock_power):
        session = {"type": "ftp_test", "duration_mins": 50, "description": "FTP Test"}
        result = build_workout(session, "2025-01-01")
        assert isinstance(result, CyclingWorkout)
        assert "FTP" in result.workoutName or "ftp" in result.workoutName.lower()


# ─── build_cycling_workout ──────────────────────────────────────────

class TestBuildCyclingWorkout:
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_outdoor_ride_has_hr_target(self, mock_hr):
        session = {"type": "easy_ride", "duration_mins": 60, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")

        assert isinstance(result, CyclingWorkout)
        assert "(Outdoor)" in result.workoutName
        assert result.estimatedDurationInSecs == 3600

    @patch("coach.workout_builder.get_power_target_for_intensity", return_value=(130, 160))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
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

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_simple_outdoor_ride_has_no_warmup(self, mock_hr):
        """Simple outdoor rides skip warmup/cooldown sections."""
        session = {"type": "easy_ride", "duration_mins": 90, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        # Simple outdoor ride: single interval step, no warmup/cooldown
        assert len(steps) == 1
        assert steps[0].stepType == STEP_INTERVAL

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_outdoor_ride_single_step_full_duration(self, mock_hr):
        session = {"type": "long_ride", "duration_mins": 120, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        # Single interval step for the full duration
        assert len(steps) == 1
        assert steps[0].endConditionValue == 120 * 60

    @patch("coach.workout_builder.get_power_target_for_intensity", return_value=(150, 180))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_indoor_ride_has_warmup_main_cooldown(self, mock_hr, mock_power):
        session = {"type": "indoor_cycling", "duration_mins": 60, "intensity": "tempo"}
        result = build_cycling_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        assert len(steps) == 3
        assert steps[0].stepType == STEP_WARMUP
        assert steps[1].stepType == STEP_INTERVAL
        assert steps[2].stepType == STEP_COOLDOWN

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=None)
    def test_no_zones_uses_no_target(self, mock_hr):
        session = {"type": "easy_ride", "duration_mins": 60, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        for step in steps:
            assert step.targetType == TARGET_NONE

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
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

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_cycling_sport_type(self, mock_hr):
        session = {"type": "easy_ride", "duration_mins": 60, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")
        assert result.workoutSegments[0].sportType == CYCLING_SPORT

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
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

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
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

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(124, 145))
    def test_outdoor_ride_step_has_hr_description(self, mock_hr):
        """Outdoor ride main step should show HR range in description."""
        session = {"type": "easy_ride", "duration_mins": 90, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")
        main_step = result.workoutSegments[0].workoutSteps[0]
        assert main_step.description == "HR 124-145 bpm"

    @patch("coach.workout_builder.get_power_target_for_intensity", return_value=(130, 160))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_indoor_ride_step_has_no_hr_description(self, mock_hr, mock_power):
        """Indoor ride steps should NOT have HR description."""
        session = {"type": "wattbike", "duration_mins": 60, "intensity": "easy"}
        result = build_cycling_workout(session, "2025-01-01")
        main_step = result.workoutSegments[0].workoutSteps[1]
        assert not hasattr(main_step, "description") or main_step.description is None


# ─── build_running_workout ──────────────────────────────────────────

class TestBuildRunningWorkout:
    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=(2.5, 3.0))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_uses_pace_when_available(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")

        assert isinstance(result, RunningWorkout)
        steps = result.workoutSegments[0].workoutSteps
        # Main step should use pace target
        main_step = steps[1]
        assert main_step.targetType == TARGET_PACE

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_falls_back_to_hr_when_no_pace(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        main_step = steps[1]
        assert main_step.targetType == TARGET_HR
        assert main_step.targetValueOne == 120
        assert main_step.targetValueTwo == 140

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=None)
    def test_no_targets_when_no_zones(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        for step in steps:
            assert step.targetType in (TARGET_NONE, TARGET_HR)  # warmup/cooldown may differ
            # Specifically the main step
        assert steps[1].targetType == TARGET_NONE

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_has_warmup_main_cooldown(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 60, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")

        steps = result.workoutSegments[0].workoutSteps
        assert len(steps) == 3
        assert steps[0].stepType == STEP_WARMUP
        assert steps[1].stepType == STEP_INTERVAL
        assert steps[2].stepType == STEP_COOLDOWN

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_estimated_duration_matches_session(self, mock_hr, mock_pace):
        session = {"type": "long_run", "duration_mins": 90, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")
        assert result.estimatedDurationInSecs == 90 * 60

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_running_sport_type(self, mock_hr, mock_pace):
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")
        assert result.workoutSegments[0].sportType == RUNNING_SPORT

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_workout_name_from_description(self, mock_hr, mock_pace):
        session = {
            "type": "easy_run",
            "duration_mins": 45,
            "intensity": "easy",
            "description": "Morning easy run",
        }
        result = build_running_workout(session, "2025-01-01")
        assert result.workoutName == "Morning easy run"

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_structure_delegates_to_structured_builder(self, mock_hr, mock_pace):
        """When session has structure, build_running_workout returns a workout
        whose top-level steps come from the structure, not the 3-step fallback."""
        session = {
            "type": "running",
            "duration_mins": 30,
            "intensity": "easy",
            "description": "L2 R4/W2 x 4",
            "structure": [
                {"phase": "warmup", "duration_secs": 180, "intensity": "recovery"},
                {"phase": "repeat", "iterations": 4, "steps": [
                    {"phase": "interval", "duration_secs": 240, "intensity": "easy"},
                    {"phase": "recovery", "duration_secs": 120, "intensity": "recovery"},
                ]},
                {"phase": "cooldown", "duration_secs": 180, "intensity": "recovery"},
            ],
        }
        result = build_running_workout(session, "2025-01-01")
        steps = result.workoutSegments[0].workoutSteps
        # 3 top-level entries: warmup, repeat group, cooldown
        assert len(steps) == 3
        assert isinstance(steps[1], RepeatGroup)


# ─── build_structured_running_workout ──────────────────────────────

class TestBuildStructuredRunningWorkout:
    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=(2.5, 3.0))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_run_walk_repeat_protocol(self, mock_hr, mock_pace):
        """The L2 R4/W2 x 4 shape: warmup, repeat(4, [run, walk]), cooldown."""
        session = {
            "type": "running",
            "duration_mins": 30,
            "description": "L2 R4/W2 x 4",
            "structure": [
                {"phase": "warmup", "duration_secs": 180, "intensity": "recovery",
                 "notes": "300m walk + 4 heel + 4 toe steps"},
                {"phase": "repeat", "iterations": 4, "steps": [
                    {"phase": "interval", "duration_secs": 240, "intensity": "easy",
                     "notes": "Run 4 min"},
                    {"phase": "recovery", "duration_secs": 120, "intensity": "recovery",
                     "notes": "Walk 2 min"},
                ]},
                {"phase": "cooldown", "duration_secs": 180, "intensity": "recovery",
                 "notes": "200-300m walk"},
            ],
        }
        result = build_structured_running_workout(session, "2025-01-01")
        steps = result.workoutSegments[0].workoutSteps

        assert isinstance(result, RunningWorkout)
        assert len(steps) == 3

        warmup, repeat_group, cooldown = steps
        assert isinstance(warmup, ExecutableStep)
        assert isinstance(repeat_group, RepeatGroup)
        assert isinstance(cooldown, ExecutableStep)

        assert warmup.stepType == STEP_WARMUP
        assert warmup.endCondition == END_TIME
        assert warmup.endConditionValue == 180
        assert warmup.description == "300m walk + 4 heel + 4 toe steps"

        assert repeat_group.numberOfIterations == 4
        assert len(repeat_group.workoutSteps) == 2
        run_step, walk_step = repeat_group.workoutSteps
        assert run_step.stepType == STEP_INTERVAL
        assert run_step.endConditionValue == 240
        assert run_step.childStepId == 1
        assert walk_step.stepType == STEP_RECOVERY
        assert walk_step.endConditionValue == 120
        assert walk_step.childStepId == 1

        assert cooldown.stepType == STEP_COOLDOWN
        assert cooldown.endConditionValue == 180

        # Estimated total = 180 + 4*(240+120) + 180 = 1800 secs (30 min)
        assert result.estimatedDurationInSecs == 1800

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=(2.5, 3.0))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_distance_based_step(self, mock_hr, mock_pace):
        session = {
            "type": "running",
            "duration_mins": 10,
            "structure": [
                {"phase": "warmup", "distance_m": 300, "intensity": "recovery"},
            ],
        }
        result = build_structured_running_workout(session, "2025-01-01")
        step = result.workoutSegments[0].workoutSteps[0]
        assert step.endCondition == END_DISTANCE
        assert step.endConditionValue == 300

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=(2.5, 3.0))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_open_lap_button_step(self, mock_hr, mock_pace):
        session = {
            "type": "running",
            "duration_mins": 10,
            "structure": [
                {"phase": "warmup", "duration_secs": "open", "intensity": "recovery"},
            ],
        }
        result = build_structured_running_workout(session, "2025-01-01")
        step = result.workoutSegments[0].workoutSteps[0]
        assert step.endCondition == END_LAP_BUTTON
        assert step.endConditionValue is None

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=(2.5, 3.0))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_explicit_pace_target(self, mock_hr, mock_pace):
        """Explicit pace target wins over intensity-based resolution."""
        session = {
            "type": "running",
            "duration_mins": 10,
            "structure": [
                {"phase": "interval", "duration_secs": 600, "pace": [3.0, 3.5],
                 "intensity": "easy"},
            ],
        }
        result = build_structured_running_workout(session, "2025-01-01")
        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_PACE
        assert step.targetValueOne == 3.0
        assert step.targetValueTwo == 3.5

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_intensity_falls_back_to_hr_when_no_pace(self, mock_hr, mock_pace):
        session = {
            "type": "running",
            "duration_mins": 10,
            "structure": [
                {"phase": "interval", "duration_secs": 600, "intensity": "easy"},
            ],
        }
        result = build_structured_running_workout(session, "2025-01-01")
        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_HR
        assert step.targetValueOne == 120
        assert step.targetValueTwo == 140

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=None)
    def test_no_target_when_no_zones_and_no_explicit_target(self, mock_hr, mock_pace):
        session = {
            "type": "running",
            "duration_mins": 10,
            "structure": [
                {"phase": "interval", "duration_secs": 600, "intensity": "easy"},
            ],
        }
        result = build_structured_running_workout(session, "2025-01-01")
        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_NONE

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=(2.5, 3.0))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_explicit_cadence_target(self, mock_hr, mock_pace):
        session = {
            "type": "running",
            "duration_mins": 10,
            "structure": [
                {"phase": "interval", "duration_secs": 600, "cadence": [170, 180]},
            ],
        }
        result = build_structured_running_workout(session, "2025-01-01")
        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_CADENCE
        assert step.targetValueOne == 170
        assert step.targetValueTwo == 180

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=(2.5, 3.0))
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_nested_repeat_groups(self, mock_hr, mock_pace):
        """Repeat inside a repeat — should produce a RepeatGroup with a child RepeatGroup."""
        session = {
            "type": "running",
            "duration_mins": 30,
            "structure": [
                {"phase": "repeat", "iterations": 2, "steps": [
                    {"phase": "repeat", "iterations": 3, "steps": [
                        {"phase": "interval", "duration_secs": 60, "intensity": "easy"},
                        {"phase": "recovery", "duration_secs": 30, "intensity": "recovery"},
                    ]},
                ]},
            ],
        }
        result = build_structured_running_workout(session, "2025-01-01")
        outer = result.workoutSegments[0].workoutSteps[0]
        assert isinstance(outer, RepeatGroup)
        assert outer.numberOfIterations == 2
        inner = outer.workoutSteps[0]
        assert isinstance(inner, RepeatGroup)
        assert inner.numberOfIterations == 3
        assert inner.childStepId == 1
        # Estimated total = 2 * 3 * (60 + 30) = 540
        assert result.estimatedDurationInSecs == 540

    @patch("coach.workout_builder.get_pace_target_for_intensity", return_value=None)
    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(120, 140))
    def test_no_structure_uses_three_step_fallback(self, mock_hr, mock_pace):
        """Regression: a session without structure still produces warmup/main/cooldown."""
        session = {"type": "easy_run", "duration_mins": 45, "intensity": "easy"}
        result = build_running_workout(session, "2025-01-01")
        steps = result.workoutSegments[0].workoutSteps
        assert len(steps) == 3
        assert steps[0].stepType == STEP_WARMUP
        assert steps[1].stepType == STEP_INTERVAL
        assert steps[2].stepType == STEP_COOLDOWN
        # No repeat groups
        assert not any(isinstance(s, RepeatGroup) for s in steps)


# ─── build_strength_workout ─────────────────────────────────────────

class TestBuildStrengthWorkout:
    """The renderer draws exactly what `exercises` says (coach/authoring.py)."""

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_simple_timed_workout_no_exercises(self, mock_library):
        session = {"type": "strength", "duration_mins": 45}
        result = build_strength_workout(session, "2025-01-01")

        assert result["sportType"] == STRENGTH_SPORT
        assert result["estimatedDurationInSecs"] == 45 * 60
        steps = result["workoutSegments"][0]["workoutSteps"]
        assert len(steps) == 1
        assert steps[0]["stepType"] == STEP_INTERVAL  # one timed block, not a 45-min "warm-up"

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_warmup_mins_renders_warmup_block_and_lap_rest(self, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "warmup_mins": 5,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        assert steps[0]["stepType"] == STEP_WARMUP
        assert steps[0]["endConditionValue"] == 300.0
        assert steps[0]["category"] == "WARM_UP"
        assert steps[1]["stepType"] == STEP_REST
        assert steps[1]["endCondition"] == END_LAP_BUTTON
        assert steps[2]["type"] == "RepeatGroupDTO"

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_no_warmup_is_inserted_unless_stated(self, mock_library):
        """The old builder inserted a 5-min CARDIO warm-up the plan never wrote."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_BACK_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]
        assert steps[0]["type"] == "RepeatGroupDTO"
        assert steps[0]["stepOrder"] == 1
        step_types = [s.get("stepType") for s in steps if s.get("type") == "ExecutableStepDTO"]
        assert STEP_WARMUP not in step_types
        assert STEP_REST not in step_types

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_skip_warmup_is_retired_with_a_warning(self, mock_library):
        from coach.workout_builder import build_workout_report
        session = {
            "type": "strength", "duration_mins": 45, "skip_warmup": True,
            "exercises": [{"name": "BARBELL_BACK_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10}],
        }
        report = build_workout_report(session, "2025-01-01")
        assert report["ok"]
        assert any(w["field"] == "skip_warmup" for w in report["warnings"])

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_exercise_creates_repeat_group(self, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 4, "reps": 8},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        assert len(steps) == 1
        repeat_group = steps[0]
        assert repeat_group["type"] == "RepeatGroupDTO"
        assert repeat_group["numberOfIterations"] == 4
        assert repeat_group["endConditionValue"] == 4.0

    @patch("coach.workout_builder.load_exercise_db",
           return_value={"BENCH_PRESS": {"category": "BENCH_PRESS", "custom": False}})
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_repeat_group_contains_exercise_and_lap_rest(self, mock_library, mock_db):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BENCH_PRESS", "category": "BENCH_PRESS", "sets": 3, "reps": 12},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        repeat_group = result["workoutSegments"][0]["workoutSteps"][0]
        inner_steps = repeat_group["workoutSteps"]

        assert len(inner_steps) == 2
        assert inner_steps[0]["stepType"] == STEP_INTERVAL
        assert inner_steps[0]["endCondition"] == END_REPS
        assert inner_steps[0]["endConditionValue"] == 12.0
        assert inner_steps[0]["exerciseName"] == "BENCH_PRESS"
        assert inner_steps[0]["category"] == "BENCH_PRESS"
        # No rest_secs stated -> lap-button rest
        assert inner_steps[1]["stepType"] == STEP_REST
        assert inner_steps[1]["endCondition"] == END_LAP_BUTTON

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_rest_secs_renders_a_timed_rest_step(self, mock_library):
        session = {
            "type": "strength", "duration_mins": 45,
            "exercises": [{"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10,
                           "rest_secs": 90}],
        }
        result = build_strength_workout(session, "2025-01-01")
        rest = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][1]
        assert rest["stepType"] == STEP_REST
        assert rest["endCondition"] == END_TIME
        assert rest["endConditionValue"] == 90.0

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_rest_secs_zero_means_no_rest_step(self, mock_library):
        session = {
            "type": "strength", "duration_mins": 45,
            "exercises": [{"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10,
                           "rest_secs": 0}],
        }
        result = build_strength_workout(session, "2025-01-01")
        inner = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"]
        assert len(inner) == 1

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_multiple_exercises_create_multiple_groups(self, mock_library):
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

        assert len(steps) == 3
        assert result["exercise_count"] == 3

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_explicit_weight_included(self, mock_library):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10, "weight_kg": 60},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        exercise_step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]

        assert exercise_step["weightValue"] == 60.0
        assert exercise_step["weightUnit"]["unitKey"] == "kilogram"

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_no_weight_is_invented(self, mock_library):
        """Regression: a 42.5 kg category baseline landed on a one-arm row the
        plan had written without a weight. Only an explicit weight_kg is sent."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "ONE_ARM_BENT_OVER_ROW", "category": "ROW", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        exercise_step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert "weightValue" not in exercise_step
        assert "weightUnit" not in exercise_step

    @patch("coach.workout_builder.load_exercise_db", return_value={
        "BARBELL_SQUAT": {"category": "SQUAT", "custom": False}
    })
    @patch("coach.workout_builder.load_exercise_library", return_value={
        "barbell squat": {"garmin_note": "Drive through heels, chest up"}
    })
    def test_exercise_library_note_fills_in_when_plan_has_no_notes(self, mock_library, mock_db):
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        exercise_step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]

        # exerciseName is set (BARBELL_SQUAT), so description is form cues only
        assert exercise_step["description"] == "Drive through heels, chest up"
        assert exercise_step["exerciseName"] == "BARBELL_SQUAT"

    @patch("coach.workout_builder.load_exercise_db", return_value={
        "SIDE_LUNGE_JUMP_OFF": {"category": "LUNGE", "custom": False}
    })
    @patch("coach.workout_builder.load_exercise_library", return_value={
        "side lunge jump off": {"garmin_note": "Explode up"}
    })
    def test_plan_notes_reach_the_watch_and_win_over_the_library(self, mock_library, mock_db):
        """Regression: the physio's 'No jump' cue was dropped and the watch
        got an exercise whose Garmin key says JUMP."""
        session = {
            "type": "strength", "duration_mins": 30,
            "exercises": [{"name": "SIDE_LUNGE_JUMP_OFF", "sets": 2, "reps": 8,
                           "notes": "No jump. Push off the left foot"}],
        }
        result = build_strength_workout(session, "2025-01-01")
        step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert step["exerciseName"] == "SIDE_LUNGE_JUMP_OFF"
        assert step["category"] == "LUNGE"
        assert step["description"] == "No jump. Push off the left foot"

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_duration_secs_renders_a_timed_hold(self, mock_library):
        """Regression: a 45 s plank became 3 x 10 reps."""
        session = {
            "type": "strength", "duration_mins": 20,
            "exercises": [{"name": "PLANK", "category": "PLANK", "sets": 3, "duration_secs": 45}],
        }
        result = build_strength_workout(session, "2025-01-01")
        step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert step["endCondition"] == END_TIME
        assert step["endConditionValue"] == 45.0

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_open_reps_end_with_the_lap_button(self, mock_library):
        session = {
            "type": "strength", "duration_mins": 20,
            "exercises": [{"name": "PULL_UP", "category": "PULL_UP", "sets": 3, "reps": "open",
                           "notes": "to failure"}],
        }
        result = build_strength_workout(session, "2025-01-01")
        step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert step["endCondition"] == END_LAP_BUTTON
        assert "endConditionValue" not in step

    @patch("coach.workout_builder.load_exercise_db", return_value={
        "DEAD_BUG": {"category": "HIP_STABILITY", "custom": False}
    })
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_each_side_is_said_on_the_cue(self, mock_library, mock_db):
        session = {
            "type": "strength", "duration_mins": 20,
            "exercises": [{"name": "DEAD_BUG", "sets": 3, "reps": 8, "each_side": True, "notes": "slow"}],
        }
        result = build_strength_workout(session, "2025-01-01")
        step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert step["description"] == "each side: slow"

    @patch("coach.workout_builder.load_exercise_db", return_value={
        "PUSH_UP": {"category": "PUSH_UP", "custom": False},
        "DEAD_BUG": {"category": "HIP_STABILITY", "custom": False},
    })
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_circuit_renders_one_repeat_group_of_rounds(self, mock_library, mock_db):
        session = {
            "type": "strength", "duration_mins": 20,
            "exercises": [{"rounds": 3, "rest_secs": 60, "name": "Core finisher", "exercises": [
                {"name": "PUSH_UP", "reps": 10},
                {"name": "DEAD_BUG", "reps": 8, "each_side": True},
            ]}],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]
        assert len(steps) == 1
        group = steps[0]
        assert group["numberOfIterations"] == 3
        assert group["description"] == "Core finisher"
        inner = group["workoutSteps"]
        assert [s["stepType"] for s in inner] == [STEP_INTERVAL, STEP_INTERVAL, STEP_REST]
        assert inner[0]["exerciseName"] == "PUSH_UP"
        assert inner[1]["exerciseName"] == "DEAD_BUG"
        assert inner[2]["endConditionValue"] == 60.0
        assert result["exercise_count"] == 2

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_name_with_spaces_is_normalised_to_a_garmin_key(self, mock_library):
        """Regression: 'Bulgarian split squat' became a nameless CARDIO step."""
        session = {
            "type": "strength", "duration_mins": 30,
            "exercises": [{"name": "Bulgarian split squat", "category": "SQUAT", "sets": 3, "reps": 8}],
        }
        result = build_strength_workout(session, "2025-01-01")
        step = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert step["category"] == "SQUAT"
        assert step["exerciseName"] == "BULGARIAN_SPLIT_SQUAT"  # no catalogue: trust the pair

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_muscle_group_category_is_rejected_with_a_suggestion(self, mock_library):
        """GLUTE used to be silently mapped to SQUAT; the coach decides now."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "HIP_THRUST", "category": "GLUTE", "sets": 3, "reps": 12},
            ],
        }
        with pytest.raises(WorkoutAuthoringError) as excinfo:
            build_strength_workout(session, "2025-01-01")
        message = excinfo.value.problems[0]["message"]
        assert "GLUTE" in message and "HIP_RAISE" in message and "SQUAT" in message

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_missing_sets_or_reps_is_an_error_not_a_default(self, mock_library):
        """Regression: {'name': 'COPENHAGEN_PLANK'} silently became 3 x 10."""
        session = {"type": "strength", "duration_mins": 45,
                   "exercises": [{"name": "COPENHAGEN_PLANK", "category": "PLANK"}]}
        with pytest.raises(WorkoutAuthoringError) as excinfo:
            build_strength_workout(session, "2025-01-01")
        messages = [p["message"] for p in excinfo.value.problems]
        assert any("sets is required" in m for m in messages)

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_sets_without_reps_or_time_is_an_error(self, mock_library):
        session = {"type": "strength", "duration_mins": 45,
                   "exercises": [{"name": "COPENHAGEN_PLANK", "category": "PLANK", "sets": 3}]}
        with pytest.raises(WorkoutAuthoringError) as excinfo:
            build_strength_workout(session, "2025-01-01")
        message = excinfo.value.problems[0]["message"]
        assert "reps" in message and "duration_secs" in message

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_unknown_key_is_an_error_naming_the_allowed_keys(self, mock_library):
        """Regression: 'weight' (for weight_kg) was silently ignored."""
        session = {"type": "strength", "duration_mins": 45,
                   "exercises": [{"name": "GOBLET_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10,
                                  "weight": 20}]}
        with pytest.raises(WorkoutAuthoringError) as excinfo:
            build_strength_workout(session, "2025-01-01")
        message = excinfo.value.problems[0]["message"]
        assert "unknown key 'weight'" in message and "weight_kg" in message

    @patch("coach.workout_builder.load_exercise_db",
           return_value={
               "FACE_PULL": {"category": "SUSPENSION", "custom": False},
               "LYING_LEG_CURL": {"category": "HAMSTRING_CURL", "custom": False},
           })
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_catalogue_category_is_mapped_through_garmin_s_rejected_set(self, mock_library, mock_db):
        """A stated valid category keeps the name; a catalogue category the
        workout API rejects (HAMSTRING_CURL) is sent as LEG_CURL without the name."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "FACE_PULL", "category": "SUSPENSION", "sets": 3, "reps": 15},
                {"name": "LYING_LEG_CURL", "sets": 3, "reps": 12},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        face_pull = steps[0]["workoutSteps"][0]
        assert face_pull["category"] == "SUSPENSION"
        assert face_pull["exerciseName"] == "FACE_PULL"

        leg_curl = steps[1]["workoutSteps"][0]
        assert leg_curl["category"] == "LEG_CURL"
        assert leg_curl["exerciseName"] == ""
        assert "Lying Leg Curl" in leg_curl["description"]

    @patch("coach.workout_builder.load_exercise_db", return_value={})
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_missing_exercise_db_preserves_names(self, mock_library, mock_db):
        """Clean install (no data/exercises.json cache): trust the caller's
        (name, category) pair instead of blanking every exercise name.
        Regression for the first-ever CI run failure on clean checkout."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BENCH_PRESS", "category": "BENCH_PRESS", "sets": 3, "reps": 12},
                {"name": "FACE_PULL", "category": "SUSPENSION", "sets": 3, "reps": 15},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        steps = result["workoutSegments"][0]["workoutSteps"]

        assert steps[0]["workoutSteps"][0]["exerciseName"] == "BENCH_PRESS"
        assert steps[1]["workoutSteps"][0]["exerciseName"] == "FACE_PULL"

    @patch("coach.workout_builder.load_exercise_db", return_value={})
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_missing_exercise_db_requires_a_category(self, mock_library, mock_db):
        session = {"type": "strength", "duration_mins": 45,
                   "exercises": [{"name": "BENCH_PRESS", "sets": 3, "reps": 12}]}
        with pytest.raises(WorkoutAuthoringError) as excinfo:
            build_strength_workout(session, "2025-01-01")
        assert "category" in excinfo.value.problems[0]["message"]

    @patch("coach.workout_builder.load_exercise_db", return_value={
        "BARBELL_SQUAT": {"category": "SQUAT", "custom": False}
    })
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_valid_category_keeps_exercise_name(self, mock_library, mock_db):
        """Valid categories keep exerciseName intact when exercise is in DB."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "BARBELL_SQUAT", "category": "SQUAT", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        ex = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert ex["category"] == "SQUAT"
        assert ex["exerciseName"] == "BARBELL_SQUAT"

    @patch("coach.workout_builder.load_exercise_db",
           return_value={"SQUAT": {"category": "SQUAT", "custom": False}})
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_unknown_exercise_clears_name(self, mock_library, mock_db):
        """Exercise missing from a PRESENT DB has exerciseName cleared and shown in
        description (an empty/absent DB now means trust-the-caller instead)."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "MADE_UP_EXERCISE", "category": "SQUAT", "sets": 4, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        ex = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert ex["category"] == "SQUAT"
        assert ex["exerciseName"] == ""
        assert "Made Up Exercise" in ex["description"]

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_unknown_category_is_an_error_not_cardio(self, mock_library):
        """Regression: an unknown category fell through to a nameless CARDIO step."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "WEIRD_EXERCISE", "category": "MADE_UP_CATEGORY", "sets": 2, "reps": 10},
            ],
        }
        with pytest.raises(WorkoutAuthoringError) as excinfo:
            build_strength_workout(session, "2025-01-01")
        assert "MADE_UP_CATEGORY" in excinfo.value.problems[0]["message"]

    @patch("coach.workout_builder.load_exercise_db", return_value={
        "DUMBBELL_ROW": {"category": "ROW", "custom": True}
    })
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_custom_exercise_clears_name(self, mock_library, mock_db):
        """Custom exercises have exerciseName cleared even with matching native category."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "DUMBBELL_ROW", "category": "ROW", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        ex = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert ex["category"] == "ROW"
        assert ex["exerciseName"] == ""
        assert "Dumbbell Row" in ex["description"]

    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_workout_name_prefers_the_session_name(self, mock_library):
        session = {"type": "strength", "duration_mins": 30, "name": "Legs A",
                   "description": "Lower body strength - completed."}
        result = build_strength_workout(session, "2025-01-01")
        assert result["workoutName"] == "Legs A"


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

    def test_rehab_type_gets_a_neutral_name_or_its_purpose(self):
        session = {"type": "rehab", "duration_mins": 15}
        assert build_pilates_workout(session, "2025-01-01")["workoutName"] == "Rehab"
        session = {"type": "rehab", "duration_mins": 15, "purpose": "Shoulder rehab set 2"}
        assert build_pilates_workout(session, "2025-01-01")["workoutName"] == "Shoulder rehab set 2"

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
        assert all(s["endCondition"] == END_DISTANCE for s in steps)

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

    def test_padel_sport_is_padel_activity_type(self):
        # 226 = Garmin's paddelball activity type; 219 is the racket_sports
        # category it once shipped with (id and key must name the same type).
        assert PADEL_SPORT["sportTypeId"] == 226
        assert PADEL_SPORT["sportTypeKey"] == "paddelball"

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


# ─── build_structured_cycling_workout ────────────────────────────────

class TestBuildStructuredIndoorWorkout:
    def test_power_watts_targets(self):
        structure = [
            {"phase": "warmup", "duration_mins": 10, "power_watts": [100, 130]},
            {"phase": "interval", "duration_mins": 20, "power_watts": [200, 240]},
            {"phase": "cooldown", "duration_mins": 5, "power_watts": [90, 110]},
        ]
        session = {"type": "wattbike", "duration_mins": 35, "structure": structure}
        result = build_structured_cycling_workout(
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

    @patch("coach.workout_builder.get_athlete_power_zones", return_value={
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
        result = build_structured_cycling_workout(
            session, "Threshold (Indoor)", 20 * 60, structure
        )

        steps = result.workoutSegments[0].workoutSteps
        step = steps[0]
        assert step.targetType == TARGET_POWER
        # 90% of 250 = 225, range is +/- 5%: 212-237
        assert step.targetValueOne == int(250 * 0.85)
        assert step.targetValueTwo == int(250 * 0.95)

    def test_cadence_target(self):
        structure = [
            {"phase": "interval", "duration_mins": 10, "cadence": [80, 90], "notes": "single-leg drills"},
        ]
        session = {"type": "wattbike", "duration_mins": 10, "structure": structure}
        result = build_structured_cycling_workout(
            session, "Technique (Indoor)", 10 * 60, structure
        )

        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_CADENCE
        assert step.targetValueOne == 80
        assert step.targetValueTwo == 90

    def test_two_targets_on_one_phase_is_an_error(self):
        structure = [
            {"phase": "interval", "duration_mins": 10, "cadence": [80, 90], "power_watts": [100, 130]},
        ]
        session = {"type": "wattbike", "duration_mins": 10, "structure": structure}
        with pytest.raises(WorkoutAuthoringError) as excinfo:
            build_structured_cycling_workout(session, "Technique (Indoor)", 10 * 60, structure)
        assert "ONE target" in excinfo.value.problems[0]["message"]

    @patch("coach.workout_builder.get_athlete_power_zones", return_value={
        "z2_endurance": [120, 162], "z4_threshold": [196, 226]})
    @patch("coach.workout_builder.get_athlete_ftp", return_value=216.0)
    def test_repeat_block_seconds_and_fractional_minutes(self, mock_ftp, mock_zones):
        """Regression: a repeat collapsed to one untargeted 5-minute step, 30 s
        became 5 minutes and 0.5 min became 0 s."""
        structure = [
            {"phase": "warmup", "duration_mins": 10, "intensity": "easy"},
            {"phase": "repeat", "iterations": 4, "steps": [
                {"phase": "interval", "duration_mins": 4, "power_pct": [100, 105]},
                {"phase": "recovery", "duration_mins": 0.5, "power_watts": [100, 120]},
            ]},
            {"phase": "interval", "duration_secs": 30, "cadence": [100, 110]},
        ]
        session = {"type": "wattbike", "duration_mins": 30, "structure": structure}
        result = build_structured_cycling_workout(session, "4x4 (Indoor)", 0, structure)
        warmup, repeat, spin = result.workoutSegments[0].workoutSteps

        assert warmup.targetType == TARGET_POWER
        assert (warmup.targetValueOne, warmup.targetValueTwo) == (120, 162)
        assert isinstance(repeat, RepeatGroup)
        assert repeat.numberOfIterations == 4
        work, rest = repeat.workoutSteps
        assert work.endConditionValue == 240
        assert (work.targetValueOne, work.targetValueTwo) == (216, 226)
        assert rest.endConditionValue == 30
        assert spin.endConditionValue == 30
        assert spin.targetType == TARGET_CADENCE
        assert result.estimatedDurationInSecs == 600 + 4 * 270 + 30

    @patch("coach.workout_builder.get_athlete_ftp", return_value=None)
    def test_power_pct_without_ftp_is_an_error(self, mock_ftp):
        structure = [{"phase": "interval", "duration_mins": 20, "power_pct": 90}]
        session = {"type": "wattbike", "duration_mins": 20, "structure": structure}
        with pytest.raises(WorkoutAuthoringError) as excinfo:
            build_structured_cycling_workout(session, "Threshold (Indoor)", 20 * 60, structure)
        assert "FTP" in excinfo.value.problems[0]["message"]

    @patch("coach.workout_builder.get_athlete_power_zones", return_value={"z2_endurance": [120, 162]})
    def test_indoor_phase_intensity_resolves_to_power(self, mock_zones):
        """Regression: an indoor phase with only `intensity` had no target."""
        structure = [{"phase": "interval", "duration_mins": 45, "intensity": "easy"}]
        session = {"type": "indoor_cycling", "duration_mins": 45, "structure": structure}
        result = build_structured_cycling_workout(session, "Easy (Indoor)", 0, structure, indoor=True)
        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_POWER
        assert (step.targetValueOne, step.targetValueTwo) == (120, 162)

    @patch("coach.workout_builder.get_hr_target_for_intensity", return_value=(140, 155))
    def test_phase_without_target_inherits_session_intensity(self, mock_hr):
        structure = [{"phase": "interval", "duration_mins": 20}]
        session = {"type": "cycling", "duration_mins": 20, "intensity": "tempo", "structure": structure}
        result = build_structured_cycling_workout(session, "Tempo (Outdoor)", 0, structure, indoor=False)
        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_HR
        assert (step.targetValueOne, step.targetValueTwo) == (140, 155)

    def test_fallback_to_cadence_when_no_power(self):
        structure = [
            {"phase": "warmup", "duration_mins": 10, "cadence": [85, 95]},
        ]
        session = {"type": "wattbike", "duration_mins": 10, "structure": structure}
        result = build_structured_cycling_workout(
            session, "Warmup (Indoor)", 10 * 60, structure
        )

        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_CADENCE

    def test_no_target_when_nothing_specified(self):
        structure = [
            {"phase": "warmup", "duration_mins": 10},
        ]
        session = {"type": "wattbike", "duration_mins": 10, "structure": structure}
        result = build_structured_cycling_workout(
            session, "Easy (Indoor)", 10 * 60, structure
        )

        step = result.workoutSegments[0].workoutSteps[0]
        assert step.targetType == TARGET_NONE

    def test_notes_added_as_description(self):
        structure = [
            {"phase": "interval", "duration_mins": 5, "notes": "Hold steady power!"},
        ]
        session = {"type": "wattbike", "duration_mins": 5, "structure": structure}
        result = build_structured_cycling_workout(
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
        result = build_structured_cycling_workout(
            session, "Workout (Indoor)", 0, structure
        )

        # Total from structure: 10+20+5 = 35 min = 2100 sec
        assert result.estimatedDurationInSecs == 2100


# ─── get_hr_target_for_intensity ────────────────────────────────────

class TestGetHrTargetForIntensity:
    @patch("coach.workout_builder.get_athlete_hr_zones", return_value=None)
    def test_uses_default_zones_when_none(self, mock_zones):
        result = get_hr_target_for_intensity("easy")
        assert result == (DEFAULT_HR_ZONES["z2_aerobic"][0], DEFAULT_HR_ZONES["z2_aerobic"][1])

    @patch("coach.workout_builder.get_athlete_hr_zones", return_value={
        "z2_aerobic": [115, 145],
    })
    def test_uses_athlete_zones(self, mock_zones):
        result = get_hr_target_for_intensity("easy")
        assert result == (115, 145)

    @patch("coach.workout_builder.get_athlete_hr_zones", return_value=None)
    def test_recovery_maps_to_z1(self, mock_zones):
        result = get_hr_target_for_intensity("recovery")
        assert result == (DEFAULT_HR_ZONES["z1_recovery"][0], DEFAULT_HR_ZONES["z1_recovery"][1])

    @patch("coach.workout_builder.get_athlete_hr_zones", return_value=None)
    def test_threshold_maps_to_z4(self, mock_zones):
        result = get_hr_target_for_intensity("threshold")
        assert result == (DEFAULT_HR_ZONES["z4_threshold"][0], DEFAULT_HR_ZONES["z4_threshold"][1])

    @patch("coach.workout_builder.get_athlete_hr_zones", return_value=None)
    def test_unknown_intensity_has_no_target(self, mock_zones):
        """Regression: 'moderate', 'z4' and 'vo2' all landed in the easy zone."""
        assert get_hr_target_for_intensity("unknown_intensity") is None
        assert get_hr_target_for_intensity("moderate") is None

    @patch("coach.workout_builder.get_athlete_hr_zones", return_value=None)
    def test_vo2_and_zone_aliases_resolve(self, mock_zones):
        z5 = (DEFAULT_HR_ZONES["z5_max"][0], DEFAULT_HR_ZONES["z5_max"][1])
        z4 = (DEFAULT_HR_ZONES["z4_threshold"][0], DEFAULT_HR_ZONES["z4_threshold"][1])
        assert get_hr_target_for_intensity("vo2") == z5
        assert get_hr_target_for_intensity("VO2max") == z5
        assert get_hr_target_for_intensity("z4") == z4
        assert get_hr_target_for_intensity("hard") == z4


# ─── Catalogue category -> workout category ─────────────────────────

class TestGarminCategoryMap:
    def test_all_exercise_db_categories_mapped(self):
        """Every catalogue category the workout API rejects is in the map,
        pointing at a valid category or at '' (no workout category)."""
        import json
        from pathlib import Path
        db_path = Path(__file__).parent.parent / "data" / "exercises.json"
        if not db_path.exists():
            pytest.skip("exercises.json not available")
        with open(db_path) as f:
            exercises = json.load(f).get("exercises", {})
        db_categories = {e.get("category") for e in exercises.values() if e.get("category")}
        for cat in db_categories:
            if cat in VALID_WORKOUT_CATEGORIES:
                continue
            assert cat in CATALOGUE_CATEGORY_TO_WORKOUT, f"{cat} missing from CATALOGUE_CATEGORY_TO_WORKOUT"
            assert CATALOGUE_CATEGORY_TO_WORKOUT[cat] in VALID_WORKOUT_CATEGORIES | {""}, (
                f"{cat} maps to {CATALOGUE_CATEGORY_TO_WORKOUT[cat]} which is not a valid workout category"
            )

    @patch("coach.workout_builder.load_exercise_db", return_value={
        "ONE_ARM_BENCH_ROW": {"category": "ROW", "custom": True}
    })
    @patch("coach.workout_builder.load_exercise_library", return_value={})
    def test_missing_plan_category_uses_db_lookup(self, mock_library, mock_db):
        """Exercise without category in plan should look up from exercise DB."""
        session = {
            "type": "strength",
            "duration_mins": 45,
            "exercises": [
                {"name": "ONE_ARM_BENCH_ROW", "sets": 3, "reps": 10},
            ],
        }
        result = build_strength_workout(session, "2025-01-01")
        ex = result["workoutSegments"][0]["workoutSteps"][0]["workoutSteps"][0]
        assert ex["category"] == "ROW"
        # Custom exercise — name cleared, shown in description instead
        assert ex["exerciseName"] == ""
        assert "One Arm Bench Row" in ex["description"]


# ─── Garmin end-condition ids ───────────────────────────────────────

class TestEndConditionIds:
    """Garmin Connect workout-API ids, confirmed by a live upload round-trip.

    END_DISTANCE once shipped as 1 (lap.button's id): Garmin stored every
    distance step — all structured swims, distance-based run steps — as a
    lap-button step and silently dropped the distance."""

    def test_ids_match_garmin(self):
        assert END_LAP_BUTTON["conditionTypeId"] == 1
        assert END_TIME["conditionTypeId"] == 2
        assert END_DISTANCE["conditionTypeId"] == 3
        assert END_REPS["conditionTypeId"] == 10

    def test_ids_are_unique(self):
        ids = [c["conditionTypeId"] for c in (END_LAP_BUTTON, END_TIME, END_DISTANCE, END_REPS)]
        assert len(set(ids)) == len(ids), "two end conditions share a Garmin id"
