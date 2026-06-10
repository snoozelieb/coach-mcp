"""Phase 3.3 — periodization lifecycle.

Covers the Lane C season layer:

1. CONFIG VALIDATION → data_quality: validate_season_config flags
   block_dates_invalid (end < start, transition before phase_start),
   a_race_in_past (with days), no_upcoming_events, and phase_overdue
   (target_transition < today). Flags only — nothing is auto-fixed.
2. RACE-PASSED LIFECYCLE: a passed A/B-priority event with no race_review
   decision auto-creates ONE season_replan pending approval (idempotent by
   event tag across snapshot calls; skipped when a race_review decision
   mentions the event; skipped for priority C/D/life_event).
3. PHASE-OVERDUE NUDGE: target_transition passed more than the grace window
   ago without a phase_transition decision since → ONE phase_transition
   proposal, same idempotency.
4. Auto-proposals flow through the normal approve_proposal /
   reject_proposal machinery, and neither outcome allows re-creation.
"""
import json
import pytest
from datetime import date, timedelta

import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_mod
import coach.parsers as parsers_mod
import coach.workout_builder as workout_builder
import coach.tools.coaching_tools as coaching_mod
import coach.tools.planning_tools as planning_mod
import coach.tools.strength_tools as strength_mod

from coach.planner import validate_season_config
from coach.tools.coaching_tools import (
    PHASE_OVERDUE_GRACE_DAYS,
    _season_lifecycle_proposals,
    get_coaching_snapshot,
)
from coach.tools.decision_tools import (
    AUTO_PROPOSAL_TAGS_KEY,
    approve_proposal,
    ensure_tagged_proposal,
    find_tagged_proposal,
    reject_proposal,
)

TODAY = date.today()


def _iso(days_offset: int) -> str:
    return (TODAY + timedelta(days=days_offset)).isoformat()


def _event(name='Sani2c Stage Race', days_offset=-10, priority='A',
           race_type='mtb_stage_race'):
    return {'name': name, 'date': _iso(days_offset), 'priority': priority,
            'type': race_type}


# ---------------------------------------------------------------------------
# 1. validate_season_config — golden case per flag (pure, no I/O)
# ---------------------------------------------------------------------------

class TestValidateSeasonConfig:
    def test_clean_config_yields_no_flags(self):
        config = {
            'current_block': {'phase': 'build', 'start_date': _iso(-14),
                              'end_date': _iso(14)},
            'periodization': {'current_phase': 'build',
                              'phase_start': _iso(-14),
                              'target_transition': _iso(14)},
            'events': [_event(days_offset=45)],
        }
        assert validate_season_config(config, TODAY) == {}

    def test_block_end_before_start_flagged(self):
        config = {
            'current_block': {'start_date': _iso(-10), 'end_date': _iso(-20)},
            'events': [_event(days_offset=30)],
        }
        flags = validate_season_config(config, TODAY)
        problems = flags['block_dates_invalid']
        assert len(problems) == 1
        assert 'end_date' in problems[0]
        assert _iso(-20) in problems[0] and _iso(-10) in problems[0]

    def test_transition_before_phase_start_flagged(self):
        config = {
            'periodization': {'phase_start': _iso(-5),
                              'target_transition': _iso(-10)},
            'events': [_event(days_offset=30)],
        }
        flags = validate_season_config(config, TODAY)
        problems = flags['block_dates_invalid']
        assert len(problems) == 1
        assert 'target_transition' in problems[0]
        assert 'phase_start' in problems[0]
        # transition < today also fires phase_overdue independently
        assert flags['phase_overdue']['days_overdue'] == 10

    def test_a_race_in_past_carries_days(self):
        config = {'events': [_event(days_offset=-12), _event(
            name='Future Fun Run', days_offset=30, priority='C')]}
        flags = validate_season_config(config, TODAY)
        assert flags['a_race_in_past'] == {
            'name': 'Sani2c Stage Race',
            'date': _iso(-12),
            'days_ago': 12,
        }
        # A future event exists, so no_upcoming_events must NOT fire
        assert 'no_upcoming_events' not in flags

    def test_past_b_or_c_race_is_not_a_race_in_past(self):
        config = {'events': [_event(days_offset=-12, priority='B'),
                             _event(name='Old crit', days_offset=-30,
                                    priority='C')]}
        flags = validate_season_config(config, TODAY)
        assert 'a_race_in_past' not in flags

    def test_no_upcoming_events_flagged(self):
        assert validate_season_config({'events': []}, TODAY)[
            'no_upcoming_events'] is True
        flags = validate_season_config(
            {'events': [_event(days_offset=-3, priority='C')]}, TODAY)
        assert flags['no_upcoming_events'] is True

    def test_event_today_counts_as_upcoming(self):
        flags = validate_season_config(
            {'events': [_event(days_offset=0, priority='C')]}, TODAY)
        assert 'no_upcoming_events' not in flags

    def test_phase_overdue_flagged_with_days(self):
        config = {'periodization': {'target_transition': _iso(-9)},
                  'events': [_event(days_offset=30)]}
        flags = validate_season_config(config, TODAY)
        assert flags['phase_overdue'] == {
            'target_transition': _iso(-9), 'days_overdue': 9}

    def test_future_transition_not_overdue(self):
        config = {'periodization': {'target_transition': _iso(3)},
                  'events': [_event(days_offset=30)]}
        assert 'phase_overdue' not in validate_season_config(config, TODAY)

    def test_garbage_dates_and_empty_config_tolerated(self):
        assert validate_season_config({}, TODAY) == {'no_upcoming_events': True}
        config = {
            'current_block': {'start_date': 'soon', 'end_date': None},
            'periodization': {'phase_start': '', 'target_transition': 'later'},
            'events': [{'name': 'x', 'date': 'not-a-date', 'priority': 'A'}, 'junk'],
        }
        assert validate_season_config(config, TODAY) == {'no_upcoming_events': True}


# ---------------------------------------------------------------------------
# Shared fixtures for everything that touches coaching_log.json
# ---------------------------------------------------------------------------

@pytest.fixture
def log_dir(data_dir, monkeypatch):
    """Redirect planner.DATA_DIR and seed an empty coaching log."""
    monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
    (data_dir / 'coaching_log.json').write_text(json.dumps({
        'decisions': [],
        'pending_approvals': [],
        'athlete_responses': [],
        'metadata': {'created': '2026-01-01'},
    }), encoding='utf-8')
    return data_dir


def _read_log(data_dir):
    return json.loads((data_dir / 'coaching_log.json').read_text(encoding='utf-8'))


def _seed_decision(data_dir, decision_type, decision_text, days_ago=2):
    log = _read_log(data_dir)
    log['decisions'].append({
        'id': f'd_seed_{len(log["decisions"]) + 1}',
        'date': _iso(-days_ago),
        'type': decision_type,
        'decision': decision_text,
        'rationale': 'seeded',
        'status': 'active',
    })
    (data_dir / 'coaching_log.json').write_text(json.dumps(log), encoding='utf-8')


# ---------------------------------------------------------------------------
# 2a. ensure_tagged_proposal — the idempotency primitive
# ---------------------------------------------------------------------------

class TestEnsureTaggedProposal:
    TAG = 'season_replan:sani2c_stage_race:2026-05-31'

    def _ensure(self):
        return ensure_tagged_proposal(
            event_tag=self.TAG, action_type='season_replan',
            proposal='Sani2c Stage Race completed — debrief and re-plan the season',
            rationale='race passed without a debrief', impact='major')

    def test_creates_tagged_major_pending_approval(self, log_dir):
        result = self._ensure()

        assert result['created'] is True
        pending = _read_log(log_dir)['pending_approvals']
        assert len(pending) == 1
        p = pending[0]
        assert p['id'] == result['proposal_id']
        assert p['event_tag'] == self.TAG
        assert p['auto_generated'] is True
        assert p['action_type'] == 'season_replan'
        assert p['impact'] == 'major'
        # Tag registry records the creation (survives approve/expiry)
        assert self.TAG in _read_log(log_dir)[AUTO_PROPOSAL_TAGS_KEY]

    def test_second_call_is_noop_while_pending(self, log_dir):
        self._ensure()
        result = self._ensure()

        assert result == {'created': False, 'existing': 'pending',
                          'event_tag': self.TAG}
        assert len(_read_log(log_dir)['pending_approvals']) == 1

    def test_approved_tag_blocks_recreation(self, log_dir):
        pid = self._ensure()['proposal_id']
        approved = json.loads(approve_proposal(pid))
        assert approved['status'] == 'approved'

        result = self._ensure()

        assert result['created'] is False
        assert result['existing'] == 'recorded'
        assert _read_log(log_dir)['pending_approvals'] == []

    def test_rejected_tag_blocks_recreation(self, log_dir):
        pid = self._ensure()['proposal_id']
        rejected = json.loads(reject_proposal(pid, 'not yet'))
        assert rejected['status'] == 'rejected'

        result = self._ensure()

        assert result['created'] is False
        assert result['existing'] in ('rejected', 'recorded')
        assert _read_log(log_dir)['pending_approvals'] == []

    def test_find_tagged_proposal_states(self, log_dir):
        log = _read_log(log_dir)
        assert find_tagged_proposal(log, self.TAG) is None

        pid = self._ensure()['proposal_id']
        assert find_tagged_proposal(_read_log(log_dir), self.TAG) == 'pending'

        reject_proposal(pid, 'no')
        assert find_tagged_proposal(_read_log(log_dir), self.TAG) == 'rejected'


# ---------------------------------------------------------------------------
# 2b. Race-passed lifecycle (helper level)
# ---------------------------------------------------------------------------

class TestRacePassedProposals:
    def test_past_a_race_creates_season_replan_proposal(self, log_dir):
        created = _season_lifecycle_proposals(
            {'events': [_event(days_offset=-10)]}, TODAY)

        assert len(created) == 1
        pending = _read_log(log_dir)['pending_approvals']
        assert len(pending) == 1
        p = pending[0]
        assert p['action_type'] == 'season_replan'
        assert p['proposal'] == (
            'Sani2c Stage Race completed — debrief and re-plan the season')
        assert p['impact'] == 'major'
        assert p['event_tag'] == f'season_replan:sani2c_stage_race:{_iso(-10)}'

    def test_past_b_race_also_triggers(self, log_dir):
        created = _season_lifecycle_proposals(
            {'events': [_event(name='Joburg Half', days_offset=-3,
                               priority='B')]}, TODAY)
        assert len(created) == 1
        assert _read_log(log_dir)['pending_approvals'][0]['proposal'].startswith(
            'Joburg Half completed')

    def test_idempotent_across_repeated_detection(self, log_dir):
        config = {'events': [_event(days_offset=-10)]}
        first = _season_lifecycle_proposals(config, TODAY)
        second = _season_lifecycle_proposals(config, TODAY)
        third = _season_lifecycle_proposals(config, TODAY)

        assert len(first) == 1
        assert second == [] and third == []
        assert len(_read_log(log_dir)['pending_approvals']) == 1

    def test_skipped_when_race_review_decision_exists(self, log_dir):
        _seed_decision(log_dir, 'race_review',
                       'SANI2C STAGE RACE debrief: legs held, pacing worked')
        created = _season_lifecycle_proposals(
            {'events': [_event(days_offset=-10)]}, TODAY)

        assert created == []
        assert _read_log(log_dir)['pending_approvals'] == []

    def test_race_review_for_other_event_does_not_suppress(self, log_dir):
        _seed_decision(log_dir, 'race_review', 'Joburg Half debrief: PB')
        created = _season_lifecycle_proposals(
            {'events': [_event(days_offset=-10)]}, TODAY)
        assert len(created) == 1

    def test_non_race_review_decision_does_not_suppress(self, log_dir):
        _seed_decision(log_dir, 'load_adjustment',
                       'Sani2c Stage Race week: cut volume')
        created = _season_lifecycle_proposals(
            {'events': [_event(days_offset=-10)]}, TODAY)
        assert len(created) == 1

    @pytest.mark.parametrize('priority', ['C', 'D', 'life_event'])
    def test_low_priority_and_life_events_skipped(self, log_dir, priority):
        created = _season_lifecycle_proposals(
            {'events': [_event(days_offset=-10, priority=priority)]}, TODAY)

        assert created == []
        assert _read_log(log_dir)['pending_approvals'] == []

    def test_future_race_skipped(self, log_dir):
        created = _season_lifecycle_proposals(
            {'events': [_event(days_offset=10)]}, TODAY)
        assert created == []

    def test_proposal_flows_through_approve(self, log_dir):
        config = {'events': [_event(days_offset=-10)]}
        pid = _season_lifecycle_proposals(config, TODAY)[0]['proposal_id']

        approved = json.loads(approve_proposal(pid))
        assert approved['status'] == 'approved'
        log = _read_log(log_dir)
        decision = next(d for d in log['decisions']
                        if d.get('approved_from') == pid)
        assert decision['type'] == 'season_replan'
        assert decision['status'] == 'active'
        # Approval must not reopen the loop
        assert _season_lifecycle_proposals(config, TODAY) == []

    def test_proposal_flows_through_reject(self, log_dir):
        config = {'events': [_event(days_offset=-10)]}
        pid = _season_lifecycle_proposals(config, TODAY)[0]['proposal_id']

        rejected = json.loads(reject_proposal(pid, 'season is over, no replan'))
        assert rejected['status'] == 'rejected'
        log = _read_log(log_dir)
        assert log['pending_approvals'] == []
        assert log['rejected_proposals'][0]['id'] == pid
        # Rejection must not reopen the loop either
        assert _season_lifecycle_proposals(config, TODAY) == []


# ---------------------------------------------------------------------------
# 3. Phase-overdue nudge (helper level)
# ---------------------------------------------------------------------------

class TestPhaseOverdueProposals:
    def _config(self, transition_days_ago, phase='build'):
        return {'periodization': {'current_phase': phase,
                                  'target_transition': _iso(-transition_days_ago)}}

    def test_overdue_transition_creates_phase_proposal(self, log_dir):
        created = _season_lifecycle_proposals(self._config(10), TODAY)

        assert len(created) == 1
        p = _read_log(log_dir)['pending_approvals'][0]
        assert p['action_type'] == 'phase_transition'
        assert p['impact'] == 'major'
        assert 'build' in p['proposal']
        assert '10 days ago' in p['proposal']
        assert p['event_tag'] == f'phase_transition:build:{_iso(-10)}'

    def test_within_grace_window_no_proposal(self, log_dir):
        created = _season_lifecycle_proposals(
            self._config(PHASE_OVERDUE_GRACE_DAYS), TODAY)
        assert created == []

    def test_just_past_grace_window_proposes(self, log_dir):
        created = _season_lifecycle_proposals(
            self._config(PHASE_OVERDUE_GRACE_DAYS + 1), TODAY)
        assert len(created) == 1

    def test_idempotent_across_calls(self, log_dir):
        config = self._config(12)
        assert len(_season_lifecycle_proposals(config, TODAY)) == 1
        assert _season_lifecycle_proposals(config, TODAY) == []
        assert len(_read_log(log_dir)['pending_approvals']) == 1

    def test_skipped_when_phase_transition_decision_since(self, log_dir):
        # update_phase logged a phase_transition AFTER the target date
        _seed_decision(log_dir, 'phase_transition',
                       'Transitioned from build to peak', days_ago=5)
        created = _season_lifecycle_proposals(self._config(10), TODAY)
        assert created == []

    def test_phase_decision_before_target_does_not_suppress(self, log_dir):
        _seed_decision(log_dir, 'phase_transition',
                       'Transitioned from base to build', days_ago=20)
        created = _season_lifecycle_proposals(self._config(10), TODAY)
        assert len(created) == 1

    def test_no_target_transition_no_proposal(self, log_dir):
        assert _season_lifecycle_proposals(
            {'periodization': {'current_phase': 'build'}}, TODAY) == []
        assert _season_lifecycle_proposals({}, TODAY) == []


# ---------------------------------------------------------------------------
# 4. Snapshot integration: flags surface + proposals idempotent across calls
# ---------------------------------------------------------------------------

class _FakeGarminClient:
    """Empty-data Garmin fake — the season layer is local-data only."""

    def get_activities_by_date(self, start, end):
        return []

    def get_training_readiness(self, d):
        return {}

    def get_hrv_data(self, d):
        return None

    def get_sleep_data(self, d):
        return {}


@pytest.fixture
def season_env(data_dir, monkeypatch):
    """Full DATA_DIR redirect + a stale season config + empty coaching log."""
    for mod in (planner, rules, fitness_mod, parsers_mod, workout_builder,
                coaching_mod, planning_mod, strength_mod):
        monkeypatch.setattr(mod, 'DATA_DIR', data_dir)
    coaching_mod._garmin_fetch_cache.clear()

    fake_call = lambda fn: fn(_FakeGarminClient())
    monkeypatch.setattr(coaching_mod, 'garmin_api_call', fake_call)
    monkeypatch.setattr(fitness_mod, 'garmin_api_call', fake_call)
    monkeypatch.setattr(planning_mod, 'garmin_api_call', fake_call)
    monkeypatch.setattr(coaching_mod, 'fetch_activity_hr_zones', lambda a: a)

    (data_dir / 'athlete.json').write_text(json.dumps({
        'personal': {'name': 'Test Athlete', 'age': 38, 'weight_kg': 78},
        'injury_history': [], 'life_constraints': {}, 'preferences': {},
    }), encoding='utf-8')
    (data_dir / 'coaching_log.json').write_text(json.dumps({
        'decisions': [], 'pending_approvals': [], 'athlete_responses': [],
        'metadata': {'created': '2026-01-01'},
    }), encoding='utf-8')
    # The stale season: A race passed 10 days ago, block end before start,
    # transition overdue, nothing upcoming.
    (data_dir / 'training_config.json').write_text(json.dumps({
        'current_block': {'phase': 'build', 'start_date': _iso(-30),
                          'end_date': _iso(-40)},
        'periodization': {'current_phase': 'build', 'phase_start': _iso(-30),
                          'target_transition': _iso(-10)},
        'events': [_event(days_offset=-10)],
    }), encoding='utf-8')
    yield data_dir
    coaching_mod._garmin_fetch_cache.clear()


class TestSnapshotSeasonIntegration:
    async def test_data_quality_carries_season_flags(self, season_env, mock_ctx):
        result = json.loads(await get_coaching_snapshot(mock_ctx))

        dq = result['data_quality']
        assert dq['a_race_in_past'] == {
            'name': 'Sani2c Stage Race', 'date': _iso(-10), 'days_ago': 10}
        assert dq['no_upcoming_events'] is True
        assert dq['phase_overdue'] == {
            'target_transition': _iso(-10), 'days_overdue': 10}
        assert len(dq['block_dates_invalid']) == 1
        assert 'end_date' in dq['block_dates_invalid'][0]

    async def test_proposals_created_once_across_snapshot_calls(
            self, season_env, mock_ctx):
        first = json.loads(await get_coaching_snapshot(mock_ctx))
        second = json.loads(await get_coaching_snapshot(mock_ctx))

        for result in (first, second):
            pending = result['coaching_memory']['pending_approvals']
            tags = sorted(p.get('event_tag') for p in pending)
            assert tags == [
                f'phase_transition:build:{_iso(-10)}',
                f'season_replan:sani2c_stage_race:{_iso(-10)}',
            ], "exactly one proposal per trigger, surfaced in the snapshot"

        on_disk = _read_log(season_env)['pending_approvals']
        assert len(on_disk) == 2

    async def test_snapshot_proposal_approve_flow(self, season_env, mock_ctx):
        result = json.loads(await get_coaching_snapshot(mock_ctx))
        pending = result['coaching_memory']['pending_approvals']
        replan = next(p for p in pending
                      if p['action_type'] == 'season_replan')

        approved = json.loads(approve_proposal(replan['id']))
        assert approved['status'] == 'approved'

        # Next snapshot: decision active, proposal gone, nothing re-created
        result = json.loads(await get_coaching_snapshot(mock_ctx))
        memory = result['coaching_memory']
        pending_types = [p['action_type']
                         for p in memory['pending_approvals']]
        assert pending_types == ['phase_transition']
        assert any('debrief and re-plan the season' in (d.get('decision') or '')
                   for d in memory['active_decisions'])
