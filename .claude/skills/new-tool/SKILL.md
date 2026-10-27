---
name: new-tool
description: Add or change an MCP tool in coach-mcp the repo way — parser, annotations inventory, clock-discipline allowlist, FakeGarminClient tests, Claude Code description budgets, doc hygiene, e2e goldens, and the session contract when rendering changes. Use when creating a new @mcp.tool, renaming/consolidating tools, or changing snapshot/push/rendering behavior.
---

# Adding an MCP tool to coach-mcp

The contract suites make every step below mandatory — skipping one fails CI by
design, so work down the list. Run everything with the venv interpreter
(`.venv/Scripts/python.exe` on this machine; the PATH `python` carries a
different FastMCP and will mislead you).

## The checklist

1. **Parsing first.** Put pure, no-I/O parsing in `coach/parsers.py` (or the
   relevant module). Session/sport vocabulary comes from `coach/taxonomy.py` —
   never invent type strings.
2. **Tool in `coach/tools/`.** `@mcp.tool(annotations={...})` — every tool MUST
   declare `readOnlyHint`/`destructiveHint`/`idempotentHint`/`openWorldHint`.
   Return structured dicts; fail gracefully with `{'error': str(e)}` after
   `logger.exception()` — always `except Exception:`, never a bare `except:`
   (it swallows KeyboardInterrupt/SystemExit). Writes go through
   `coach.storage` (atomic + locked; a lint test forbids bare file I/O in
   `coach/`). A tool with side effects (like the snapshot's ingestion) must
   NOT claim readOnlyHint. Tools that prescribe or write plans sit behind the
   snapshot gate (`require_snapshot`, `coach/session_state.py`); consequential
   writes that lift protection need the athlete's own words
   (`athlete_statement` + rationale, logged as a decision).
3. **Classify it in `tests/test_annotations.py`** — the inventories are
   explicit; an unclassified tool fails the suite.
4. **Description budgets (Claude Code).** Upfront-loaded tool descriptions are
   cut at 4,096 characters, deferred (tool-search) ones at 16,384, and tool
   search matches only the first 4,096 — so a gate contract must sit in the
   head of the docstring. FastMCP drops the `Args:`/`Returns:` sections from
   the description it publishes; measure the published length
   (`mcp.list_tools()`), not the docstring. The four `_meta
   anthropic/alwaysLoad` tools and `update_weekly_plan` are pinned under 4,096
   by `tests/test_tool_meta.py`; `SERVER_INSTRUCTIONS` (`coach/mcp_app.py`)
   stays under the 3,900-char budget of `tests/test_server_instructions.py`
   (Claude Code cuts instructions at 4,096) — new doctrine goes into the
   `coach://coaching/doctrine` resource, never into the instructions.
5. **Clock discipline.** If the tool reads the clock, resolve `today`/`now`
   ONCE at the tool boundary and thread `today: date` through helpers. Add the
   (file, function) pair to the `tests/test_clock_discipline.py` allowlist with
   a justification — stale allowlist entries also fail.
6. **Tests.** Happy + error paths through **FakeGarminClient**
   (`tests/conftest.py`) + `sandbox_data_dir` (seed inputs into the sandbox,
   assert on persisted output). Route Garmin traffic with
   `patch_garmin_everywhere(monkeypatch, client)`; per-endpoint failures via
   `overrides={'get_sleep_data': Exception('down')}`. Never hand-roll Garmin
   mocks. Async tools: pytest-asyncio auto mode + the `mock_ctx` fixture.
   Use relative dates (`date.today() + timedelta(...)`) — hardcoded dates rot.
   Import coach modules as `import coach.planner as planner`, never
   `from coach.planner import f` — monkeypatch targets go stale otherwise.
   Tests can never touch live `data/` (autouse sandbox + hash tripwire).
7. **Counts and docs.** Bump the tool/prompt/resource counts in `CLAUDE.md`
   (overview bullets, the architecture tree, the test count) and the tools
   table in `README.md`. `CHANGELOG.md` gets an `[Unreleased]` entry.
8. **Run** `.venv/Scripts/python.exe -m pytest -q` and `python -m ruff check .`
   (CI runs both; ruff is pinned in `requirements-dev.txt`).

## When the change is bigger than one tool

- **Snapshot or push behavior** → extend the golden-schema e2e tests
  (`tests/test_e2e_snapshot.py`, `tests/test_e2e_push.py`). The push is two
  steps (preview with the rendered step tree per session and the
  `unrenderable` list, then confirm with the single-use token); the confirm
  uploads exactly the previewed payload.
- **How a session renders on the watch** → the session contract lives in
  `coach/authoring.py` (strict models, vocabulary, teaching errors, generated
  contract document) and the renderer in `coach/workout_builder.py` draws
  what the plan says and defaults nothing. A new key means: model field +
  renderer + `tests/test_authoring.py` + `tests/test_workout_builder.py`, the
  `coach://authoring/contract` text (generated), the `update_weekly_plan`
  docstring summary (budget!), and the sport skills under
  `.claude/skills/*-session-author/` whose JSON examples `tests/test_skills.py`
  validates. Coaching judgment (how a session should be composed) goes into
  those skills, not into Python heuristics.
- **Renaming or consolidating tools** → renames are atomic. Doc hygiene in
  `tests/test_consolidations.py` bans stale tool names in CLAUDE.md, prompts,
  the doctrine resource, SERVER_INSTRUCTIONS, and daily_loop; the old→new
  table in CLAUDE.md's "Removed / consolidated tools" section must document
  every rename, and that section must stay ahead of `## Commands`.
- **New Garmin endpoint** → add the call shape to
  `tests/test_garmin_contract.py` and a realistic response shape to
  FakeGarminClient (list-vs-dict fidelity is load-bearing). Verify against
  Garmin's live catalogues, not library enums (activity types and workout
  types differ).
- **Doctrine-relevant behavior** → update `coach/resources.py`
  (coach://coaching/doctrine, completeness pinned by
  `tests/test_server_instructions.py`) and, if a hard mandate changes,
  `SERVER_INSTRUCTIONS` within its budget. Never add doctrine to CLAUDE.md.
- **A tool the model must see without searching** → `meta={"anthropic/alwaysLoad": True}`
  (and `anthropic/requiresUserInteraction` for consent steps); add it to the
  exact set in `tests/test_tool_meta.py` and keep its description under 4,096.

## Design bar

Single responsibility; the tool exists because the LLM coach repeatedly needs
data or an action it can't get cleanly today. Prefer extending
`get_coaching_snapshot()` sections or `query_metrics` kinds over adding a
near-duplicate tool. Safety rules are code-enforced, not prompt-enforced:
if the tool guards something, the guard is a test, not a sentence in a docstring.

FastMCP here is the standalone 4.x package (`fastmcp>=4.1,<5`, MCP SDK 2,
spec 2026-07-28 era). Do not build on server-initiated elicitation or
sampling (`ctx.elicit()`/`ctx.sample()`): the 2026-07-28 spec deprecates
sampling/roots/logging (SEP-2577) and replaces server-initiated requests with
the MRTR pattern (SEP-2322), and FastMCP 4 removed them; interactive input is
MRTR. Tests run with `FASTMCP_MCP_CAMELCASE_COMPAT=false`,
so annotation fields read as `tool.annotations.read_only_hint`.
