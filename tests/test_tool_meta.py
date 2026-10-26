"""Per-tool `_meta` hints that Claude Code honours.

Claude Code loads only tool NAMES and the server instructions at session
start and fetches tool definitions through tool search on demand. A tool
marked ``_meta["anthropic/alwaysLoad"]: true`` is loaded upfront instead, so
the mandates that name it ("snapshot first", the injury gate's only
gate-lifter, grounding drill-downs, the push) never wait on a search step.

Upfront-loaded descriptions are cut at 4,096 characters (deferred ones at
16,384), so every always-load tool must keep its description under that cap
— a long docstring would lose its tail exactly where it is read most.
"""
import coach.server  # noqa: F401 — registers every tool on the shared mcp
from coach.mcp_app import mcp

ALWAYS_LOAD = {
    'get_coaching_snapshot',   # mandate 1: snapshot first
    'update_injury_status',    # mandate 2: the only gate-lifter
    'query_metrics',           # mandate 4: sport_history drill-down
    'push_plan_to_garmin',     # the consequential write
}

UPFRONT_DESCRIPTION_CAP = 4096


async def _tools():
    return {t.name: t for t in await mcp.list_tools()}


async def test_always_load_set_is_exact():
    tools = await _tools()
    marked = {n for n, t in tools.items()
              if (t.meta or {}).get('anthropic/alwaysLoad') is True}
    assert marked == ALWAYS_LOAD, (
        f'alwaysLoad drift: extra={sorted(marked - ALWAYS_LOAD)} '
        f'missing={sorted(ALWAYS_LOAD - marked)}')


async def test_always_load_descriptions_fit_the_upfront_cap():
    tools = await _tools()
    too_long = {n: len(tools[n].description or '') for n in ALWAYS_LOAD
                if len(tools[n].description or '') > UPFRONT_DESCRIPTION_CAP}
    assert not too_long, (
        f'alwaysLoad descriptions are cut at {UPFRONT_DESCRIPTION_CAP} chars '
        f'when loaded upfront: {too_long}')


async def test_gated_tool_descriptions_lead_with_their_gates():
    """Tool search matches only the first 4,096 chars of a description, and an
    upfront load cuts there. The gate contracts must sit inside that window."""
    tools = await _tools()
    head = (tools['update_weekly_plan'].description or '')[:UPFRONT_DESCRIPTION_CAP]
    for marker in ('purpose_gate', 'injury_gate', 'plan_dates', 'grounding_warnings'):
        assert marker in head, f'update_weekly_plan: {marker!r} past the 4,096-char window'
    head = (tools['training_program'].description or '')[:UPFRONT_DESCRIPTION_CAP]
    for marker in ('capacity_anchor_mismatch', 'injury_gate', 'program_dates'):
        assert marker in head, f'training_program: {marker!r} past the 4,096-char window'
