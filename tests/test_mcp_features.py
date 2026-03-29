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


# ---------------------------------------------------------------------------
# Interactive tools — fallback path tests
# ---------------------------------------------------------------------------

class TestInteractiveTools:
    @pytest.mark.asyncio
    async def test_smart_brief_fallback_when_sampling_unavailable(self, mock_ctx):
        """When ctx.sample() raises, should return structured data."""
        mock_ctx.sample = AsyncMock(side_effect=Exception("Sampling not supported"))

        result = json.loads(await generate_smart_brief(mock_ctx))
        assert isinstance(result, dict)
        # Should contain the fallback note
        assert 'note' in result or 'athlete_name' in result

    @pytest.mark.asyncio
    async def test_check_in_fallback_when_elicitation_unavailable(self, mock_ctx):
        """When ctx.elicit() raises, should return question list."""
        mock_ctx.elicit = AsyncMock(side_effect=Exception("Elicitation not supported"))

        result = json.loads(await interactive_check_in(mock_ctx))
        assert isinstance(result, dict)
        assert 'questions' in result or 'note' in result
