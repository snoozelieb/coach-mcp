"""Tests for MCP prompts, resources, and interactive tools.

Smoke tests to verify these features don't crash on clean or normal installs.
"""
import json
import pytest
from unittest.mock import AsyncMock

import coach.planner as planner
import coach.rules as rules
import coach.fitness as fitness_mod

from coach.prompts import (
    weekly_planning_prompt,
    morning_brief_prompt,
    injury_assessment_prompt,
    week_review_prompt,
    onboarding_prompt,
)
from coach.resources import (
    athlete_profile_resource,
    weekly_plan_resource,
    training_config_resource,
    coaching_decisions_resource,
    current_time_context_resource,
)
from coach.tools.interactive_tools import (
    generate_smart_brief,
    interactive_check_in,
)


# ---------------------------------------------------------------------------
# Fixture: redirect DATA_DIR to empty temp dir
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def empty_data_dir(tmp_path, monkeypatch):
    """Point all modules at an empty temp dir."""
    monkeypatch.setattr(planner, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(rules, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(fitness_mod, 'DATA_DIR', tmp_path)
    # Write minimal methodology.json so load_methodology() doesn't fail
    (tmp_path / 'methodology.json').write_text(json.dumps({
        'personas': {},
        'safety_rules': {},
        'race_templates': {},
    }))
    # Write minimal training_config.json
    (tmp_path / 'training_config.json').write_text(json.dumps({
        'events': [],
        'current_block': {},
    }))
    # Write minimal athlete.json
    (tmp_path / 'athlete.json').write_text(json.dumps({
        'personal': {'name': 'Test'},
        'injury_history': [],
        'life_constraints': {},
    }))


# ---------------------------------------------------------------------------
# Prompt smoke tests
# ---------------------------------------------------------------------------

class TestPrompts:
    def test_weekly_planning_returns_prompt_result(self):
        result = weekly_planning_prompt()
        assert result.messages is not None
        assert len(result.messages) > 0

    def test_weekly_planning_with_notes(self):
        result = weekly_planning_prompt(notes="Focus on cycling this week")
        msg = result.messages[0]
        text = msg.content if isinstance(msg.content, str) else msg.content.text
        assert "Focus on cycling" in text

    def test_morning_brief_returns_prompt_result(self):
        result = morning_brief_prompt()
        assert result.messages is not None

    def test_injury_assessment_returns_prompt_result(self):
        result = injury_assessment_prompt(body_region="knee")
        # Message.content may be a TextContent object or string
        msg = result.messages[0]
        text = msg.content if isinstance(msg.content, str) else msg.content.text
        assert "knee" in text

    def test_injury_assessment_with_description(self):
        result = injury_assessment_prompt(body_region="shin", description="sharp pain when running")
        msg = result.messages[0]
        text = msg.content if isinstance(msg.content, str) else msg.content.text
        assert "sharp pain" in text

    def test_week_review_returns_prompt_result(self):
        result = week_review_prompt()
        assert result.messages is not None

    def test_onboarding_returns_prompt_result(self):
        result = onboarding_prompt()
        assert result.messages is not None


# ---------------------------------------------------------------------------
# Resource smoke tests
# ---------------------------------------------------------------------------

class TestResources:
    def test_athlete_profile_resource(self):
        result = json.loads(athlete_profile_resource())
        assert isinstance(result, dict)
        assert 'personal' in result

    def test_weekly_plan_resource_no_plan(self):
        result = json.loads(weekly_plan_resource())
        assert isinstance(result, dict)
        # No plan file exists, should gracefully return no_plan or empty
        assert 'status' in result or 'days' in result

    def test_training_config_resource(self):
        result = json.loads(training_config_resource())
        assert isinstance(result, dict)

    def test_coaching_decisions_resource(self):
        result = json.loads(coaching_decisions_resource())
        assert isinstance(result, dict)

    def test_current_time_context_resource(self):
        result = json.loads(current_time_context_resource())
        assert isinstance(result, dict)
        expected_keys = {
            'timestamp', 'date', 'day_of_week', 'hour', 'minute',
            'time_period', 'is_weekend', 'timezone_note',
        }
        assert set(result.keys()) == expected_keys
        assert result['time_period'] in (
            'early_morning', 'morning', 'afternoon', 'evening', 'night'
        )


# ---------------------------------------------------------------------------
# Interactive tools — data-gathering (sampling/elicitation paths removed)
# ---------------------------------------------------------------------------

class TestInteractiveTools:
    def test_smart_brief_returns_structured_data(self):
        result = json.loads(generate_smart_brief())
        assert isinstance(result, dict)
        assert 'framing' in result
        assert 'current_time_context' in result
        assert 'athlete_name' in result
        assert 'today_plan' in result
        assert 'active_injuries' in result

    def test_check_in_returns_question_set(self):
        result = json.loads(interactive_check_in())
        assert isinstance(result, dict)
        assert 'questions' in result
        question_ids = [q['id'] for q in result['questions']]
        assert question_ids == ['feeling', 'sleep', 'niggles']
        assert 'current_time_context' in result
        assert 'coaching_note' in result
