# coach-mcp

<!-- mcp-name: io.github.snoozelieb/coach-mcp -->

[![CI](https://github.com/snoozelieb/coach-mcp/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/snoozelieb/coach-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An opinionated AI training coach as an MCP server. It pulls your real data from
Garmin Connect and **prescribes with authority** — science-based load management
(ACWR), **code-enforced injury gates** (the server rejects plans that violate an
active injury restriction, no matter what the LLM says), and **persistent
coaching memory** so decisions, rationale, and your adaptation patterns survive
between conversations. It will tell you "no" when your enthusiasm exceeds your
capacity.

All health data and credentials stay on your machine — see
[Security & Privacy](#security--privacy).

## Quickstart

You need Python 3.12+, a free Garmin Connect account, and an MCP client
(Claude Code, Claude Desktop, or Cursor).

### Option A: uvx (recommended)

No install step — your MCP client runs the server on demand:

```bash
uvx garmin-coach-mcp
```

Jump to [Connect your MCP client](#connect-your-mcp-client) and use `uvx` as
the command.

### Option B: from source

```bash
git clone https://github.com/snoozelieb/coach-mcp.git
cd coach-mcp

python -m venv .venv
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
cp .env.example .env   # then edit: GARMIN_EMAIL, GARMIN_PASSWORD
python server.py
```

## Connect your MCP client

The server needs two environment variables: `GARMIN_EMAIL` and
`GARMIN_PASSWORD`. Optional: `COACH_DATA_DIR` (where your coaching data lives)
and `ANTHROPIC_API_KEY` (only for the standalone `daily_loop.py --llm` script).
From a source checkout, a `.env` file works too.

### Claude Code

```bash
claude mcp add coach-mcp \
  --env GARMIN_EMAIL=you@example.com \
  --env GARMIN_PASSWORD=your_garmin_password \
  -- uvx garmin-coach-mcp
```

Or in `.mcp.json`:

```json
{
  "mcpServers": {
    "coach-mcp": {
      "command": "uvx",
      "args": ["garmin-coach-mcp"],
      "env": {
        "GARMIN_EMAIL": "you@example.com",
        "GARMIN_PASSWORD": "your_garmin_password",
        "COACH_DATA_DIR": "/path/to/your/coach-data"
      }
    }
  }
}
```

Running from source instead: `claude mcp add coach-mcp -- python /full/path/to/coach-mcp/server.py`

### Claude Desktop

In `claude_desktop_config.json` (Settings → Developer → Edit Config):

```json
{
  "mcpServers": {
    "coach-mcp": {
      "command": "uvx",
      "args": ["garmin-coach-mcp"],
      "env": {
        "GARMIN_EMAIL": "you@example.com",
        "GARMIN_PASSWORD": "your_garmin_password",
        "COACH_DATA_DIR": "/path/to/your/coach-data"
      }
    }
  }
}
```

### Cursor

In `.cursor/mcp.json` (project) or `~/.cursor/mcp.json` (global):

```json
{
  "mcpServers": {
    "coach-mcp": {
      "command": "uvx",
      "args": ["garmin-coach-mcp"],
      "env": {
        "GARMIN_EMAIL": "you@example.com",
        "GARMIN_PASSWORD": "your_garmin_password",
        "COACH_DATA_DIR": "/path/to/your/coach-data"
      }
    }
  }
}
```

If you installed with `pip install garmin-coach-mcp` instead of uvx, use
`"command": "garmin-coach-mcp"` with no args in any of the blocks above.

## First run

1. **Create your profile.** From a source checkout, run the interactive wizard:

   ```bash
   python scripts/setup_wizard.py
   ```

   It creates your athlete profile, training config, and empty plan/memory
   files in the data directory. Alternatively, create the two required files
   by hand and let the coach fill in the rest via conversation:

   ```bash
   echo '{"personal":{"name":null},"injury_history":[],"life_constraints":{}}' > data/athlete.json
   echo '{"events":[],"current_block":{"phase":"base"}}' > data/training_config.json
   ```

2. **Pull your Garmin baseline.** In your MCP client, say:

   > "Run refresh_athlete_baseline and set up my training."

   The coach pulls your name, weight, age, HR data, and training capacity from
   Garmin, then starts the onboarding conversation — goals, constraints,
   injury history, race calendar.

3. **Garmin MFA / expired session.** Garmin logins are token-cached. If tools
   start returning `AUTH_REQUIRED`, recover with:

   ```bash
   python scripts/garmin_login.py
   ```

   It does a fresh credential login, prompts for the MFA code if Garmin asks,
   and saves new tokens. Restart the MCP server afterwards.

## How it works

1. **Snapshot first** — every coaching conversation starts from
   `get_coaching_snapshot()`: current time context, 7-day week grid (rest days
   explicit), fitness metrics, plan adherence, open anomalies, injuries, sleep
   gate.
2. **Load hierarchy before prescribing** — overall ACWR (injury gate, 0.8–1.3
   sweet spot), then sport-specific ACWR (spike detection), then
   sport-specific CTL (race readiness).
3. **Hard gates are code, not vibes** — `update_weekly_plan` and
   `push_plan_to_garmin` reject sessions that violate an active injury's
   restricted activities, and every non-rest session must carry a `purpose` or
   the save is refused.
4. **Curiosity with memory** — planned-vs-actual anomalies (missed session,
   type mismatch, activity on a rest day) register once with a lifecycle
   (open → asked → resolved); the coach asks you what happened instead of
   silently assuming.
5. **Everything persists** — decisions, approvals, adaptation patterns, and
   season lifecycle (race debriefs, phase transitions) live in local JSON and
   carry across sessions.

## MCP surface

49 tools — you don't call them directly; the coach uses them during
conversation:

| Category | Tools |
|----------|-------|
| **Coaching core** | `get_coaching_snapshot` (canonical, sectioned), `get_compliance_report`, `get_coaching_score` |
| **Planning** | `get_weekly_plan`, `update_weekly_plan`, `push_plan_to_garmin`, `get_week_constraints`, `get_weekly_prescription`, `get_periodization_status`, `update_phase` |
| **Garmin data** | `query_metrics` (kind=fitness/intensity/daily/readiness/personal_records), `get_activities_range` |
| **Athlete** | `get_athlete`, `update_athlete`, `set_ftp`, `set_threshold_pace`, `analyze_ftp_test`, `refresh_athlete_baseline`, `refresh_fitness_history`, `get_onboarding_guide` |
| **Methodology** | `get_methodology`, `update_methodology` |
| **Races** | `races` (action=list/add/update/research), `remove_race` |
| **Strength** | `sync_strength_session`, `get_strength_baseline`, `approve_progression`, `set_exercise_preference`, `generate_strength_workout`, `add_exercise` |
| **Injuries** | `diagnose_injury`, `research_injury`, `update_injury_status` |
| **Research** | `research_exercise`, `list_exercises`, `research_sport` |
| **Memory** | `log_coaching_decision`, `get_active_decisions`, `update_decision_status`, `record_athlete_response`, `get_response_patterns`, `resolve_anomaly` |
| **Approvals** | `propose_coaching_action`, `list_pending_approvals`, `approve_proposal`, `reject_proposal` |
| **Interactive** | `generate_smart_brief`, `interactive_check_in` |

Every tool carries MCP annotations (read-only / destructive / idempotent /
open-world), enforced by tests.

**5 prompts**: `weekly_planning`, `morning_brief`, `injury_assessment`,
`week_review`, `onboarding`.

**6 resources**: `coach://athlete/profile`, `coach://plan/current`,
`coach://config/training`, `coach://coaching/decisions`, `coach://context/now`,
`coach://coaching/doctrine` (the long-form coaching doctrine).

## Security & Privacy

Everything stays on your machine:

- **Credentials**: `GARMIN_EMAIL`/`GARMIN_PASSWORD` live in your MCP client
  config or a local `.env`. Garmin OAuth tokens are cached in a local token
  store (`.garth/` in a source checkout, a per-user `garmin-tokens` directory
  for installed copies; override with `COACH_TOKEN_DIR`).
- **Health data**: all coaching data (profile, plans, fitness history, sleep,
  coaching memory) is local JSON in your data directory. There is no backend,
  no telemetry, no analytics.
- **What leaves your machine**: requests to Garmin's own API (your
  credentials/tokens, sent only to Garmin); whatever your MCP client sends to
  its LLM as part of the conversation; optional public web-page fetches when
  the coach researches a race, injury, or exercise; and, only if you run
  `daily_loop.py --llm`, one request to the Anthropic API.
- **Single athlete per data directory** by design. For multiple athletes, run
  separate server instances with separate `COACH_DATA_DIR`s.

See [SECURITY.md](SECURITY.md) for details and how to report issues.

### Data directory

Resolution order: `COACH_DATA_DIR` env var → `data/` in a source checkout → a
per-user data directory (created on first run for installed packages). The
only file shipped with the package is `methodology.json` (safety rules, race
templates, personas); everything personal is created locally and never
committed.

## Advanced

```bash
# HTTP transport (streamable-http) instead of stdio
COACH_TRANSPORT=streamable-http FASTMCP_PORT=8000 garmin-coach-mcp

# Code Mode (search/execute meta-tools instead of 49 individual tools)
pip install fastmcp[code-mode]
COACH_CODE_MODE=1 garmin-coach-mcp

# Standalone morning audit
python scripts/daily_loop.py          # template-based brief
python scripts/daily_loop.py --llm    # LLM brief (needs ANTHROPIC_API_KEY)

# Tests (1,333 tests; clean checkouts use committed sanitized fixtures)
pip install -r requirements-dev.txt
python -m pytest -q
```

## Architecture

`server.py` registers tools from the `coach/` package (11 tool modules, pure
parsers, a typed pydantic storage layer, CTL/ATL/ACWR fitness math, a Garmin
client with token-first auth, and a workout builder that pushes structured
workouts to your watch). The project went through a five-phase modernization —
auth rebuild, schema layer, hard gates, sectioned snapshot, packaging — whose
full history and rationale live in
[docs/UPGRADE_ROADMAP.md](docs/UPGRADE_ROADMAP.md). Development conventions
are in [CLAUDE.md](CLAUDE.md).

## License

[MIT](LICENSE)
