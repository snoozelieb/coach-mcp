"""
Tests for planner module - context building, plan and suggestion management.
"""
import json
import pytest
from pathlib import Path
from datetime import date, timedelta
from coach.planner import (
    build_planning_context,
    save_suggestion,
    get_pending_suggestions,
    approve_suggestion,
    reject_suggestion,
    create_empty_week_template,
    save_json_file,
    load_json_file,
    get_week_constraints,
    DATA_DIR,
)
import coach.planner as planner


class TestBuildPlanningContext:
    def test_includes_all_required_sections(self):
        context = build_planning_context(
            athlete_profile={'baseline': {'avg_weekly_volume_hrs': 5}},
            training_config={'current_block': {'phase': 'base'}},
            recent_activities=[],
            compliance_status={'overall_compliant': True},
            today_recovery={'score': 75},
        )

        assert 'today' in context
        assert 'day_of_week' in context
        assert 'athlete_profile' in context
        assert 'current_block' in context
        assert 'compliance' in context
        assert 'recovery' in context
        assert 'upcoming_events' in context

    def test_filters_recent_activities_to_7_days(self):
        today = date.today()
        activities = [
            {'date': (today - timedelta(days=3)).isoformat(), 'type': 'running'},
            {'date': (today - timedelta(days=10)).isoformat(), 'type': 'cycling'},
        ]

        context = build_planning_context(
            athlete_profile={},
            training_config={},
            recent_activities=activities,
            compliance_status={},
            today_recovery={},
        )

        assert len(context['activities_last_7_days']) == 1
        assert context['activities_last_7_days'][0]['type'] == 'running'

    def test_extracts_all_future_events_excludes_past(self):
        today = date.today()
        config = {
            'events': [
                {'date': (today + timedelta(days=30)).isoformat(), 'name': 'Near Event', 'priority': 'A'},
                {'date': (today + timedelta(days=100)).isoformat(), 'name': 'Far Event', 'priority': 'B'},
                {'date': (today - timedelta(days=5)).isoformat(), 'name': 'Past Event', 'priority': 'C'},
            ]
        }

        context = build_planning_context(
            athlete_profile={},
            training_config=config,
            recent_activities=[],
            compliance_status={},
            today_recovery={},
        )

        # All future events included, past excluded, sorted by days_until
        assert len(context['upcoming_events']) == 2
        assert context['upcoming_events'][0]['name'] == 'Near Event'
        assert context['upcoming_events'][0]['days_until'] == 30
        assert context['upcoming_events'][1]['name'] == 'Far Event'
        assert context['upcoming_events'][1]['days_until'] == 100

    def test_identifies_next_a_race(self):
        today = date.today()
        config = {
            'events': [
                {'date': (today + timedelta(days=45)).isoformat(), 'name': 'A Race', 'priority': 'A'},
                {'date': (today + timedelta(days=20)).isoformat(), 'name': 'B Race', 'priority': 'B'},
            ]
        }

        context = build_planning_context(
            athlete_profile={},
            training_config=config,
            recent_activities=[],
            compliance_status={},
            today_recovery={},
        )

        assert context['next_a_race'] is not None
        assert context['next_a_race']['name'] == 'A Race'

    def test_includes_pending_suggestions(self):
        suggestions = [{'id': 'sug_1', 'description': 'Test suggestion'}]

        context = build_planning_context(
            athlete_profile={},
            training_config={},
            recent_activities=[],
            compliance_status={},
            today_recovery={},
            pending_suggestions=suggestions,
        )

        assert len(context['pending_suggestions']) == 1
        assert context['pending_suggestions'][0]['id'] == 'sug_1'


class TestSuggestionManagement:
    @pytest.fixture
    def temp_data_dir(self, tmp_path):
        """Temporarily redirect DATA_DIR to tmp_path."""
        original = planner.DATA_DIR
        planner.DATA_DIR = tmp_path
        yield tmp_path
        planner.DATA_DIR = original

    def test_save_and_retrieve_suggestion(self, temp_data_dir):
        suggestion_id = save_suggestion({
            'type': 'pillar_adjustment',
            'description': 'Increase strength to 3x/week',
            'rationale': 'Consistent overachievement',
        })

        assert suggestion_id.startswith('sug_')

        pending = get_pending_suggestions()
        assert len(pending) == 1
        assert pending[0]['description'] == 'Increase strength to 3x/week'
        assert pending[0]['status'] == 'pending'

    def test_approve_suggestion_moves_to_history(self, temp_data_dir):
        suggestion_id = save_suggestion({
            'type': 'test',
            'description': 'Test suggestion',
        })

        result = approve_suggestion(suggestion_id)

        assert result is not None
        assert result['status'] == 'approved'

        pending = get_pending_suggestions()
        assert len(pending) == 0

        # Check history
        data = load_json_file('suggestions.json')
        assert len(data['history']) == 1
        assert data['history'][0]['status'] == 'approved'

    def test_reject_suggestion_with_reason(self, temp_data_dir):
        suggestion_id = save_suggestion({
            'type': 'test',
            'description': 'Test suggestion',
        })

        result = reject_suggestion(suggestion_id, reason='Not applicable')

        assert result is not None
        assert result['status'] == 'rejected'
        assert result['rejection_reason'] == 'Not applicable'

    def test_approve_nonexistent_returns_none(self, temp_data_dir):
        result = approve_suggestion('nonexistent_id')
        assert result is None

    def test_reject_nonexistent_returns_none(self, temp_data_dir):
        result = reject_suggestion('nonexistent_id')
        assert result is None


class TestCreateEmptyWeekTemplate:
    def test_creates_7_day_structure(self):
        template = create_empty_week_template()

        assert 'days' in template
        assert len(template['days']) == 7

    def test_days_start_from_today(self):
        today = date.today()
        template = create_empty_week_template()

        assert template['week_start'] == today.isoformat()
        assert today.isoformat() in template['days']

    def test_each_day_has_required_fields(self):
        template = create_empty_week_template()

        for day_date, day_data in template['days'].items():
            assert 'day_name' in day_data
            assert 'planned' in day_data
            assert 'actual' in day_data
            assert 'status' in day_data
            assert day_data['status'] == 'pending'

    def test_days_are_consecutive(self):
        today = date.today()
        template = create_empty_week_template()

        dates = sorted(template['days'].keys())
        for i, day_str in enumerate(dates):
            expected = (today + timedelta(days=i)).isoformat()
            assert day_str == expected


class TestUpdateWeeklyPlanValidation:
    """Tests for structure validation in update_weekly_plan."""

    def test_rejects_non_dict_plan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(planner, 'DATA_DIR', tmp_path)
        from coach.tools.planning_tools import update_weekly_plan
        result = json.loads(update_weekly_plan(json.dumps([1, 2, 3])))
        assert 'error' in result
        assert 'object' in result['error'].lower() or 'dict' in result['error'].lower()

    def test_rejects_plan_without_days(self, tmp_path, monkeypatch):
        monkeypatch.setattr(planner, 'DATA_DIR', tmp_path)
        from coach.tools.planning_tools import update_weekly_plan
        result = json.loads(update_weekly_plan(json.dumps({'week_start': '2026-02-08'})))
        assert 'error' in result
        assert 'days' in result['error']

    def test_rejects_plan_with_non_dict_days(self, tmp_path, monkeypatch):
        monkeypatch.setattr(planner, 'DATA_DIR', tmp_path)
        from coach.tools.planning_tools import update_weekly_plan
        result = json.loads(update_weekly_plan(json.dumps({'days': 'not a dict'})))
        assert 'error' in result
        assert 'days' in result['error']

    def test_accepts_valid_plan(self, tmp_path, monkeypatch):
        monkeypatch.setattr(planner, 'DATA_DIR', tmp_path)
        from coach.tools.planning_tools import update_weekly_plan
        valid_plan = {'days': {'2026-02-08': {'planned': {'type': 'running'}}}}
        result = json.loads(update_weekly_plan(json.dumps(valid_plan)))
        assert result['status'] == 'success'


# ---------------------------------------------------------------------------
# get_week_constraints
# ---------------------------------------------------------------------------

class TestGetWeekConstraints:
    def test_basic_constraints(self):
        athlete = {
            'life_constraints': {'blocked_days': ['Wednesday']},
            'training_pillars': {
                'strength': {
                    'target_type': 'sessions',
                    'target_sessions_per_week': 2,
                    'types': ['strength_training'],
                },
            },
        }
        training_config = {
            'current_block': {'phase': 'build'},
        }
        methodology = {
            'session_guidelines': {
                'strength': {
                    'build': {'duration_range': [45, 60], 'intensity': 'moderate'},
                },
            },
        }
        result = get_week_constraints(
            athlete=athlete,
            training_config=training_config,
            methodology=methodology,
        )
        assert result['blocked_days'] == ['Wednesday']
        assert result['phase'] == 'build'
        assert 'strength' in result['pillar_requirements']
        assert result['pillar_requirements']['strength']['min_sessions'] == 2
        assert 'strength' in result['session_guidelines']

    def test_empty_athlete(self):
        result = get_week_constraints(athlete={}, training_config={}, methodology={})
        assert result['phase'] == 'base'
        assert 'blocked_days' not in result
        assert 'pillar_requirements' not in result

    def test_injury_restrictions(self):
        injuries = [
            {'name': 'knee', 'status': 'active', 'severity': 'moderate',
             'restrictions': ['no running']},
            {'name': 'old_ankle', 'status': 'resolved'},  # Should be excluded
        ]
        result = get_week_constraints(
            athlete={}, training_config={}, methodology={},
            injuries=injuries,
        )
        assert len(result['injury_restrictions']) == 1
        assert result['injury_restrictions'][0]['name'] == 'knee'

    def test_a_race_key_sessions(self):
        training_config = {
            'current_block': {'phase': 'build'},
            'events': [
                {'name': 'sani2c', 'priority': 'A', 'type': 'multi_day_mtb', 'date': '2026-05-01'},
            ],
        }
        methodology = {
            'race_templates': {
                'multi_day_mtb': {
                    'key_sessions': [
                        {'type': 'long_ride', 'priority': 'critical'},
                        {'type': 'intervals', 'priority': 'high'},
                        {'type': 'recovery', 'priority': 'medium'},
                    ],
                    'phase_guidance': {'build': 'Focus on sustained power.'},
                },
            },
        }
        result = get_week_constraints(
            athlete={}, training_config=training_config, methodology=methodology,
        )
        assert result['a_race']['name'] == 'sani2c'
        assert result['a_race']['sport'] == 'cycling'
        assert 'long_ride' in result['key_session_types']
        assert 'intervals' in result['key_session_types']
        assert 'recovery' not in result['key_session_types']  # medium priority excluded
        assert result['phase_guidance'] == 'Focus on sustained power.'

    def test_chronic_misses_from_diagnostics(self):
        compliance_diagnostics = {
            'per_pillar': {
                'strength': {'met_weeks': 1, 'total_weeks': 4, 'chronic_miss': True},
                'endurance': {'met_weeks': 4, 'total_weeks': 4, 'chronic_miss': False},
            },
        }
        result = get_week_constraints(
            athlete={}, training_config={}, methodology={},
            compliance_diagnostics=compliance_diagnostics,
        )
        assert result['chronic_misses'] == ['strength']

    def test_hours_pillar_converted_to_mins(self):
        athlete = {
            'training_pillars': {
                'endurance': {
                    'target_type': 'hours',
                    'target_hours_per_week': 4,
                    'types': ['cycling', 'running'],
                },
            },
        }
        result = get_week_constraints(athlete=athlete, training_config={}, methodology={})
        assert result['pillar_requirements']['endurance']['min_mins'] == 240

    def test_session_guidelines_filtered_by_phase(self):
        methodology = {
            'session_guidelines': {
                '_description': 'should be skipped',
                'intervals': {
                    'base': {'intensity': 'moderate'},
                    'build': {'intensity': 'hard'},
                },
            },
        }
        training_config = {'current_block': {'phase': 'base'}}
        result = get_week_constraints(
            athlete={}, training_config=training_config, methodology=methodology,
        )
        assert result['session_guidelines']['intervals']['intensity'] == 'moderate'
