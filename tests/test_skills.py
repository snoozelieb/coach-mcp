"""The sport authoring skills and the workout-author agent are reviewable
markdown, but their JSON examples must obey the session contract — a skill
that teaches a stale key is worse than none. Every fenced ```json session in
the skills validates against coach/authoring.py with NO catalogue (so every
off-catalogue exercise example must state its category, as a clean install
would need).
"""
import json
import re
from pathlib import Path

import ast

import pytest

from coach.authoring import validate_session
from coach.resources import COACHING_DOCTRINE
from coach.schemas import WeeklyPlan

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / '.claude' / 'skills'
SPORT_SKILLS = ('running-session-author', 'cycling-session-author',
                'strength-session-author', 'mobility-session-author')
AGENT_FILE = ROOT / '.claude' / 'agents' / 'workout-author.md'

# Claude Code lists a skill's description from startup (1,536-char cap per
# skill); the Agent Skills spec caps names at 64 and descriptions at 1,024.
NAME_MAX = 64
DESCRIPTION_MAX = 1024


def _frontmatter(text: str) -> dict:
    match = re.match(r'^---\n(.*?)\n---\n', text, re.S)
    assert match, 'missing YAML frontmatter'
    fields = {}
    for line in match.group(1).splitlines():
        key, _, value = line.partition(':')
        fields[key.strip()] = value.strip().strip('"')
    return fields


def _json_blocks(text: str) -> list:
    return [json.loads(block) for block in re.findall(r'```json\n(.*?)\n```', text, re.S)]


@pytest.mark.parametrize('skill', SPORT_SKILLS)
class TestSportSkills:
    def test_frontmatter_names_the_skill_and_fits_the_budgets(self, skill):
        text = (SKILLS_DIR / skill / 'SKILL.md').read_text(encoding='utf-8')
        meta = _frontmatter(text)
        assert meta['name'] == skill
        assert len(meta['name']) <= NAME_MAX
        assert 0 < len(meta['description']) <= DESCRIPTION_MAX
        # A skill named like the MCP server silently drops the server (claude-code #85827)
        assert meta['name'] not in ('coach', 'coach-mcp', 'garmin-coach-mcp')

    def test_every_json_example_satisfies_the_contract(self, skill):
        text = (SKILLS_DIR / skill / 'SKILL.md').read_text(encoding='utf-8')
        sessions = [b for b in _json_blocks(text) if isinstance(b, dict) and 'type' in b]
        assert sessions, 'a sport skill carries at least one worked example'
        for session in sessions:
            report = validate_session(session, catalogue=None)
            assert report.ok, (skill, session.get('name'), report.errors)
            assert session.get('purpose'), 'every example states its purpose (the purpose gate)'

    def test_skill_teaches_the_preview_loop(self, skill):
        text = (SKILLS_DIR / skill / 'SKILL.md').read_text(encoding='utf-8')
        assert 'preview_workout' in text
        assert 'update_weekly_plan' in text

    def test_every_json_example_passes_the_typed_save_schema(self, skill):
        """The preview loop dead-ends if the typed WeeklyPlan schema rejects a
        session the contract accepts (red-team finding S1)."""
        text = (SKILLS_DIR / skill / 'SKILL.md').read_text(encoding='utf-8')
        for session in [b for b in _json_blocks(text) if isinstance(b, dict) and 'type' in b]:
            WeeklyPlan.model_validate({'days': {'2030-01-01': {'planned': session}}})

    def test_skill_sizes_from_capacity_and_memory_not_templates(self, skill):
        text = (SKILLS_DIR / skill / 'SKILL.md').read_text(encoding='utf-8')
        assert 'record_athlete_response' in text and 'adaptation_patterns' in text
        if skill != 'mobility-session-author':
            assert 'capacity' in text


def test_doctrine_worked_example_obeys_the_contract():
    """The doctrine resource carries the canonical structured-run example; it
    must not drift from the contract the renderer enforces."""
    start = COACHING_DOCTRINE.index("    {'type': 'running'")
    end = COACHING_DOCTRINE.index("## Injury Protocol", start)
    literal = '\n'.join(line.strip() for line in COACHING_DOCTRINE[start:end].splitlines())
    session = ast.literal_eval(literal)
    report = validate_session(session, catalogue=None)
    assert report.ok, report.errors
    WeeklyPlan.model_validate({'days': {'2030-01-01': {'planned': session}}})


class TestWorkoutAuthorAgent:
    def test_agent_definition_is_tracked_and_well_formed(self):
        assert AGENT_FILE.exists()
        meta = _frontmatter(AGENT_FILE.read_text(encoding='utf-8'))
        assert meta['name'] == 'workout-author'
        assert 'preview_workout' in meta['description']

    def test_agent_never_saves_or_pushes_on_its_own(self):
        text = AGENT_FILE.read_text(encoding='utf-8')
        assert 'never call update_weekly_plan or push_plan_to_garmin' in text
        meta = _frontmatter(text)
        # Prompt-only "never" decays; the agent holds an ALLOWLIST of read and
        # preview tools, so the tools that write, push or ingest do not exist for it
        allowed = {t.strip() for t in meta.get('tools', '').split(',') if t.strip()}
        assert 'mcp__coach-mcp__preview_workout' in allowed
        assert 'mcp__coach-mcp__list_exercises' in allowed
        assert 'disallowedTools' not in meta
        for tool in ('update_weekly_plan', 'push_plan_to_garmin', 'update_injury_status',
                     'training_program', 'get_coaching_snapshot', 'update_athlete',
                     'diagnose_injury', 'approve_proposal', 'update_phase', 'remove_race'):
            assert not any(tool in t for t in allowed), tool
        for tool in ('Edit', 'Write', 'Bash'):
            assert tool not in allowed, tool
        assert 'missing_brief' in text

    def test_gitignore_tracks_only_this_agent(self):
        ignore = (ROOT / '.gitignore').read_text(encoding='utf-8')
        assert '!.claude/agents/workout-author.md' in ignore
        assert '.claude/agents/*' in ignore  # the user's other agents stay local
