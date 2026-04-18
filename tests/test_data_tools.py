"""Tests for tools/data_tools.py — integration tests for the daily metrics composition.

Only tests get_daily_metrics (composes 3 API calls + 3 parsers). The other
tools (get_activities_range, get_personal_records) are thin wrappers around
parsers already tested in test_parsers.py.
"""
import json
from unittest.mock import Mock, patch


SAMPLE_READINESS = [{
    'calendarDate': '2026-01-15',
    'score': 72,
    'level': 'HIGH',
    'sleepScore': 85,
    'recoveryTimeInHours': 12,
    'hrvStatus': 'BALANCED',
    'acuteLoad': 450.5,
    'feedbackPhrase': 'Ready to push.',
}]


class TestGetDailyMetrics:
    @patch('coach.tools.data_tools.garmin_api_call')
    def test_composes_three_api_calls_into_one_result(self, mock_api_call, garmin_fixtures):
        from coach.tools.data_tools import get_daily_metrics

        mock_client = Mock()
        mock_client.get_user_summary.return_value = garmin_fixtures["user_summary"]
        mock_client.get_body_battery.return_value = garmin_fixtures["body_battery"]
        mock_client.get_training_readiness.return_value = SAMPLE_READINESS
        mock_api_call.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        result = json.loads(get_daily_metrics())

        assert result['rhr'] == 40
        assert result['body_battery'] == 33
        assert result['sleep_score'] == 85
        assert 'date' in result

    @patch('coach.tools.data_tools.garmin_api_call')
    def test_handles_empty_responses_gracefully(self, mock_api_call):
        from coach.tools.data_tools import get_daily_metrics

        mock_client = Mock()
        mock_client.get_user_summary.return_value = {}
        mock_client.get_body_battery.return_value = []
        mock_client.get_training_readiness.return_value = []
        mock_api_call.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        result = json.loads(get_daily_metrics())

        assert result['rhr'] == 'N/A'
        assert result['body_battery'] == 'N/A'
        assert result['sleep_score'] == 'N/A'

    @patch('coach.tools.data_tools.garmin_api_call')
    def test_sleep_score_falls_back_when_readiness_missing_field(self, mock_api_call, garmin_fixtures):
        from coach.tools.data_tools import get_daily_metrics

        mock_client = Mock()
        mock_client.get_user_summary.return_value = garmin_fixtures["user_summary"]
        mock_client.get_body_battery.return_value = garmin_fixtures["body_battery"]
        mock_client.get_training_readiness.return_value = [{
            'calendarDate': '2026-01-15',
            'score': 72,
            'level': 'HIGH',
        }]
        mock_api_call.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        result = json.loads(get_daily_metrics())

        assert result['sleep_score'] == 'N/A'
        assert result['rhr'] == 40

    @patch('coach.tools.data_tools.garmin_api_call')
    def test_api_error_returns_error_json(self, mock_api_call):
        from coach.tools.data_tools import get_daily_metrics

        mock_api_call.side_effect = Exception("Connection timeout")
        result = json.loads(get_daily_metrics())

        assert 'error' in result
        assert 'Connection timeout' in result['error']


class TestGetActivitiesRangeDateValidation:
    """Tests for date validation in get_activities_range."""

    def test_invalid_start_date_returns_error(self):
        from coach.tools.data_tools import get_activities_range
        result = json.loads(get_activities_range("not-a-date"))
        assert 'error' in result
        assert 'start_date' in result['error']

    def test_invalid_end_date_returns_error(self):
        from coach.tools.data_tools import get_activities_range
        result = json.loads(get_activities_range("2026-01-01", "bad-date"))
        assert 'error' in result
        assert 'end_date' in result['error']

    @patch('coach.tools.data_tools.garmin_api_call')
    def test_valid_dates_proceed_normally(self, mock_api_call):
        from coach.tools.data_tools import get_activities_range
        mock_api_call.return_value = []
        result = json.loads(get_activities_range("2026-01-01", "2026-01-07"))
        assert 'error' not in result
