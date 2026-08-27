# CLAUDE.md

Development context for Claude Code (claude.ai/code) sessions in this repository.

> **Coaching doctrine lives in the server, not in this file.** Every MCP client —
> Claude Code included — receives the coaching identity at connection time via
> `SERVER_INSTRUCTIONS` (`coach/mcp_app.py`; test-enforced under 2,000 chars because
> Claude Code truncates MCP instructions at 2KB) and the long-form
> `coach://coaching/doctrine` resource (uncapped, fetched on demand; completeness
> pinned by `tests/test_server_instructions.py`). When coaching from this repo,
> follow those surfaces — snapshot first, injury hard gate, verify athlete claims —
> and read the doctrine resource before planning sessions. Do **not** copy doctrine
> into this file: the duplicate is the copy that drifts.

## Project Overview

An **adaptive AI training coach** MCP server: fetches fitness data from Garmin
Connect, maintains a rolling 7-day plan with a PURPOSE for every session, persists
coaching memory (decisions, anomalies, approvals) across sessions, and pushes
workouts to the Garmin calendar. Published as `garmin-coach-mcp` on PyPI and
`io.github.snoozelieb/coach-mcp` on the MCP registry.

The safety-critical coaching rules are **code-enforced, not prompt-enforced**:
`update_weekly_plan` rejects non-rest sessions without a `purpose` (purpose gate)
and sessions matching an active/improving injury's `restricted_activities` (injury
gate — taxonomy-aware, re-run on the full plan at push); plans entirely in the past
or with days >21 in the future are rejected; anomalies and season auto-proposals
are idempotent by stable id / event-tag, so rejections are remembered forever.
See `coach/tools/planning_tools.py` and `coach/tools/decision_tools.py`.

### Removed / consolidated tools

Removed in the first rationalization pass — their data is in `get_coaching_snapshot()`:
`get_planning_context`, `get_goal_progress` (→ `snapshot.goal_progress`),
`list_pending_suggestions` (→ `snapshot.coaching_memory.pending_approvals`),
`get_load_status` (→ `snapshot.fitness_metrics.acwr_status`).

Consolidated in Phase 2 (same behavior, new entry point — bodies moved, not rewritten):

| Old tool | New call |
|----------|----------|
| `list_races()` | `races(action='list')` |
| `add_race(...)` | `races(action='add', ...)` |
| `update_race(...)` | `races(action='update', ...)` |
| `research_race(...)` | `races(action='research', ...)` |
| `get_fitness_status(days)` | `query_metrics(kind='fitness', days=N)` |
| `get_intensity_distribution(days)` | `query_metrics(kind='intensity', days=N)` |
| `get_daily_metrics()` | `query_metrics(kind='daily')` |
| `get_training_readiness(for_date)` | `query_metrics(kind='readiness', for_date=...)` |
| `get_personal_records()` | `query_metrics(kind='personal_records')` |

`remove_race` stays standalone (destructive — clients must see destructiveHint
per-tool). `get_activities_range` stays standalone (explicit range semantics).
`get_compliance_report` stays — minimal check whose math matches `snapshot.compliance`.

## Commands

```bash
python server.py                              # Run the MCP server (stdio, from a checkout)
garmin-coach-mcp                              # Same server via the console entry point (pip/uvx; coach-mcp is an alias)
COACH_DATA_DIR=/path/to/data garmin-coach-mcp # Point the server at an explicit data directory
COACH_TRANSPORT=http python server.py         # Run with HTTP transport
COACH_CODE_MODE=1 python server.py            # Run with Code Mode (search/execute meta-tools)
python -m pytest -v                           # Run all tests
python scripts/daily_loop.py                  # Morning audit (scheduled 06:30 via Task Scheduler "CoachMCP Morning Audit")
python scripts/garmin_login.py                # Manual Garmin token recovery (native auth + MFA)
```

## MCP Framework

Standalone **FastMCP v3.4.x** (`from fastmcp import FastMCP`, pinned `fastmcp>=3.4,<4`),
not the bundled v1 in the `mcp` SDK. Upgrading an env from fastmcp <3.3 requires
`pip uninstall fastmcp fastmcp-slim` then a fresh install — the 3.3 distribution
split breaks in-place upgrades.

- **49 tools** across 11 modules in `coach/tools/`; every tool declares
  readOnly/destructive/idempotent/openWorld annotations — `tests/test_annotations.py`
  is the contract (unclassified tools fail the suite by design).
- **5 prompts** (`coach/prompts.py`), **6 resources** (`coach/resources.py`,
  including `coach://coaching/doctrine` and `coach://context/now`).
- Planning tools return structured dicts; `update_weekly_plan` takes a typed `plan`
  dict validated by pydantic (`coach/schemas.py`; `plan_json` is a deprecated alias).
- Transports: stdio (default), `http`/`streamable-http`; `sse` accepted for legacy
  setups only (spec-deprecated since MCP 2026-07-28 — don't use).
- Never build on `ctx.elicit()`/`ctx.sample()` — MCP spec 2026-07-28 removed
  server-initiated elicitation/sampling (MRTR pattern replaces them).

## Architecture

```
server.py                  # Entry shim → coach/server.py (transport/code-mode config)
coach/
├── config.py              # Shared config, thresholds, data-dir resolution, pattern registry
├── taxonomy.py            # Canonical sport/session vocabulary (single source)
├── schemas.py             # Pydantic plan schemas (WeeklyPlan)
├── storage.py             # Atomic, locked, migrating JSON writes
├── fitness.py             # CTL/ATL/TSB (EWMA) + ACWR (rolling 7d:28d primary)
├── garmin_client.py       # Token-first auth, MFA hard stop, 600s AUTH_REQUIRED latch
├── parsers.py             # Pure parsing of Garmin responses; setup gate
├── planner.py             # Context building, plan lifecycle (prune + archive)
├── rules.py               # classify_activity, safety rules
├── workout_builder.py     # Plan sessions → Garmin workouts
├── mcp_app.py             # Shared FastMCP instance + SERVER_INSTRUCTIONS
├── prompts.py, resources.py
└── tools/                 # 49 MCP tools: data, fitness, athlete, planning,
                           # coaching (snapshot), strength, injury, research,
                           # decision, race, interactive
scripts/                   # daily_loop, garmin_login, capture_fixtures,
                           # recompute_acwr_history, setup_wizard, ... (non-exhaustive)
tests/                     # pytest suite (~1,300 tests; see Testing below)
```

`get_coaching_snapshot()` is a **read-modify-write pipeline**, not a read: every
call ingests new activities, persists sleep/readiness, registers anomalies,
auto-transitions overdue decisions, and files season auto-proposals — sections
shape the payload, never the side effects (hence not readOnlyHint).

## Data Files

All personal files live in the resolved data dir; only `methodology.json` ships
with the package (seeded on first run, never overwritten).

| File | Purpose |
|------|---------|
| `athlete.json` / `athlete_baseline.json` | WHO — profile + Garmin-derived capacity |
| `methodology.json` | HOW — safety rules, race templates, personas |
| `training_config.json` | WHAT — events, periodization, goals |
| `weekly_plan.json` | CURRENT — rolling 7-day plan (history in `plan_history.json`) |
| `fitness_history.json` | Daily loads, CTL/ATL snapshots, sleep history |
| `coaching_log.json` | MEMORY — decisions, patterns, approvals, anomalies |
| `exercise_library.json` | Cached exercise form cues for Garmin notes |

## Environment

Requires `GARMIN_EMAIL` and `GARMIN_PASSWORD` (`.env` in a checkout, or the MCP
client's `env` block). `ANTHROPIC_API_KEY` only for `scripts/daily_loop.py --llm`.
Optional: `COACH_DATA_DIR`, `COACH_TOKEN_DIR`, `COACH_TRANSPORT`, `COACH_CODE_MODE`,
`COACH_LLM_MODEL` (daily_loop --llm model), `FASTMCP_HOST`/`FASTMCP_PORT`.

Data dir resolution (installed copies must never write into site-packages):
`COACH_DATA_DIR` env var → `data/` next to a source checkout → per-user data dir.

### Garmin Authentication

garminconnect 0.3.5 directly — no custom Cloudflare code (the library's `curl_cffi`
handles Garmin's SSO natively). Token-first restore from `.garth/garmin_tokens.json`;
on failure ONE non-interactive credential login (MFA is a hard stop — never blocks
on input); tools then raise `GarminAuthRequiredError` whose message names the fix
(`python scripts/garmin_login.py`), and a process latch fails fast for ~10 minutes.
Restart the server after re-login. `tests/test_garmin_contract.py` pins every
garminconnect call shape so dependency breaks fail CI loudly.

## Testing

1,333 tests across 38 files (CI: GitHub Actions on push/PR + weekly cron;
installs `requirements-dev.txt`). Four patterns are THE way tests are written:

- **FakeGarminClient** (`tests/conftest.py`) — canonical fake for ALL Garmin
  traffic; realistic response shapes (list-vs-dict fidelity is load-bearing).
  Route with `patch_garmin_everywhere`; override per-endpoint via `overrides=`;
  assert on the call log. Never hand-roll Garmin mocks.
- **Fixtures fallback** — `garmin_fixtures` loads committed synthetic
  `tests/fixtures/garmin_sample.json`, overlaid by the gitignored real capture
  (`test_fixtures.json`) when present. See the `/capture-fixtures` skill.
- **Live-data sandbox guard** (autouse) — tests can NEVER touch live `data/`:
  `sandbox_data_dir` redirects `DATA_DIR` everywhere; a session-scoped hash
  tripwire fails the run loudly if anything under `data/` changed.
- **Clock discipline** — naked `date.today()`/`datetime.now()` in `coach/` is
  banned outside the AST-lint allowlist (`tests/test_clock_discipline.py`).
  Resolve the clock once at the tool boundary, thread `today: date` through.
  In tests use relative dates — hardcoded dates rot.

Import coach modules as `import coach.planner as planner`, never
`from coach.planner import f` — otherwise monkeypatch targets go stale and
patches silently never take effect.

Adding or changing a tool: use the `/new-tool` skill (the full checklist:
parser → tool → annotations inventory → clock allowlist → tests → e2e goldens).

## Project Skills

`.claude/skills/`: `mcp-builder` (MCP server design guide), `new-tool` (add an
MCP tool the repo way), `release` (version bump → tag → PyPI/registry),
`capture-fixtures` (owner-run fixture refresh), `acwr-recompute` (supervised
ACWR history migration).

## Known Issues / TODO

### 3. Rehab sessions not pushed to Garmin calendar
- Rehab sessions skipped with "unknown workout type" when pushing to Garmin
- Need to add rehab as supported workout type or bundle into strength sessions
