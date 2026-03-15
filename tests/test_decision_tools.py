"""Tests for tools/decision_tools.py — coaching decisions, approvals, athlete responses."""
import json
import pytest
from datetime import date, timedelta

import planner
from tools.decision_tools import (
    log_coaching_decision,
    get_active_decisions,
    update_decision_status,
    propose_major_change,
    list_pending_approvals,
    approve_coaching_change,
    reject_coaching_change,
    record_athlete_response,
    get_response_patterns,
)


@pytest.fixture
def decision_dir(data_dir, monkeypatch):
    """Redirect planner.DATA_DIR to tmp_path and seed empty coaching log."""
    monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
    (data_dir / 'coaching_log.json').write_text(json.dumps({
        'decisions': [],
        'pending_approvals': [],
        'athlete_responses': [],
        'metadata': {'created': '2026-01-01'},
    }))
    return data_dir


# ---------------------------------------------------------------------------
# log_coaching_decision
# ---------------------------------------------------------------------------

class TestLogCoachingDecision:
    def test_happy_path(self, decision_dir):
        result = json.loads(log_coaching_decision(
            decision_type='load_adjustment',
            decision='Reduce volume 10%',
            rationale='ACWR 1.4 — elevated risk',
        ))

        assert result['status'] == 'logged'
        assert result['decision_id'].startswith('d_')
        assert 'review_date' in result

    def test_review_date_custom(self, decision_dir):
        result = json.loads(log_coaching_decision(
            decision_type='exercise_selection',
            decision='Use hip thrusts',
            rationale='Glute weakness',
            review_days=14,
        ))

        expected = (date.today() + timedelta(days=14)).isoformat()
        assert result['review_date'] == expected


# ---------------------------------------------------------------------------
# get_active_decisions
# ---------------------------------------------------------------------------

class TestGetActiveDecisions:
    def test_filters_active(self, decision_dir):
        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        log['decisions'] = [
            {'id': 'd1', 'date': '2026-01-01', 'status': 'active', 'type': 'load_adjustment',
             'decision': 'd1', 'rationale': 'r1', 'review_date': '2026-02-01'},
            {'id': 'd2', 'date': '2026-01-02', 'status': 'completed', 'type': 'load_adjustment',
             'decision': 'd2', 'rationale': 'r2', 'review_date': '2026-02-01'},
        ]
        (decision_dir / 'coaching_log.json').write_text(json.dumps(log))

        result = json.loads(get_active_decisions())

        assert result['count'] == 1
        assert result['active_decisions'][0]['id'] == 'd1'

    def test_identifies_due_for_review(self, decision_dir):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        log['decisions'] = [
            {'id': 'd_due', 'date': '2026-01-01', 'status': 'active', 'type': 'load_adjustment',
             'decision': 'd', 'rationale': 'r', 'review_date': yesterday},
        ]
        (decision_dir / 'coaching_log.json').write_text(json.dumps(log))

        result = json.loads(get_active_decisions())

        assert 'd_due' in result['due_for_review']

    def test_empty_log(self, decision_dir):
        result = json.loads(get_active_decisions())
        assert result['count'] == 0
        assert result['active_decisions'] == []


# ---------------------------------------------------------------------------
# update_decision_status
# ---------------------------------------------------------------------------

class TestUpdateDecisionStatus:
    def test_valid_change(self, decision_dir):
        log_coaching_decision('load_adjustment', 'd1', 'r1')
        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        decision_id = log['decisions'][0]['id']

        result = json.loads(update_decision_status(decision_id, 'completed', 'Worked well'))

        assert result['status'] == 'updated'
        assert result['new_status'] == 'completed'
        assert result['outcome'] == 'Worked well'

    def test_not_found(self, decision_dir):
        result = json.loads(update_decision_status('nonexistent_id', 'completed'))
        assert 'error' in result
        assert 'not found' in result['error']

    def test_invalid_status(self, decision_dir):
        log_coaching_decision('load_adjustment', 'd1', 'r1')
        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        decision_id = log['decisions'][0]['id']

        result = json.loads(update_decision_status(decision_id, 'invalid_status'))
        assert 'error' in result
        assert 'Invalid status' in result['error']


# ---------------------------------------------------------------------------
# propose_major_change
# ---------------------------------------------------------------------------

class TestProposeMajorChange:
    def test_creates_proposal(self, decision_dir):
        result = json.loads(propose_major_change(
            change_type='phase_transition',
            proposal='Move to build phase',
            rationale='Base phase complete, CTL at target',
        ))

        assert result['status'] == 'proposed'
        assert result['proposal_id'].startswith('p_')
        assert 'expires' in result

    def test_expiry_date(self, decision_dir):
        result = json.loads(propose_major_change('phase_transition', 'p1', 'r1'))
        expected = (date.today() + timedelta(days=3)).isoformat()
        assert result['expires'] == expected


# ---------------------------------------------------------------------------
# list_pending_approvals
# ---------------------------------------------------------------------------

class TestListPendingApprovals:
    def test_filters_expired(self, decision_dir):
        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        log['pending_approvals'] = [
            {'id': 'p_expired', 'proposed_date': '2025-01-01', 'type': 'test',
             'proposal': 'old', 'rationale': 'r', 'expires': '2025-01-04'},
            {'id': 'p_valid', 'proposed_date': '2026-02-01', 'type': 'test',
             'proposal': 'new', 'rationale': 'r', 'expires': '2027-02-04'},
        ]
        (decision_dir / 'coaching_log.json').write_text(json.dumps(log))

        result = json.loads(list_pending_approvals())

        assert result['count'] == 1
        assert result['pending_approvals'][0]['id'] == 'p_valid'
        assert 'p_expired' in result['expired']


# ---------------------------------------------------------------------------
# approve_coaching_change
# ---------------------------------------------------------------------------

class TestApproveCoachingChange:
    def test_approve_moves_to_decisions(self, decision_dir):
        proposal = json.loads(propose_major_change('phase_transition', 'Move to build', 'CTL ready'))
        proposal_id = proposal['proposal_id']

        result = json.loads(approve_coaching_change(proposal_id))

        assert result['status'] == 'approved'
        assert result['now_active'] is True

        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        assert len(log['pending_approvals']) == 0
        active = [d for d in log['decisions'] if d['status'] == 'active']
        assert len(active) == 1
        assert active[0]['approved_from'] == proposal_id
        # Content must be carried from proposal to decision
        assert active[0]['decision'] == 'Move to build'
        assert active[0]['rationale'] == 'CTL ready'
        assert active[0]['type'] == 'phase_transition'

    def test_not_found(self, decision_dir):
        result = json.loads(approve_coaching_change('nonexistent'))
        assert 'error' in result


# ---------------------------------------------------------------------------
# reject_coaching_change
# ---------------------------------------------------------------------------

class TestRejectCoachingChange:
    def test_reject_archives_proposal(self, decision_dir):
        proposal = json.loads(propose_major_change('phase_transition', 'Bad idea', 'rationale'))
        proposal_id = proposal['proposal_id']

        result = json.loads(reject_coaching_change(proposal_id, reason='Not ready yet'))

        assert result['status'] == 'rejected'
        assert result['reason'] == 'Not ready yet'

        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        assert len(log['pending_approvals']) == 0
        assert len(log['rejected_proposals']) == 1

    def test_not_found(self, decision_dir):
        result = json.loads(reject_coaching_change('nonexistent'))
        assert 'error' in result


# ---------------------------------------------------------------------------
# record_athlete_response
# ---------------------------------------------------------------------------

class TestRecordAthleteResponse:
    def test_caps_at_200_keeping_newest(self, decision_dir):
        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        log['athlete_responses'] = [
            {'date': '2026-01-01', 'stimulus': f's{i}', 'response': f'r{i}'}
            for i in range(205)
        ]
        (decision_dir / 'coaching_log.json').write_text(json.dumps(log))

        record_athlete_response('new_stimulus', 'new_response')

        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        assert len(log['athlete_responses']) == 200
        # Newest entry must survive — oldest entries are the ones dropped
        assert log['athlete_responses'][-1]['stimulus'] == 'new_stimulus'
        assert log['athlete_responses'][0]['stimulus'] == 's6'  # s0-s5 dropped

    def test_records_with_pattern(self, decision_dir):
        result = json.loads(record_athlete_response(
            stimulus='Long ride 2.5hrs Z2',
            response='Felt strong, readiness 72 next day',
            pattern='handles_volume_well',
        ))

        assert result['status'] == 'recorded'
        assert result['pattern'] == 'handles_volume_well'

    def test_records_numeric_fields(self, decision_dir):
        record_athlete_response(
            stimulus='Interval session',
            response='Readiness dropped 8 points',
            load_change_pct=18.5,
            compliance_result=True,
            readiness_delta=-8.0,
            injury_flag=False,
            session_purpose_achieved=True,
        )
        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        rec = log['athlete_responses'][-1]
        assert rec['load_change_pct'] == 18.5
        assert rec['compliance_result'] is True
        assert rec['readiness_delta'] == -8.0
        assert rec['injury_flag'] is False
        assert rec['session_purpose_achieved'] is True

    def test_numeric_fields_optional(self, decision_dir):
        """Existing call without numeric fields still works."""
        record_athlete_response('ride', 'good')
        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        rec = log['athlete_responses'][-1]
        assert 'load_change_pct' not in rec
        assert 'compliance_result' not in rec


# ---------------------------------------------------------------------------
# get_response_patterns
# ---------------------------------------------------------------------------

class TestGetResponsePatterns:
    def test_extracts_patterns(self, decision_dir):
        log = json.loads((decision_dir / 'coaching_log.json').read_text())
        log['athlete_responses'] = [
            {'date': '2026-01-01', 'stimulus': 's', 'response': 'r', 'pattern': 'handles_volume_well'},
            {'date': '2026-01-02', 'stimulus': 's', 'response': 'r', 'pattern': 'handles_volume_well'},
            {'date': '2026-01-03', 'stimulus': 's', 'response': 'r', 'pattern': 'recovers_quickly'},
        ]
        (decision_dir / 'coaching_log.json').write_text(json.dumps(log))

        result = json.loads(get_response_patterns())

        assert result['pattern_count'] == 2
        assert result['patterns']['handles_volume_well']['count'] == 2
        assert result['patterns']['recovers_quickly']['count'] == 1

    def test_empty_responses(self, decision_dir):
        result = json.loads(get_response_patterns())

        assert result['pattern_count'] == 0
        assert result['recent_responses'] == []
