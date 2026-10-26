"""Athlete activity claims: recorded as coaching memory, verified against
Garmin, never counted as training, surfaced until resolved."""
import json
from datetime import date, timedelta

import coach.planner as planner
import coach.claims as claims_mod
import coach.tools.decision_tools as decision_mod
import scripts.daily_loop as daily_loop

TODAY = date.today()


def _day(offset):
    return (TODAY - timedelta(days=offset)).isoformat()


def _patch_garmin(monkeypatch, activities_by_date):
    class _Client:
        def get_activities_by_date(self, start, end):
            return activities_by_date.get(start, [])

    monkeypatch.setattr(decision_mod, 'garmin_api_call', lambda fn: fn(_Client()))


def _raw(day_iso, type_key):
    return {'activityId': 42, 'activityName': 'x', 'startTimeLocal': f'{day_iso}T07:00:00.0',
            'activityType': {'typeKey': type_key}, 'duration': 1800.0}


class TestClaimsModule:
    def test_upsert_keeps_confirmed_state(self):
        log = {}
        first = claims_mod.new_claim(date_iso=_day(1), activity_type='running',
                                     duration_mins=30, note=None, today=TODAY,
                                     garmin_state='present')
        first.update(verified=True, verified_on=TODAY.isoformat(), verified_by='garmin')
        claims_mod.upsert_claim(log, first)
        again = claims_mod.new_claim(date_iso=_day(1), activity_type='Running',
                                     duration_mins=35, note='felt good', today=TODAY,
                                     garmin_state='absent')
        merged = claims_mod.upsert_claim(log, again)
        assert len(log['activity_claims']) == 1
        assert merged['verified'] is True and merged['duration_mins'] == 35

    def test_verify_matches_persisted_activity_and_expires_old_claims(self):
        log = {'activity_claims': [
            claims_mod.new_claim(date_iso=_day(1), activity_type='running', duration_mins=None,
                                 note=None, today=TODAY, garmin_state='absent'),
            claims_mod.new_claim(date_iso=_day(20), activity_type='padel', duration_mins=None,
                                 note=None, today=TODAY, garmin_state='absent'),
            claims_mod.new_claim(date_iso=_day(3), activity_type='cycling', duration_mins=None,
                                 note=None, today=TODAY, garmin_state='absent'),
        ]}
        loads = {_day(1): {'activities': [{'id': 1, 'type': 'trail_running'}]}}
        changed = claims_mod.verify_claims(log, loads, TODAY)
        states = {c['activity_type']: c['verified'] for c in claims_mod.claims(log)}
        assert states == {'running': True, 'padel': False, 'cycling': None}
        assert {c['activity_type'] for c in changed} == {'running', 'padel'}
        view = claims_mod.unverified_claims_view(log, TODAY)
        assert [c['activity_type'] for c in view] == ['cycling']
        assert view[0]['days_ago'] == 3


class TestRecordActivityClaimTool:
    def test_absent_on_garmin_records_a_pending_claim(self, monkeypatch):
        _patch_garmin(monkeypatch, {})
        result = decision_mod.record_activity_claim(_day(0), 'running', 40, 'ran before work')
        assert result['status'] == 'recorded'
        assert result['garmin_state_at_claim'] == 'absent'
        assert 'sync' in result['next']
        stored = planner.load_coaching_log()['activity_claims']
        assert stored[0]['verified'] is None and stored[0]['note'] == 'ran before work'

    def test_present_on_garmin_verifies_immediately(self, monkeypatch):
        _patch_garmin(monkeypatch, {_day(1): [_raw(_day(1), 'running')]})
        result = decision_mod.record_activity_claim(_day(1), 'running')
        assert result['status'] == 'verified'
        assert result['claim']['verified_by'] == 'garmin'

    def test_garmin_unavailable_still_records(self, monkeypatch):
        def boom(fn):
            raise RuntimeError('offline')
        monkeypatch.setattr(decision_mod, 'garmin_api_call', boom)
        result = decision_mod.record_activity_claim(_day(0), 'padel')
        assert result['status'] == 'recorded'
        assert result['garmin_state_at_claim'] == 'unavailable'

    def test_repeat_claim_is_one_record(self, monkeypatch):
        _patch_garmin(monkeypatch, {})
        decision_mod.record_activity_claim(_day(0), 'running', 30)
        decision_mod.record_activity_claim(_day(0), 'running', 35)
        stored = planner.load_coaching_log()['activity_claims']
        assert len(stored) == 1 and stored[0]['duration_mins'] == 35

    def test_future_and_bad_dates_rejected(self, monkeypatch):
        _patch_garmin(monkeypatch, {})
        assert decision_mod.record_activity_claim((TODAY + timedelta(days=1)).isoformat(),
                                                  'running')['error'] == 'future_date'
        assert decision_mod.record_activity_claim('yesterday', 'running')['error'] == 'invalid_date'
        assert decision_mod.record_activity_claim(_day(0), '  ')['error'] == 'missing_type'


class TestAuditWithClaims:
    def test_claimed_planned_session_is_not_a_miss(self, monkeypatch):
        d1 = _day(1)
        planner.save_coaching_log({'activity_claims': [
            claims_mod.new_claim(date_iso=d1, activity_type='running', duration_mins=30,
                                 note=None, today=TODAY, garmin_state='absent')]})
        plan = {'days': {d1: {'planned': {'type': 'running'}}}}
        monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)
        monkeypatch.setattr(daily_loop, 'get_activities_range', lambda s, e: json.dumps([]))
        merged = {}
        monkeypatch.setattr(daily_loop, 'merge_plan_days', lambda u: merged.update(u))

        result = daily_loop.audit_yesterday(TODAY)

        assert result['status'] == 'claimed'
        assert merged[d1]['status'] == 'claimed'
        assert 'claimed' in daily_loop.REAUDIT_STATUSES

    def test_claim_of_a_different_type_does_not_mask_a_missed_run(self, monkeypatch):
        d1 = _day(1)
        planner.save_coaching_log({'activity_claims': [
            claims_mod.new_claim(date_iso=d1, activity_type='yoga', duration_mins=20,
                                 note=None, today=TODAY, garmin_state='absent')]})
        plan = {'days': {d1: {'planned': {'type': 'running'}}}}
        monkeypatch.setattr(daily_loop, 'get_current_plan', lambda: plan)
        monkeypatch.setattr(daily_loop, 'get_activities_range', lambda s, e: json.dumps([]))
        monkeypatch.setattr(daily_loop, 'merge_plan_days', lambda u: None)

        assert daily_loop.audit_yesterday(TODAY)['status'] == 'missed'
