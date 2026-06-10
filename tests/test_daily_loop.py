"""Tests for scripts/daily_loop.py — planned-session normalization, the
audit_yesterday NameError regression, and LLM model selection.

No network: Garmin reads and plan persistence are patched, and the
anthropic SDK is replaced with an in-memory stub via sys.modules.
"""
import json
import sys
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import scripts.daily_loop as daily_loop


# ---------------------------------------------------------------------------
# _normalize_planned
# ---------------------------------------------------------------------------

def test_normalize_planned_single_dict():
    session = {'type': 'strength', 'duration_mins': 45}
    assert daily_loop._normalize_planned(session) == [session]


def test_normalize_planned_list_passthrough():
    sessions = [{'type': 'strength'}, {'type': 'running'}]
    assert daily_loop._normalize_planned(sessions) == sessions


def test_normalize_planned_list_drops_non_dict_entries():
    sessions = [{'type': 'strength'}, None, 'rest']
    assert daily_loop._normalize_planned(sessions) == [{'type': 'strength'}]


def test_normalize_planned_none_and_junk():
    assert daily_loop._normalize_planned(None) == []
    assert daily_loop._normalize_planned('rest') == []
    assert daily_loop._normalize_planned(42) == []


# ---------------------------------------------------------------------------
# audit_yesterday
# ---------------------------------------------------------------------------

def _patch_audit_deps(monkeypatch, plan, activities):
    """Patch the Garmin/plan I/O used by audit_yesterday."""
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)
    monkeypatch.setattr(
        daily_loop, 'get_activities_range',
        lambda start, end: json.dumps(activities),
    )
    save_mock = MagicMock()
    monkeypatch.setattr(daily_loop, 'save_weekly_plan', save_mock)
    return save_mock


def test_audit_yesterday_dict_planned_missed(monkeypatch):
    """Regression: returning 'planned' raised NameError whenever the plan
    was current (yesterday present in plan['days'])."""
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    session = {'type': 'strength', 'duration_mins': 45}
    plan = {'days': {yesterday: {'planned': session}}}  # old single-dict shape
    save_mock = _patch_audit_deps(monkeypatch, plan, [])

    result = daily_loop.audit_yesterday(today)

    assert result['status'] == 'missed'
    assert result['planned'] == [session]  # normalized to a list
    assert result['actual_count'] == 0
    save_mock.assert_called_once()


def test_audit_yesterday_list_planned_completed(monkeypatch):
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    plan = {'days': {yesterday: {'planned': [
        {'type': 'strength'},
        {'type': 'running'},
    ]}}}
    activities = [{'type': 'strength'}, {'type': 'running'}]
    _patch_audit_deps(monkeypatch, plan, activities)

    result = daily_loop.audit_yesterday(today)

    assert result['status'] == 'completed'
    assert result['planned'] == [{'type': 'strength'}, {'type': 'running'}]
    assert result['actual_count'] == 2


def test_audit_yesterday_rest_day_taken(monkeypatch):
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    plan = {'days': {yesterday: {'planned': None}}}
    _patch_audit_deps(monkeypatch, plan, [])

    result = daily_loop.audit_yesterday(today)

    assert result['status'] == 'rest_taken'
    assert result['planned'] == []


def test_audit_yesterday_no_plan(monkeypatch):
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: None)

    result = daily_loop.audit_yesterday(date.today())

    assert result['status'] == 'no_plan'


# ---------------------------------------------------------------------------
# generate_morning_brief
# ---------------------------------------------------------------------------

def test_generate_morning_brief_handles_list_planned(monkeypatch):
    today_str = date.today().isoformat()
    plan = {'days': {today_str: {'planned': [
        {'type': 'strength', 'description': 'Upper body', 'duration_mins': 45},
        {'type': 'mobility', 'description': 'Hip openers'},
    ]}}}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)

    brief = daily_loop.generate_morning_brief({}, {}, {})

    assert 'Upper body (45 mins)' in brief
    assert 'Hip openers' in brief


def test_generate_morning_brief_handles_dict_planned(monkeypatch):
    today_str = date.today().isoformat()
    plan = {'days': {today_str: {'planned': {
        'type': 'running', 'description': 'Easy Z2 run', 'duration_mins': 60,
    }}}}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)

    brief = daily_loop.generate_morning_brief({}, {}, {})

    assert 'Easy Z2 run (60 mins)' in brief


def test_generate_morning_brief_rest_day(monkeypatch):
    today_str = date.today().isoformat()
    plan = {'days': {today_str: {'planned': None}}}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)

    brief = daily_loop.generate_morning_brief({}, {}, {})

    assert 'Rest day' in brief


# ---------------------------------------------------------------------------
# _generate_llm_brief — model selection + SDK fallback (no network)
# ---------------------------------------------------------------------------

class _FakeMessages:
    def __init__(self):
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(content=[SimpleNamespace(text='fake llm brief')])


class _FakeAnthropicClient:
    last_instance = None

    def __init__(self, api_key=None):
        self.api_key = api_key
        self.messages = _FakeMessages()
        _FakeAnthropicClient.last_instance = self


def _install_fake_anthropic(monkeypatch):
    fake_module = SimpleNamespace(Anthropic=_FakeAnthropicClient)
    monkeypatch.setitem(sys.modules, 'anthropic', fake_module)
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'test-key')
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: None)


async def test_llm_brief_default_model(monkeypatch):
    _install_fake_anthropic(monkeypatch)
    monkeypatch.delenv('COACH_LLM_MODEL', raising=False)

    brief = await daily_loop._generate_llm_brief({}, {}, {})

    assert brief == 'fake llm brief'
    used_model = _FakeAnthropicClient.last_instance.messages.kwargs['model']
    assert used_model == daily_loop.DEFAULT_LLM_MODEL == 'claude-sonnet-4-6'


async def test_llm_brief_env_model_override(monkeypatch):
    _install_fake_anthropic(monkeypatch)
    monkeypatch.setenv('COACH_LLM_MODEL', 'claude-test-override')

    brief = await daily_loop._generate_llm_brief({}, {}, {})

    assert brief == 'fake llm brief'
    used_model = _FakeAnthropicClient.last_instance.messages.kwargs['model']
    assert used_model == 'claude-test-override'


async def test_llm_brief_falls_back_when_sdk_missing(monkeypatch, caplog):
    # sys.modules[name] = None makes `import anthropic` raise ImportError
    monkeypatch.setitem(sys.modules, 'anthropic', None)
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: None)

    brief = await daily_loop._generate_llm_brief({}, {}, {})

    assert brief.startswith('## Morning Brief')  # template fallback
    assert any('anthropic' in r.message for r in caplog.records)


async def test_llm_brief_falls_back_without_api_key(monkeypatch):
    fake_module = SimpleNamespace(Anthropic=_FakeAnthropicClient)
    monkeypatch.setitem(sys.modules, 'anthropic', fake_module)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: None)

    brief = await daily_loop._generate_llm_brief({}, {}, {})

    assert brief.startswith('## Morning Brief')  # template fallback


# ---------------------------------------------------------------------------
# Import smoke test for the new recovery script
# ---------------------------------------------------------------------------

def test_garmin_login_module_imports():
    """scripts/garmin_login.py must import cleanly (main() is guarded)."""
    import scripts.garmin_login  # noqa: F401

    assert callable(scripts.garmin_login.main)
