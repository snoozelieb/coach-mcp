# CLAUDE.md

Development context for Claude Code (claude.ai/code) sessions in this repository.

> **Coaching doctrine lives in the server, not in this file.** Every MCP client —
> Claude Code included — receives the coaching identity at connection time via
> `SERVER_INSTRUCTIONS` (`coach/mcp_app.py`; test-enforced under 3,900 chars because
> Claude Code sends the model only the first 4,096) and the long-form
> `coach://coaching/doctrine` resource (uncapped, fetched on demand; completeness
> pinned by `tests/test_server_instructions.py`). When coaching from this repo,
> follow those surfaces — the four hard mandates (snapshot first, injury hard gate,
> verify before confirming, verify before prescribing) — and read the doctrine
> resource before planning sessions. Do **not** copy doctrine
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
Capacity grounding is code too: `snapshot.capacity` is derived from persisted
`daily_loads` (`coach/fitness.py`: `capacity_by_sport`, `derive_sport_capacity`);
`update_weekly_plan` returns `grounding_warnings` (advisory), while
`training_program(action='set')` *rejects* a program whose capacity anchor the
history contradicts, and runs the injury gate as well.
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

`query_metrics` has since gained `kind='sport_history'` (new, not a renamed tool).
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
python scripts/setup_wizard.py                # Interactive first-run profile setup
garmin-coach-setup / garmin-coach-login       # Same two helpers as console scripts (installed copies)
python scripts/backfill_history.py --since YYYY-MM-DD --check   # Diary-hole repair (dry run; --apply to write)
python scripts/authoring_eval.py --check      # Render the authoring eval's golden sessions (no model, CI runs it)
python scripts/authoring_eval.py --run --model sonnet --budget-usd 6   # Owner-run: the model authors every brief (costs money)
```

## MCP Framework

Standalone **FastMCP v4.1.x** (`from fastmcp import FastMCP`, pinned `fastmcp>=4.1,<5`),
not the server bundled in the `mcp` SDK. FastMCP 4 runs on MCP SDK 2 (the
2026-07-28 "stateless" era; it still serves older clients) and httpx2. Tests run
with `FASTMCP_MCP_CAMELCASE_COMPAT=false`, so read annotation fields in
snake_case (`tool.annotations.read_only_hint`). A downgrade to 3.4 is a plain
re-pin. Four core tools carry `_meta["anthropic/alwaysLoad"]` so Claude Code
loads them upfront (`tests/test_tool_meta.py`); their descriptions must stay
under the 4,096-char upfront cap, and gate contracts sit in the first 4,096
chars of every gated tool's description because tool search indexes only that
window.

- **52 tools** across 11 modules in `coach/tools/`; every tool declares
  readOnly/destructive/idempotent/openWorld annotations — `tests/test_annotations.py`
  is the contract (unclassified tools fail the suite by design).
- **7 prompts** (`coach/prompts.py`), **7 resources** (`coach/resources.py`,
  including `coach://coaching/doctrine`, `coach://context/now` and the
  generated `coach://authoring/contract`).
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
├── server.py              # main(): transport + Code Mode selection, tool registration
├── config.py              # Shared config, thresholds, data-dir resolution, pattern registry
├── taxonomy.py            # Canonical sport/session vocabulary + capacity buckets (single source)
├── schemas.py             # Pydantic schemas (WeeklyPlan, TrainingProgram)
├── storage.py             # Atomic, locked, migrating JSON writes
├── fitness.py             # CTL/ATL/TSB (EWMA), ACWR (rolling 7d:28d), per-sport capacity, backfill
├── garmin_client.py       # Token-first auth, MFA hard stop, 600s AUTH_REQUIRED latch
├── parsers.py             # Pure parsing of Garmin responses; setup gate
├── planner.py             # Context building, plan lifecycle (prune + archive)
├── rules.py               # classify_activity, safety rules
├── authoring.py           # Session contract: strict phase/exercise models, vocabulary,
│                          #   teaching errors, generated contract document
├── workout_builder.py     # Renders plan sessions as Garmin workouts (what the plan says,
│                          #   nothing defaulted) + the preview step tree
├── mcp_app.py             # Shared FastMCP instance + SERVER_INSTRUCTIONS
├── prompts.py, resources.py
├── web_utils.py           # HTML fetch/strip for the research tools
├── defaults/              # methodology.json shipped with the package
└── tools/                 # 52 MCP tools: data, fitness, athlete, planning,
                           # coaching (snapshot), strength, injury, research,
                           # decision, race, interactive
scripts/                   # daily_loop, garmin_login, capture_fixtures,
                           # recompute_acwr_history, setup_wizard, ... (non-exhaustive)
tests/                     # pytest suite (see Testing below)
evals/authoring/           # authoring eval: briefs, checks, golden sessions, a synthetic
                           #   athlete and a slice of the Garmin catalogue (scripts/authoring_eval.py)
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
| `training_program.json` | MACRO — multi-week program with capacity anchor (`training_program` tool) |
| `fitness_history.json` | Daily loads (source of `snapshot.capacity`), CTL/ATL snapshots, sleep history |
| `coaching_log.json` | MEMORY — decisions, patterns, approvals, anomalies |
| `exercise_library.json` | Cached exercise form cues for Garmin notes |
| `exercises.json` | Exercise catalogue used by strength/research tools and the workout builder |

`backfill_history` (tool and script) copies `fitness_history.json` into a sibling
`backups/` folder inside the data dir before applying. Like `data/`, it holds personal data and
must never be committed.

## Environment

Requires `GARMIN_EMAIL` and `GARMIN_PASSWORD` (`.env` in a checkout, or the MCP
client's `env` block).
Optional: `COACH_DATA_DIR`, `COACH_TOKEN_DIR`, `COACH_TRANSPORT`, `COACH_CODE_MODE`
(needs the `code-mode` extra), `FASTMCP_HOST`/`FASTMCP_PORT`. The morning audit
(`scripts/daily_loop.py`) has no LLM path: it ingests, audits yesterday against
the plan (writing `unverified`, never fiction, when Garmin is unreachable),
re-checks the last 3 days, and exits non-zero on failure.

Data dir resolution (installed copies must never write into site-packages):
`COACH_DATA_DIR` env var → `data/` next to a source checkout → per-user data dir
(`%LOCALAPPDATA%` on Windows, `$XDG_DATA_HOME` elsewhere). `FASTMCP_HOST`/`FASTMCP_PORT`
are read by FastMCP itself, not by repo code.

### Garmin Authentication

garminconnect 0.3.20 (exact pin) directly — no custom Cloudflare code (the library's `curl_cffi`
handles Garmin's SSO natively). Token-first restore from `.garth/garmin_tokens.json`;
on failure ONE non-interactive credential login (MFA is a hard stop — never blocks
on input); tools then raise `GarminAuthRequiredError` whose message names the fix
(`garmin-coach-login`, or `python scripts/garmin_login.py` from a checkout), and a
process latch fails fast for ~10 minutes.
Restart the server after re-login. `tests/test_garmin_contract.py` pins every
garminconnect call shape so dependency breaks fail CI loudly.

## Testing

1,762 tests across 52 files (CI: GitHub Actions on push/PR + weekly cron —
a `ruff check .` lint job plus the suite on Python 3.12/3.13/3.14; both install
`requirements-dev.txt`). Four patterns are THE way tests are written:

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
- **Startup imports** — every module under `coach/` is imported when
  `coach.server` is: `tests/test_imports.py` starts the real entry point in
  a fresh interpreter and diffs `sys.modules` against the files on disk.
  Function-local imports exist only to break cycles and must resolve from
  modules already loaded; a long-running server that imports a file for
  the first time inside a tool call loads it against old siblings after a
  code update (the 2026-10-27 push crash). Restart the server after
  changing code.

Import coach modules as `import coach.planner as planner`, never
`from coach.planner import f` — otherwise monkeypatch targets go stale and
patches silently never take effect.

Adding or changing a tool: use the `/new-tool` skill (the full checklist:
parser → tool → annotations inventory → clock allowlist → tests → e2e goldens).

## Project Skills

`.claude/skills/`: `mcp-builder` (MCP server design guide), `new-tool` (add an
MCP tool the repo way), `release` (version bump → tag → PyPI/registry),
`capture-fixtures` (owner-run fixture refresh), `acwr-recompute` (supervised
ACWR history migration), and the four sport authoring skills
`running-session-author`, `cycling-session-author`, `strength-session-author`,
`mobility-session-author` (how a session of that sport is composed and written
in the session contract; their JSON examples are validated by
`tests/test_skills.py`). `.claude/agents/workout-author.md` is the Claude Code
agent that authors one session against `preview_workout` until the step tree
matches the brief; its `tools` list is an allowlist of read and preview tools,
so it cannot save, push or ingest. The same skills are meant to be
uploaded to claude.ai as custom skills (they then sync into Claude Code and
cover Claude Desktop).

## Session authoring

Coaching judgment about how a session is composed lives in the sport skills
(markdown); code holds the invariants. `coach/authoring.py` is the ONE
vocabulary for `structure` (runs, rides) and `exercises` (strength): strict
models with teaching errors (unknown keys name the allowed keys; nothing the
plan states is defaulted — no 3x10, no category weights, no library cue or
baseline load without a flag; plain runs and indoor rides split warm-up and
cool-down by documented percentages unless `warmup_mins`/`cooldown_mins` are
stated, and the contract document says so). The injury gate also reads
strength exercises on the sessions exactly as the push expands them: every
content word of a free-text restriction, stemmed, with negations and
qualifiers dropped, must appear in the exercise's Garmin key or category
('no jumping' catches BOX_JUMP and every PLYO movement; sport words stay on
the session type; `coach/authoring.py` holds the vocabulary and
`update_injury_status` reports how many catalogue keys a restriction reaches).
`update_weekly_plan` rejects contract violations (`authoring_error`),
`push_plan_to_garmin`'s preview carries the rendered step tree per session and
lists `unrenderable` sessions (never uploaded), and `preview_workout(session)`
renders one session for the authoring loop (`preview_workout()` returns the
contract). Garmin facts that are data, not judgment (the 35 workout categories,
the catalogue categories the API rejects, the one-category-per-name cache
corrections) stay in `coach/authoring.py`.

## Known Issues / TODO

- **`CTL_TARGETS` are on the wrong scale.** The race-type CTL targets in
  `coach/config.py` were written for TRIMP-style loads, while `daily_loads`
  carry Garmin's EPOC `activityTrainingLoad` (6-8x larger per session), so a
  target like 65 reads low against the athlete's real CTL (the Aug 2026 incident
  quoted CTL 103 against it). The snapshot's `ctl_target` is advisory until the
  targets are recalibrated from the athlete's own history; that is a coaching
  decision for the owner, not a code fix.
- **Padel workouts have no sport type on Garmin.** Garmin's workout service has
  no padel/racket sport (`workout-service/workout/types` lists 12 sports), so
  padel pushes are stored with sport `(0, None)`. Recorded padel activities are
  type 226 `paddelball` and classify correctly. Keep `PADEL_SPORT` as is; see
  the comment in `coach/workout_builder.py` before changing it.
