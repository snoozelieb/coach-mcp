"""
Tests for parsers.py - pure parsing functions for Garmin API responses.

These tests have zero MCP dependency and test pure data transformations.
"""
import json
import pytest
from parsers import (
    check_setup,
    parse_resting_heart_rate,
    parse_sleep_score,
    parse_body_battery,
    parse_activity,
    parse_activities,
    parse_training_readiness,
    parse_personal_records,
    calculate_baseline,
    parse_user_profile,
)
from conftest import (
    SAMPLE_RUNNING_ACTIVITY,
    SAMPLE_STRENGTH_ACTIVITY,
    SAMPLE_PR_DATA,
    SAMPLE_TRAINING_READINESS,
    SAMPLE_PARSED_ACTIVITIES,
)


class TestParseRestingHeartRateWithRealData:
    def test_parses_real_garmin_response(self, garmin_fixtures):
        result = parse_resting_heart_rate(garmin_fixtures["user_summary"])
        assert result == 40

    def test_missing_key_returns_na(self, garmin_fixtures):
        stats_without_rhr = {k: v for k, v in garmin_fixtures["user_summary"].items() if k != 'restingHeartRate'}
        assert parse_resting_heart_rate(stats_without_rhr) == 'N/A'

    def test_empty_dict_returns_na(self):
        assert parse_resting_heart_rate({}) == 'N/A'


class TestParseSleepScoreWithRealData:
    def test_parses_real_garmin_response(self, garmin_fixtures):
        result = parse_sleep_score(garmin_fixtures["user_summary"])
        assert result == 'N/A'

    def test_returns_value_when_present(self, garmin_fixtures):
        stats_with_sleep = {**garmin_fixtures["user_summary"], 'sleepScore': 85}
        assert parse_sleep_score(stats_with_sleep) == 85

    def test_empty_dict_returns_na(self):
        assert parse_sleep_score({}) == 'N/A'


class TestParseBodyBatteryWithRealData:
    def test_parses_real_garmin_response(self, garmin_fixtures):
        result = parse_body_battery(garmin_fixtures["body_battery"])
        assert result == 33

    def test_gets_last_value_not_first(self, garmin_fixtures):
        result = parse_body_battery(garmin_fixtures["body_battery"])
        assert result != 40
        assert result == 33

    def test_empty_list_returns_na(self):
        assert parse_body_battery([]) == 'N/A'

    def test_none_returns_na(self):
        assert parse_body_battery(None) == 'N/A'

    def test_missing_values_array_returns_na(self):
        malformed = [{"date": "2025-12-01"}]
        assert parse_body_battery(malformed) == 'N/A'

    def test_empty_values_array_returns_na(self):
        empty_values = [{"date": "2025-12-01", "bodyBatteryValuesArray": []}]
        assert parse_body_battery(empty_values) == 'N/A'


class TestParseActivity:
    def test_parses_running_activity(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)

        assert result['activity_id'] == 12345678901
        assert result['date'] == '2025-12-01'
        assert result['name'] == 'Morning Run'
        assert result['type'] == 'running'
        assert result['duration_mins'] == 45.0
        assert result['distance_km'] == 8.0
        assert result['avg_hr'] == 145
        assert result['max_hr'] == 168
        assert result['calories'] == 520

    def test_calculates_pace_for_running(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)

        # 45 mins for 8km = 5.625 min/km
        assert 'avg_pace_min_km' in result
        assert result['avg_pace_min_km'] == 5.62  # rounded to 2 decimal places

    def test_handles_strength_activity_no_distance(self):
        result = parse_activity(SAMPLE_STRENGTH_ACTIVITY)

        assert result['type'] == 'strength_training'
        assert result['duration_mins'] == 60.0
        assert result['distance_km'] is None
        assert 'avg_pace_min_km' not in result  # No pace for non-running

    def test_handles_missing_fields(self):
        minimal = {
            'activityId': 999,
            'startTimeLocal': '2025-12-01T00:00:00.0',
        }
        result = parse_activity(minimal)

        assert result['activity_id'] == 999
        assert result['date'] == '2025-12-01'
        assert result['name'] == 'Unnamed'
        assert result['type'] == 'unknown'
        assert result['duration_mins'] == 0.0
        assert result['distance_km'] is None

    def test_parses_power_data_when_present(self):
        activity_with_power = {
            **SAMPLE_RUNNING_ACTIVITY,
            'avgPower': 200,
            'maxPower': 350,
            'normPower': 220,
        }
        result = parse_activity(activity_with_power)

        assert result['avg_power'] == 200
        assert result['max_power'] == 350
        assert result['norm_power'] == 220

    def test_power_fields_none_when_absent(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)

        assert result['avg_power'] is None
        assert result['max_power'] is None
        assert result['norm_power'] is None

    def test_start_time_full_timestamp(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)
        assert result['start_time'] == '2025-12-01T06:30:00.0'

    def test_elevation_gain_and_loss(self):
        activity = {
            **SAMPLE_RUNNING_ACTIVITY,
            'elevationGain': 350.5,
            'elevationLoss': 340.2,
        }
        result = parse_activity(activity)
        assert result['elevation_gain'] == 350.5
        assert result['elevation_loss'] == 340.2

    def test_elevation_none_when_absent(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)
        assert result['elevation_gain'] is None
        assert result['elevation_loss'] is None

    def test_event_type_parsed(self):
        activity = {
            **SAMPLE_RUNNING_ACTIVITY,
            'eventType': {'typeId': 4, 'typeKey': 'race'},
        }
        result = parse_activity(activity)
        assert result['event_type'] == 'race'

    def test_event_type_none_when_absent(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)
        assert result['event_type'] is None

    def test_description_parsed(self):
        activity = {
            **SAMPLE_RUNNING_ACTIVITY,
            'description': 'Felt great, knee was fine',
        }
        result = parse_activity(activity)
        assert result['description'] == 'Felt great, knee was fine'

    def test_description_none_when_absent(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)
        assert result['description'] is None

    def test_moving_duration_parsed(self):
        activity = {
            **SAMPLE_RUNNING_ACTIVITY,
            'movingDuration': 2400,  # 40 mins moving out of 45 total
        }
        result = parse_activity(activity)
        assert result['moving_duration_mins'] == 40.0

    def test_moving_duration_none_when_absent(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)
        assert result['moving_duration_mins'] is None

    def test_running_cadence_parsed(self):
        activity = {
            **SAMPLE_RUNNING_ACTIVITY,
            'averageRunningCadenceInStepsPerMinute': 172,
        }
        result = parse_activity(activity)
        assert result['avg_cadence'] == 172

    def test_cycling_cadence_parsed(self):
        cycling = {
            'activityId': 99999,
            'activityName': 'Ride',
            'startTimeLocal': '2025-12-01T08:00:00.0',
            'activityType': {'typeKey': 'cycling', 'parentTypeId': 2},
            'duration': 3600,
            'distance': 30000,
            'averageBikingCadenceInRevPerMinute': 85,
        }
        result = parse_activity(cycling)
        assert result['avg_cadence'] == 85

    def test_cadence_none_when_absent(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)
        assert result['avg_cadence'] is None

    def test_training_effect_parsed(self):
        activity = {
            **SAMPLE_RUNNING_ACTIVITY,
            'aerobicTrainingEffect': 3.2,
            'anaerobicTrainingEffect': 1.5,
        }
        result = parse_activity(activity)
        assert result['training_effect'] == {'aerobic': 3.2, 'anaerobic': 1.5}

    def test_training_effect_none_when_absent(self):
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)
        assert result['training_effect'] is None

    def test_training_effect_partial(self):
        """Only aerobic present — still returns dict."""
        activity = {
            **SAMPLE_RUNNING_ACTIVITY,
            'aerobicTrainingEffect': 2.8,
        }
        result = parse_activity(activity)
        assert result['training_effect'] == {'aerobic': 2.8, 'anaerobic': None}

    def test_power_fields_present_for_cycling(self):
        cycling_activity = {
            'activityId': 99999,
            'activityName': 'Wattbike Session',
            'startTimeLocal': '2025-12-01T08:00:00.0',
            'activityType': {'typeKey': 'indoor_cycling', 'parentTypeId': 2},
            'duration': 3600,
            'distance': 25000,
            'averageHR': 140,
            'maxHR': 165,
            'avgPower': 180,
            'maxPower': 310,
            'normPower': 195,
        }
        result = parse_activity(cycling_activity)

        assert result['type'] == 'indoor_cycling'
        assert result['avg_power'] == 180
        assert result['norm_power'] == 195


class TestParseActivities:
    def test_parses_list_of_activities(self):
        activities = [SAMPLE_RUNNING_ACTIVITY, SAMPLE_STRENGTH_ACTIVITY]
        result = parse_activities(activities)

        assert len(result) == 2
        assert result[0]['type'] == 'running'
        assert result[1]['type'] == 'strength_training'

    def test_handles_empty_list(self):
        result = parse_activities([])
        assert result == []


class TestParsePersonalRecords:
    def test_parses_time_based_record(self):
        result = parse_personal_records(SAMPLE_PR_DATA)

        five_k = next(r for r in result if '5k' in r['record_type'])
        assert five_k['value'] == 1320
        assert five_k['value_formatted'] == '22:00'
        assert five_k['date'] == '2025-06-15'
        assert five_k['activity_id'] == 11111111111

    def test_formats_hours_correctly(self):
        pr_with_hours = {
            'personalRecords': [{
                'prTypeLabelKey': 'pr_running_fastest_marathon_time',
                'value': 12600,  # 3:30:00
                'unitKey': 'time',
            }]
        }
        result = parse_personal_records(pr_with_hours)

        assert result[0]['value_formatted'] == '3:30:00'

    def test_handles_distance_record(self):
        result = parse_personal_records(SAMPLE_PR_DATA)

        longest = next(r for r in result if 'distance' in r['record_type'])
        assert longest['value'] == 21100
        assert longest['value_formatted'] == 21100  # Not time, so unchanged

    def test_handles_empty_records(self):
        result = parse_personal_records({'personalRecords': []})
        assert result == []

    def test_handles_missing_key(self):
        result = parse_personal_records({})
        assert result == []


class TestParseTrainingReadiness:
    def test_parses_readiness_data(self):
        result = parse_training_readiness(SAMPLE_TRAINING_READINESS)

        assert result['date'] == '2025-12-01'
        assert result['score'] == 72
        assert result['level'] == 'HIGH'
        assert result['sleep_score'] == 85
        assert result['recovery_time_hrs'] == 12
        assert result['hrv_status'] == 'BALANCED'
        assert result['acute_load'] == 450.5
        assert 'ready for a hard workout' in result['feedback']

    def test_handles_list_response(self):
        # Some API responses come as a list
        result = parse_training_readiness([SAMPLE_TRAINING_READINESS])

        assert result['score'] == 72
        assert result['level'] == 'HIGH'

    def test_handles_empty_data(self):
        # Empty dict is falsy, returns error
        result = parse_training_readiness({})

        assert 'error' in result

    def test_handles_none(self):
        result = parse_training_readiness(None)

        assert 'error' in result


class TestCalculateBaseline:
    def test_calculates_weekly_volume(self):
        result = calculate_baseline(SAMPLE_PARSED_ACTIVITIES)

        # Week 1 (Nov 25-28): 45 + 60 + 30 = 135 mins = 2.25 hrs
        # Week 2 (Dec 1-5): 60 + 90 + 45 + 75 = 270 mins = 4.5 hrs
        # Avg = (2.25 + 4.5) / 2 = 3.375 hrs
        assert result['avg_weekly_volume_hrs'] == 3.4  # Rounded to 1 decimal
        assert result['max_weekly_volume_hrs'] == 4.5

    def test_counts_activity_types(self):
        result = calculate_baseline(SAMPLE_PARSED_ACTIVITIES)

        assert result['activity_distribution']['running'] == 4
        assert result['activity_distribution']['strength_training'] == 2
        assert result['activity_distribution']['cycling'] == 1

    def test_calculates_typical_week(self):
        result = calculate_baseline(SAMPLE_PARSED_ACTIVITIES)

        # 4 runs over 2 weeks = 2.0 per week
        # 2 strength over 2 weeks = 1.0 per week
        # 1 cycling over 2 weeks = 0.5 per week
        assert result['typical_week']['running'] == 2.0
        assert result['typical_week']['strength_training'] == 1.0
        assert result['typical_week']['cycling'] == 0.5

    def test_tracks_weeks_analyzed(self):
        result = calculate_baseline(SAMPLE_PARSED_ACTIVITIES)

        assert result['weeks_analyzed'] == 2
        assert result['total_activities'] == 7

    def test_handles_empty_list(self):
        result = calculate_baseline([])

        assert result['avg_weekly_volume_hrs'] == 0
        assert result['max_weekly_volume_hrs'] == 0
        assert result['activity_distribution'] == {}
        assert result['typical_week'] == {}
        assert result['total_activities'] == 0

    def test_handles_missing_duration(self):
        activities = [
            {'date': '2025-12-01', 'type': 'running', 'duration_mins': None},
            {'date': '2025-12-02', 'type': 'running', 'duration_mins': 30.0},
        ]
        result = calculate_baseline(activities)

        # None should be treated as 0
        assert result['avg_weekly_volume_hrs'] == 0.5  # 30 mins = 0.5 hrs

    def test_handles_invalid_date(self):
        activities = [
            {'date': 'invalid-date', 'type': 'running', 'duration_mins': 30.0},
            {'date': '2025-12-01', 'type': 'running', 'duration_mins': 60.0},
        ]
        result = calculate_baseline(activities)

        # Invalid date should be skipped for week calculation
        assert result['total_activities'] == 2
        assert result['weeks_analyzed'] == 1


class TestCheckSetup:
    """Test check_setup() returns True/False based on data file existence."""

    def test_returns_true_when_all_files_exist(self, tmp_path, monkeypatch):
        """check_setup returns True when athlete.json and training_config.json exist."""
        import parsers
        monkeypatch.setattr(parsers, 'DATA_DIR', tmp_path)

        # Create the required files
        (tmp_path / "athlete.json").write_text("{}")
        (tmp_path / "training_config.json").write_text("{}")

        assert check_setup() is True

    def test_returns_false_when_athlete_missing(self, tmp_path, monkeypatch):
        """check_setup returns False when athlete.json is missing."""
        import parsers
        monkeypatch.setattr(parsers, 'DATA_DIR', tmp_path)

        # Only create training_config.json
        (tmp_path / "training_config.json").write_text("{}")

        assert check_setup() is False

    def test_returns_false_when_training_config_missing(self, tmp_path, monkeypatch):
        """check_setup returns False when training_config.json is missing."""
        import parsers
        monkeypatch.setattr(parsers, 'DATA_DIR', tmp_path)

        # Only create athlete.json
        (tmp_path / "athlete.json").write_text("{}")

        assert check_setup() is False

    def test_returns_false_when_both_missing(self, tmp_path, monkeypatch):
        """check_setup returns False when both required files are missing."""
        import parsers
        monkeypatch.setattr(parsers, 'DATA_DIR', tmp_path)

        # Create no files - empty directory
        assert check_setup() is False

    def test_returns_true_with_extra_files(self, tmp_path, monkeypatch):
        """check_setup returns True even when extra files exist alongside required ones."""
        import parsers
        monkeypatch.setattr(parsers, 'DATA_DIR', tmp_path)

        # Create required files plus extras
        (tmp_path / "athlete.json").write_text("{}")
        (tmp_path / "training_config.json").write_text("{}")
        (tmp_path / "methodology.json").write_text("{}")
        (tmp_path / "extra_stuff.txt").write_text("hello")

        assert check_setup() is True


class TestParseUserProfile:
    """Tests for parse_user_profile — Garmin profile data extraction."""

    def test_full_data(self):
        """Parses name, weight, age from all three Garmin responses."""
        result = parse_user_profile(
            full_name={'firstName': 'John', 'lastName': 'Doe', 'displayName': 'johndoe'},
            user_profile={'userData': {'birthDate': '1990-06-15'}},
            body_composition={'totalAverage': {'weight': 75000}},
        )
        assert result['full_name'] == 'John Doe'
        assert result['display_name'] == 'johndoe'
        assert result['weight_kg'] == 75.0
        assert result['birth_date'] == '1990-06-15'
        assert isinstance(result['age'], int)
        assert result['age'] >= 35  # Born 1990, test written 2026

    def test_empty_inputs(self):
        """Returns all None when given no data."""
        result = parse_user_profile()
        assert result['full_name'] is None
        assert result['weight_kg'] is None
        assert result['age'] is None

    def test_none_inputs(self):
        """Handles None for all arguments."""
        result = parse_user_profile(None, None, None)
        assert result['full_name'] is None
        assert result['weight_kg'] is None

    def test_partial_name(self):
        """Handles missing last name."""
        result = parse_user_profile(full_name={'firstName': 'John'})
        assert result['full_name'] == 'John'

    def test_weight_conversion_from_grams(self):
        """Converts Garmin grams to kg correctly."""
        result = parse_user_profile(
            body_composition={'totalAverage': {'weight': 82300}}
        )
        assert result['weight_kg'] == 82.3

    def test_zero_weight_ignored(self):
        """Zero weight is treated as missing."""
        result = parse_user_profile(
            body_composition={'totalAverage': {'weight': 0}}
        )
        assert result['weight_kg'] is None

    def test_birth_date_without_user_data_wrapper(self):
        """Handles user profile without userData wrapper (flat structure)."""
        result = parse_user_profile(
            user_profile={'birthDate': '1985-03-20'}
        )
        assert result['birth_date'] == '1985-03-20'
        assert result['age'] >= 40

    def test_invalid_birth_date(self):
        """Invalid birth date doesn't crash, age stays None."""
        result = parse_user_profile(
            user_profile={'userData': {'birthDate': 'not-a-date'}}
        )
        assert result['birth_date'] == 'not-a-date'
        assert result['age'] is None

    def test_display_name_fallback_from_user_profile(self):
        """Falls back to displayName from user profile when full_name has none."""
        result = parse_user_profile(
            user_profile={'userData': {'displayName': 'athlete42'}}
        )
        assert result['display_name'] == 'athlete42'
