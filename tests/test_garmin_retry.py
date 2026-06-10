"""
Tests for Garmin auth flow and API retry behavior in coach/garmin_client.py.

Verifies:
- Auth errors (GarminConnectAuthenticationError) trigger force_refresh and retry
- Persistent auth errors (bad creds) raise after one retry
- Rate limit errors (429) wait and retry
- Other exceptions propagate immediately without retry
- get_garmin_client(): token-first flow, ONE non-interactive credential login,
  needs_mfa -> GarminAuthRequiredError, and the process-level auth latch
  (one expired session must not trigger N login attempts during a snapshot)
"""
import time
import pytest
from unittest.mock import Mock, patch, call

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)
from coach.garmin_client import (
    AUTH_REQUIRED_MESSAGE,
    GarminAuthRequiredError,
    garmin_api_call,
    get_garmin_client,
    _cached_client,
)
import coach.garmin_client as garmin_client


@pytest.fixture(autouse=True)
def _reset_auth_state():
    """Each test starts with no cached client and a disarmed auth latch."""
    garmin_client._cached_client = None
    garmin_client._auth_failed_at = None
    yield
    garmin_client._cached_client = None
    garmin_client._auth_failed_at = None


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


# ---------------------------------------------------------------------------
# get_garmin_client() auth flow (garminconnect 0.3.5 native rebuild)
# ---------------------------------------------------------------------------

@pytest.fixture
def token_dir(tmp_path, monkeypatch):
    """Redirect TOKEN_DIR to an existing tmp dir (token-first phase runs)."""
    d = tmp_path / ".garth"
    d.mkdir()
    monkeypatch.setattr(garmin_client, 'TOKEN_DIR', str(d))
    return str(d)


@pytest.fixture
def garmin_creds(monkeypatch):
    monkeypatch.setenv('GARMIN_EMAIL', 'athlete@example.com')
    monkeypatch.setenv('GARMIN_PASSWORD', 'hunter2')


def _make_cred_client():
    """A mock Garmin whose ONE credential login succeeds without MFA."""
    cred_client = Mock()
    cred_client.client.login.return_value = (None, None)
    cred_client.connectapi.side_effect = [
        {"displayName": "athlete-123", "fullName": "Athlete Example"},
        {"userData": {"measurementSystem": "metric"}},
    ]
    return cred_client


class TestAuthRequiredErrorMessage:
    def test_message_is_exact_actionable_string(self):
        """Every tool surfaces {'error': str(e)} — the message must be the
        documented remediation, character for character."""
        assert str(GarminAuthRequiredError()) == (
            "AUTH_REQUIRED: Garmin session expired or needs MFA. "
            "Run: python scripts/garmin_login.py"
        )
        assert str(GarminAuthRequiredError()) == AUTH_REQUIRED_MESSAGE


class TestTokenFirstFlow:
    @patch('coach.garmin_client.Garmin')
    def test_valid_tokens_no_credential_login(self, mock_garmin_cls, token_dir):
        """Steady state: tokens load, zero credential traffic."""
        token_client = Mock()
        mock_garmin_cls.return_value = token_client

        result = get_garmin_client()

        assert result is token_client
        # Token phase must use a credential-less Garmin() so garminconnect's
        # internal fallback can never cascade into an SSO login.
        mock_garmin_cls.assert_called_once_with()
        token_client.login.assert_called_once_with(tokenstore=token_dir)
        token_client.client.login.assert_not_called()

    @patch('coach.garmin_client.Garmin')
    def test_client_is_cached_across_calls(self, mock_garmin_cls, token_dir):
        token_client = Mock()
        mock_garmin_cls.return_value = token_client

        first = get_garmin_client()
        second = get_garmin_client()

        assert first is second
        assert mock_garmin_cls.call_count == 1

    @patch('coach.garmin_client.Garmin')
    def test_missing_token_dir_skips_token_phase(
        self, mock_garmin_cls, tmp_path, monkeypatch, garmin_creds
    ):
        monkeypatch.setattr(
            garmin_client, 'TOKEN_DIR', str(tmp_path / "does-not-exist")
        )
        cred_client = _make_cred_client()
        mock_garmin_cls.return_value = cred_client

        result = get_garmin_client()

        assert result is cred_client
        # Only the credential-phase constructor ran (with creds + return_on_mfa).
        mock_garmin_cls.assert_called_once_with(
            'athlete@example.com', 'hunter2', return_on_mfa=True
        )


class TestCredentialLoginFlow:
    @patch('coach.garmin_client.Garmin')
    def test_stale_tokens_trigger_exactly_one_credential_login(
        self, mock_garmin_cls, token_dir, garmin_creds
    ):
        token_client = Mock()
        token_client.login.side_effect = Exception("token store stale")
        cred_client = _make_cred_client()
        mock_garmin_cls.side_effect = [token_client, cred_client]

        result = get_garmin_client()

        assert result is cred_client
        cred_client.client.login.assert_called_once_with(
            'athlete@example.com', 'hunter2', return_on_mfa=True
        )
        # New tokens persisted where the token-first phase reads them.
        cred_client.client.dump.assert_called_once_with(token_dir)
        # Profile populated (return_on_mfa login path skips it inside the lib).
        assert cred_client.display_name == "athlete-123"
        assert cred_client.full_name == "Athlete Example"
        assert cred_client.unit_system == "metric"

    @patch('coach.garmin_client.Garmin')
    def test_needs_mfa_raises_auth_required_without_blocking(
        self, mock_garmin_cls, token_dir, garmin_creds
    ):
        token_client = Mock()
        token_client.login.side_effect = Exception("token store stale")
        cred_client = Mock()
        cred_client.client.login.return_value = ("needs_mfa", None)
        mock_garmin_cls.side_effect = [token_client, cred_client]

        with pytest.raises(GarminAuthRequiredError) as exc_info:
            get_garmin_client()

        assert str(exc_info.value) == AUTH_REQUIRED_MESSAGE
        # Never persist tokens from an incomplete MFA login.
        cred_client.client.dump.assert_not_called()

    @patch('coach.garmin_client.Garmin')
    def test_credential_login_failure_wrapped_as_auth_required(
        self, mock_garmin_cls, token_dir, garmin_creds
    ):
        token_client = Mock()
        token_client.login.side_effect = Exception("token store stale")
        cred_client = Mock()
        cred_client.client.login.side_effect = GarminConnectTooManyRequestsError(
            "All login strategies rate limited (429)."
        )
        mock_garmin_cls.side_effect = [token_client, cred_client]

        with pytest.raises(GarminAuthRequiredError) as exc_info:
            get_garmin_client()

        assert str(exc_info.value) == AUTH_REQUIRED_MESSAGE
        # Original cause preserved for server-side debugging.
        assert isinstance(
            exc_info.value.__cause__, GarminConnectTooManyRequestsError
        )

    @patch('coach.garmin_client.Garmin')
    def test_missing_credentials_raise_auth_required(
        self, mock_garmin_cls, token_dir, monkeypatch
    ):
        monkeypatch.delenv('GARMIN_EMAIL', raising=False)
        monkeypatch.delenv('GARMIN_PASSWORD', raising=False)
        token_client = Mock()
        token_client.login.side_effect = Exception("token store stale")
        mock_garmin_cls.return_value = token_client

        with pytest.raises(GarminAuthRequiredError):
            get_garmin_client()


class TestAuthLatch:
    """One expired session must not trigger N login attempts per snapshot."""

    @patch('coach.garmin_client.Garmin')
    def test_failed_login_arms_latch_and_fails_fast(
        self, mock_garmin_cls, token_dir, garmin_creds
    ):
        token_client = Mock()
        token_client.login.side_effect = Exception("token store stale")
        cred_client = Mock()
        cred_client.client.login.return_value = ("needs_mfa", None)
        mock_garmin_cls.side_effect = [token_client, cred_client]

        with pytest.raises(GarminAuthRequiredError):
            get_garmin_client()
        constructions_after_failure = mock_garmin_cls.call_count

        # Every subsequent call inside the latch window fails immediately:
        # no Garmin construction, no token load, no login traffic.
        for _ in range(5):
            with pytest.raises(GarminAuthRequiredError):
                get_garmin_client()
        assert mock_garmin_cls.call_count == constructions_after_failure

    @patch('coach.garmin_client.Garmin')
    def test_latch_applies_to_force_refresh_too(
        self, mock_garmin_cls, token_dir, garmin_creds
    ):
        garmin_client._auth_failed_at = time.monotonic()

        with pytest.raises(GarminAuthRequiredError):
            get_garmin_client(force_refresh=True)
        mock_garmin_cls.assert_not_called()

    @patch('coach.garmin_client.Garmin')
    def test_latch_expires_after_window(self, mock_garmin_cls, token_dir):
        garmin_client._auth_failed_at = (
            time.monotonic() - garmin_client.AUTH_LATCH_SECS - 1
        )
        token_client = Mock()
        mock_garmin_cls.return_value = token_client

        result = get_garmin_client()

        assert result is token_client
        # Successful auth clears the stale latch entirely.
        assert garmin_client._auth_failed_at is None

    @patch('coach.garmin_client.Garmin')
    def test_successful_credential_login_clears_latch(
        self, mock_garmin_cls, token_dir, garmin_creds
    ):
        garmin_client._auth_failed_at = (
            time.monotonic() - garmin_client.AUTH_LATCH_SECS - 1
        )
        token_client = Mock()
        token_client.login.side_effect = Exception("token store stale")
        cred_client = _make_cred_client()
        mock_garmin_cls.side_effect = [token_client, cred_client]

        result = get_garmin_client()

        assert result is cred_client
        assert garmin_client._auth_failed_at is None


class TestGarminApiCallAuthRequired:
    @patch('coach.garmin_client.get_garmin_client')
    def test_auth_required_propagates_through_retry_path(self, mock_get_client):
        """Stale cached client 401s, the forced re-login needs a human:
        the tool gets GarminAuthRequiredError (actionable), not the 401."""
        stale_client = Mock()
        mock_get_client.side_effect = [stale_client, GarminAuthRequiredError()]

        def fn(client):
            raise GarminConnectAuthenticationError("Session expired")

        with pytest.raises(GarminAuthRequiredError):
            garmin_api_call(fn)
