# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

An **adaptive AI training coach** MCP server that:
- Fetches fitness data from Garmin Connect
- Maintains a 7-day rolling training plan with PURPOSE for each session
- Uses **persistent coaching memory** - decisions survive between sessions
- **Understands the athlete** - discovers goals, constraints, history via conversation
- **Prescribes with authority** - coach decides what's best, athlete follows
- Uses Garmin's load metrics for intensity recommendations
- Requires user approval for major coaching changes (but coach makes recommendations)

## Coaching Philosophy

**You are the coach. You know better.** The athlete trusts you to:

1. **Be science-based, not opinion-based**
   - If you don't know something, research it before recommending
   - Use `research_injury()`, `research_race()`, `research_sport()`, `research_exercise()` to gather evidence
   - Base training loads on actual data (Garmin metrics, compliance history)

2. **Push back on bad ideas**
   - If an athlete wants to do something stupid (race on an injury, skip recovery, overtrain), say NO
   - Explain WHY it's a bad idea with evidence
   - Don't be a pleaser - be honest even when it's not what they want to hear

3. **Adapt approach, not standards**
   - Personalize HOW you train them (pillars, goals, schedule)
   - Never compromise on safety constraints (rest after races, injury protocols)
   - An ultra runner and a beginner have different plans, but both follow sound principles

4. **Help them achieve their dreams**
   - Understand what success looks like for THEM
   - Build a realistic path to get there
   - Protect them from themselves when enthusiasm exceeds capacity

5. **Be direct and clear**
   - "You need rest" not "Maybe consider possibly taking it easy"
   - "This is a bad idea because X" not "That's interesting but have you thought about..."
   - Give recommendations, not menus of options

**Remember:** Athletes hire coaches because they DON'T know what's best. Your job is to know for them.

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
| `update_athlete(section, data)` | Update profile section (personal, preferences, training_pillars, etc.) | Confirmation |
| `refresh_athlete_baseline()` | Generate baseline from 6mo Garmin history | JSON summary |
| `get_onboarding_guide()` | Get personas and onboarding conversation guide | JSON guide |

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
| `push_plan_to_garmin()` | Push workouts to Garmin calendar | JSON summary |

**push_plan_to_garmin() workflow:**
- Converts sessions to Garmin workout format with targets:
  - **Cycling**: HR zone targets from athlete profile
  - **Running**: Pace zone targets (if threshold set) or HR fallback
  - **Swimming**: 25m pool setting
  - **Strength**: Full exercise list with sets/reps
  - **Yoga/Mobility**: Timed sessions
- Automatically expands double sessions (e.g., AM ride + PM mobility → 2 workouts)
- Skips optional sessions (e.g., pool_sauna) and rest days
- Deletes existing workouts from plan period before pushing (prevents duplicates)
- Returns summary: pushed count, dates, any errors

### Performance Testing Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `set_threshold_pace(pace, time_trial_mins, time_trial_distance_km)` | Set running threshold from test | Pace zones |
| `set_ftp(ftp_watts, test_avg_watts, test_duration_mins)` | Set cycling FTP from test | Power zones |

**set_threshold_pace examples:**
```python
set_threshold_pace(pace="5:30")  # Direct: 5:30/km
set_threshold_pace(time_trial_mins=30, time_trial_distance_km=6.2)  # From 30-min TT
```
- Calculates pace zones using Jack Daniels methodology
- Running workouts then use pace targets instead of HR

**set_ftp examples:**
```python
set_ftp(test_avg_watts=265, test_duration_mins=20)  # From 20-min test (×0.95)
set_ftp(ftp_watts=250)  # Direct value
```
- Calculates 7-zone power model
- Cycling workouts then use power targets

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

### Coaching Decision Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `log_coaching_decision(type, decision, rationale)` | Record significant coaching decisions | Decision ID |
| `get_active_decisions()` | Get all active coaching decisions | JSON array |
| `update_decision_status(id, status, outcome)` | Update or close a decision | Confirmation |
| `propose_major_change(type, proposal, rationale)` | Propose major change requiring approval | Proposal ID |
| `list_pending_approvals()` | View pending coaching changes | JSON array |
| `approve_coaching_change(id)` | Approve a pending change | Confirmation |
| `reject_coaching_change(id, reason)` | Reject a pending change | Confirmation |
| `record_athlete_response(stimulus, response, pattern)` | Track athlete adaptation patterns | Confirmation |
| `get_response_patterns()` | Get identified athlete patterns | JSON object |

**Coaching continuity workflow:**
1. At session start: call `get_active_decisions()` to load previous decisions
2. During planning: decisions should influence recommendations
3. When making significant choices: call `log_coaching_decision()` to persist
4. For major changes (phase transition, >15% volume change): use `propose_major_change()`
5. After reviewing completed sessions: call `record_athlete_response()` to track patterns

### Load & Goal Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_load_status()` | Training readiness, load ratio, recommendations | JSON object |
| `get_goal_progress(days)` | Balance across race/fun/aesthetics goals | JSON with percentages |

**Goal Balance (target split):**
- Race Preparation: 50% (sani2c training)
- Fun Activities: 25% (Padel, Ultimate Frisbee)
- Aesthetics: 25% (Upper body strength, gym)

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

### Research Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `research_race(name, url)` | Course info, elevation, difficulty | JSON with training focus |
| `research_sport(sport_name, url)` | Training principles for unfamiliar sports | JSON with periodization, injuries, demands |
| `research_exercise(exercise_name, url)` | Form cues, muscles, progressions | JSON with technique info |
| `research_injury(...)` | See Injury Tools above | Treatment & recovery info |

**research_sport workflow:**
- Use when onboarding athlete in unfamiliar sport (climbing, CrossFit, rowing, martial arts)
- Returns: training approaches, periodization patterns, common injuries, physical demands
- Example: `research_sport("rock climbing")` - fetches Wikipedia and extracts training-relevant info

**research_exercise workflow:**
- Use when building strength programs or finding injury-safe alternatives
- Returns: muscles worked, form cues, safety notes, variations
- Example: `research_exercise("romanian deadlift")` - proper form and progressions

## Architecture

```
coach-mcp/
├── server.py              # MCP server with all tools
├── garmin_client.py       # Garmin auth with token caching
├── workout_builder.py     # Converts plan sessions to Garmin workouts
├── rules.py               # Compliance checker, safety rules
├── planner.py             # Context builder, plan/suggestion management
├── daily_loop.py          # Morning audit automation
├── notifier.py            # Notification system (console/email)
├── config.py              # Shared configuration and constants
├── data/
│   ├── athlete.json           # WHO - personal info, life constraints, preferences
│   ├── athlete_baseline.json  # WHO - Garmin-derived capacity (auto-generated)
│   ├── methodology.json       # HOW - pillars, safety rules, race templates
│   ├── training_config.json   # WHAT - events, periodization, goal balance
│   ├── weekly_plan.json       # CURRENT - rolling 7-day plan with session PURPOSE
│   ├── coaching_log.json      # MEMORY - coaching decisions, patterns, approvals
│   └── suggestions.json       # Pending LLM suggestions
├── test_server.py         # Server tests (46 tests)
├── test_rules.py          # Rules tests (23 tests)
├── test_planner.py        # Planner tests (14 tests)
└── test_fixtures.json     # Real Garmin API responses (gitignored)
```

## Data File Structure

### athlete.json - WHO the athlete is
Manually edited. Contains:
- `training_pillars`: **personalized** training pillars (based on persona, customized via onboarding)
- `personal`: name, age, max_hr, HR zones, FTP, power_zones, threshold_pace, pace_zones, weight
- `life_constraints`: recurring commitments (e.g., Wednesday Padel), preferred training times, work schedule
- `injury_history`: past injuries with status and notes
- `swimming`: experience level, pace, comfortable distance, strokes (ask user to personalize)
- `pilates`: experience, class preference, focus areas, injury considerations
- `preferences`: likes, dislikes, equipment, gym_access
- `coaching_notes`: free-form notes for the AI coach

### athlete_baseline.json - Garmin-derived capacity
Auto-generated by `refresh_athlete_baseline()`. Contains:
- `baseline`: avg/max weekly volume, activity distribution
- `personal_records`: all PRs from Garmin
- `last_refreshed`: timestamp

### methodology.json - HOW to train (shared templates)
Rarely changes. Contains:
- `personas`: starting templates (endurance_athlete, strength_athlete, recreational, return_from_injury, multi_sport)
- `default_pillar_templates`: fallback pillars if athlete has none configured
- `safety_constraints`: max consecutive hard days, rest after race
- `race_templates`: key sessions and phase guidance by race type
- `recovery_protocols`: pre-sleep stretching by activity type (cycling, running, high-intensity)

### training_config.json - WHAT they're training for
User-edited for race calendar. Contains:
- `events`: race calendar with A/B/C/D priorities
- `current_block`: phase, dates, volume target, focus
- `periodization`: phase definitions (base/build/peak/taper) with intensity distributions
- `goals`: **flexible goals array** - any type (event, health, wellness, aesthetics, fun)
- `weekly_structure`: preferred long day, strength days, rest rules

### coaching_log.json - LLM MEMORY
Auto-managed by coaching tools. Contains:
- `decisions`: logged coaching decisions with rationale and status
- `pending_approvals`: major changes awaiting user approval
- `athlete_responses`: tracked adaptation patterns

## Training Pillars

**Personalized in athlete.json** (not fixed in methodology). Each athlete has their own pillars based on:
- Selected persona (starting template)
- Goals (event-focused vs wellness vs strength)
- Time available
- Customization via onboarding conversation

Example pillar structure:
```json
{"name": "endurance", "target_hours_per_week": 4, "target_type": "hours", "types": ["cycling", "running"]}
{"name": "strength", "target_sessions_per_week": 2, "target_type": "sessions", "types": ["strength_training"]}
```

Use `get_onboarding_guide()` to start the personalization process.

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
- [x] `notifier.py` - Console/Email notifications
- [x] LLM integration with `--llm` flag

## Environment

Requires `.env` with:
```
GARMIN_EMAIL=your@email.com
GARMIN_PASSWORD=yourpassword
ANTHROPIC_API_KEY=sk-ant-xxx  # For LLM integration
```

## Testing Pattern

Tests use real API responses captured in `test_fixtures.json`. When adding new tools:
1. Create parsing function (pure, no I/O)
2. Add MCP tool with `@mcp.tool()` decorator
3. Write tests with sample data matching Garmin structure
4. Run: `python -m pytest -v`

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
