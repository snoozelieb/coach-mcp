"""
Tests for training rule engine.
"""
import pytest
from unittest.mock import patch
from rules import (
    classify_activity,
    check_weekly_compliance,
    check_safety_rules,
    get_upcoming_events,
)


class TestClassifyActivity:
    def test_classifies_strength_training(self):
        activity = {'type': 'strength_training', 'duration_mins': 45}
        result = classify_activity(activity)

        assert result['is_strength'] is True
        assert result['is_mobility'] is False
        assert result['is_long_effort'] is False

    def test_classifies_yoga_as_mobility(self):
        activity = {'type': 'yoga', 'duration_mins': 30}
        result = classify_activity(activity)

        assert result['is_mobility'] is True
        assert result['is_strength'] is False

    def test_classifies_pilates_as_mobility(self):
        activity = {'type': 'pilates', 'duration_mins': 20}
        result = classify_activity(activity)

        assert result['is_mobility'] is True

    def test_classifies_long_run_as_long_effort(self):
        activity = {'type': 'running', 'duration_mins': 75}
        result = classify_activity(activity)

        assert result['is_long_effort'] is True

    def test_short_run_not_long_effort(self):
        activity = {'type': 'running', 'duration_mins': 45}
        result = classify_activity(activity)

        assert result['is_long_effort'] is False

    def test_classifies_ultimate_as_hard(self):
        activity = {'type': 'ultimate_disc', 'duration_mins': 60}
        result = classify_activity(activity)

        assert result['is_hard'] is True

    def test_classifies_high_hr_as_hard(self):
        activity = {'type': 'running', 'duration_mins': 30, 'avg_hr': 165}
        result = classify_activity(activity)

        assert result['is_hard'] is True

    def test_classifies_high_max_hr_as_hard(self):
        activity = {'type': 'running', 'duration_mins': 30, 'max_hr': 185}
        result = classify_activity(activity)

        assert result['is_hard'] is True

    def test_easy_run_not_hard(self):
        activity = {'type': 'running', 'duration_mins': 30, 'avg_hr': 130}
        result = classify_activity(activity)

        assert result['is_hard'] is False

    def test_handles_missing_fields(self):
        activity = {'type': 'unknown'}
        result = classify_activity(activity)

        assert result['is_strength'] is False
        assert result['is_mobility'] is False
        assert result['is_long_effort'] is False
        assert result['is_hard'] is False


class TestCheckWeeklyCompliance:
    def test_detects_strength_deficit(self):
        activities = [
            {'type': 'running', 'duration_mins': 45},
            {'type': 'strength_training', 'duration_mins': 45},  # Only 1
        ]
        pillars = {
            'strength_sessions_per_week': 2,
            'mobility_minutes_per_week': 0,
            'long_effort_per_week': 0,
        }

        result = check_weekly_compliance(activities, pillars)

        assert result['strength']['completed'] == 1
        assert result['strength']['deficit'] == 1
        assert result['strength']['compliant'] is False
        assert 'strength' in result['deficits']

    def test_detects_mobility_deficit(self):
        activities = [
            {'type': 'yoga', 'duration_mins': 30},
            {'type': 'pilates', 'duration_mins': 20},  # Total 50 mins
        ]
        pillars = {
            'strength_sessions_per_week': 0,
            'mobility_minutes_per_week': 90,  # Requires 90
            'long_effort_per_week': 0,
        }

        result = check_weekly_compliance(activities, pillars)

        assert result['mobility']['completed'] == 50
        assert result['mobility']['deficit'] == 40
        assert result['mobility']['compliant'] is False

    def test_detects_long_effort_compliance(self):
        activities = [
            {'type': 'running', 'duration_mins': 90},  # Long effort
            {'type': 'running', 'duration_mins': 30},  # Not long enough
        ]
        pillars = {
            'strength_sessions_per_week': 0,
            'mobility_minutes_per_week': 0,
            'long_effort_per_week': 1,
        }

        result = check_weekly_compliance(activities, pillars)

        assert result['long_effort']['completed'] == 1
        assert result['long_effort']['compliant'] is True

    def test_calculates_volume_compliance(self):
        activities = [
            {'type': 'running', 'duration_mins': 60},
            {'type': 'strength_training', 'duration_mins': 45},
            {'type': 'cycling', 'duration_mins': 90},
        ]
        pillars = {
            'strength_sessions_per_week': 0,
            'mobility_minutes_per_week': 0,
            'long_effort_per_week': 0,
            'weekly_volume_target_hrs': 4.0,  # Target 4 hrs
        }

        result = check_weekly_compliance(activities, pillars)

        # Total: 195 mins = 3.25 hrs, rounded to 3.2 hrs = 80% of 4 hrs
        assert result['volume']['actual_hrs'] == 3.2  # rounded
        assert result['volume']['percent'] == 80
        assert result['volume']['compliant'] is True  # 80%+ is acceptable

    def test_overall_compliance_all_met(self):
        activities = [
            {'type': 'strength_training', 'duration_mins': 45},
            {'type': 'strength_training', 'duration_mins': 45},
            {'type': 'yoga', 'duration_mins': 60},
            {'type': 'pilates', 'duration_mins': 30},
            {'type': 'running', 'duration_mins': 75},
        ]
        pillars = {
            'strength_sessions_per_week': 2,
            'mobility_minutes_per_week': 90,
            'long_effort_per_week': 1,
            'weekly_volume_target_hrs': 4.0,
        }

        result = check_weekly_compliance(activities, pillars)

        assert result['overall_compliant'] is True
        assert result['deficits'] == []

    def test_handles_empty_activities(self):
        pillars = {
            'strength_sessions_per_week': 2,
            'mobility_minutes_per_week': 90,
            'long_effort_per_week': 1,
        }

        result = check_weekly_compliance([], pillars)

        assert result['strength']['completed'] == 0
        assert result['mobility']['completed'] == 0
        assert result['long_effort']['completed'] == 0
        assert result['overall_compliant'] is False


class TestCheckSafetyRules:
    def test_warns_on_consecutive_hard_days(self):
        activities = [
            {'type': 'ultimate_disc', 'duration_mins': 60, 'date': '2025-12-05'},
            {'type': 'interval_training', 'duration_mins': 45, 'date': '2025-12-04'},
        ]

        result = check_safety_rules(activities, constraints={'max_consecutive_hard_days': 2})

        assert any('consecutive hard days' in w for w in result['warnings'])

    def test_blocks_hard_after_max_consecutive(self):
        activities = [
            {'type': 'ultimate_disc', 'duration_mins': 60},
            {'type': 'hiit', 'duration_mins': 30},
        ]
        today_plan = {'type': 'interval_training', 'duration_mins': 45}

        result = check_safety_rules(
            activities,
            today_plan,
            constraints={'max_consecutive_hard_days': 2}
        )

        assert result['safe'] is False
        assert any('consecutive hard days' in b for b in result['blocked'])

    def test_safe_when_no_issues(self):
        activities = [
            {'type': 'running', 'duration_mins': 30, 'avg_hr': 130},
            {'type': 'yoga', 'duration_mins': 45},
        ]

        result = check_safety_rules(activities)

        assert result['safe'] is True
        assert result['blocked'] == []

    def test_warns_on_recent_race(self):
        activities = [
            {'type': 'running', 'name': 'Park Run Race', 'duration_mins': 25},
        ]

        result = check_safety_rules(activities)

        assert any('race' in w.lower() for w in result['warnings'])


class TestGetUpcomingEvents:
    @patch('rules.load_training_config')
    def test_returns_events_within_range(self, mock_config):
        from datetime import date, timedelta

        today = date.today()
        mock_config.return_value = {
            'events': [
                {'date': (today + timedelta(days=10)).isoformat(), 'name': 'Event 1'},
                {'date': (today + timedelta(days=100)).isoformat(), 'name': 'Too Far'},
                {'date': (today + timedelta(days=30)).isoformat(), 'name': 'Event 2'},
            ]
        }

        result = get_upcoming_events(days_ahead=56)

        assert len(result) == 2
        assert result[0]['name'] == 'Event 1'  # Closest first
        assert result[1]['name'] == 'Event 2'

    @patch('rules.load_training_config')
    def test_calculates_days_until(self, mock_config):
        from datetime import date, timedelta

        today = date.today()
        mock_config.return_value = {
            'events': [
                {'date': (today + timedelta(days=15)).isoformat(), 'name': 'Event'},
            ]
        }

        result = get_upcoming_events()

        assert result[0]['days_until'] == 15

    @patch('rules.load_training_config')
    def test_excludes_past_events(self, mock_config):
        from datetime import date, timedelta

        today = date.today()
        mock_config.return_value = {
            'events': [
                {'date': (today - timedelta(days=5)).isoformat(), 'name': 'Past Event'},
                {'date': (today + timedelta(days=10)).isoformat(), 'name': 'Future'},
            ]
        }

        result = get_upcoming_events()

        assert len(result) == 1
        assert result[0]['name'] == 'Future'
