# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**"The Enforcer"** - An AI-driven training coach MCP server that:
- Fetches fitness data from Garmin Connect
- Maintains a 7-day rolling training plan
- Enforces training pillars (Strength, Mobility, Long Effort)
- Provides LLM-driven coaching with daily briefs

**Athlete context:** Running, Cycling, High-intensity games (Ultimate). Injury-prone, needs enforced Mobility + Strength. Multiple target events with A/B/C priority.

## Commands

```bash
# Run the MCP server
python server.py

# Run all tests (83 tests)
python -m pytest test_server.py test_rules.py test_planner.py -v

# Run tests for specific module
python -m pytest test_rules.py -v

# Run morning audit (standalone mode)
python daily_loop.py

# Run morning audit with LLM
python daily_loop.py --llm
```

## MCP Tools Reference

### Data Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_daily_metrics()` | RHR, Body Battery, Sleep Score | JSON object |
| `get_activities_range(start, end)` | Activity history | JSON array |
| `get_personal_records()` | All PBs | JSON array |
| `get_training_readiness(date)` | Recovery score, HRV, load | JSON object |
| `get_athlete()` | Full athlete profile (personal, constraints, preferences) | JSON object |
| `update_athlete(section, data)` | Update profile section (personal, preferences, add_injury, etc.) | Confirmation |
| `refresh_athlete_baseline()` | Generate baseline from 6mo Garmin history | JSON summary |

### Methodology Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_methodology()` | View pillars, constraints, race templates, activity classifications | JSON object |
| `update_methodology(section, data)` | Update pillars, constraints, or race templates | Confirmation |

**update_methodology sections:** `pillars`, `safety_constraints`, `add_race_template`, `update_race_template`

### Compliance Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_compliance_report(days)` | Weekly pillar compliance | JSON with deficits |

### Planning Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_planning_context()` | Full context for LLM planning | JSON object |
| `get_weekly_plan()` | Current 7-day plan | JSON object |
| `update_weekly_plan(plan_json)` | Save new/updated plan | Confirmation |

### Race Management Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `list_races()` | View all races with days_until | JSON array |
| `add_race(name, date, priority, ...)` | Add new A/B/C race | Confirmation |
| `remove_race(name)` | Remove race by name | Confirmation |
| `update_race(name, ...)` | Update any race field | Confirmation |
| `research_race(name or url)` | Fetch race info for training context | JSON with course/elevation/terrain |

### Suggestion Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `propose_suggestion(type, desc, rationale, change)` | LLM proposes config change | Suggestion ID |
| `list_pending_suggestions()` | See pending suggestions | JSON array |
| `approve_suggestion(id)` | User approves suggestion | Confirmation |
| `reject_suggestion(id, reason)` | User rejects suggestion | Confirmation |

### Injury Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `diagnose_injury(location, answers)` | Clinical assessment (two-phase) | Questions then diagnosis |
| `research_injury(injury_type, severity, url)` | Web research for treatment & recovery | JSON with researched info |
| `update_injury_status(date, status, notes)` | Track injury progress | Confirmation |

**diagnose_injury workflow:**
1. Phase 1: Call with `location` only → returns clinical assessment questions
2. Phase 2: Call with `location` + `answers` (JSON) → returns possible conditions, severity, recommendations

**Supported body regions:** shin, knee, ankle, back, shoulder, hip, foot, calf

**research_injury workflow:**
- Auto-search: `research_injury("shin splints")` - searches Wikipedia and extracts treatment/rehab info
- Direct URL: `research_injury("tendinitis", url="https://...")` - fetches and parses specific resource
- Each injury is researched uniquely from web sources rather than using static protocols

## Architecture

```
coach-mcp/
├── server.py              # MCP server with all tools
├── garmin_client.py       # Garmin auth with token caching
├── rules.py               # Compliance checker, safety rules
├── planner.py             # Context builder, plan/suggestion management
├── daily_loop.py          # Morning audit automation
├── notifier.py            # Notification system (console/telegram)
├── config.py              # Shared configuration and constants
├── data/
│   ├── athlete.json           # WHO - personal info, life constraints, preferences
│   ├── athlete_baseline.json  # WHO - Garmin-derived capacity (auto-generated)
│   ├── methodology.json       # HOW - pillars, safety rules, race templates
│   ├── training_config.json   # WHAT - events, current training block
│   ├── weekly_plan.json       # CURRENT - rolling 7-day plan
│   └── suggestions.json       # Pending LLM suggestions
├── test_server.py         # Server tests (46 tests)
├── test_rules.py          # Rules tests (23 tests)
├── test_planner.py        # Planner tests (14 tests)
└── test_fixtures.json     # Real Garmin API responses (gitignored)
```

## Data File Structure

### athlete.json - WHO the athlete is
Manually edited. Contains:
- `personal`: name, age, max_hr, HR zones, FTP, weight
- `life_constraints`: recurring commitments (e.g., Wednesday Padel), preferred training times, work schedule
- `injury_history`: past injuries with status and notes
- `preferences`: likes, dislikes, equipment
- `coaching_notes`: free-form notes for the AI coach

### athlete_baseline.json - Garmin-derived capacity
Auto-generated by `refresh_athlete_baseline()`. Contains:
- `baseline`: avg/max weekly volume, activity distribution
- `personal_records`: all PRs from Garmin
- `last_refreshed`: timestamp

### methodology.json - HOW to train
Rarely changes. Contains:
- `pillars`: strength 2x/week, mobility 90min/week, long effort 1x/week
- `safety_constraints`: max consecutive hard days, rest after race
- `race_templates`: key sessions and phase guidance by race type

### training_config.json - WHAT they're training for
User-edited for race calendar. Contains:
- `events`: race calendar with A/B/C/D priorities
- `current_block`: phase, dates, volume target, focus

## Training Pillars

Defined in `data/methodology.json`:
- **Strength:** 2 sessions per week
- **Mobility:** 90 minutes per week (yoga, pilates, stretching)
- **Long Effort:** 1 session of 60+ minutes cardio per week

## Activity Classification

`rules.py:classify_activity()` categorizes activities:
- **Strength:** strength_training, indoor_cardio, functional_strength
- **Mobility:** yoga, pilates, stretching, breathwork
- **Long Effort:** 60+ min running, cycling, swimming
  - **Hard:** ultimate_disc, hiit, interval_training, or avg_hr > 150

## Garmin API Response Structures

**Activities** (`get_activities_by_date`):
```python
activity['activityType']['typeKey']  # "running", "strength_training", etc.
activity['duration']  # seconds
activity['distance']  # meters
activity['averageHR'], activity['maxHR']
```

**Personal Records** (`get_personal_record`):
```python
pr_data['personalRecords'][0]['prTypeLabelKey']  # "pr_running_fastest_5k_time"
pr_data['personalRecords'][0]['value']  # seconds for time-based
```

**Training Readiness** (`get_training_readiness`):
```python
readiness['score']  # 0-100
readiness['level']  # "PRIME", "HIGH", "MODERATE", "LOW"
readiness['hrvStatus'], readiness['acuteLoad']
```

## Implementation Status

All 4 sprints complete:

### Sprint 1: Data Foundation
- [x] `get_activities_range()` - Activity history
- [x] `get_personal_records()` - PBs
- [x] `get_training_readiness()` - Recovery metrics
- [x] `refresh_athlete_baseline()` - Generate baseline from 6mo Garmin history
- [x] `get_athlete()` - Full athlete profile with life constraints
- [x] `calculate_baseline()` - Weekly volume and activity distribution

### Sprint 2: Rule Engine
- [x] Data files: `athlete.json`, `methodology.json`, `training_config.json`
- [x] `classify_activity()` - Categorize by pillar
- [x] `check_weekly_compliance()` - Validate pillars met
- [x] `check_safety_rules()` - Consecutive hard days, post-race rest
- [x] `get_compliance_report()` MCP tool

### Sprint 3: LLM Planning
- [x] `build_planning_context()` - Assemble all context for LLM
- [x] Planning MCP tools (get/update weekly plan)
- [x] Suggestion system (propose, list, approve, reject)

### Sprint 4: Automation
- [x] `daily_loop.py` - Morning audit script
- [x] `notifier.py` - Console/Telegram notifications
- [x] LLM integration with `--llm` flag

## Environment

Requires `.env` with:
```
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=yourpassword
ANTHROPIC_API_KEY=sk-ant-xxx  # For LLM integration
TELEGRAM_BOT_TOKEN=xxx        # Optional, for notifications
TELEGRAM_CHAT_ID=xxx          # Optional, for notifications
```

## Testing Pattern

Tests use real API responses captured in `test_fixtures.json`. When adding new tools:
1. Create parsing function (pure, no I/O)
2. Add MCP tool with `@mcp.tool()` decorator
3. Write tests with sample data matching Garmin structure
4. Run: `python -m pytest -v`

## The Enforcer Personality

The LLM coach persona (defined in `daily_loop.py`):
- Direct and honest - no sugar-coating
- Evidence-based - always cite the data
- Supportive but firm - care about the athlete's goals
- Action-oriented - every message should guide the next step

## When to Suggest New Tools

The AI coach should proactively identify when a new tool would improve coaching capability. Before implementing features, ask: **"Should this be a tool?"**

### Signs a new tool is needed:
1. **Repeated manual work** - If the LLM keeps doing the same data gathering/transformation
2. **Missing context** - Can't make good decisions without information that could be fetched
3. **User friction** - User has to manually provide data that could be automated
4. **Pattern emerges** - Same type of request comes up multiple times

### Tool proposal process:
1. Identify the gap in current tooling
2. Describe what the tool would do and what it returns
3. Explain how it improves coaching decisions
4. Use `propose_suggestion(type='new_tool', ...)` to formally suggest it

### Examples of good tool suggestions:
- "I notice I can't see your sleep trends. A `get_sleep_history(days)` tool would help me correlate recovery with training load."
- "You keep asking about weather for race day. A `get_race_weather(name)` tool could fetch forecasts automatically."
- "Your injury history isn't tracked. An `add_injury(type, date, notes)` tool would help me adjust training safely."

### Tool design principles:
- **Single responsibility** - One tool, one job
- **Return JSON** - Structured data the LLM can reason about
- **Fail gracefully** - Return `{'error': ...}` not exceptions
- **Include context** - Return enough info for decisions, not just raw data
