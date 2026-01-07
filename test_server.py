"""
Tests for Garmin MCP server parsing functions.
Uses real API response data captured in test_fixtures.json
"""
import json
import pytest
from pathlib import Path
from unittest.mock import Mock, patch
from server import (
    parse_resting_heart_rate,
    parse_sleep_score,
    parse_body_battery,
    parse_activity,
    parse_activities,
    parse_training_readiness,
    parse_personal_records,
    calculate_baseline,
)


# Load real fixture data
FIXTURES_PATH = Path(__file__).parent / "test_fixtures.json"
with open(FIXTURES_PATH) as f:
    FIXTURES = json.load(f)

REAL_STATS = FIXTURES["user_summary"]
REAL_BODY_BATTERY = FIXTURES["body_battery"]


class TestParseRestingHeartRateWithRealData:
    def test_parses_real_garmin_response(self):
        """Test against actual Garmin API response from 2025-12-01."""
        result = parse_resting_heart_rate(REAL_STATS)
        assert result == 40  # Actual RHR from that day

    def test_missing_key_returns_na(self):
        stats_without_rhr = {k: v for k, v in REAL_STATS.items() if k != 'restingHeartRate'}
        assert parse_resting_heart_rate(stats_without_rhr) == 'N/A'

    def test_empty_dict_returns_na(self):
        assert parse_resting_heart_rate({}) == 'N/A'


class TestParseSleepScoreWithRealData:
    def test_parses_real_garmin_response(self):
        """Test against actual Garmin API response from 2025-12-01."""
        result = parse_sleep_score(REAL_STATS)
        # sleepScore key not present in this response, so returns N/A
        assert result == 'N/A'

    def test_returns_value_when_present(self):
        stats_with_sleep = {**REAL_STATS, 'sleepScore': 85}
        assert parse_sleep_score(stats_with_sleep) == 85

    def test_empty_dict_returns_na(self):
        assert parse_sleep_score({}) == 'N/A'


class TestParseBodyBatteryWithRealData:
    def test_parses_real_garmin_response(self):
        """Test against actual Garmin API response from 2025-12-01."""
        result = parse_body_battery(REAL_BODY_BATTERY)
        assert result == 33  # Last body battery value from that day

    def test_gets_last_value_not_first(self):
        """Verify we get the most recent (last) reading."""
        # From fixture: values are [40, 99, 85, 57, 57, 33]
        # First value is 40, last is 33
        result = parse_body_battery(REAL_BODY_BATTERY)
        assert result != 40  # Not the first value
        assert result == 33  # The last value

    def test_empty_list_returns_na(self):
        assert parse_body_battery([]) == 'N/A'

    def test_none_returns_na(self):
        assert parse_body_battery(None) == 'N/A'

    def test_missing_values_array_returns_na(self):
        malformed = [{"date": "2025-12-01"}]  # No bodyBatteryValuesArray
        assert parse_body_battery(malformed) == 'N/A'

    def test_empty_values_array_returns_na(self):
        empty_values = [{"date": "2025-12-01", "bodyBatteryValuesArray": []}]
        assert parse_body_battery(empty_values) == 'N/A'


class TestGetDailyMetricsIntegration:
    @patch('server.get_garmin_client')
    def test_returns_json_with_real_data(self, mock_get_client):
        """Test full integration using real fixture data."""
        from server import get_daily_metrics
        import json

        mock_client = Mock()
        mock_client.get_user_summary.return_value = REAL_STATS
        mock_client.get_body_battery.return_value = REAL_BODY_BATTERY
        mock_get_client.return_value = mock_client

        result = json.loads(get_daily_metrics())

        assert result['rhr'] == 40
        assert result['body_battery'] == 33
        # sleepScore not present in fixture, shows as N/A
        assert result['sleep_score'] == 'N/A'
        assert 'date' in result

    @patch('server.get_garmin_client')
    def test_handles_api_error(self, mock_get_client):
        from server import get_daily_metrics
        import json

        mock_get_client.side_effect = Exception("Connection timeout")
        result = json.loads(get_daily_metrics())

        assert 'error' in result
        assert 'Connection timeout' in result['error']

    @patch('server.get_garmin_client')
    def test_handles_empty_responses(self, mock_get_client):
        from server import get_daily_metrics
        import json

        mock_client = Mock()
        mock_client.get_user_summary.return_value = {}
        mock_client.get_body_battery.return_value = []
        mock_get_client.return_value = mock_client

        result = json.loads(get_daily_metrics())

        assert result['rhr'] == 'N/A'
        assert result['body_battery'] == 'N/A'
        assert result['sleep_score'] == 'N/A'


# Sample activity data matching Garmin API structure
SAMPLE_RUNNING_ACTIVITY = {
    'activityId': 12345678901,
    'activityName': 'Morning Run',
    'startTimeLocal': '2025-12-01T06:30:00.0',
    'activityType': {
        'typeId': 1,
        'typeKey': 'running',
        'parentTypeId': 17,
    },
    'duration': 2700,  # 45 mins in seconds
    'distance': 8000,  # 8km in meters
    'averageHR': 145,
    'maxHR': 168,
    'calories': 520,
}

SAMPLE_STRENGTH_ACTIVITY = {
    'activityId': 12345678902,
    'activityName': 'Strength Training',
    'startTimeLocal': '2025-12-02T17:00:00.0',
    'activityType': {
        'typeId': 13,
        'typeKey': 'strength_training',
        'parentTypeId': 29,
    },
    'duration': 3600,  # 60 mins
    'distance': None,
    'averageHR': 110,
    'maxHR': 135,
    'calories': 380,
}


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


class TestGetActivitiesRangeIntegration:
    @patch('server.get_garmin_client')
    def test_returns_json_array(self, mock_get_client):
        from server import get_activities_range

        mock_client = Mock()
        mock_client.get_activities_by_date.return_value = [
            SAMPLE_RUNNING_ACTIVITY,
            SAMPLE_STRENGTH_ACTIVITY,
        ]
        mock_get_client.return_value = mock_client

        result = get_activities_range('2025-12-01', '2025-12-02')
        parsed = json.loads(result)

        assert isinstance(parsed, list)
        assert len(parsed) == 2
        assert parsed[0]['type'] == 'running'

    @patch('server.get_garmin_client')
    def test_handles_api_error(self, mock_get_client):
        from server import get_activities_range

        mock_get_client.side_effect = Exception("API timeout")
        result = get_activities_range('2025-12-01')
        parsed = json.loads(result)

        assert 'error' in parsed
        assert 'API timeout' in parsed['error']


# Sample personal records data matching Garmin API structure
SAMPLE_PR_DATA = {
    'personalRecords': [
        {
            'prTypeLabelKey': 'pr_running_fastest_5k_time',
            'value': 1320,  # 22:00 in seconds
            'unitKey': 'time',
            'prStartTimeGmtFormatted': '2025-06-15T08:30:00.0',
            'activityId': 11111111111,
        },
        {
            'prTypeLabelKey': 'pr_running_fastest_10k_time',
            'value': 2820,  # 47:00 in seconds
            'unitKey': 'time',
            'prStartTimeGmtFormatted': '2025-09-22T07:00:00.0',
            'activityId': 22222222222,
        },
        {
            'prTypeLabelKey': 'pr_running_longest_distance',
            'value': 21100,  # 21.1 km
            'unitKey': 'meter',
            'prStartTimeGmtFormatted': '2025-10-10T06:00:00.0',
            'activityId': 33333333333,
        },
    ]
}


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


class TestGetPersonalRecordsIntegration:
    @patch('server.get_garmin_client')
    def test_returns_json_array(self, mock_get_client):
        from server import get_personal_records

        mock_client = Mock()
        mock_client.get_personal_record.return_value = SAMPLE_PR_DATA
        mock_get_client.return_value = mock_client

        result = get_personal_records()
        parsed = json.loads(result)

        assert isinstance(parsed, list)
        assert len(parsed) == 3

    @patch('server.get_garmin_client')
    def test_handles_api_error(self, mock_get_client):
        from server import get_personal_records

        mock_get_client.side_effect = Exception("Auth failed")
        result = get_personal_records()
        parsed = json.loads(result)

        assert 'error' in parsed


# Sample training readiness data matching Garmin API structure
SAMPLE_TRAINING_READINESS = {
    'calendarDate': '2025-12-01',
    'score': 72,
    'level': 'HIGH',
    'sleepScore': 85,
    'recoveryTimeInHours': 12,
    'hrvStatus': 'BALANCED',
    'acuteLoad': 450.5,
    'feedbackPhrase': 'Your body is well recovered and ready for a hard workout.',
}


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


class TestGetTrainingReadinessIntegration:
    @patch('server.get_garmin_client')
    def test_returns_json_object(self, mock_get_client):
        from server import get_training_readiness

        mock_client = Mock()
        mock_client.get_training_readiness.return_value = SAMPLE_TRAINING_READINESS
        mock_get_client.return_value = mock_client

        result = get_training_readiness('2025-12-01')
        parsed = json.loads(result)

        assert parsed['score'] == 72
        assert parsed['level'] == 'HIGH'

    @patch('server.get_garmin_client')
    def test_handles_api_error(self, mock_get_client):
        from server import get_training_readiness

        mock_get_client.side_effect = Exception("Network error")
        result = get_training_readiness()
        parsed = json.loads(result)

        assert 'error' in parsed


# Sample parsed activities for baseline testing
SAMPLE_PARSED_ACTIVITIES = [
    {'date': '2025-11-25', 'type': 'running', 'duration_mins': 45.0},
    {'date': '2025-11-26', 'type': 'strength_training', 'duration_mins': 60.0},
    {'date': '2025-11-28', 'type': 'running', 'duration_mins': 30.0},
    {'date': '2025-12-01', 'type': 'running', 'duration_mins': 60.0},
    {'date': '2025-12-02', 'type': 'cycling', 'duration_mins': 90.0},
    {'date': '2025-12-03', 'type': 'strength_training', 'duration_mins': 45.0},
    {'date': '2025-12-05', 'type': 'running', 'duration_mins': 75.0},
]


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


class TestRefreshAthleteBaselineIntegration:
    @patch('server.get_garmin_client')
    def test_returns_success_summary(self, mock_get_client, tmp_path):
        from server import refresh_athlete_baseline, DATA_DIR
        import server

        # Temporarily redirect DATA_DIR to tmp_path
        original_data_dir = server.DATA_DIR
        server.DATA_DIR = tmp_path

        try:
            mock_client = Mock()
            mock_client.get_activities_by_date.return_value = [
                SAMPLE_RUNNING_ACTIVITY,
                SAMPLE_STRENGTH_ACTIVITY,
            ]
            mock_client.get_personal_record.return_value = SAMPLE_PR_DATA
            mock_get_client.return_value = mock_client

            result = refresh_athlete_baseline()
            parsed = json.loads(result)

            assert parsed['status'] == 'success'
            assert parsed['activities_analyzed'] == 2
            assert parsed['personal_records_count'] == 3
            assert 'avg_weekly_volume_hrs' in parsed
        finally:
            server.DATA_DIR = original_data_dir

    @patch('server.get_garmin_client')
    def test_creates_baseline_file(self, mock_get_client, tmp_path):
        from server import refresh_athlete_baseline
        import server

        original_data_dir = server.DATA_DIR
        server.DATA_DIR = tmp_path

        try:
            mock_client = Mock()
            mock_client.get_activities_by_date.return_value = [SAMPLE_RUNNING_ACTIVITY]
            mock_client.get_personal_record.return_value = SAMPLE_PR_DATA
            mock_get_client.return_value = mock_client

            refresh_athlete_baseline()

            # Now saves to athlete_baseline.json instead of athlete_profile.json
            profile_path = tmp_path / 'athlete_baseline.json'
            assert profile_path.exists()

            with open(profile_path) as f:
                profile = json.load(f)

            assert 'baseline' in profile
            assert 'personal_records' in profile
            assert 'last_refreshed' in profile
        finally:
            server.DATA_DIR = original_data_dir

    @patch('server.get_garmin_client')
    def test_handles_api_error(self, mock_get_client):
        from server import refresh_athlete_baseline

        mock_get_client.side_effect = Exception("Auth failed")
        result = refresh_athlete_baseline()
        parsed = json.loads(result)

        assert 'error' in parsed
        assert 'Auth failed' in parsed['error']
