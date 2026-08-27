"""Phase 2.3: MCP tool annotation contract.

Every registered tool must declare behavior hints (readOnlyHint /
destructiveHint / idempotentHint / openWorldHint) so MCP clients can make
safe auto-approval decisions. The expected sets below are EXPLICIT
inventories — when you add a tool, classify it here or the suite fails.

Classification rules (mirror of the decorator literals in coach/tools/*.py):
- readOnlyHint=True: the tool's full call graph performs no file writes and
  no Garmin mutations. Caching counts as a write (research_exercise).
- destructiveHint=True: push_plan_to_garmin (deletes prior pushed workouts)
  and remove_race only.
- idempotentHint=True: re-running with the same args yields the same state
  (set_*/update_* keyed writes, approve_*/reject_* status flips, idempotent
  refresh/ingest). Append-per-call tools (log_*, races (action='add'
  appends), record_*, sync_strength_session, diagnose_injury, update_phase,
  update_athlete) are NOT idempotent.
- openWorldHint=True: the tool hits Garmin or the open web. Consolidated
  tools (races, query_metrics) carry the union of their actions/kinds:
  races is open-world because action='research' hits the web; query_metrics
  is open-world because most kinds hit Garmin.
"""
import pytest

import server  # noqa: F401 — imports all tool modules, triggers registration
from coach.mcp_app import mcp

# ---------------------------------------------------------------------------
# Expected inventories (explicit — keep sorted)
# ---------------------------------------------------------------------------

READ_ONLY_TOOLS = {
    # local data only
    'generate_smart_brief',
    'get_athlete',
    'get_coaching_score',
    'get_methodology',
    'get_onboarding_guide',
    'get_periodization_status',
    'get_response_patterns',
    'get_strength_baseline',
    'get_week_constraints',
    'get_weekly_plan',
    'interactive_check_in',
    'list_exercises',
    'list_pending_approvals',
    # read-only but fetches from Garmin or the web
    'analyze_ftp_test',
    'generate_strength_workout',
    'get_activities_range',
    'get_compliance_report',
    'get_weekly_prescription',
    'query_metrics',
    'research_injury',
    'research_sport',
}

DESTRUCTIVE_TOOLS = {
    'push_plan_to_garmin',  # deletes previously pushed workouts before upload
    'remove_race',
}

IDEMPOTENT_WRITE_TOOLS = {
    # keyed local writes — same args, same resulting state
    'add_exercise',
    'approve_progression',
    'approve_proposal',
    'get_active_decisions',  # loads decisions, persists overdue->needs_review (idempotent)
    'reject_proposal',
    'remove_race',
    'resolve_anomaly',
    'set_exercise_preference',
    'set_ftp',
    'set_threshold_pace',
    'update_decision_status',
    'update_injury_status',
    'update_methodology',
    'update_weekly_plan',
    # idempotent refresh/ingest/cache (also hit Garmin or web)
    'backfill_history',     # add-only: repeat runs over the same range add nothing
    'get_coaching_snapshot',
    'refresh_athlete_baseline',
    'refresh_fitness_history',
    'research_exercise',
}

NON_IDEMPOTENT_WRITE_TOOLS = {
    # append an entry on every call — re-running duplicates
    'diagnose_injury',
    'log_coaching_decision',
    'propose_coaching_action',
    'races',                # action='add' appends an event per call
    'record_athlete_response',
    'sync_strength_session',
    'update_athlete',       # add_commitment / add_injury sections append
    'update_phase',         # appends a coaching-log decision per call
    'push_plan_to_garmin',  # workout IDs / schedule entries differ per push
}

OPEN_WORLD_TOOLS = {
    # Garmin
    'analyze_ftp_test',
    'backfill_history',
    'generate_strength_workout',
    'get_activities_range',
    'get_coaching_snapshot',
    'get_compliance_report',
    'get_weekly_prescription',
    'push_plan_to_garmin',
    'query_metrics',        # daily/readiness/intensity/personal_records hit Garmin
    'refresh_athlete_baseline',
    'refresh_fitness_history',
    'sync_strength_session',
    # open web
    'diagnose_injury',
    'races',                # action='research' fetches the race's web page
    'research_exercise',
    'research_injury',
    'research_sport',
}

ALL_CLASSIFIED = READ_ONLY_TOOLS | IDEMPOTENT_WRITE_TOOLS | NON_IDEMPOTENT_WRITE_TOOLS


@pytest.fixture(scope='module')
async def tools_by_name():
    tools = await mcp.list_tools()
    return {t.name: t for t in tools}


class TestAnnotationPresence:
    async def test_every_tool_has_annotations(self, tools_by_name):
        missing = [n for n, t in tools_by_name.items() if t.annotations is None]
        assert not missing, f'Tools registered without annotations: {sorted(missing)}'

    async def test_every_tool_is_classified(self, tools_by_name):
        unclassified = set(tools_by_name) - ALL_CLASSIFIED
        assert not unclassified, (
            f'New tools must be classified in test_annotations.py: {sorted(unclassified)}'
        )

    async def test_no_stale_inventory_entries(self, tools_by_name):
        stale = ALL_CLASSIFIED - set(tools_by_name)
        assert not stale, f'Inventory lists tools that are not registered: {sorted(stale)}'


class TestReadOnly:
    async def test_read_only_tools_marked_read_only(self, tools_by_name):
        wrong = [n for n in READ_ONLY_TOOLS
                 if not tools_by_name[n].annotations.readOnlyHint]
        assert not wrong, f'Expected readOnlyHint=True: {sorted(wrong)}'

    async def test_writing_tools_not_marked_read_only(self, tools_by_name):
        writers = set(tools_by_name) - READ_ONLY_TOOLS
        wrong = [n for n in writers if tools_by_name[n].annotations.readOnlyHint]
        assert not wrong, f'Tools that write must not claim readOnlyHint: {sorted(wrong)}'

    async def test_no_read_only_tool_marked_destructive(self, tools_by_name):
        wrong = [n for n in READ_ONLY_TOOLS
                 if tools_by_name[n].annotations.destructiveHint]
        assert not wrong, f'Read-only tools marked destructive: {sorted(wrong)}'


class TestDestructive:
    async def test_destructive_tools_marked(self, tools_by_name):
        for name in DESTRUCTIVE_TOOLS:
            assert tools_by_name[name].annotations.destructiveHint is True, (
                f'{name} must carry destructiveHint=True'
            )

    async def test_only_expected_tools_destructive(self, tools_by_name):
        marked = {n for n, t in tools_by_name.items()
                  if t.annotations.destructiveHint}
        assert marked == DESTRUCTIVE_TOOLS, (
            f'Unexpected destructive set: {sorted(marked)}'
        )


class TestIdempotency:
    async def test_idempotent_writers_marked(self, tools_by_name):
        wrong = [n for n in IDEMPOTENT_WRITE_TOOLS
                 if tools_by_name[n].annotations.idempotentHint is not True]
        assert not wrong, f'Expected idempotentHint=True: {sorted(wrong)}'

    async def test_non_idempotent_writers_not_marked(self, tools_by_name):
        wrong = [n for n in NON_IDEMPOTENT_WRITE_TOOLS
                 if tools_by_name[n].annotations.idempotentHint]
        assert not wrong, (
            f'Append-per-call tools must not claim idempotentHint: {sorted(wrong)}'
        )


class TestOpenWorld:
    async def test_open_world_set_exact(self, tools_by_name):
        marked = {n for n, t in tools_by_name.items()
                  if t.annotations.openWorldHint}
        assert marked == OPEN_WORLD_TOOLS, (
            f'openWorldHint drift. extra={sorted(marked - OPEN_WORLD_TOOLS)} '
            f'missing={sorted(OPEN_WORLD_TOOLS - marked)}'
        )

    async def test_local_tools_marked_closed_world(self, tools_by_name):
        local = set(tools_by_name) - OPEN_WORLD_TOOLS
        wrong = [n for n in local
                 if tools_by_name[n].annotations.openWorldHint is not False]
        assert not wrong, (
            f'Local-only tools must set openWorldHint=False explicitly: {sorted(wrong)}'
        )
