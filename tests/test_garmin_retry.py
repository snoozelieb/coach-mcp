"""
Tests for Garmin API retry behavior in garmin_api_call wrapper.

Verifies:
- Auth errors (GarminConnectAuthenticationError) trigger force_refresh and retry
- Persistent auth errors (bad creds) raise after one retry
- Rate limit errors (429) wait and retry
- Other exceptions propagate immediately without retry
"""
import time
import pytest
from unittest.mock import Mock, patch, call

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from coach.garmin_client import garmin_api_call, _cached_client
import coach.garmin_client as garmin_client


class TestGarminApiCallAuthRetry:
    """Test authentication error handling and retry logic."""

    @patch('coach.garmin_client.get_garmin_client')
    def test_auth_error_triggers_force_refresh_and_retries(self, mock_get_client):
        """On auth error, should invalidate cache and retry with fresh client."""
        stale_client = Mock()
        fresh_client = Mock()

        # First call returns stale client, second returns fresh
        mock_get_client.side_effect = [stale_client, fresh_client]

        # First attempt raises auth error, second succeeds
        stale_client_call_count = 0

        def mock_fn(client):
            if client is stale_client:
                raise GarminConnectAuthenticationError("Session expired")
            return {"data": "success"}

        result = garmin_api_call(mock_fn)

        assert result == {"data": "success"}
        # Should have called get_garmin_client twice: once normal, once with force_refresh
        assert mock_get_client.call_count == 2
        mock_get_client.assert_any_call()
        mock_get_client.assert_any_call(force_refresh=True)

    @patch('coach.garmin_client.get_garmin_client')
    def test_persistent_auth_error_raises_after_one_retry(self, mock_get_client):
        """If re-login also fails with auth error, should raise (no infinite loop)."""
        mock_client = Mock()
        mock_get_client.return_value = mock_client

        def mock_fn(client):
            raise GarminConnectAuthenticationError("Bad credentials")

        with pytest.raises(GarminConnectAuthenticationError):
            garmin_api_call(mock_fn)

        # Called twice: initial + one retry
        assert mock_get_client.call_count == 2


class TestGarminApiCallRateLimit:
    """Test rate limit (429) handling."""

    @patch('coach.garmin_client.time.sleep')
    @patch('coach.garmin_client.get_garmin_client')
    def test_rate_limit_waits_and_retries(self, mock_get_client, mock_sleep):
        """On 429, should wait GARMIN_RATE_LIMIT_WAIT_SECS and retry."""
        mock_client = Mock()
        mock_get_client.return_value = mock_client

        call_count = 0

        def mock_fn(client):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise GarminConnectTooManyRequestsError("Rate limited")
            return {"data": "after_wait"}

        result = garmin_api_call(mock_fn)

        assert result == {"data": "after_wait"}
        mock_sleep.assert_called_once_with(10)  # GARMIN_RATE_LIMIT_WAIT_SECS

    @patch('coach.garmin_client.time.sleep')
    @patch('coach.garmin_client.get_garmin_client')
    def test_persistent_rate_limit_raises(self, mock_get_client, mock_sleep):
        """If still rate-limited after wait, should raise."""
        mock_client = Mock()
        mock_get_client.return_value = mock_client

        def mock_fn(client):
            raise GarminConnectTooManyRequestsError("Still rate limited")

        with pytest.raises(GarminConnectTooManyRequestsError):
            garmin_api_call(mock_fn)

        mock_sleep.assert_called_once_with(10)


class TestGarminApiCallOtherErrors:
    """Test that non-auth, non-rate-limit errors propagate immediately."""

    @patch('coach.garmin_client.get_garmin_client')
    def test_connection_error_not_retried(self, mock_get_client):
        """Connection errors should propagate without retry."""
        mock_client = Mock()
        mock_get_client.return_value = mock_client

        def mock_fn(client):
            raise ConnectionError("Network unreachable")

        with pytest.raises(ConnectionError):
            garmin_api_call(mock_fn)

        # Only one get_garmin_client call (no retry)
        assert mock_get_client.call_count == 1

    @patch('coach.garmin_client.get_garmin_client')
    def test_value_error_not_retried(self, mock_get_client):
        """Generic errors should propagate without retry."""
        mock_client = Mock()
        mock_get_client.return_value = mock_client

        def mock_fn(client):
            raise ValueError("Bad data")

        with pytest.raises(ValueError):
            garmin_api_call(mock_fn)

        assert mock_get_client.call_count == 1

    @patch('coach.garmin_client.get_garmin_client')
    def test_runtime_error_not_retried(self, mock_get_client):
        """Runtime errors should propagate without retry."""
        mock_client = Mock()
        mock_get_client.return_value = mock_client

        def mock_fn(client):
            raise RuntimeError("Something broke")

        with pytest.raises(RuntimeError):
            garmin_api_call(mock_fn)

        assert mock_get_client.call_count == 1


class TestGarminApiCallHappyPath:
    """Test normal (no error) behavior."""

    @patch('coach.garmin_client.get_garmin_client')
    def test_passes_client_to_function(self, mock_get_client):
        """Should get client and pass it to the provided function."""
        mock_client = Mock()
        mock_get_client.return_value = mock_client

        result = garmin_api_call(lambda c: c.get_activities_by_date("2025-01-01", "2025-01-07"))

        mock_client.get_activities_by_date.assert_called_once_with("2025-01-01", "2025-01-07")

    @patch('coach.garmin_client.get_garmin_client')
    def test_returns_function_result(self, mock_get_client):
        """Should return whatever the function returns."""
        mock_client = Mock()
        mock_client.get_personal_record.return_value = [{"pr": "5k", "time": 1200}]
        mock_get_client.return_value = mock_client

        result = garmin_api_call(lambda c: c.get_personal_record())

        assert result == [{"pr": "5k", "time": 1200}]


class TestScheduleWorkoutRetry:
    """Test that schedule_workout uses garmin_api_call internally."""

    @patch('coach.garmin_client.garmin_api_call')
    def test_schedule_workout_uses_garmin_api_call(self, mock_api_call):
        """schedule_workout should delegate to garmin_api_call."""
        from coach.garmin_client import schedule_workout

        mock_api_call.return_value = {"status": "scheduled"}

        result = schedule_workout(12345, "2025-06-15")

        assert result == {"status": "scheduled"}
        mock_api_call.assert_called_once()
