"""Tests for scripts/daily_loop.py — planned-session normalization, the
audit_yesterday NameError regression, the verified-or-unverified audit
contract, the re-audit window, and the exit code.

No network: Garmin reads and plan persistence are patched.
"""
import json
from datetime import date, timedelta
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
# Verified-or-unverified: a Garmin error payload is never an activity list
# ---------------------------------------------------------------------------

def _error_payload(start, end):
    return json.dumps({'error': 'AUTH_REQUIRED: Garmin session expired'})


def test_audit_yesterday_error_payload_is_unverified_not_bonus(monkeypatch):
    """Regression: the error dict was truthy, so a Garmin outage on a planned
    rest day was recorded as a 'bonus' workout with the error as 'actual'."""
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    plan = {'days': {yesterday: {'planned': None}}}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)
    monkeypatch.setattr(daily_loop, 'get_activities_range', _error_payload)
    save_mock = MagicMock()
    monkeypatch.setattr(daily_loop, 'save_weekly_plan', save_mock)

    result = daily_loop.audit_yesterday(today)

    assert result['status'] == 'unverified'
    assert plan['days'][yesterday]['status'] == 'unverified'
    assert 'actual' not in plan['days'][yesterday]  # never persist fiction
    save_mock.assert_called_once()


def test_audit_yesterday_error_payload_on_training_day_is_unverified(monkeypatch):
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    plan = {'days': {yesterday: {'planned': {'type': 'running'}}}}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)
    monkeypatch.setattr(daily_loop, 'get_activities_range', _error_payload)
    monkeypatch.setattr(daily_loop, 'save_weekly_plan', MagicMock())

    result = daily_loop.audit_yesterday(today)

    assert result['status'] == 'unverified'
    assert result['actual_count'] == 0
    assert plan['days'][yesterday]['status'] == 'unverified'


def test_audit_yesterday_unparseable_payload_is_unverified(monkeypatch):
    today = date.today()
    yesterday = (today - timedelta(days=1)).isoformat()
    plan = {'days': {yesterday: {'planned': {'type': 'running'}}}}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)
    monkeypatch.setattr(daily_loop, 'get_activities_range', lambda s, e: 'not json')
    monkeypatch.setattr(daily_loop, 'save_weekly_plan', MagicMock())

    assert daily_loop.audit_yesterday(today)['status'] == 'unverified'


# ---------------------------------------------------------------------------
# Re-audit window: late syncs and recovered outages heal earlier days
# ---------------------------------------------------------------------------

def test_audit_recent_heals_a_missed_day_when_garmin_shows_it(monkeypatch):
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    plan = {'days': {
        d1: {'planned': {'type': 'strength'}},
        d2: {'planned': {'type': 'running'}, 'status': 'missed'},
    }}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)

    def fake_range(start, end):
        return json.dumps([{'type': 'running'}] if start == d2 else [{'type': 'strength'}])

    monkeypatch.setattr(daily_loop, 'get_activities_range', fake_range)
    save_mock = MagicMock()
    monkeypatch.setattr(daily_loop, 'save_weekly_plan', save_mock)

    result = daily_loop.audit_recent(today)

    assert result['status'] == 'completed'           # yesterday
    assert plan['days'][d2]['status'] == 'completed'  # healed
    assert result['reaudited'] == [{'date': d2, 'from': 'missed', 'to': 'completed'}]
    assert save_mock.call_count == 2  # yesterday's save + the healed day


def test_audit_recent_re_checks_unverified_days(monkeypatch):
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d3 = (today - timedelta(days=3)).isoformat()
    plan = {'days': {
        d1: {'planned': None},
        d3: {'planned': None, 'status': 'unverified'},
    }}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)
    monkeypatch.setattr(daily_loop, 'get_activities_range', lambda s, e: json.dumps([]))
    monkeypatch.setattr(daily_loop, 'save_weekly_plan', MagicMock())

    result = daily_loop.audit_recent(today)

    assert plan['days'][d3]['status'] == 'rest_taken'
    assert result['reaudited'] == [{'date': d3, 'from': 'unverified', 'to': 'rest_taken'}]


def test_audit_recent_leaves_verified_days_alone(monkeypatch):
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    plan = {'days': {
        d1: {'planned': None},
        d2: {'planned': {'type': 'running'}, 'status': 'completed',
             'actual': [{'type': 'running'}]},
    }}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)
    calls = []

    def fake_range(start, end):
        calls.append(start)
        return json.dumps([])

    monkeypatch.setattr(daily_loop, 'get_activities_range', fake_range)
    monkeypatch.setattr(daily_loop, 'save_weekly_plan', MagicMock())

    result = daily_loop.audit_recent(today)

    assert calls == [d1]  # the completed day is not re-fetched
    assert result['reaudited'] == []
    assert plan['days'][d2]['status'] == 'completed'


def test_audit_recent_stays_unverified_while_garmin_is_down(monkeypatch):
    today = date.today()
    d1 = (today - timedelta(days=1)).isoformat()
    d2 = (today - timedelta(days=2)).isoformat()
    plan = {'days': {
        d1: {'planned': {'type': 'running'}},
        d2: {'planned': {'type': 'running'}, 'status': 'unverified'},
    }}
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)
    monkeypatch.setattr(daily_loop, 'get_activities_range', _error_payload)
    monkeypatch.setattr(daily_loop, 'save_weekly_plan', MagicMock())

    result = daily_loop.audit_recent(today)

    assert result['status'] == 'unverified'
    assert result['reaudited'] == []
    assert plan['days'][d2]['status'] == 'unverified'


# ---------------------------------------------------------------------------
# Exit code: the scheduler must see an unverified or failed run
# ---------------------------------------------------------------------------

def _stub_audit(status):
    async def _run():
        return {'status': status, 'morning_brief': 'brief'}
    return _run


def test_main_exit_code_zero_on_success(monkeypatch, capsys):
    monkeypatch.setattr(daily_loop, 'run_morning_audit', _stub_audit('success'))
    assert daily_loop.main() == 0
    assert 'brief' in capsys.readouterr().out


def test_main_exit_code_nonzero_when_unverified(monkeypatch, capsys):
    monkeypatch.setattr(daily_loop, 'run_morning_audit', _stub_audit('unverified'))
    assert daily_loop.main() == 1


def test_main_exit_code_nonzero_on_error(monkeypatch, capsys):
    monkeypatch.setattr(daily_loop, 'run_morning_audit', _stub_audit('error'))
    assert daily_loop.main() == 1


def test_brief_names_an_unverified_yesterday(monkeypatch):
    monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: None)
    brief = daily_loop.generate_morning_brief({}, {}, {'status': 'unverified'})
    assert 'not verified' in brief


# ---------------------------------------------------------------------------
# Import smoke test for the new recovery script
# ---------------------------------------------------------------------------

def test_garmin_login_module_imports():
    """scripts/garmin_login.py must import cleanly (main() is guarded)."""
    import scripts.garmin_login  # noqa: F401

    assert callable(scripts.garmin_login.main)
