# AI Training Coach

An MCP server that connects to Garmin Connect and provides AI-driven training coaching with persistent coaching memory, structured training pillars, and race periodization.

## Quick Start

### Prerequisites

- **Python 3.10+**
- A **Garmin Connect** account (free)
- An MCP client — recommended: [Claude Code](https://claude.ai/code)

### 1. Clone & set up virtual environment

```bash
git clone https://github.com/snoozelieb/coach-mcp.git
cd coach-mcp

python -m venv .venv

# Activate the virtual environment:
# Linux/macOS:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Create your `.env` file

```bash
cp .env.example .env
```

Edit `.env` and fill in your Garmin credentials:

```env
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=your_garmin_password
```

### 4. Run the setup wizard

```bash
python scripts/setup_wizard.py
```

This creates your personal data files in `data/`:
- `athlete.json` — your profile, HR zones, preferences
- `training_config.json` — training phase and race calendar
- `weekly_plan.json` — your rolling 7-day plan
- `coaching_log.json` — coaching memory (decisions, patterns)

You can also set up manually or via the MCP tools (`update_athlete()`, `add_race()`).

### 5. Connect to Claude Code

```bash
claude mcp add coach-mcp python server.py
```

### 6. Start coaching

Open Claude Code and say something like:

> "I'd like to set up my training plan. I'm training for [your race/goal] on [date]."

The coach will pull your Garmin data, ask about your goals and constraints, and build a personalized plan. From there, check in regularly — the coach remembers previous decisions and adapts as you go.

## Features

- **Garmin Integration**: Fetches activities, recovery metrics, and personal records
- **Training Pillars**: Enforces Strength (2x/week), Mobility (90min/week), Long Effort (1x/week)
- **Race Periodization**: Plans training around A/B/C/D priority races
- **Rolling 7-Day Plans**: Maintains and updates weekly training plans with PURPOSE for each session
- **Persistent Coaching Memory**: Decisions, athlete responses, and adaptation patterns survive between sessions
- **Compliance Tracking**: Monitors pillar adherence and safety rules
- **Injury Management**: Diagnosis, research, and recovery tracking
- **Strength Progression**: Tracks baselines, suggests weight increases, syncs from Garmin
- **Push to Garmin**: Sends structured workouts to your Garmin calendar

## Data Files

| File | Purpose | Git Status |
|------|---------|------------|
| `data/methodology.json` | Training pillars and safety rules | Shared (committed) |
| `data/exercises.json` | Garmin exercise reference data | Generated (gitignored) — run `scripts/fetch_exercises.py` |
| `data/athlete.json` | Your personal info, preferences | Personal (gitignored) |
| `data/athlete_baseline.json` | Garmin-derived training capacity | Personal (gitignored) |
| `data/training_config.json` | Your race calendar and current phase | Personal (gitignored) |
| `data/weekly_plan.json` | Current 7-day rolling plan | Personal (gitignored) |
| `data/coaching_log.json` | Coaching memory (decisions, patterns) | Personal (gitignored) |
| `data/exercise_library.json` | Cached exercise form cues for Garmin notes | Personal (gitignored) |

Personal data files are created by the setup wizard and stay on your machine.

## MCP Tools Overview

The server exposes 50+ tools organized by function:

| Category | Tools | What they do |
|----------|-------|-------------|
| **Garmin Data** | `get_daily_metrics`, `get_activities_range`, `get_personal_records` | Fetch activity history, recovery metrics, and personal bests |
| **Fitness** | `refresh_athlete_baseline`, `get_training_readiness`, `get_load_status` | Training load, fitness/fatigue balance, ACWR |
| **Coaching** | `get_coaching_snapshot`, `get_compliance_report`, `get_coaching_score` | Full coaching state, pillar compliance, self-assessment |
| **Planning** | `get_planning_context`, `get_weekly_plan`, `update_weekly_plan`, `push_plan_to_garmin` | Build, update, and push training plans |
| **Athlete** | `update_athlete`, `set_ftp`, `set_threshold_pace` | Manage profile, power zones, pace zones |
| **Races** | `add_race`, `list_races`, `research_race`, `update_race`, `remove_race` | Race calendar and course research |
| **Strength** | `sync_strength_session`, `generate_strength_workout`, `get_strength_baseline` | Track gym work, auto-progress weights |
| **Injuries** | `diagnose_injury`, `research_injury`, `update_injury_status` | Clinical assessment, research, recovery tracking |
| **Research** | `research_exercise`, `list_exercises`, `research_sport` | Exercise form cues, sport-specific training info |
| **Decisions** | `log_coaching_decision`, `record_athlete_response`, `propose_major_change` | Persistent coaching memory and approval flow |

You don't need to call these directly — the coach uses them automatically during conversation.

## Running Tests

```bash
python -m pytest -v
```

## Morning Audit (standalone)

```bash
python scripts/daily_loop.py        # Standalone mode
python scripts/daily_loop.py --llm  # With LLM coaching (requires ANTHROPIC_API_KEY)
```

## Environment

Requires `.env` with `GARMIN_EMAIL` and `GARMIN_PASSWORD`. `ANTHROPIC_API_KEY` is optional (only needed for `daily_loop.py --llm`).

## License

MIT
