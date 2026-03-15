# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> **Note:** Coaching identity is embedded in the MCP server itself via `SERVER_INSTRUCTIONS` in `mcp_app.py`. Any MCP client receives coaching identity at connection time. This file supplements with development context for Claude Code sessions.

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

## Curiosity Protocol

**When data looks unusual, ASK before concluding.** The snapshot surfaces anomalies — your job is to be curious about them, not auto-resolve them.

- **Type mismatch** (plan says "race", actual is "cycling"): "That doesn't look like a race — what happened?"
- **Duration >50% off** from plan: "You cut that short — by choice or circumstance?"
- **Activity on rest day**: "You did X on a rest day — feeling good or restless?"
- **Missing session** on training day: "Missed session — skip or life got in the way?"
- **Unusually high/low HR** for the activity type: "HR was X during Y — are you feeling OK?"
- **Event type is 'race'** but no race was planned: "Garmin tagged this as a race — was it?"

The snapshot flags these automatically as anomalies in the planned-vs-actual comparison. **Never silently resolve an anomaly** — always check with the athlete first. A coach who assumes is worse than one who asks.

## Science-Based Coaching Model

The coach operates at multiple timeframes:

```
SEASON (months)           <- Where are we going?
├── A-race target, phase sequence, fitness trajectory

BLOCK (4-8 weeks)         <- What phase are we in?
├── Phase: base/build/peak/taper
├── Intensity distribution targets (80/20)

WEEK                      <- What should this week look like?
├── Volume target (adjusted for ACWR)
├── Key sessions to prioritize

DAY                       <- What should today look like?
├── Adapt based on readiness + conversation
```

**Key Principle: The conversation IS the coaching.** Tools provide data and structure. The LLM provides intelligence and adaptation.

### MANDATORY Coaching Sequence

**CRITICAL: Before making ANY coaching recommendations, call `get_coaching_snapshot()` first.**

This returns: current plan, actual activities, planned vs actual comparison (with anomalies), fitness metrics (CTL/ATL/TSB/ACWR as structured data), compliance, recovery, sleep, adaptation patterns, sport priorities, active injuries, data quality flags.

Check `data_quality` in the snapshot — it flags missing critical data (weight, age, name), unavailable recovery/sleep, and stale fitness history. The LLM should surface these to the athlete and recommend running `refresh_athlete_baseline()` to auto-populate from Garmin.

### Load Hierarchy (Injury Prevention)

**Check these three levels IN ORDER before prescribing any training:**

1. **OVERALL ACWR** (total body injury gate) — if > 1.3, back off EVERYTHING
2. **SPORT-SPECIFIC ACWR** (spike detection) — catches "hasn't run in 4 weeks, now wants to"
3. **SPORT-SPECIFIC CTL** (race readiness) — build toward target WITHOUT violating levels 1 or 2

The snapshot includes `fitness_metrics.acwr_status` (structured: `{value, zone, safe}`) and `load_hierarchy` with these checks pre-computed.

### Multi-Sport Handling

When an athlete has races in multiple sports:

1. **Sport Priority Analysis**: Snapshot calculates volume distribution weighted by race priority x time proximity
2. **Shared Sessions**: Strength, mobility, recovery benefit ALL sports — schedule regardless of sport focus
3. **Sport-Specific Sessions**: Key sessions from race templates, prioritized by sport priority
4. **Volume Constraint**: Total weekly load must respect overall ACWR — don't spike total body stress chasing sport-specific CTL

### Sleep as Foundation (Training Gate)

Sleep is a GATE for training decisions, not just a metric. Without adequate sleep, training becomes catabolic:
- High-intensity intervals most affected (effect size -1.57)
- Strength/power least affected (effect size -0.39)
- Early AM workouts that cut into sleep are COUNTERPRODUCTIVE (effect size -1.17)

The snapshot includes sleep data with `avg_hours`, `scores`, and `deficit_flag`. Use these to decide what training is appropriate — the LLM reasons about the athlete's specific context rather than following fixed thresholds.

### Personalizing Load Decisions

The `volume_data.load_increase_pcts` provides a range: [10, 15, 25] (conservative, standard, aggressive).

Choose where in the range based on `adaptation_patterns`, `sleep.trend_direction`, `recovery.hrv_trend`, and `compliance.compliance_rate_pct`:
- **Red flags** (sleep < 6.5hr, HRV declining, compliance < 60%) -> Conservative
- **Green signals** (sleep > 7.5hr improving, compliance > 85%, HRV improving) -> Aggressive
- **Mixed/unknown** -> Standard

Always record reasoning with `log_coaching_decision()`.

### Adaptation Patterns

Check `adaptation_patterns` before load decisions. These are learned from `record_athlete_response()` calls:
- `handles_volume_well` -> more aggressive on volume
- `recovers_quickly` -> shorter rest between hard sessions
- `needs_extra_rest_after_intensity` -> add recovery day after intervals

New athlete with empty patterns? Start conservative. Log responses after every week.

### Coaching Score

Use `get_coaching_score()` periodically to evaluate effectiveness:
- Progress (40%): CTL trajectory toward A-race goal
- Health (30%): Injuries, ACWR status
- Achievability (20%): Compliance rate
- Adaptation (10%): Response patterns logged

### Load Management (ACWR Reference)

Based on ACWR (Acute:Chronic Workload Ratio) research:
- **0.8-1.3**: Sweet spot - safe to train normally
- **< 0.8**: Undertrained - safe to increase load
- **> 1.3**: Elevated risk - reduce intensity
- **> 1.5**: High risk - mandatory load reduction

The snapshot provides `fitness_metrics.acwr_status` with `{value, zone, safe}` — the zone labels map to these research-backed thresholds.

### Coaching Continuity

Coaching decisions persist across sessions via `coaching_log.json`:
1. **Session start**: Call `get_active_decisions()` to load previous decisions
2. **During planning**: Previous decisions should influence recommendations
3. **Significant choices**: Call `log_coaching_decision()` to persist rationale
4. **Major changes** (phase transition, >15% volume change): Use `propose_major_change()` for user approval
5. **After completed sessions**: Call `record_athlete_response()` to track patterns

### Tool Selection Quick Reference

| Question | Tool |
|----------|------|
| Any coaching recommendation | `get_coaching_snapshot()` (MANDATORY first) |
| Plan next week from scratch | `get_planning_context()` |
| Quick load/ACWR check | `get_load_status()` |
| Pillar compliance | `get_compliance_report()` |
| Coaching self-assessment | `get_coaching_score()` |
| Push harder or back off? | Check `adaptation_patterns` in snapshot |

### Adaptive Coaching Flow

1. **Start of conversation**: Call `get_coaching_snapshot()` FIRST
2. **Analyze**: Current state, what's working, what's missing, any anomalies
3. **Plan**: Build/adjust based on snapshot + athlete conversation
4. **Adapt**: When things change, check snapshot again
5. **End of week**: Check compliance, update fitness, plan next week

## Commands

```bash
python server.py                              # Run the MCP server
python -m pytest -v                           # Run all tests
python -m pytest tests/test_rules.py -v       # Run specific module tests
python scripts/daily_loop.py                  # Morning audit (standalone)
python scripts/daily_loop.py --llm            # Morning audit with LLM
```

## Architecture

```
coach-mcp/
├── server.py              # MCP server orchestrator (imports tool modules)
├── mcp_app.py             # Shared FastMCP instance
├── garmin_client.py       # Garmin auth with token caching + retry
├── workout_builder.py     # Converts plan sessions to Garmin workouts
├── fitness.py             # CTL/ATL/TSB calculations, intensity distribution
├── rules.py               # Compliance checker, safety rules, classify_activity
├── planner.py             # Context builder, plan/suggestion management
├── parsers.py             # Pure parsing functions for Garmin API responses
├── config.py              # Shared configuration and constants
├── scripts/
│   ├── daily_loop.py      # Morning audit automation
│   ├── fetch_exercises.py # Fetch exercise DB from Garmin
│   └── setup_wizard.py   # First-run setup wizard
├── tools/
│   ├── data_tools.py      # get_daily_metrics, get_activities_range, get_personal_records
│   ├── fitness_tools.py   # refresh_athlete_baseline (+ Garmin profile pull), get_training_readiness, etc.
│   ├── athlete_tools.py   # update_athlete, set_threshold_pace, set_ftp, etc.
│   ├── planning_tools.py  # get_planning_context, get_weekly_plan, push_plan_to_garmin, get_week_constraints, etc.
│   ├── coaching_tools.py  # get_coaching_snapshot, get_compliance_report, get_coaching_score (+ 11 helpers)
│   ├── strength_tools.py  # sync_strength_session, generate_strength_workout, etc.
│   ├── injury_tools.py    # diagnose_injury, research_injury, update_injury_status
│   ├── research_tools.py  # research_exercise, list_exercises, research_sport
│   ├── decision_tools.py  # log_coaching_decision, record_athlete_response, etc.
│   ├── race_tools.py      # research_race, list/add/remove/update_race
│   ├── suggestion_tools.py # propose/list/approve/reject_suggestion
│   └── goal_tools.py      # get_goal_progress
├── data/
│   ├── athlete.json           # WHO - personal info, constraints, preferences, pillars
│   ├── athlete_baseline.json  # WHO - Garmin-derived capacity (auto-generated)
│   ├── methodology.json       # HOW - safety rules, race templates, personas
│   ├── training_config.json   # WHAT - events, periodization, goals
│   ├── weekly_plan.json       # CURRENT - rolling 7-day plan with session PURPOSE
│   ├── fitness_history.json   # FITNESS - daily loads, CTL/ATL snapshots, sleep history
│   ├── coaching_log.json      # MEMORY - decisions, patterns, approvals
│   ├── exercise_library.json  # FORM - cached exercise form cues for Garmin notes
│   └── suggestions.json       # Pending LLM suggestions
└── tests/                     # pytest suite (see pyproject.toml for config)
```

## Activity Classification

`rules.py:classify_activity()` categorizes activities and returns:
- `is_strength`, `is_mobility`, `is_long_effort`, `is_hard` (booleans)
- `hr_intensity_pct` (float 0.0-1.0) — avg_hr / athlete_max_hr

When `athlete_max_hr` is provided, `is_hard` uses relative threshold (>78% of max HR).
Without it, falls back to absolute thresholds from config. This ensures the safety gate
(`check_safety_rules()` consecutive hard day check) works regardless of athlete profile availability.

## Garmin Profile Auto-Population

`refresh_athlete_baseline()` now pulls athlete profile data from Garmin:
- **Name** from `get_full_name()`
- **Weight** from `get_body_composition()` (Garmin stores in grams, converted to kg)
- **Birth date + age** from `get_user_profile()`

This data is saved under the `garmin_profile` key in `athlete_baseline.json` (separate from manual `athlete.json`). On each refresh, `None` fields in `athlete.json` personal section are auto-populated from Garmin data. Manually set values are **never** overwritten.

## Data Files

| File | Purpose | Managed By |
|------|---------|------------|
| `athlete.json` | Personal info, pillars, constraints, strength baselines | Manual + tools (auto-populated from Garmin) |
| `athlete_baseline.json` | Garmin-derived capacity + profile (auto-generated) | `refresh_athlete_baseline()` |
| `methodology.json` | Safety rules, race templates, personas | Rarely changes |
| `training_config.json` | Events, periodization, goals | Manual + tools |
| `weekly_plan.json` | Rolling 7-day plan with session PURPOSE | `update_weekly_plan()` |
| `fitness_history.json` | Daily loads, CTL/ATL snapshots, sleep history | Auto-updated by snapshot |
| `coaching_log.json` | Decisions, patterns, approvals | Coaching decision tools |
| `exercise_library.json` | Cached exercise form cues for Garmin notes | `research_exercise()` |

## Environment

Requires `.env` with `GARMIN_EMAIL`, `GARMIN_PASSWORD`, and `ANTHROPIC_API_KEY`.

## Testing

544 tests across 15 test files. Tests use real API responses captured in `test_fixtures.json` (gitignored).

Pattern for new tools:
1. Create parsing function (pure, no I/O) in `parsers.py`
2. Add MCP tool with `@mcp.tool()` decorator in `tools/`
3. Write tests with sample data matching Garmin structure
4. Run: `python -m pytest -v`

Key testing patterns:
- Patch `garmin_api_call` where it's **used** (the tool module), not where it's defined
- Redirect `DATA_DIR` via `monkeypatch.setattr(planner, 'DATA_DIR', data_dir)` for file I/O tests
- Clean install tests must monkeypatch DATA_DIR in **all** modules that use it

## When to Suggest New Tools

The coach should proactively identify gaps. Use `propose_suggestion(type='new_tool', ...)` when:
- Repeated manual data gathering that could be automated
- Missing context preventing good coaching decisions
- Same type of request comes up multiple times

Tool design: single responsibility, return JSON, fail gracefully with `{'error': ...}`.

## Reliability & Safety

- **Logging**: All 17 modules use `logging.getLogger(__name__)`. Every tool `except` block calls `logger.exception()` before returning JSON errors. Server-side tracebacks are preserved while clients get clean error messages.
- **Atomic writes**: `save_json_file()` and `save_fitness_history()` write to `.tmp` then `Path.replace()` — a crash mid-write can't corrupt data files.
- **Input validation**: `get_activities_range()` validates date format before API calls. `update_weekly_plan()` validates plan structure (must be dict with `days` dict). `research_injury()` rejects invalid severity.
- **No bare except**: All `except:` blocks use `except Exception:` to avoid swallowing `KeyboardInterrupt`/`SystemExit`.

## Known Issues / TODO

### ~~2. Coaching snapshot shows partial data when Garmin API fails~~ FIXED
- `data_quality` dict in snapshot now explicitly flags: missing weight/age/name, unavailable recovery/sleep, stale fitness_history
- All silent fallbacks now log warnings with `exc_info=True` for server-side debugging

### 3. Rehab sessions not pushed to Garmin calendar
- Rehab sessions skipped with "unknown workout type" when pushing to Garmin
- Need to add rehab as supported workout type or bundle into strength sessions

### ~~4. Coach doesn't flag missed sessions~~ FIXED
- `_compare_planned_actual()` now surfaces anomalies (missing, type_mismatch, duration_delta, unplanned)
- Curiosity Protocol in CLAUDE.md guides the coach to ASK about anomalies rather than auto-resolve
