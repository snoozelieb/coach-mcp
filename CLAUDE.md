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

## Science-Based Coaching Model

The coach operates at multiple timeframes, adapting plans while keeping long-term goals in sight:

```
SEASON (months)           ← Where are we going?
├── get_periodization_status()
├── A-race target, phase sequence
└── Fitness trajectory (CTL building toward race)

BLOCK (4-8 weeks)         ← What phase are we in?
├── Phase: base/build/peak/taper
├── Intensity distribution targets (80/20)
└── Volume trend (building/maintaining/reducing)

WEEK                      ← What should this week look like?
├── get_weekly_prescription()
├── Volume target (adjusted for ACWR)
├── Key sessions to prioritize
└── Constraints (injuries, life events)

DAY                       ← What should today look like?
├── get_training_readiness()
├── Adapt based on readiness score
└── Conversation with athlete
```

**Key Principle: The conversation IS the coaching.** Tools provide data and structure. The LLM provides intelligence and adaptation. When life gets in the way, the coach adjusts while keeping the athlete on track for their goals.

### MANDATORY Coaching Sequence

**CRITICAL: Before making ANY coaching recommendations, you MUST call `get_coaching_snapshot()` first.**

This prevents the coaching error of prescribing without understanding current state.

```
┌─────────────────────────────────────────────────────────────┐
│  BEFORE ANY RECOMMENDATION, CALL get_coaching_snapshot()   │
│                                                             │
│  This returns:                                              │
│  ├── Current weekly plan (what's planned)                   │
│  ├── Activities this week (what's actual)                   │
│  ├── Planned vs actual (gaps/completion rate)               │
│  ├── Fitness metrics (CTL, ATL, TSB, ACWR)                  │
│  ├── Compliance status (pillars met/missing)                │
│  ├── Recovery status (today's readiness)                    │
│  ├── Sport priority breakdown (multi-sport blending)        │
│  └── Active injuries and restrictions                       │
└─────────────────────────────────────────────────────────────┘
```

**Why this matters:**
- The coach proposed a plan without checking the existing plan = BAD
- The coach must see current state before recommending changes = GOOD
- The snapshot enforces this by bundling everything in one call

### Multi-Sport Handling

When an athlete has races in multiple sports (e.g., cycling A-race + running B-race):

1. **Sport Priority Analysis**: `get_coaching_snapshot()` calculates volume distribution
   - Weights based on: race priority (A>B>C>D) × time proximity (closer = higher weight)
   - Example: Cycling A-race 114 days away + Running B-race 45 days away → Running gets more volume NOW

2. **Shared Sessions**: Strength, mobility, recovery benefit ALL sports
   - Schedule these regardless of sport focus
   - They don't compete for sport-specific volume

3. **Sport-Specific Sessions**: Key sessions from race templates
   - `long_mtb_ride` for cycling races
   - `long_trail_run` for running races
   - Prioritize based on sport priority analysis

### Adaptive Coaching Flow

1. **Start of any coaching conversation**: Call `get_coaching_snapshot()` FIRST
2. **Analyze the snapshot**: What's the current state? What's working? What's missing?
3. **Planning**: Build/adjust plan based on snapshot + athlete conversation
4. **Adaptation**: When things change (missed session, feeling great/terrible), check snapshot again
5. **End of week**: Check compliance, update fitness history, plan next week

### Sleep as Foundation (Training Gate)

**Sleep is not just a metric - it's a GATE for training decisions.**

Without adequate sleep, training becomes CATABOLIC (breakdown) not ANABOLIC (building):
- ↓ Testosterone, ↓ Growth Hormone, ↑ Cortisol
- ↑ Inflammatory markers, impaired glycogen resynthesis
- **Adaptation literally cannot happen**

Research shows performance impact by exercise type (effect size):
- High-intensity intervals: **-1.57** (most affected)
- Skill/coordination: -1.06
- Aerobic endurance: -0.54
- Strength/power: -0.39 (least affected)

**Sleep Status → Training Modifications:**

| Status | Avg Sleep | Training Cap | Skip | Notes |
|--------|-----------|--------------|------|-------|
| Adequate | ≥7.5hrs | None | - | Full training |
| Borderline | 7-7.5hrs | Caution | - | Monitor, prioritize sleep |
| **Deficit** | 6.5-7hrs | Moderate | FTP tests, max efforts | No adaptation capacity |
| Severe | <6.5hrs | Recovery only | All intensity | Rest until sleep improves |

**Critical:** Early AM workouts that cut into sleep are COUNTERPRODUCTIVE (effect size -1.17). Sleeping in is more valuable than the workout.

The `get_coaching_snapshot()` tool now includes `sleep.training_modifications` with specific guidance.

### Load Management (Injury Prevention)

Based on ACWR (Acute:Chronic Workload Ratio) research:
- **0.8-1.3**: Sweet spot - safe to train normally
- **< 0.8**: Undertrained - safe to increase load
- **> 1.3**: Elevated risk - reduce intensity
- **> 1.5**: High risk - mandatory load reduction

The `get_fitness_status()` tool tracks this automatically and provides recommendations.

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

## Tool Hierarchy for Coaching Context

Understanding which tool to use and when prevents redundant data fetching and ensures efficient coaching.

### 1. `get_coaching_snapshot()` - MANDATORY First Call

**When:** Any coaching conversation, making recommendations, adjusting plans

**Returns:** Everything needed for coaching decisions in one call:
- Current weekly plan
- Actual activities (planned vs completed)
- Fitness metrics (CTL/ATL/TSB/ACWR)
- Compliance status
- Sleep analysis with training modifications
- Recovery status
- Sport priority breakdown
- Active injuries

**Use this as the ENTRY POINT for all coaching interactions.**

### 2. `get_planning_context()` - Full Context for Weekly Planning

**When:** Building a weekly plan from scratch, major plan revisions

**Returns:** Extended context including:
- Athlete profile (personal info, constraints, preferences)
- Event calendar with race analysis
- Methodology (pillars, safety rules)
- Current plan
- Fitness trajectory

**Use when you need the full picture, not just current state.**

### 3. `get_load_status()` - Quick Fitness Check

**When:** Need only load/ACWR status, no plan context needed

**Returns:** Just fitness metrics:
- CTL, ATL, TSB
- ACWR status and risk level
- Fitness trend

**Use for quick load checks without full coaching context.**

### 4. `get_compliance_report(days)` - Pillar Tracking Only

**When:** Checking if weekly pillars are met

**Returns:** Compliance against pillars:
- Strength sessions
- Mobility minutes
- Long efforts
- Volume

**Use for pillar-focused analysis.**

### Tool Selection Guide

| Question | Tool to Use |
|----------|-------------|
| "What should I train today?" | `get_coaching_snapshot()` |
| "Am I overtrained?" | `get_coaching_snapshot()` or `get_load_status()` |
| "Did I hit my pillars this week?" | `get_compliance_report()` |
| "Plan next week from scratch" | `get_planning_context()` |
| "How's my sleep affecting training?" | `get_coaching_snapshot()` |
| "What's my fitness trending?" | `get_load_status()` |

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

### Fitness Tracking Tools (Science-Based)
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_fitness_status(days)` | CTL, ATL, TSB, ACWR with trend analysis | JSON with metrics, insights |
| `refresh_fitness_history(days)` | Backfill fitness history from Garmin | JSON summary |
| `get_intensity_distribution(days)` | Zone distribution vs 80/20 target | JSON with polarization score |

**Key Metrics:**
- **CTL (Chronic Training Load)**: 42-day weighted fitness level
- **ATL (Acute Training Load)**: 7-day weighted fatigue level
- **TSB (Training Stress Balance)**: Form = CTL - ATL. Positive = fresh, negative = fatigued
- **ACWR (Acute:Chronic Workload Ratio)**: Injury risk. Sweet spot is 0.8-1.3

**First-time setup:** Run `refresh_fitness_history(365)` to backfill from Garmin history.

### Periodization Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_periodization_status()` | Current phase, weeks to race, phase guidance | JSON with position in season |
| `get_weekly_prescription()` | This week's targets based on phase + fitness | JSON with volume, intensity, constraints |
| `update_phase(phase, notes)` | Transition to new training phase | Confirmation |

**get_weekly_prescription() is the key tool for adaptive coaching.** It combines:
- Phase demands (from periodization)
- Current fitness (CTL, ACWR)
- Recovery status (Garmin readiness)
- Life constraints (injuries, travel, commitments)

The LLM uses this prescription as a starting point, then adapts through conversation.

### Planning Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `get_coaching_snapshot()` | **MANDATORY FIRST CALL** - complete coaching context | JSON with plan, actual, fitness, compliance |
| `get_planning_context()` | Full context for LLM planning | JSON object |
| `get_weekly_plan()` | Current 7-day plan | JSON object |
| `update_weekly_plan(plan_json)` | Save new/updated plan | Confirmation |
| `push_plan_to_garmin()` | Push workouts to Garmin calendar | JSON summary |

**get_coaching_snapshot() is the MANDATORY first call.** It returns:
- `weekly_plan`: What's currently planned
- `activities_this_week`: What's been done (actual)
- `planned_vs_actual`: Comparison with gaps/completion rate
- `fitness_metrics`: CTL, ATL, TSB, ACWR with coaching insights
- `compliance`: Pillar status (strength, mobility, long effort)
- `recovery`: Today's readiness score and recommendation
- `sport_priorities`: Multi-sport volume distribution (if multiple races)
- `active_injuries`: Current restrictions
- `coaching_checklist`: Quick status flags (has_plan, acwr_safe, compliance_ok)

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
| `research_exercise(exercise_name, save_to_library)` | Form cues, setup, mistakes | JSON + saves to library |
| `research_injury(...)` | See Injury Tools above | Treatment & recovery info |

**research_sport workflow:**
- Use when onboarding athlete in unfamiliar sport (climbing, CrossFit, rowing, martial arts)
- Returns: training approaches, periodization patterns, common injuries, physical demands
- Example: `research_sport("rock climbing")` - fetches Wikipedia and extracts training-relevant info

**research_exercise workflow:**
- Use when athlete is unsure how to perform an exercise
- Primary source: muscleandstrength.com (has video guides)
- Returns: setup instructions, key cues, common mistakes, modifications, **video_url**
- **Saves to exercise library** (default) for use in Garmin workout notes
- Example: `research_exercise("Romanian deadlift")` - returns form cues + video link

**Video URL protocol:**
- Each exercise includes a `video_url` field linking to muscleandstrength.com form guide
- The Garmin note includes a shortened video link for quick reference
- Example garmin_note: `"Squeeze shoulder blades, elbows 45deg. Video: muscleandstrength.com/exercises/dumbbell-bench-press"`

**Exercise library → Garmin workout notes:**
1. Call `research_exercise("hip thrust")` to learn the exercise
2. Form cues + video URL cached in `data/exercise_library.json`
3. When `push_plan_to_garmin()` builds strength workouts, it includes the notes with video links
4. You see form cues + video link on your Garmin watch/app during the workout

### Strength Sync Tools
| Tool | Purpose | Returns |
|------|---------|---------|
| `sync_strength_session(activity_id)` | Pull completed exercise data from Garmin and update baselines | JSON with synced exercises, PRs, progressions |
| `get_strength_baseline(exercise)` | View current strength baselines | JSON with weights, reps, history |
| `approve_progression(exercise)` | Approve suggested weight progression | Confirmation with new weight |
| `set_exercise_preference(exercise_group, preferred_variation)` | Set preferred exercise variation | Confirmation |

**Strength sync workflow:**
1. Athlete completes gym session on Garmin watch
2. `get_coaching_snapshot()` auto-syncs recent strength sessions
3. Baselines are updated with actual weights used
4. If target reps completed, progression suggestion generated (+2.5kg)
5. Athlete approves progression → next workout uses new weight
6. `push_plan_to_garmin()` uses baseline weights when building workouts

**Exercise equivalence groups:**
- Related exercises share progression (e.g., barbell bench = dumbbell bench)
- Groups: BENCH_PRESS, ROW, PULL_UP, SHOULDER_PRESS, CURL, TRICEPS_EXTENSION, SQUAT, DEADLIFT, LATERAL_RAISE, CORE
- Use `set_exercise_preference()` to set preferred variation in each group

**Example flow:**
```
User: "I just finished my gym session"
Coach: [calls get_coaching_snapshot()]
→ "Synced strength session. Bench press: 3x12 @ 10kg. You completed all target reps - I suggest progressing to 12.5kg next session."
User: "yes bump it up"
Coach: [calls approve_progression("bench_press")]
→ "Bench press progression approved. Next session: 3x12 @ 12.5kg"
```

## Architecture

```
coach-mcp/
├── server.py              # MCP server with all tools
├── garmin_client.py       # Garmin auth with token caching
├── workout_builder.py     # Converts plan sessions to Garmin workouts
├── fitness.py             # CTL/ATL/TSB calculations, intensity distribution
├── rules.py               # Compliance checker, safety rules
├── planner.py             # Context builder, plan/suggestion management
├── daily_loop.py          # Morning audit automation
├── config.py              # Shared configuration and constants
├── data/
│   ├── athlete.json           # WHO - personal info, life constraints, preferences
│   ├── athlete_baseline.json  # WHO - Garmin-derived capacity (auto-generated)
│   ├── methodology.json       # HOW - pillars, safety rules, race templates
│   ├── training_config.json   # WHAT - events, periodization, goals
│   ├── weekly_plan.json       # CURRENT - rolling 7-day plan with session PURPOSE
│   ├── fitness_history.json   # FITNESS - daily loads, CTL/ATL snapshots
│   ├── coaching_log.json      # MEMORY - coaching decisions, patterns, approvals
│   ├── exercise_library.json  # FORM - cached exercise form cues for Garmin notes
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
- `strength_baseline`: per-exercise weight/reps baselines (auto-synced from Garmin)
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
