"""
Tests for parsers.py - pure parsing functions for Garmin API responses.

These tests have zero MCP dependency and test pure data transformations.
"""
import json
import pytest
from datetime import datetime
from coach.parsers import (
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
    parse_hr_zones,
    parse_hr_time_in_zones,
    build_current_time_context,
    parse_hrv_data,
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



class TestParseSleepScoreWithRealData:
    def test_parses_real_garmin_response(self, garmin_fixtures):
        result = parse_sleep_score(garmin_fixtures["user_summary"])
        assert result == 'N/A'

    def test_returns_value_when_present(self, garmin_fixtures):
        stats_with_sleep = {**garmin_fixtures["user_summary"], 'sleepScore': 85}
        assert parse_sleep_score(stats_with_sleep) == 85



class TestBuildCurrentTimeContext:
    @pytest.mark.parametrize("hour,expected_period", [
        (3, 'night'),
        (4, 'early_morning'),
        (7, 'early_morning'),
        (8, 'morning'),
        (11, 'morning'),
        (12, 'afternoon'),
        (16, 'afternoon'),
        (17, 'evening'),
        (20, 'evening'),
        (21, 'night'),
        (23, 'night'),
        (0, 'night'),
    ])
    def test_time_period_boundaries(self, hour, expected_period):
        now = datetime(2026, 4, 15, hour, 30)
        result = build_current_time_context(now)
        assert result['time_period'] == expected_period
        assert result['hour'] == hour

    def test_returns_all_expected_keys(self):
        result = build_current_time_context(datetime(2026, 4, 15, 10, 30))
        expected_keys = {
            'timestamp', 'date', 'day_of_week', 'hour', 'minute',
            'time_period', 'is_weekend', 'timezone_note',
        }
        assert set(result.keys()) == expected_keys

    def test_weekend_detection(self):
        saturday = build_current_time_context(datetime(2026, 4, 18, 10, 0))
        sunday = build_current_time_context(datetime(2026, 4, 19, 10, 0))
        monday = build_current_time_context(datetime(2026, 4, 20, 10, 0))
        friday = build_current_time_context(datetime(2026, 4, 17, 10, 0))
        assert saturday['is_weekend'] is True
        assert sunday['is_weekend'] is True
        assert monday['is_weekend'] is False
        assert friday['is_weekend'] is False

    def test_day_of_week_string(self):
        wednesday = build_current_time_context(datetime(2026, 4, 15, 10, 0))
        assert wednesday['day_of_week'] == 'Wednesday'
        assert wednesday['date'] == '2026-04-15'

    def test_timestamp_format(self):
        result = build_current_time_context(datetime(2026, 4, 15, 10, 30, 45))
        assert result['timestamp'] == '2026-04-15T10:30:45'

    def test_default_uses_now(self):
        # Should not raise, returns populated dict
        result = build_current_time_context()
        assert result['hour'] is not None
        assert result['time_period'] in ('early_morning', 'morning', 'afternoon', 'evening', 'night')


class TestParseHRVData:
    SAMPLE_HRV = {
        'hrvSummary': {
            'calendarDate': '2026-04-18',
            'status': 'BALANCED',
            'lastNightAvg': 58,
            'lastNight5MinHigh': 72,
            'weeklyAvg': 55,
            'baseline': {
                'lowUpper': 45,
                'balancedLow': 50,
                'balancedUpper': 70,
                'markerValue': 58,
            },
            'feedbackPhrase': 'Your HRV is within baseline — good recovery.',
        }
    }

    def test_parses_summary(self):
        result = parse_hrv_data(self.SAMPLE_HRV)
        assert result['status'] == 'BALANCED'
        assert result['last_night_avg'] == 58
        assert result['weekly_avg'] == 55
        assert result['baseline_low'] == 45
        assert result['feedback'].startswith('Your HRV')

    def test_none_returns_none(self):
        assert parse_hrv_data(None) is None

    def test_empty_dict_returns_none(self):
        assert parse_hrv_data({}) is None

    def test_missing_summary_returns_none(self):
        assert parse_hrv_data({'some_other_key': 'value'}) is None


class TestTrainingReadinessHRVOverlay:
    def test_overlay_fills_null_hrv_status(self):
        readiness = {'score': 72, 'level': 'HIGH', 'hrvStatus': None,
                     'sleepScore': 85}
        hrv = {'hrvSummary': {'status': 'BALANCED', 'lastNightAvg': 58,
                               'weeklyAvg': 55, 'baseline': {'lowUpper': 45}}}
        result = parse_training_readiness(readiness, hrv_data=hrv)
        assert result['hrv_status'] == 'BALANCED'
        assert result['hrv_last_night_avg'] == 58

    def test_readiness_hrv_status_preserved_when_present(self):
        readiness = {'score': 72, 'hrvStatus': 'UNBALANCED'}
        hrv = {'hrvSummary': {'status': 'BALANCED', 'lastNightAvg': 50}}
        result = parse_training_readiness(readiness, hrv_data=hrv)
        # readiness hrv_status wins when not null
        assert result['hrv_status'] == 'UNBALANCED'
        # HRV detail still overlaid
        assert result['hrv_last_night_avg'] == 50

    def test_no_hrv_data_still_parses_readiness(self):
        readiness = {'score': 72, 'level': 'HIGH'}
        result = parse_training_readiness(readiness)
        assert result['score'] == 72
        assert 'hrv_last_night_avg' not in result

    def test_empty_readiness_with_hrv_returns_overlay(self):
        hrv = {'hrvSummary': {'status': 'LOW', 'lastNightAvg': 30}}
        result = parse_training_readiness({}, hrv_data=hrv)
        assert result['hrv_status'] == 'LOW'
        assert result['hrv_last_night_avg'] == 30


class TestParseBodyBatteryWithRealData:
    def test_parses_real_garmin_response(self, garmin_fixtures):
        result = parse_body_battery(garmin_fixtures["body_battery"])
        assert result == 33

    def test_gets_last_value_not_first(self, garmin_fixtures):
        result = parse_body_battery(garmin_fixtures["body_battery"])
        assert result != 40
        assert result == 33



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


class TestGarminTrainingLoadParsing:
    def test_garmin_training_load_extracted(self):
        """Raw activity with activityTrainingLoad → parsed as garmin_training_load."""
        activity = {
            **SAMPLE_RUNNING_ACTIVITY,
            'activityTrainingLoad': 127.5,
        }
        result = parse_activity(activity)
        assert result['garmin_training_load'] == 127.5

    def test_garmin_training_load_none_when_absent(self):
        """Missing activityTrainingLoad → None."""
        result = parse_activity(SAMPLE_RUNNING_ACTIVITY)
        assert result['garmin_training_load'] is None


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

    def test_handles_list_input(self):
        """Garmin API returns a list directly, not a dict with personalRecords key."""
        pr_list = [
            {'prTypeLabelKey': 'pr_running_fastest_5k_time', 'value': 1320,
             'unitKey': 'time', 'prStartTimeGmtFormatted': '2025-06-15 08:00:00',
             'activityId': 11111111111},
            {'prTypeLabelKey': 'pr_running_longest_distance', 'value': 21100,
             'unitKey': 'distance'},
        ]
        result = parse_personal_records(pr_list)
        assert len(result) == 2
        assert result[0]['record_type'] == 'pr_running_fastest_5k_time'
        assert result[0]['value_formatted'] == '22:00'

    def test_handles_none_type_key(self):
        """Records with no type key default to 'unknown'."""
        result = parse_personal_records([{'value': 42}])
        assert len(result) == 1
        assert result[0]['record_type'] == 'unknown'

    def test_handles_none_input(self):
        """None input returns empty list."""
        result = parse_personal_records(None)
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
        import coach.parsers as parsers
        monkeypatch.setattr(parsers, 'DATA_DIR', tmp_path)

        # Create the required files
        (tmp_path / "athlete.json").write_text("{}")
        (tmp_path / "training_config.json").write_text("{}")

        assert check_setup() is True

    def test_returns_false_when_athlete_missing(self, tmp_path, monkeypatch):
        """check_setup returns False when athlete.json is missing."""
        import coach.parsers as parsers
        monkeypatch.setattr(parsers, 'DATA_DIR', tmp_path)

        # Only create training_config.json
        (tmp_path / "training_config.json").write_text("{}")

        assert check_setup() is False

    def test_returns_false_when_training_config_missing(self, tmp_path, monkeypatch):
        """check_setup returns False when training_config.json is missing."""
        import coach.parsers as parsers
        monkeypatch.setattr(parsers, 'DATA_DIR', tmp_path)

        # Only create athlete.json
        (tmp_path / "athlete.json").write_text("{}")

        assert check_setup() is False

    def test_returns_false_when_both_missing(self, tmp_path, monkeypatch):
        """check_setup returns False when both required files are missing."""
        import coach.parsers as parsers
        monkeypatch.setattr(parsers, 'DATA_DIR', tmp_path)

        # Create no files - empty directory
        assert check_setup() is False

    def test_returns_true_with_extra_files(self, tmp_path, monkeypatch):
        """check_setup returns True even when extra files exist alongside required ones."""
        import coach.parsers as parsers
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
        """Parses name, weight, weight_date, age, max_hr from all three Garmin responses."""
        result = parse_user_profile(
            full_name={'firstName': 'John', 'lastName': 'Doe', 'displayName': 'johndoe'},
            user_profile={'userData': {'birthDate': '1990-06-15', 'maxHeartRate': 192}},
            body_composition={
                'dateWeightList': [{'calendarDate': '2026-02-07', 'weight': 75000}],
                'totalAverage': {'weight': 75000},
            },
        )
        assert result['full_name'] == 'John Doe'
        assert result['display_name'] == 'johndoe'
        assert result['weight_kg'] == 75.0
        assert result['weight_date'] == '2026-02-07'
        assert result['birth_date'] == '1990-06-15'
        assert isinstance(result['age'], int)
        assert result['age'] >= 35  # Born 1990, test written 2026
        assert result['max_hr'] == 192

    def test_max_hr_extracted(self):
        """Extracts maxHeartRate from user profile."""
        result = parse_user_profile(
            user_profile={'userData': {'maxHeartRate': 185}}
        )
        assert result['max_hr'] == 185

    def test_max_hr_none_when_absent(self):
        """Missing maxHeartRate returns None."""
        result = parse_user_profile(
            user_profile={'userData': {'birthDate': '1990-01-01'}}
        )
        assert result['max_hr'] is None

    def test_max_hr_zero_ignored(self):
        """Zero maxHeartRate treated as missing."""
        result = parse_user_profile(
            user_profile={'userData': {'maxHeartRate': 0}}
        )
        assert result['max_hr'] is None

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

    def test_weight_from_date_list_uses_most_recent(self):
        """Uses the most recent entry from dateWeightList."""
        result = parse_user_profile(
            body_composition={
                'dateWeightList': [
                    {'calendarDate': '2026-01-20', 'weight': 80000},
                    {'calendarDate': '2026-02-05', 'weight': 82300},
                    {'calendarDate': '2026-01-28', 'weight': 81000},
                ],
            }
        )
        assert result['weight_kg'] == 82.3
        assert result['weight_date'] == '2026-02-05'

    def test_weight_falls_back_to_total_average(self):
        """Falls back to totalAverage when dateWeightList is empty."""
        result = parse_user_profile(
            body_composition={'dateWeightList': [], 'totalAverage': {'weight': 82300}}
        )
        assert result['weight_kg'] == 82.3
        assert result['weight_date'] is None

    def test_zero_weight_ignored(self):
        """Zero weight entries are skipped."""
        result = parse_user_profile(
            body_composition={
                'dateWeightList': [{'calendarDate': '2026-02-01', 'weight': 0}],
                'totalAverage': {'weight': 0},
            }
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


class TestParseHrZones:
    """Tests for parse_hr_zones — Garmin biometric HR zone parsing."""

    SAMPLE_GARMIN_ZONES = [{
        "trainingMethod": "HR_MAX",
        "restingHeartRateUsed": None,
        "lactateThresholdHeartRateUsed": 170,
        "zone1Floor": 100,
        "zone2Floor": 120,
        "zone3Floor": 140,
        "zone4Floor": 155,
        "zone5Floor": 170,
        "maxHeartRateUsed": 190,
        "sport": "DEFAULT",
        "changeState": "UNCHANGED",
    }]

    def test_parses_default_zones(self):
        """Extracts 5 HR zones from Garmin biometric response."""
        result = parse_hr_zones(self.SAMPLE_GARMIN_ZONES)
        assert result['z1_recovery'] == [100, 119]
        assert result['z2_aerobic'] == [120, 139]
        assert result['z3_tempo'] == [140, 154]
        assert result['z4_threshold'] == [155, 169]
        assert result['z5_max'] == [170, 190]

    def test_includes_max_hr_and_lthr(self):
        """Includes max_hr and LTHR in output."""
        result = parse_hr_zones(self.SAMPLE_GARMIN_ZONES)
        assert result['max_hr'] == 190
        assert result['lthr'] == 170

    def test_empty_list_returns_none(self):
        assert parse_hr_zones([]) is None

    def test_none_returns_none(self):
        assert parse_hr_zones(None) is None

    def test_missing_zone_floor_returns_none(self):
        """If any zone floor is missing, returns None."""
        incomplete = [{"sport": "DEFAULT", "maxHeartRateUsed": 190,
                       "zone1Floor": 100, "zone2Floor": 120}]
        assert parse_hr_zones(incomplete) is None

    def test_picks_default_sport(self):
        """When multiple sport entries exist, picks DEFAULT."""
        multi = [
            {"sport": "RUNNING", "zone1Floor": 105, "zone2Floor": 125,
             "zone3Floor": 145, "zone4Floor": 160, "zone5Floor": 175, "maxHeartRateUsed": 195},
            {"sport": "DEFAULT", "zone1Floor": 100, "zone2Floor": 120,
             "zone3Floor": 140, "zone4Floor": 155, "zone5Floor": 170,
             "maxHeartRateUsed": 190, "lactateThresholdHeartRateUsed": 170},
        ]
        result = parse_hr_zones(multi)
        assert result['z1_recovery'] == [100, 119]
        assert result['max_hr'] == 190

    def test_string_full_name(self):
        """Garmin get_full_name() returns a plain string, not a dict."""
        result = parse_user_profile(full_name='Jane Smith')
        assert result['full_name'] == 'Jane Smith'
        assert result['display_name'] == 'Jane Smith'

    def test_empty_string_full_name(self):
        """Empty string treated as no name."""
        result = parse_user_profile(full_name='   ')
        assert result['full_name'] is None

    def test_display_name_fallback_from_user_profile(self):
        """Falls back to displayName from user profile when full_name has none."""
        result = parse_user_profile(
            user_profile={'userData': {'displayName': 'athlete42'}}
        )
        assert result['display_name'] == 'athlete42'


class TestParseHrTimeInZones:
    """Tests for parse_hr_time_in_zones() — per-activity HR time-in-zone parsing."""

    SAMPLE_5_ZONE = [
        {"zoneNumber": 1, "secsInZone": 4282.946, "zoneLowBoundary": 116},
        {"zoneNumber": 2, "secsInZone": 7981.892, "zoneLowBoundary": 131},
        {"zoneNumber": 3, "secsInZone": 2581.99, "zoneLowBoundary": 146},
        {"zoneNumber": 4, "secsInZone": 864.991, "zoneLowBoundary": 161},
        {"zoneNumber": 5, "secsInZone": 403.999, "zoneLowBoundary": 176},
    ]

    def test_valid_5_zone_response(self):
        result = parse_hr_time_in_zones(self.SAMPLE_5_ZONE)
        assert result is not None
        assert result['z1'] == round(4282.946 / 60, 1)
        assert result['z2'] == round(7981.892 / 60, 1)
        assert result['z3'] == round(2581.99 / 60, 1)
        assert result['z4'] == round(864.991 / 60, 1)
        assert result['z5'] == round(403.999 / 60, 1)

    def test_returns_none_for_empty_list(self):
        assert parse_hr_time_in_zones([]) is None

    def test_returns_none_for_none(self):
        assert parse_hr_time_in_zones(None) is None

    def test_returns_none_for_non_list(self):
        assert parse_hr_time_in_zones("not a list") is None

    def test_skips_entries_missing_zone_number(self):
        data = [
            {"secsInZone": 100, "zoneLowBoundary": 116},
            {"zoneNumber": 2, "secsInZone": 200, "zoneLowBoundary": 131},
        ]
        result = parse_hr_time_in_zones(data)
        assert result == {'z2': round(200 / 60, 1)}

    def test_skips_entries_missing_secs(self):
        data = [
            {"zoneNumber": 1, "zoneLowBoundary": 116},
            {"zoneNumber": 2, "secsInZone": 300, "zoneLowBoundary": 131},
        ]
        result = parse_hr_time_in_zones(data)
        assert 'z1' not in result
        assert result['z2'] == round(300 / 60, 1)

    def test_seconds_to_minutes_conversion(self):
        data = [{"zoneNumber": 1, "secsInZone": 3600, "zoneLowBoundary": 100}]
        result = parse_hr_time_in_zones(data)
        assert result['z1'] == 60.0

    def test_returns_none_when_all_entries_invalid(self):
        data = [{"zoneLowBoundary": 116}]
        assert parse_hr_time_in_zones(data) is None
