"""Contract tests pinning the garminconnect API surface coach-mcp uses.

The 0.2 -> 0.3 garminconnect upgrade renamed the transport attribute
(``.garth`` -> ``.client``) and dropped garth entirely. The code kept
importing fine and only failed at runtime, in production, mid-coaching.
This file makes any such dependency rename fail loudly in CI instead:

- Every Garmin / Client method and attribute the codebase calls must exist,
  and its signature must still bind the exact call shape we use.
- The login contract (tokenstore kwarg, return_on_mfa, the "needs_mfa"
  sentinel, resume_login, token dump/load) is pinned because
  coach/garmin_client.py and scripts/garmin_login.py are built on it.
- A source scan keeps the inventory honest: any NEW `lambda c: c.method(...)`
  call site added to the codebase is auto-checked against the installed
  library even if nobody updates the static tables below.

No network access: tests only construct ``Garmin()`` (pure attribute setup —
verified against the installed 0.3.5 source) and inspect signatures/source.
"""
import inspect
import re
from pathlib import Path

import pytest

import garminconnect
from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)
from garminconnect.client import Client

REPO_ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Static inventory: every Garmin-level method the codebase calls, with the
# exact (args, kwargs) shape used at the call site. Grep-derived from:
#   lambda c[,:]... c.get_/c.upload_...   (garmin_api_call call sites)
#   coach/garmin_client.py, scripts/garmin_login.py
# ---------------------------------------------------------------------------
GARMIN_CALL_SHAPES: dict[str, tuple[tuple, dict]] = {
    # data_tools / fitness_tools / coaching_tools / athlete_tools / strength_tools
    "get_activities_by_date": (("2026-01-01", "2026-01-07"), {}),
    "get_activity_splits": (("12345",), {}),
    "get_activity_exercise_sets": (("12345",), {}),
    "get_activity_hr_in_timezones": (("12345",), {}),
    "get_personal_record": ((), {}),
    "get_user_summary": (("2026-01-01",), {}),
    "get_body_battery": (("2026-01-01",), {}),
    "get_training_readiness": (("2026-01-01",), {}),
    "get_hrv_data": (("2026-01-01",), {}),
    "get_sleep_data": (("2026-01-01",), {}),
    "get_user_profile": ((), {}),
    "get_full_name": ((), {}),
    "get_body_composition": (("2026-01-01", "2026-01-31"), {}),
    # planning_tools (push_plan_to_garmin)
    "get_workouts": ((), {}),
    "upload_workout": (({"workoutName": "x"},), {}),
    "upload_cycling_workout": ((object(),), {}),
    "upload_running_workout": ((object(),), {}),
    # garmin_client._populate_profile / scripts/garmin_login.py
    "connectapi": (("/userprofile-service/socialProfile",), {}),
    "login": ((), {"tokenstore": "/tmp/tokens"}),
    "resume_login": ((None, "123456"), {}),
}

# Transport-level (Garmin().client) methods the codebase calls.
CLIENT_CALL_SHAPES: dict[str, tuple[tuple, dict]] = {
    # garmin_client.schedule_workout
    "post": (("connectapi", "/workout-service/schedule/1"), {"json": {"date": "2026-01-01"}}),
    # planning_tools push: delete stale workouts
    "delete": (("connectapi", "/workout-service/workout/1"), {"api": True}),
    # fitness_tools heart-rate zones drill-down, garmin_login.py verification
    "connectapi": (("/biometric-service/heartRateZones",), {}),
    # garmin_client._credential_login (ONE non-interactive credential login)
    "login": (("e@example.com", "secret"), {"return_on_mfa": True}),
    # scripts/garmin_login.py MFA completion
    "resume_login": ((None, "123456"), {}),
    # token persistence (garmin_client + garmin_login.py)
    "dump": (("/tmp/tokens",), {}),
    "load": (("/tmp/tokens",), {}),
}

# Plain attributes read off Garmin instances.
GARMIN_ATTRS = [
    "client",
    "display_name",
    "full_name",
    "unit_system",
    "garmin_workouts_schedule_url",
    "garmin_connect_user_settings_url",
]

# garminconnect.workout symbols used by coach/workout_builder.py.
WORKOUT_MODULE_SYMBOLS = [
    "CyclingWorkout",
    "RunningWorkout",
    "WorkoutSegment",
    "ExecutableStep",
    "RepeatGroup",
    "create_warmup_step",
    "create_cooldown_step",
    "create_interval_step",
    "create_recovery_step",
]


@pytest.fixture(scope="module")
def garmin() -> Garmin:
    """A Garmin instance constructed offline.

    Garmin.__init__ only assigns attributes and builds the transport Client
    (requests.Session setup) — no network I/O. If this fixture ever starts
    hitting the network, the dependency's constructor contract changed.
    """
    return Garmin()


class TestGarminMethodContract:
    """Every Garmin method the codebase uses exists and binds our call shape."""

    @pytest.mark.parametrize("method_name", sorted(GARMIN_CALL_SHAPES))
    def test_method_exists_and_binds(self, garmin, method_name):
        assert hasattr(garmin, method_name), (
            f"garminconnect.Garmin lost method '{method_name}' — "
            "every call site using it will break at runtime"
        )
        method = getattr(garmin, method_name)
        assert callable(method)
        args, kwargs = GARMIN_CALL_SHAPES[method_name]
        try:
            inspect.signature(method).bind(*args, **kwargs)
        except TypeError as e:
            pytest.fail(
                f"Garmin.{method_name} no longer accepts the call shape the "
                f"codebase uses (args={args}, kwargs={kwargs}): {e}"
            )

    def test_constructor_accepts_credentials_and_return_on_mfa(self):
        """garmin_client._credential_login builds Garmin(email, password, return_on_mfa=True)."""
        sig = inspect.signature(Garmin.__init__)
        sig.bind(None, "e@example.com", "secret", return_on_mfa=True)

    def test_constructor_works_with_no_arguments(self):
        """The token-first phase uses a credential-less Garmin()."""
        client = Garmin()
        assert client.username is None

    def test_login_returns_mfa_status_tuple(self):
        """Garmin.login returns (mfa_status, _) and uses the 'needs_mfa' sentinel.

        coach/garmin_client.py and scripts/garmin_login.py both branch on the
        literal string 'needs_mfa' — pin it against silent renames.
        """
        src = inspect.getsource(Client.login)
        assert "needs_mfa" in src, (
            "Client.login source no longer mentions the 'needs_mfa' sentinel; "
            "the MFA detection in coach/garmin_client.py is broken"
        )
        ret = inspect.signature(Garmin.login).return_annotation
        assert "tuple" in str(ret), (
            f"Garmin.login return annotation changed to {ret!r}; callers "
            "unpack '(mfa_status, _)'"
        )


class TestClientTransportContract:
    """The .client transport attribute and its methods (the 0.2->0.3 break)."""

    def test_client_attribute_is_transport(self, garmin):
        # THE regression this file exists for: 0.3 renamed .garth -> .client.
        assert isinstance(garmin.client, Client), (
            "Garmin().client is no longer the transport Client — "
            "token dump/load and raw post/delete call sites will break"
        )

    @pytest.mark.parametrize("method_name", sorted(CLIENT_CALL_SHAPES))
    def test_method_exists_and_binds(self, garmin, method_name):
        assert hasattr(garmin.client, method_name), (
            f"garminconnect Client transport lost method '{method_name}'"
        )
        method = getattr(garmin.client, method_name)
        args, kwargs = CLIENT_CALL_SHAPES[method_name]
        try:
            inspect.signature(method).bind(*args, **kwargs)
        except TypeError as e:
            pytest.fail(
                f"Client.{method_name} no longer accepts the call shape the "
                f"codebase uses (args={args}, kwargs={kwargs}): {e}"
            )

    def test_tokenstore_file_name_stable(self):
        """Token persistence and scripts/garmin_login.py docs reference
        .garth/garmin_tokens.json — pin the on-disk file name."""
        assert "garmin_tokens.json" in inspect.getsource(Client.dump)
        assert "garmin_tokens.json" in inspect.getsource(Client.load)


class TestGarminAttributeContract:
    @pytest.mark.parametrize("attr", GARMIN_ATTRS)
    def test_attribute_exists(self, garmin, attr):
        assert hasattr(garmin, attr), (
            f"garminconnect.Garmin lost attribute '{attr}'"
        )

    def test_schedule_url_shape(self, garmin):
        """schedule_workout() builds '<garmin_workouts_schedule_url>/<id>'."""
        assert garmin.garmin_workouts_schedule_url.startswith("/workout-service")


class TestWorkoutModuleContract:
    """coach/workout_builder.py imports typed workout models + step helpers."""

    @pytest.mark.parametrize("symbol", WORKOUT_MODULE_SYMBOLS)
    def test_symbol_importable(self, symbol):
        from garminconnect import workout

        assert hasattr(workout, symbol), (
            f"garminconnect.workout lost '{symbol}' — workout_builder.py "
            "imports it at module level"
        )

    @pytest.mark.parametrize("model_name", ["RunningWorkout", "CyclingWorkout"])
    def test_workout_models_expose_workout_name(self, model_name):
        """planning_tools reads `workout.workoutName` off uploaded workouts."""
        from garminconnect import workout

        model = getattr(workout, model_name)
        assert "workoutName" in model.model_fields


class TestExceptionContract:
    """coach/garmin_client.py's retry logic catches these by type."""

    @pytest.mark.parametrize(
        "exc",
        [
            GarminConnectAuthenticationError,
            GarminConnectTooManyRequestsError,
            GarminConnectConnectionError,
        ],
    )
    def test_exception_exported(self, exc):
        assert issubclass(exc, Exception)
        assert hasattr(garminconnect, exc.__name__)


# ---------------------------------------------------------------------------
# Dynamic safety net: scan the codebase for the canonical garmin_api_call
# lambda pattern and verify every method name found actually exists on the
# installed library. This keeps coverage honest as new call sites appear,
# even if nobody updates the static tables above.
# ---------------------------------------------------------------------------
_LAMBDA_GARMIN_RE = re.compile(r"lambda c(?:,[^:]*)?:\s*c\.(\w+)\(")
_LAMBDA_CLIENT_RE = re.compile(r"lambda c(?:,[^:]*)?:\s*c\.client\.(\w+)\(")


def _scan_codebase_calls() -> tuple[set[str], set[str]]:
    garmin_methods: set[str] = set()
    client_methods: set[str] = set()
    for folder in ("coach", "scripts"):
        for py_file in (REPO_ROOT / folder).rglob("*.py"):
            text = py_file.read_text(encoding="utf-8")
            client_methods.update(_LAMBDA_CLIENT_RE.findall(text))
            garmin_methods.update(
                m for m in _LAMBDA_GARMIN_RE.findall(text) if m != "client"
            )
    return garmin_methods, client_methods


def test_every_lambda_call_site_method_exists():
    """Every `garmin_api_call(lambda c: c.X(...))` in coach/ + scripts/ must
    resolve against the installed garminconnect."""
    garmin_methods, client_methods = _scan_codebase_calls()
    # Sanity: the scan itself must find the well-known call sites — if the
    # regex rots, this test must not silently pass on an empty set.
    assert "get_activities_by_date" in garmin_methods

    missing_garmin = sorted(m for m in garmin_methods if not hasattr(Garmin, m))
    missing_client = sorted(m for m in client_methods if not hasattr(Client, m))
    assert not missing_garmin, (
        f"Codebase calls Garmin methods missing from the installed "
        f"garminconnect: {missing_garmin}"
    )
    assert not missing_client, (
        f"Codebase calls Client transport methods missing from the installed "
        f"garminconnect: {missing_client}"
    )


def test_every_lambda_garmin_method_is_in_static_inventory():
    """New lambda call sites should also get a pinned call shape above."""
    garmin_methods, client_methods = _scan_codebase_calls()
    unpinned = sorted(garmin_methods - set(GARMIN_CALL_SHAPES))
    assert not unpinned, (
        f"New Garmin call sites found without a pinned call shape in "
        f"GARMIN_CALL_SHAPES (add them so signature drift is caught): {unpinned}"
    )
    unpinned_client = sorted(client_methods - set(CLIENT_CALL_SHAPES))
    assert not unpinned_client, (
        f"New Client transport call sites without a pinned call shape in "
        f"CLIENT_CALL_SHAPES: {unpinned_client}"
    )
