"""Consent at the tool level: lifting an injury protection needs the athlete's
own words plus the coach's reasoning, and leaves a decision behind."""
import json

import coach.planner as planner
from coach.tools.injury_tools import update_injury_status

INJURY = {'date': '2026-05-20', 'type': 'peroneal tendon', 'status': 'active',
          'severity': 'moderate', 'restricted_activities': ['running', 'jumping'],
          'safe_activities': ['cycling']}


def _seed(sandbox):
    (sandbox / 'athlete.json').write_text(json.dumps({'injury_history': [dict(INJURY)]}),
                                          encoding='utf-8')


def _injury(sandbox):
    return json.loads((sandbox / 'athlete.json').read_text(encoding='utf-8'))['injury_history'][0]


class TestInjuryResolution:
    def test_resolving_without_consent_changes_nothing(self, sandbox_data_dir):
        _seed(sandbox_data_dir)
        result = json.loads(update_injury_status('2026-05-20', new_status='resolved'))
        assert result['error'] == 'athlete_confirmation_required'
        assert result['current_restricted_activities'] == ['jumping', 'running']
        assert _injury(sandbox_data_dir)['status'] == 'active'

    def test_statement_alone_is_not_consent(self, sandbox_data_dir):
        _seed(sandbox_data_dir)
        result = json.loads(update_injury_status(
            '2026-05-20', new_status='resolved', athlete_statement='I feel fine'))
        assert result['error'] == 'athlete_confirmation_required'

    def test_resolving_with_consent_logs_a_reviewable_decision(self, sandbox_data_dir):
        _seed(sandbox_data_dir)
        result = json.loads(update_injury_status(
            '2026-05-20', new_status='resolved',
            athlete_statement='No pain for three weeks, physio cleared me',
            rationale='physio clearance 2026-10-20; pain-free hopping test'))
        assert result['status'] == 'success'
        assert _injury(sandbox_data_dir)['status'] == 'resolved'
        decision = planner.load_coaching_log()['decisions'][-1]
        assert decision['id'] == result['decisions_logged'][0]
        assert decision['type'] == 'injury_resolved'
        assert decision['athlete_statement'] == 'No pain for three weeks, physio cleared me'
        assert decision['review_date'] > decision['date']

    def test_notes_and_improving_need_no_consent(self, sandbox_data_dir):
        _seed(sandbox_data_dir)
        result = json.loads(update_injury_status(
            '2026-05-20', new_status='improving', notes='walked 30 min pain-free'))
        assert result['status'] == 'success'
        assert _injury(sandbox_data_dir)['status'] == 'improving'
        assert _injury(sandbox_data_dir)['progress_notes'][0]['note'] == 'walked 30 min pain-free'


class TestRestrictionsArmTheGate:
    def test_adding_restrictions_needs_no_consent(self, sandbox_data_dir):
        _seed(sandbox_data_dir)
        result = json.loads(update_injury_status(
            '2026-05-20', restricted_activities=['running', 'jumping', 'padel']))
        assert result['status'] == 'success'
        assert _injury(sandbox_data_dir)['restricted_activities'] == ['jumping', 'padel', 'running']
        assert 'decisions_logged' not in result

    def test_removing_a_restriction_needs_consent(self, sandbox_data_dir):
        _seed(sandbox_data_dir)
        refused = json.loads(update_injury_status(
            '2026-05-20', restricted_activities=['jumping']))
        assert refused['error'] == 'athlete_confirmation_required'
        assert _injury(sandbox_data_dir)['restricted_activities'] == ['running', 'jumping']

        result = json.loads(update_injury_status(
            '2026-05-20', restricted_activities=['jumping'],
            athlete_statement='Running felt fine twice this week',
            rationale='two pain-free 20 min runs, swelling gone'))
        assert result['status'] == 'success'
        assert _injury(sandbox_data_dir)['restricted_activities'] == ['jumping']
        decision = planner.load_coaching_log()['decisions'][-1]
        assert decision['type'] == 'injury_restrictions_relaxed'

    def test_empty_restrictions_on_an_active_injury_warn(self, sandbox_data_dir):
        _seed(sandbox_data_dir)
        result = json.loads(update_injury_status(
            '2026-05-20', restricted_activities=[],
            athlete_statement='I am fully fine', rationale='cleared'))
        assert result['status'] == 'success'
        assert 'not armed' in result['warning']

    def test_safe_activities_are_free_to_edit(self, sandbox_data_dir):
        _seed(sandbox_data_dir)
        result = json.loads(update_injury_status(
            '2026-05-20', safe_activities=['cycling', 'swimming']))
        assert result['status'] == 'success'
        assert _injury(sandbox_data_dir)['safe_activities'] == ['cycling', 'swimming']
