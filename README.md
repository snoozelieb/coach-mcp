# AI Training Coach

An MCP server that connects to Garmin Connect and acts as your personal training coach — with persistent memory, science-based load management, and race periodization. Built for Claude.

## Quick Start

### Prerequisites

- **Python 3.10+**
- A **Garmin Connect** account (free)
- **Claude Code** (`npm install -g @anthropic-ai/claude-code`)

### 1. Clone and install

```bash
git clone https://github.com/snoozelieb/coach-mcp.git
cd coach-mcp

python -m venv .venv

# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Add your Garmin credentials

```bash
cp .env.example .env
```

Edit `.env`:

```env
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=your_garmin_password
```

### 3. Run the setup wizard

```bash
python scripts/setup_wizard.py
```

This creates your personal data files in `data/` — athlete profile, training config, empty plan, and coaching memory. The server requires these files to start.

> **Minimal setup:** If you want to skip the wizard and get going fast, create the two required files manually:
> ```bash
> mkdir -p data
> echo '{"personal":{"name":null},"injury_history":[],"life_constraints":{}}' > data/athlete.json
> echo '{"events":[],"current_block":{"phase":"base"}}' > data/training_config.json
> ```
> Then let the coach fill in your profile via conversation.

### 4. Connect to Claude

From the project directory:

```bash
claude mcp add coach-mcp -- python server.py
```

Or with an explicit path (works from anywhere):

```bash
claude mcp add coach-mcp -- python /full/path/to/coach-mcp/server.py
```

This registers the MCP server with Claude Code. The server runs locally via stdio — your Garmin data never leaves your machine.

### 5. Start coaching

Open Claude Code and say:

> "I'd like to set up my training. I'm preparing for [your race/goal] on [date]."

The coach will pull your Garmin data, learn about your goals and constraints, and prescribe a training plan. It remembers decisions between sessions and adapts as you progress.

## How It Works

The coach operates like a real coach:

1. **Understands you first** — pulls Garmin history, asks about goals, constraints, injuries
2. **Prescribes with authority** — tells you what to do and why, doesn't offer menus
3. **Tracks compliance** — compares planned vs actual, flags anomalies
4. **Adapts over time** — learns your response patterns, adjusts load accordingly
5. **Remembers everything** — decisions, rationale, and patterns persist between sessions

### What the coach uses

| Data Source | What It Provides |
|-------------|------------------|
| **Garmin Connect** | Activities, HR zones, training load (EPOC), readiness, sleep, body battery, personal records |
| **Your profile** (`athlete.json`) | Goals, constraints, injury history, preferences |
| **Training config** (`training_config.json`) | Race calendar, current phase, periodization |
| **Coaching memory** (`coaching_log.json`) | Past decisions, adaptation patterns, approval history |

### Load management

The coach uses a three-level safety hierarchy before prescribing any training:

1. **Overall ACWR** — total body injury gate (must be < 1.3)
2. **Sport-specific ACWR** — catches spikes (e.g., no running for 4 weeks then a long run)
3. **Sport-specific CTL** — race readiness (build toward target without violating levels 1-2)

## MCP Server Capabilities

### Tools

| Category | Examples |
|----------|---------|
| **Coaching** | `get_coaching_snapshot` (canonical context), `get_compliance_report`, `get_coaching_score` |
| **Planning** | `get_weekly_plan`, `update_weekly_plan`, `push_plan_to_garmin`, `get_week_constraints`, `get_weekly_prescription`, `get_periodization_status` |
| **Garmin Data** | `get_daily_metrics`, `get_activities_range`, `get_personal_records` |
| **Fitness** | `refresh_athlete_baseline`, `get_training_readiness` (HRV overlay), `get_fitness_status`, `get_intensity_distribution` |
| **Athlete** | `update_athlete`, `set_ftp`, `set_threshold_pace` |
| **Races** | `add_race`, `list_races`, `research_race` |
| **Strength** | `sync_strength_session`, `generate_strength_workout` |
| **Injuries** | `diagnose_injury`, `research_injury`, `update_injury_status` |
| **Research** | `research_exercise`, `research_sport` |
| **Decisions** | `log_coaching_decision`, `record_athlete_response`, `propose_major_change` |
| **Interactive** | `generate_smart_brief` (sampling), `interactive_check_in` (elicitation) |

You don't call these directly — the coach uses them during conversation.

### 5 Prompt Templates

Pre-built coaching workflows that Claude can invoke:

| Prompt | Use |
|--------|-----|
| `weekly_planning` | Build next week's plan with full context |
| `morning_brief` | Daily check-in: yesterday, today, readiness |
| `injury_assessment` | Structured injury diagnosis workflow |
| `week_review` | End-of-week review and adaptation |
| `onboarding` | New athlete setup conversation |

### 4 Resources

Read-only data endpoints for context:

| Resource | URI |
|----------|-----|
| Athlete profile | `coach://athlete/profile` |
| Weekly plan | `coach://plan/current` |
| Training config | `coach://config/training` |
| Coaching decisions | `coach://coaching/decisions` |

## Your First Conversation

After setup, start with one of these:

**New to coaching:**
> "I'm new here. Help me set up my training."

The coach will walk you through onboarding — asking about your background, goals, and constraints before prescribing a training approach.

**Have a race coming up:**
> "I have [race name] on [date]. It's a [distance/type]. Can you build me a plan?"

The coach will pull your Garmin data, assess your current fitness, and build a periodized plan working back from race day.

**Ongoing coaching:**
> "Good morning, how should I train today?"

The coach checks your readiness, sleep, compliance, and adapts today's session accordingly.

**Something hurts:**
> "My [body part] has been bothering me since [when]."

The coach runs a clinical assessment, researches the condition, and modifies your plan.

## Data Files

All personal data stays on your machine (gitignored):

| File | Purpose | Created By |
|------|---------|------------|
| `data/athlete.json` | Profile, HR zones, constraints, injuries | Setup wizard or coach |
| `data/athlete_baseline.json` | Garmin-derived training capacity | `refresh_athlete_baseline()` |
| `data/training_config.json` | Race calendar, current phase | Setup wizard or coach |
| `data/weekly_plan.json` | Rolling 7-day plan with session PURPOSE | Coach |
| `data/coaching_log.json` | Coaching memory (decisions, patterns) | Coach |
| `data/fitness_history.json` | Daily loads, CTL/ATL, sleep history | Auto-updated |
| `data/exercise_library.json` | Cached exercise form cues | `research_exercise()` |

Shared (committed):

| File | Purpose |
|------|---------|
| `data/methodology.json` | Safety rules, race templates, personas |

## Advanced

### Transport options

By default the server uses stdio (for Claude Code). For remote deployment:

```bash
# HTTP (streamable)
COACH_TRANSPORT=streamable-http FASTMCP_PORT=8000 python server.py

# SSE
COACH_TRANSPORT=sse python server.py
```

### Code Mode

For clients that support it, Code Mode replaces all 61 tools with search/execute meta-tools, reducing token overhead:

```bash
pip install fastmcp[code-mode]
COACH_CODE_MODE=1 python server.py
```

### Morning audit (standalone)

```bash
python scripts/daily_loop.py             # Template-based brief
python scripts/daily_loop.py --llm       # LLM-powered brief (needs ANTHROPIC_API_KEY)
```

### Running tests

```bash
pip install -r requirements-dev.txt
python -m pytest -v
```

557 tests across 16 test files.

## License

MIT
