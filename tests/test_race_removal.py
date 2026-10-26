"""remove_race matches the exact name: an empty or partial name deletes nothing."""
import json

import coach.planner as planner
from coach.tools.race_tools import remove_race

EVENTS = [
    {'name': 'Two Oceans Half', 'date': '2027-04-03', 'priority': 'A'},
    {'name': 'Two Oceans Ultra', 'date': '2027-04-03', 'priority': 'B'},
    {'name': 'Parkrun', 'date': '2026-11-07', 'priority': 'C'},
]


def _seed():
    planner.save_json_file('training_config.json', {'events': [dict(e) for e in EVENTS]})


def _names():
    return [e['name'] for e in planner.load_json_file('training_config.json')['events']]


def test_empty_name_removes_nothing():
    _seed()
    result = json.loads(remove_race('   '))
    assert result['error'] == 'missing_name'
    assert len(_names()) == 3


def test_partial_name_removes_nothing_and_suggests():
    _seed()
    result = json.loads(remove_race('Two Oceans'))
    assert 'nothing was removed' in result['error']
    assert result['suggestions'] == ['Two Oceans Half', 'Two Oceans Ultra']
    assert len(_names()) == 3


def test_exact_name_case_insensitive_removes_one():
    _seed()
    result = json.loads(remove_race('two oceans half'))
    assert result['status'] == 'success'
    assert _names() == ['Two Oceans Ultra', 'Parkrun']


def test_requires_user_interaction_meta():
    import asyncio
    from coach.mcp_app import mcp
    tool = asyncio.run(mcp.get_tool('remove_race'))
    assert (tool.meta or {}).get('anthropic/requiresUserInteraction') is True
