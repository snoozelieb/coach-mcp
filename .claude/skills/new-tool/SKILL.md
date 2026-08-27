---
name: new-tool
description: Add or change an MCP tool in coach-mcp the repo way — parser, annotations inventory, clock-discipline allowlist, FakeGarminClient tests, doc hygiene, e2e goldens. Use when creating a new @mcp.tool, renaming/consolidating tools, or changing snapshot/push behavior.
---

# Adding an MCP tool to coach-mcp

The contract suites make every step below mandatory — skipping one fails CI by
design, so work down the list.

## The checklist

1. **Parsing first.** Put pure, no-I/O parsing in `coach/parsers.py` (or the
   relevant module). Session/sport vocabulary comes from `coach/taxonomy.py` —
   never invent type strings.
2. **Tool in `coach/tools/`.** `@mcp.tool(annotations={...})` — every tool MUST
   declare `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`.
   Return structured dicts; fail gracefully with `{'error': str(e)}` after
   `logger.exception()` — always `except Exception:`, never a bare `except:`
   (it swallows KeyboardInterrupt/SystemExit). Writes go through
   `coach.storage` (atomic + locked).
   A tool with side effects (like the snapshot's ingestion) must NOT claim
   readOnlyHint.
3. **Classify it in `tests/test_annotations.py`** — the inventories are
   explicit; an unclassified tool fails the suite.
4. **Clock discipline.** If the tool reads the clock, resolve `today`/`now`
   ONCE at the tool boundary and thread `today: date` through helpers. Add the
   (file, function) pair to the `tests/test_clock_discipline.py` allowlist with
   a justification — stale allowlist entries also fail.
5. **Tests.** Happy + error paths through **FakeGarminClient**
   (`tests/conftest.py`) + `sandbox_data_dir` (seed inputs into the sandbox,
   assert on persisted output). Route Garmin traffic with
   `patch_garmin_everywhere(monkeypatch, client)`; per-endpoint failures via
   `overrides={'get_sleep_data': Exception('down')}`. Never hand-roll Garmin
   mocks. Async tools: pytest-asyncio auto mode + the `mock_ctx` fixture.
   Use relative dates (`date.today() + timedelta(...)`) — hardcoded dates rot.
   Import coach modules as `import coach.planner as planner`, never
   `from coach.planner import f` — monkeypatch targets go stale otherwise.
6. **Run** `python -m pytest -v`.

## When the change is bigger than one tool

- **Snapshot or push behavior** → extend the golden-schema e2e tests
  (`tests/test_e2e_snapshot.py`, `tests/test_e2e_push.py`).
- **Renaming or consolidating tools** → renames are atomic. Doc hygiene in
  `tests/test_consolidations.py` bans stale tool names in CLAUDE.md, prompts,
  the doctrine resource, SERVER_INSTRUCTIONS, and daily_loop; the old→new
  table in CLAUDE.md's "Removed / consolidated tools" section must document
  every rename, and that section must stay ahead of `## Commands`.
- **New Garmin endpoint** → add the call shape to
  `tests/test_garmin_contract.py` and a realistic response shape to
  FakeGarminClient (list-vs-dict fidelity is load-bearing).
- **Doctrine-relevant behavior** → update `coach/resources.py`
  (coach://coaching/doctrine) and, if a hard mandate changes,
  `SERVER_INSTRUCTIONS` in `coach/mcp_app.py` — keep it under the 2,000-char
  budget (`tests/test_server_instructions.py`). Never add doctrine to CLAUDE.md.

## Design bar

Single responsibility; the tool exists because the LLM coach repeatedly needs
data or an action it can't get cleanly today. Prefer extending
`get_coaching_snapshot()` sections or `query_metrics` kinds over adding a
near-duplicate tool.
