"""Phase 2.4 consolidation contract: races + query_metrics.

The old list_races / add_race / update_race / research_race tools became
actions on the single `races` tool; get_fitness_status /
get_intensity_distribution / get_daily_metrics / get_training_readiness /
get_personal_records became kinds on `query_metrics`. Bodies were MOVED
(not rewritten) into private impls — these tests pin:

1. routing parity: each action/kind returns exactly what the old
   implementation returns for the same inputs
2. registry hygiene: old tool names are gone; races / query_metrics /
   remove_race are registered with the agreed annotations
3. doc hygiene: no stale tool names in CLAUDE.md (outside the historical
   "Removed / consolidated tools" and "Known Issues" sections), the prompt
   templates, the coaching doctrine, or scripts/daily_loop.py
"""
import json
import re
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import server  # noqa: F401 — imports all tool modules, triggers registration
from coach.mcp_app import mcp, SERVER_INSTRUCTIONS
from coach.resources import COACHING_DOCTRINE
import coach.fitness as fitness_lib
import coach.planner as planner
import coach.rules as rules
from coach.tools.data_tools import _daily_metrics, _personal_records
from coach.tools.fitness_tools import (
    query_metrics,
    _fitness_status,
    _intensity_distribution,
    _training_readiness,
)
from coach.tools.race_tools import (
    races,
    _add_race,
    _list_races,
    _research_race,
    _update_race,
)

from conftest import SAMPLE_PR_DATA, SAMPLE_RUNNING_ACTIVITY

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TODAY = date.today()

OLD_TOOL_NAMES = {
    'list_races', 'add_race', 'update_race', 'research_race',
    'get_fitness_status', 'get_intensity_distribution', 'get_daily_metrics',
    'get_training_readiness', 'get_personal_records',
}

SAMPLE_READINESS = [{
    'calendarDate': TODAY.isoformat(),
    'score': 72,
    'level': 'HIGH',
    'sleepScore': 85,
    'recoveryTimeInHours': 12,
    'hrvStatus': 'BALANCED',
    'acuteLoad': 450.5,
    'feedbackPhrase': 'Ready to push.',
}]

RACE_PAGE_TEXT = (
    "The Gravel Classic covers 120 km of gravel roads with 2,400 m of "
    "climbing and elevation gain through technical singletrack sections."
)


def _seed_config(data_dir, events):
    (data_dir / 'training_config.json').write_text(
        json.dumps({'events': events}), encoding='utf-8')


def _sample_events():
    return [
        {'date': (TODAY + timedelta(days=60)).isoformat(),
         'name': 'Gravel Classic', 'priority': 'A', 'type': 'road_cycling',
         'url': 'https://example.com/gravel-classic'},
        {'date': (TODAY + timedelta(days=21)).isoformat(),
         'name': 'Tune-up 10k', 'priority': 'C', 'type': '10k'},
    ]


@pytest.fixture
def race_env(tmp_path, monkeypatch):
    """Sandbox the data dir for every module race_tools touches."""
    monkeypatch.setattr(rules, 'DATA_DIR', tmp_path)
    monkeypatch.setattr(planner, 'DATA_DIR', tmp_path)
    _seed_config(tmp_path, _sample_events())
    return tmp_path


@pytest.fixture
def fitness_env(tmp_path, monkeypatch):
    """Seed a v2 fitness history and point coach.fitness at it."""
    monkeypatch.setattr(fitness_lib, 'DATA_DIR', tmp_path)
    daily_loads = {}
    for i in range(35):
        day = (TODAY - timedelta(days=i)).isoformat()
        load = 60.0 if i % 2 == 0 else 40.0
        daily_loads[day] = {
            'total': load,
            'by_sport': {'cycling': load},
            'activities': [{
                'id': i, 'type': 'cycling', 'sport': 'cycling',
                'duration_mins': 60, 'load': load, 'date': day,
            }],
        }
    history = {
        'schema_version': 2,
        'daily_loads': daily_loads,
        'snapshots': [],
        'sleep_history': [],
        'last_updated': TODAY.isoformat(),
    }
    (tmp_path / 'fitness_history.json').write_text(
        json.dumps(history), encoding='utf-8')
    return tmp_path


# ---------------------------------------------------------------------------
# races: each action routes to the moved implementation
# ---------------------------------------------------------------------------

class TestRacesParity:
    def test_list_matches_old_impl(self, race_env):
        via_dispatcher = races(action='list')
        direct = _list_races()

        assert via_dispatcher == direct
        assert via_dispatcher['count'] == 2
        # Sorted by date with days_until computed, like the old list_races
        assert [r['name'] for r in via_dispatcher['races']] == \
            ['Tune-up 10k', 'Gravel Classic']
        assert via_dispatcher['races'][0]['days_until'] == 21

    def test_add_matches_old_impl(self, race_env):
        args = dict(name='Spring Marathon',
                    race_date=(TODAY + timedelta(days=120)).isoformat(),
                    priority='B', race_type='marathon', distance_km=42.2)

        via_dispatcher = races(action='add', **args)
        _seed_config(race_env, _sample_events())  # reset for the direct call
        direct = _add_race(**args)

        assert via_dispatcher == direct
        assert via_dispatcher['status'] == 'success'
        assert via_dispatcher['event']['priority'] == 'B'
        assert via_dispatcher['sport'] == 'running'

    def test_add_duplicate_priority_rejected_like_old_impl(self, race_env):
        result = races(action='add', name='Second A Race',
                       race_date=(TODAY + timedelta(days=90)).isoformat(),
                       priority='A')
        assert 'error' in result
        assert 'Gravel Classic' in result['error']

    def test_add_requires_name_date_priority(self, race_env):
        result = races(action='add')
        assert 'error' in result
        for field in ('name', 'race_date', 'priority'):
            assert field in result['error']

    def test_update_matches_old_impl(self, race_env):
        new_date = (TODAY + timedelta(days=75)).isoformat()

        via_dispatcher = races(action='update', name='gravel',
                               new_date=new_date, target_time='6:30')
        _seed_config(race_env, _sample_events())  # reset for the direct call
        direct = _update_race('gravel', new_date=new_date, target_time='6:30')

        assert via_dispatcher == direct
        assert via_dispatcher['status'] == 'success'
        assert via_dispatcher['event']['date'] == new_date
        assert via_dispatcher['event']['target_time'] == '6:30'

    def test_update_requires_name(self, race_env):
        result = races(action='update', new_priority='B')
        assert 'error' in result
        assert 'name' in result['error']

    @patch('coach.tools.race_tools.fetch_page_text')
    def test_research_matches_old_impl(self, mock_fetch, race_env):
        mock_fetch.return_value = RACE_PAGE_TEXT

        via_dispatcher = races(action='research',
                               url='https://example.com/gravel-classic')
        direct = _research_race(url='https://example.com/gravel-classic')

        assert via_dispatcher == direct
        assert via_dispatcher['detected_info']['distance_km'] == 120
        assert via_dispatcher['detected_info']['surface'] == 'gravel'

    @patch('coach.tools.race_tools.fetch_page_text')
    def test_research_resolves_url_from_race_name(self, mock_fetch, race_env):
        mock_fetch.return_value = RACE_PAGE_TEXT
        result = races(action='research', name='Gravel Classic')
        assert result['url'] == 'https://example.com/gravel-classic'

    def test_research_without_name_or_url_errors(self, race_env):
        result = races(action='research')
        assert result == {'error': 'Provide either a race name or URL'}

    def test_unknown_action_lists_valid_actions(self, race_env):
        result = races(action='delete', name='Gravel Classic')
        assert 'error' in result
        for action in ('list', 'add', 'update', 'research'):
            assert action in result['error']


# ---------------------------------------------------------------------------
# query_metrics: each kind routes to the moved implementation
# ---------------------------------------------------------------------------

class TestQueryMetricsParity:
    def test_fitness_matches_old_impl(self, fitness_env):
        via_dispatcher = query_metrics(kind='fitness', days=30)
        direct = _fitness_status(30)

        assert via_dispatcher == direct
        overall = via_dispatcher['metrics']['overall']
        assert overall['ctl'] > 0
        assert overall['acwr_status'] in ('low', 'optimal', 'elevated', 'danger')
        assert 'cycling' in via_dispatcher['metrics']['by_sport']
        assert via_dispatcher['data_quality']['days_with_data'] == 35

    def test_fitness_no_data_matches_old_impl(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fitness_lib, 'DATA_DIR', tmp_path)
        via_dispatcher = query_metrics(kind='fitness')
        assert via_dispatcher == _fitness_status(30)
        assert via_dispatcher['status'] == 'no_data'

    @patch('coach.tools.fitness_tools.fetch_activity_hr_zones',
           side_effect=lambda acts: acts)
    @patch('coach.tools.fitness_tools.garmin_api_call')
    def test_intensity_matches_old_impl(self, mock_api, _mock_zones,
                                        tmp_path, monkeypatch):
        monkeypatch.setattr(fitness_lib, 'DATA_DIR', tmp_path)  # no athlete.json
        mock_api.return_value = [SAMPLE_RUNNING_ACTIVITY]

        via_dispatcher = query_metrics(kind='intensity', days=28)
        direct = _intensity_distribution(28)

        assert via_dispatcher == direct
        assert via_dispatcher['period']['days'] == 28
        assert via_dispatcher['period']['activities_count'] == 1

    @patch('coach.tools.fitness_tools.garmin_api_call')
    def test_intensity_no_activities_matches_old_impl(self, mock_api):
        mock_api.return_value = []
        via_dispatcher = query_metrics(kind='intensity', days=28)
        assert via_dispatcher == _intensity_distribution(28)
        assert via_dispatcher['status'] == 'no_activities'

    @patch('coach.tools.data_tools.garmin_api_call')
    def test_daily_matches_old_impl(self, mock_api):
        mock_client = Mock()
        mock_client.get_user_summary.return_value = {'restingHeartRate': 42}
        mock_client.get_body_battery.return_value = [
            {'bodyBatteryValuesArray': [[1700000000000, 55]]}]
        mock_client.get_training_readiness.return_value = SAMPLE_READINESS
        mock_api.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        via_dispatcher = query_metrics(kind='daily')
        direct = _daily_metrics()

        assert via_dispatcher == direct
        assert via_dispatcher['rhr'] == 42
        assert via_dispatcher['sleep_score'] == 85
        assert via_dispatcher['date'] == TODAY.isoformat()

    @patch('coach.tools.fitness_tools.garmin_api_call')
    def test_readiness_matches_old_impl(self, mock_api):
        mock_client = Mock()
        mock_client.get_training_readiness.return_value = SAMPLE_READINESS
        mock_client.get_hrv_data.return_value = {
            'hrvSummary': {
                'status': 'BALANCED', 'lastNightAvg': 52, 'weeklyAvg': 55,
                'baseline': {'balancedLow': 45, 'balancedUpper': 62},
                'feedbackPhrase': 'HRV_BALANCED',
            },
        }
        mock_api.side_effect = lambda fn, *a, **kw: fn(mock_client, *a, **kw)

        for_date = TODAY.isoformat()
        via_dispatcher = query_metrics(kind='readiness', for_date=for_date)
        direct = _training_readiness(for_date)

        assert via_dispatcher == direct
        assert via_dispatcher['score'] == 72
        assert via_dispatcher['hrv_last_night_avg'] == 52

    @patch('coach.tools.data_tools.garmin_api_call')
    def test_personal_records_matches_old_impl(self, mock_api):
        mock_api.return_value = SAMPLE_PR_DATA

        via_dispatcher = query_metrics(kind='personal_records')
        direct = _personal_records()

        assert via_dispatcher == direct
        assert via_dispatcher['count'] == 3
        assert len(via_dispatcher['personal_records']) == 3

    def test_unknown_kind_lists_valid_kinds(self):
        result = query_metrics(kind='bogus')
        assert 'error' in result
        for kind in ('fitness', 'intensity', 'daily', 'readiness',
                     'personal_records'):
            assert kind in result['error']

    @patch('coach.tools.data_tools.garmin_api_call')
    def test_garmin_failure_returns_error_dict(self, mock_api):
        mock_api.side_effect = Exception('Connection timeout')
        result = query_metrics(kind='daily')
        assert 'error' in result
        assert 'Connection timeout' in result['error']


# ---------------------------------------------------------------------------
# Registry hygiene: old names gone, new tools registered + annotated
# ---------------------------------------------------------------------------

@pytest.fixture(scope='module')
async def tools_by_name():
    tools = await mcp.list_tools()
    return {t.name: t for t in tools}


class TestRegistry:
    async def test_old_tool_names_absent(self, tools_by_name):
        leftovers = OLD_TOOL_NAMES & set(tools_by_name)
        assert not leftovers, (
            f'Consolidated tools still registered: {sorted(leftovers)}'
        )

    async def test_consolidated_tools_present(self, tools_by_name):
        for name in ('races', 'query_metrics', 'remove_race',
                     'get_activities_range'):
            assert name in tools_by_name, f'{name} missing from registry'

    async def test_races_annotations(self, tools_by_name):
        ann = tools_by_name['races'].annotations
        assert ann.readOnlyHint is False      # add/update write config
        assert ann.destructiveHint is False   # deletion lives on remove_race
        assert ann.idempotentHint is False    # action='add' appends per call
        assert ann.openWorldHint is True      # action='research' hits the web

    async def test_query_metrics_annotations(self, tools_by_name):
        ann = tools_by_name['query_metrics'].annotations
        assert ann.readOnlyHint is True
        assert ann.openWorldHint is True      # most kinds hit Garmin
        assert not ann.destructiveHint

    async def test_remove_race_stays_standalone_destructive(self, tools_by_name):
        ann = tools_by_name['remove_race'].annotations
        assert ann.destructiveHint is True
        assert ann.readOnlyHint is False
        assert ann.openWorldHint is False

    async def test_dispatcher_docstrings_cover_every_action_and_kind(
            self, tools_by_name):
        races_doc = tools_by_name['races'].description
        for action in ('list', 'add', 'update', 'research'):
            assert f"'{action}'" in races_doc
        metrics_doc = tools_by_name['query_metrics'].description
        for kind in ('fitness', 'intensity', 'daily', 'readiness',
                     'personal_records'):
            assert f"'{kind}'" in metrics_doc


# ---------------------------------------------------------------------------
# Doc hygiene: renames are atomic — no stale tool names in living docs
# ---------------------------------------------------------------------------

def _stale_hits(text: str) -> list[str]:
    """Old tool names appearing as whole identifiers (add_race won't match
    add_race_template — '_' is a word character, so \\b doesn't fire)."""
    return sorted(n for n in OLD_TOOL_NAMES if re.search(rf'\b{n}\b', text))


class TestDocHygiene:
    def test_claude_md_has_no_stale_tool_names(self):
        text = (PROJECT_ROOT / 'CLAUDE.md').read_text(encoding='utf-8')

        # The historical sections legitimately name removed tools — strip
        # them, then require the rest of the doc to be fully renamed.
        known_issues = text.find('## Known Issues / TODO')
        assert known_issues != -1
        text = text[:known_issues]

        removed_start = text.find('### Removed / consolidated tools')
        removed_end = text.find('## Commands')
        assert removed_start != -1 and removed_end > removed_start
        text = text[:removed_start] + text[removed_end:]

        assert _stale_hits(text) == [], (
            'CLAUDE.md references consolidated tools outside the historical '
            f'sections: {_stale_hits(text)}'
        )

    def test_claude_md_removed_table_maps_old_to_new(self):
        text = (PROJECT_ROOT / 'CLAUDE.md').read_text(encoding='utf-8')
        for old in OLD_TOOL_NAMES:
            assert old in text, (
                f'CLAUDE.md old→new table must document the {old} rename'
            )
        assert "races(action='list')" in text
        assert "query_metrics(kind='fitness', days=N)" in text

    def test_prompts_have_no_stale_tool_names(self):
        text = (PROJECT_ROOT / 'coach' / 'prompts.py').read_text(encoding='utf-8')
        assert _stale_hits(text) == []

    def test_doctrine_has_no_stale_tool_names(self):
        assert _stale_hits(COACHING_DOCTRINE) == []

    def test_doctrine_documents_consolidated_tools(self):
        assert 'query_metrics' in COACHING_DOCTRINE
        assert "races(action=" in COACHING_DOCTRINE
        assert 'remove_race' in COACHING_DOCTRINE

    def test_server_instructions_have_no_stale_tool_names(self):
        assert _stale_hits(SERVER_INSTRUCTIONS) == []

    def test_daily_loop_has_no_stale_tool_names(self):
        text = (PROJECT_ROOT / 'scripts' / 'daily_loop.py').read_text(
            encoding='utf-8')
        assert _stale_hits(text) == []
