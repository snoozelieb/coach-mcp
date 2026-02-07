"""Tests for tools/coaching_tools.py — helper functions (pure logic, no mocking needed)."""
import pytest
from datetime import date
from unittest.mock import patch

from tools.coaching_tools import (
    _compare_planned_actual,
    _parse_readiness_for_snapshot,
    _build_adaptation_signals,
    _analyze_sport_priorities,
)


# ---------------------------------------------------------------------------
# _compare_planned_actual
# ---------------------------------------------------------------------------

class TestComparePlannedActual:
    def test_no_plan_returns_status(self):
        result = _compare_planned_actual(None, [], date(2026, 1, 15))
        assert result['status'] == 'no_plan'

    def test_empty_plan_days_returns_no_plan(self):
        result = _compare_planned_actual({'days': {}}, [], date(2026, 1, 15))
        assert result['status'] == 'no_plan'

    def test_plan_with_only_rest_days(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'rest'}},
                '2026-01-14': {'planned': {}},
            }
        }
        result = _compare_planned_actual(plan, [], date(2026, 1, 15))
        assert result['sessions_planned'] == 0

    def test_all_completed(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 45}},
                '2026-01-14': {'planned': {'type': 'strength_training', 'duration_mins': 60}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
            {'date': '2026-01-14', 'type': 'strength_training', 'duration_mins': 55},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_planned'] == 2
        assert result['sessions_completed'] == 2
        assert result['sessions_missed'] == 0
        assert result['completion_rate'] == 100.0

    def test_missed_sessions(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 45}},
                '2026-01-14': {'planned': {'type': 'strength_training', 'duration_mins': 60}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        assert result['sessions_missed'] == 1
        assert result['completion_rate'] == 50.0
        assert len(result['gaps']) == 1

    def test_pending_future_sessions(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 45}},
                '2026-01-16': {'planned': {'type': 'cycling', 'duration_mins': 90}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        assert result['sessions_pending'] == 1

    def test_duration_surplus_flagged(self):
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 45}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 75},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))
        assert len(result['surpluses']) == 1

    def test_duration_gap_flagged(self):
        """A session significantly shorter than planned should be flagged as a gap."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'cycling', 'duration_mins': 90}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'cycling', 'duration_mins': 30},  # 67% short
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1  # Still counts as "completed"
        assert len(result['gaps']) == 1            # But flagged as a duration gap

    def test_only_first_activity_per_day_used(self):
        """When multiple activities exist on the same day, only the first is checked.
        This is a known limitation — the second activity is silently ignored."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'cycling', 'duration_mins': 90}},
            }
        }
        # Morning run + evening cycling — code only sees the run
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 30},
            {'date': '2026-01-13', 'type': 'cycling', 'duration_mins': 90},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        # Counts as completed (because *an* activity exists) but actual type is wrong
        assert result['sessions_completed'] == 1
        detail = result['details'][0]
        assert detail['actual'] == 'running'  # First match, not the planned cycling

    def test_invalid_date_key_skipped(self):
        """Non-ISO date keys in plan are silently skipped, not counted."""
        plan = {
            'days': {
                'Monday': {'planned': {'type': 'running', 'duration_mins': 45}},
                '2026-01-14': {'planned': {'type': 'strength_training', 'duration_mins': 60}},
            }
        }
        activities = [
            {'date': '2026-01-14', 'type': 'strength_training', 'duration_mins': 55},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        # Only the valid ISO date should be processed
        assert result['sessions_planned'] == 1
        assert result['sessions_completed'] == 1

    def test_zero_planned_duration_no_gap_or_surplus(self):
        """When planned_duration is 0 (or missing), the duration comparison is skipped.
        This guards against a division-by-zero in the percentage calculation."""
        plan = {
            'days': {
                '2026-01-13': {'planned': {'type': 'running', 'duration_mins': 0}},
            }
        }
        activities = [
            {'date': '2026-01-13', 'type': 'running', 'duration_mins': 45},
        ]
        result = _compare_planned_actual(plan, activities, date(2026, 1, 15))

        assert result['sessions_completed'] == 1
        assert result['gaps'] == []
        assert result['surpluses'] == []


# ---------------------------------------------------------------------------
# _parse_readiness_for_snapshot
# ---------------------------------------------------------------------------

class TestParseReadinessForSnapshot:
    def test_empty_data(self):
        result = _parse_readiness_for_snapshot({})
        assert result['status'] == 'unavailable'

    def test_none_data(self):
        result = _parse_readiness_for_snapshot(None)
        assert result['status'] == 'unavailable'


# ---------------------------------------------------------------------------
# _build_adaptation_signals
# ---------------------------------------------------------------------------

class TestBuildAdaptationSignals:
    @patch('tools.coaching_tools.load_coaching_log')
    def test_all_data_present(self, mock_log):
        mock_log.return_value = {
            'athlete_responses': [
                {'date': '2026-01-01', 'stimulus': 'test', 'response': 'positive', 'pattern': 'handles_volume_well'},
                {'date': '2026-01-02', 'stimulus': 'test', 'response': 'good', 'pattern': 'handles_volume_well'},
            ]
        }

        sleep_data = {'avg_duration_hrs': 7.5, 'avg_score': 80, 'recent_avg_duration': 7.8,
                      'recent_trend': 0.5, 'status': 'adequate', 'acute_status': 'good',
                      'poor_quality_nights': 0}
        recovery = {'score': 72, 'level': 'HIGH', 'hrv_status': 'BALANCED'}
        compliance = {'overall_compliant': True, 'strength': {'compliant': True}, 'mobility': {'compliant': True}}

        result = _build_adaptation_signals(sleep_data, recovery, compliance, {}, date(2026, 1, 15))

        assert result['sleep']['avg_7d_hrs'] == 7.5
        assert result['sleep']['trend'] == 'improving'
        assert result['recovery']['readiness_score'] == 72
        assert result['compliance']['overall_compliant'] is True
        assert result['adaptation_patterns']['handles_volume_well'] is True
        assert result['adaptation_patterns']['total_responses'] == 2

    @patch('tools.coaching_tools.load_coaching_log')
    def test_empty_coaching_log(self, mock_log):
        mock_log.return_value = {'athlete_responses': []}

        result = _build_adaptation_signals(None, {}, {}, {}, date(2026, 1, 15))

        assert result['sleep']['avg_7d_hrs'] is None
        assert result['sleep']['trend'] == 'stable'
        assert result['adaptation_patterns']['handles_volume_well'] is False
        assert result['adaptation_patterns']['total_responses'] == 0

    @patch('tools.coaching_tools.load_coaching_log')
    def test_declining_sleep_trend(self, mock_log):
        mock_log.return_value = {'athlete_responses': []}

        sleep_data = {'avg_duration_hrs': 6.0, 'avg_score': 60, 'recent_avg_duration': 5.8,
                      'recent_trend': -0.5, 'status': 'deficit', 'acute_status': 'poor',
                      'poor_quality_nights': 3}

        result = _build_adaptation_signals(sleep_data, {}, {}, {}, date(2026, 1, 15))

        assert result['sleep']['trend'] == 'declining'
        assert result['sleep']['deficit_days_7d'] == 3

    @patch('tools.coaching_tools.load_coaching_log')
    def test_coaching_log_exception_handled(self, mock_log):
        mock_log.side_effect = Exception("File not found")

        result = _build_adaptation_signals(None, {}, {}, {}, date(2026, 1, 15))

        assert result['adaptation_patterns']['handles_volume_well'] is None
        assert result['adaptation_patterns']['patterns_logged'] == 0

    @patch('tools.coaching_tools.load_coaching_log')
    def test_tied_patterns_default_to_false(self, mock_log):
        """When competing patterns have equal counts (e.g., 2x handles_volume_well
        vs 2x struggles_with_volume), the comparison > returns False."""
        mock_log.return_value = {
            'athlete_responses': [
                {'pattern': 'handles_volume_well'},
                {'pattern': 'handles_volume_well'},
                {'pattern': 'struggles_with_volume'},
                {'pattern': 'struggles_with_volume'},
            ]
        }

        result = _build_adaptation_signals(None, {}, {}, {}, date(2026, 1, 15))

        # Tie: 2 == 2, so 2 > 2 is False
        assert result['adaptation_patterns']['handles_volume_well'] is False

    @patch('tools.coaching_tools.load_coaching_log')
    def test_compliance_rate_calculation(self, mock_log):
        """Compliance rate is calculated from pillar counts, not from overall_compliant."""
        mock_log.return_value = {'athlete_responses': []}

        compliance = {
            'overall_compliant': False,
            'strength': {'compliant': True},
            'mobility': {'compliant': False},
            'long_effort': {'compliant': True},
        }

        result = _build_adaptation_signals(None, {}, compliance, {}, date(2026, 1, 15))

        # 2 of 3 pillars met = 67%
        assert result['compliance']['rate_this_week'] == 67.0


# ---------------------------------------------------------------------------
# _analyze_sport_priorities
# ---------------------------------------------------------------------------

class TestAnalyzeSportPriorities:
    def test_single_cycling_event(self):
        events = [{'name': 'sani2c', 'date': '2026-06-01', 'priority': 'A', 'type': 'multi_day_mtb'}]
        result = _analyze_sport_priorities(events, {}, {})

        assert 'cycling' in result['sports']
        assert result['sports']['cycling']['primary_focus'] is True
        assert result['has_multi_sport'] is False

    def test_multi_sport_volume_sums_to_100(self):
        events = [
            {'name': 'sani2c', 'date': '2026-06-01', 'priority': 'A', 'type': 'multi_day_mtb'},
            {'name': '10k trail', 'date': '2026-04-01', 'priority': 'B', 'type': 'trail_ultra'},
        ]
        result = _analyze_sport_priorities(events, {}, {})

        assert result['has_multi_sport'] is True
        total_pct = sum(s['volume_pct'] for s in result['sports'].values())
        assert abs(total_pct - 100.0) < 0.5

    def test_past_events_filtered(self):
        events = [
            {'name': 'past_race', 'date': '2025-01-01', 'priority': 'A', 'type': 'road_cycling'},
            {'name': 'future_race', 'date': '2026-06-01', 'priority': 'B', 'type': 'trail_ultra'},
        ]
        result = _analyze_sport_priorities(events, {}, {})

        assert 'cycling' not in result['sports']
        assert 'running' in result['sports']

    def test_no_events(self):
        result = _analyze_sport_priorities([], {}, {})

        assert result['sports'] == {}
        assert result['has_multi_sport'] is False

    def test_closer_race_gets_higher_volume_pct(self):
        """A close B-race should get more volume than a far A-race when time_weight
        dominates. Verify the actual percentages, not just has_multi_sport."""
        events = [
            {'name': 'far_race', 'date': '2026-09-01', 'priority': 'A', 'type': 'multi_day_mtb'},
            {'name': 'close_race', 'date': '2026-02-20', 'priority': 'B', 'type': 'trail_ultra'},
        ]
        result = _analyze_sport_priorities(events, {}, {})

        # close B-race (13 days away): priority_weight=3, time_weight=4 → score=12
        # far A-race (206 days away): priority_weight=4, time_weight=1 → score=4
        running = result['sports']['running']
        cycling = result['sports']['cycling']
        assert running['volume_pct'] > cycling['volume_pct']
        assert running['total_score'] == 12
        assert cycling['total_score'] == 4
