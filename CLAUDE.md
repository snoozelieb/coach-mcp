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

**The first key of the snapshot is `current_time_context`** — date, day_of_week, hour, minute, time_period (early_morning/morning/afternoon/evening/night), is_weekend. Verify it BEFORE any advice: morning fueling differs from evening recovery; "do today's session" is wrong if today is already done. The lightweight `coach://context/now` resource exposes the same data if you only need a time check. Server local time is assumed to equal athlete local time (stdio transport on the athlete's machine).

**`week_grid` is the rest-day-visible 7-day view.** Rolling 7-day window ending today, each day keyed by ISO date with `day_of_week`, `activity_count`, `types_summary` (`"cycling+strength"` or `"REST"`), `total_duration_mins`, `total_load`, `is_rest`, `is_today`. Scan this before any weekly-pattern comment; aggregate metrics hide zero-activity days.

**VERIFY BEFORE CONFIRMING athlete claims.** If the athlete says "I did X today", check `week_grid[today]` before responding. If `is_rest: true` or types don't match, ask, don't assume.

**`plan_adherence`** is per-pillar: `{strength, mobility, long_effort}` each with `planned`, `completed`, `skipped_dates`, `pending_dates`, `deficit`. This gives "planned 5 strength, completed 3, skipped Monday + Wednesday" at a glance without a separate tool call.

**`recovery.hrv_*`** is populated from the dedicated `/hrv-service/hrv` endpoint (Garmin's training_readiness often returns null for hrv_status). Fields: `hrv_status`, `hrv_last_night_avg`, `hrv_weekly_avg`, `hrv_baseline_low`, `hrv_baseline_high`, `hrv_feedback`.

**`sleep.nights`** is the last 7 nights with full per-night detail: `bedtime`, `wake_time`, `duration_hrs`, `score`, `deep_mins`/`deep_pct`, `rem_mins`/`rem_pct`, `light_mins`/`light_pct`, `awake_mins`, `avg_hr`, `respiration`, `sleep_stress`. `sleep.bedtime_drift` (over 14-day window) flags "later"/"earlier"/"stable" direction + `drift_mins_per_wk` — a meaningful overtraining signal when bedtime drifts >15 min/wk later.

The snapshot also returns: current plan, actual activities, planned vs actual comparison (with anomalies), fitness metrics (CTL/ATL/TSB/ACWR as structured data), compliance, recovery, sleep, adaptation patterns, sport priorities, active injuries, data quality flags.

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
4. **Changes needing approval** (phase transition, >15% volume change, pillar tweak): Use `propose_coaching_action(action_type, proposal, rationale, impact='major')` — athlete must `approve_proposal` or `reject_proposal`
5. **After completed sessions**: Call `record_athlete_response()` to track patterns

### Tool Selection Quick Reference

| Question | Tool |
|----------|------|
| Any coaching recommendation | `get_coaching_snapshot()` (MANDATORY first) |
| Plan next week from scratch | `get_coaching_snapshot()` + `get_week_constraints()` + `get_weekly_prescription()` |
| Custom-window ACWR check | `get_fitness_status(days=N)` (snapshot already has current) |
| Pillar compliance | `get_compliance_report()` or `snapshot.compliance` |
| Coaching self-assessment | `get_coaching_score()` |
| Push harder or back off? | Check `adaptation_patterns` in snapshot |

### Canonical Coaching Flow

```
1. START OF EVERY CONVERSATION
   → get_coaching_snapshot()
   → Check current_time_context (date, day, time_period)
   → INJURY GATE: scan snapshot.injuries — any status in {active, improving}
       with restricted_activities MUST be honoured. Hard gate: never prescribe
       a restricted activity regardless of ACWR, readiness, or plan.
   → Scan flags.active_injuries (quick count) + acwr_warnings
   → Scan week_grid (rest days explicit)
   → Scan planned_vs_actual.anomalies + plan_adherence.skipped_dates

2. ATHLETE CLAIM VERIFICATION
   → "I did X today" → check week_grid[today] BEFORE confirming
   → If is_rest=true or types mismatch → ask, don't assume

3. PLAN BUILDING
   → get_coaching_snapshot() (already done at step 1)
   → get_week_constraints() — guardrails (includes injury restrictions)
   → get_weekly_prescription() — volume + intensity targets
   → Build plan — every session must respect snapshot.injuries[*].restricted_activities
   → update_weekly_plan() → push_plan_to_garmin()
   → Both tools CODE-ENFORCE the injury gate: sessions matching an active/improving
     injury's restricted_activities are rejected (override_injury_gate=True bypasses
     with logged rationale — use only with athlete consent)

4. DRILL-DOWNS (when snapshot data isn't enough)
   → get_fitness_status(days=N) — custom-window CTL/ACWR
   → get_intensity_distribution(days=N) — zone analysis
   → get_activities_range(start, end) — specific period detail
   → get_training_readiness(for_date) — historical readiness (with HRV overlay)

5. MUTATIONS
   → Coaching memory: log_coaching_decision, record_athlete_response
   → Approvals: propose_coaching_action → approve_proposal / reject_proposal
   → Athlete profile: update_athlete, set_ftp, set_threshold_pace
   → Plan: update_weekly_plan, push_plan_to_garmin, update_phase
```

### Removed tools

The following tools were removed in the Phase 2 rationalization — their data is in
`get_coaching_snapshot()`:

- `get_planning_context` → `get_coaching_snapshot` (strict superset)
- `get_goal_progress` → `snapshot.goal_progress`
- `list_pending_suggestions` → `snapshot.coaching_memory.pending_approvals`
- `get_load_status` → `snapshot.fitness_metrics.acwr_status` or `get_fitness_status(days=N)` (the standalone used different math and disagreed with the snapshot)

`get_compliance_report` stays — minimal 1-API-call check when you don't need the full
snapshot; math matches `snapshot.compliance`.

## Commands

```bash
python server.py                              # Run the MCP server (stdio)
COACH_TRANSPORT=http python server.py         # Run with HTTP transport
COACH_CODE_MODE=1 python server.py            # Run with Code Mode (search/execute)
python -m pytest -v                           # Run all tests
python -m pytest tests/test_rules.py -v       # Run specific module tests
python scripts/daily_loop.py                  # Morning audit (standalone; scheduled daily 06:30 via Task Scheduler "CoachMCP Morning Audit")
python scripts/daily_loop.py --llm            # Morning audit with LLM (model via COACH_LLM_MODEL, default claude-sonnet-4-6)
python scripts/garmin_login.py                # Manual Garmin token recovery (garminconnect native auth + MFA)
```

## MCP Framework

Uses **standalone FastMCP v3.2.x** (`from fastmcp import FastMCP`), not the bundled v1 in the `mcp` SDK.

> **2KB limit:** Claude Code truncates MCP server instructions at 2KB. `SERVER_INSTRUCTIONS` is kept
> under 1,900 chars (test-enforced); the long-form doctrine lives in the `coach://coaching/doctrine` resource.

| Feature | Status | Details |
|---------|--------|---------|
| **Tools** | 54 tools | `@mcp.tool()` — sync and async |
| **Prompts** | 5 prompts | `coach/prompts.py` — weekly_planning, morning_brief, injury_assessment, week_review, onboarding |
| **Resources** | 6 resources | `coach/resources.py` — coach://athlete/profile, coach://plan/current, coach://config/training, coach://coaching/decisions, coach://context/now, coach://coaching/doctrine |
| **Context** | 2 async tools | `get_coaching_snapshot`, `refresh_athlete_baseline` use `ctx: Context` for progress reporting |
| **Sampling** | (removed) | `ctx.sample()` unsupported by Claude Code — `generate_smart_brief` now returns structured data for the coach to render |
| **Elicitation** | (removed) | Claude Code NOW supports elicitation (it didn't when this was removed) — re-adding `ctx.elicit()` flows is planned for Phase 2 of docs/UPGRADE_ROADMAP.md; `interactive_check_in` currently returns a question set |
| **Code Mode** | Optional | `COACH_CODE_MODE=1` — replaces tools with search/schema/execute meta-tools |
| **Transport** | stdio (default) | `COACH_TRANSPORT=http|sse|streamable-http` for remote deployment |

## Architecture

```
coach-mcp/
├── server.py                # Entry point (imports coach package, transport/code-mode config)
├── coach/                   # Core package
│   ├── __init__.py
│   ├── config.py            # Shared configuration and constants
│   ├── fitness.py           # CTL/ATL/TSB calculations, intensity distribution
│   ├── garmin_client.py     # Garmin auth with token caching + retry
│   ├── mcp_app.py           # Shared FastMCP instance (from fastmcp import FastMCP)
│   ├── parsers.py           # Pure parsing functions for Garmin API responses
│   ├── planner.py           # Context builder, plan/suggestion management
│   ├── prompts.py           # MCP prompt templates (5 coaching workflows)
│   ├── resources.py         # MCP resources (5 read-only data endpoints)
│   ├── rules.py             # Compliance checker, safety rules, classify_activity
│   ├── web_utils.py         # HTML stripping + page fetching
│   ├── workout_builder.py   # Converts plan sessions to Garmin workouts
│   └── tools/               # 54 MCP tools across 11 modules
│       ├── data_tools.py      (3) # get_daily_metrics, get_activities_range, get_personal_records
│       ├── fitness_tools.py   (7) # refresh_athlete_baseline, get_training_readiness (HRV overlay), get_fitness_status, refresh_fitness_history, get_intensity_distribution, get_onboarding_guide, get_athlete
│       ├── athlete_tools.py   (6) # update_athlete, set_threshold_pace, set_ftp, analyze_ftp_test, get_methodology, update_methodology
│       ├── planning_tools.py  (7) # get_periodization_status, get_weekly_prescription, update_phase, get_weekly_plan, update_weekly_plan, push_plan_to_garmin, get_week_constraints
│       ├── coaching_tools.py  (3) # get_coaching_snapshot (canonical), get_compliance_report, get_coaching_score  (+ 13 pure helpers)
│       ├── strength_tools.py  (6) # sync_strength_session, get_strength_baseline, approve_progression, set_exercise_preference, generate_strength_workout, add_exercise
│       ├── injury_tools.py    (3) # diagnose_injury, research_injury, update_injury_status
│       ├── research_tools.py  (3) # research_exercise, list_exercises, research_sport
│       ├── decision_tools.py  (9) # log_coaching_decision, get_active_decisions, update_decision_status, propose_coaching_action, list_pending_approvals, approve_proposal, reject_proposal, record_athlete_response, get_response_patterns
│       ├── race_tools.py      (5) # research_race, list/add/remove/update_race
│       └── interactive_tools.py (2) # generate_smart_brief (data shape for brief), interactive_check_in (question set)
├── scripts/
│   ├── daily_loop.py        # Morning audit automation (async, --llm for Anthropic API)
│   ├── fetch_exercises.py   # Fetch exercise DB from Garmin
│   └── setup_wizard.py     # First-run setup wizard
├── data/
│   ├── athlete.json           # WHO - personal info, constraints, preferences, pillars
│   ├── athlete_baseline.json  # WHO - Garmin-derived capacity (auto-generated)
│   ├── methodology.json       # HOW - safety rules, race templates, personas
│   ├── training_config.json   # WHAT - events, periodization, goals
│   ├── weekly_plan.json       # CURRENT - rolling 7-day plan with session PURPOSE
│   ├── fitness_history.json   # FITNESS - daily loads, CTL/ATL snapshots, sleep history
│   ├── coaching_log.json      # MEMORY - decisions, patterns, approvals
│   ├── exercise_library.json  # FORM - cached exercise form cues for Garmin notes
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

Optional environment variables:
- `COACH_TRANSPORT` — MCP transport: `stdio` (default), `http`, `streamable-http`, `sse`
- `COACH_CODE_MODE` — Set to `1` to enable Code Mode transform
- `FASTMCP_HOST` / `FASTMCP_PORT` — HTTP bind address (default: 127.0.0.1:5000)

### Garmin Authentication

Native garminconnect **0.3.5** auth (vendored DI-token client, curl_cffi TLS impersonation
for Cloudflare). No garth, no Playwright, no browser — the old fallback stack is deleted.

1. **Normal flow** (token-first): `coach/garmin_client.py` restores the saved session from
   `.garth/garmin_tokens.json` via a credential-less `Garmin().login(tokenstore=...)` — silent
2. **Token failure**: ONE non-interactive credential login (`return_on_mfa=True`). If Garmin
   asks for MFA, the server cannot answer headlessly — it never blocks on input
3. **Auth failure**: tools raise `GarminAuthRequiredError`, whose `str()` is exactly
   `AUTH_REQUIRED: Garmin session expired or needs MFA. Run: python scripts/garmin_login.py`
   so every tool's `{'error': str(e)}` is actionable. A process-level latch then makes
   subsequent `get_garmin_client()` calls fail fast for ~10 minutes (one expired session
   never triggers N login attempts during a snapshot)
4. **Recovery**: `python scripts/garmin_login.py` — interactive credential login + MFA prompt,
   persists tokens to the same tokenstore. Restart the MCP server afterwards (clears the latch
   and the cached dead client)

`tests/test_garmin_contract.py` pins every garminconnect method/attribute/call-shape the
codebase uses, so a dependency rename (like the 0.2→0.3 `.garth` → `.client` break) fails CI
loudly instead of failing silently in production.

## Testing

718 tests across 21 test files (CI: GitHub Actions on push/PR + weekly cron to catch date-rot tests).
Some tests use real API responses captured in `test_fixtures.json` (gitignored; those tests skip when
it's absent, so clean checkouts stay green). Async tools tested with `pytest-asyncio` (auto mode) and
`mock_ctx` fixture from `conftest.py`. Use relative dates (`date.today() + timedelta(...)`) in tests —
hardcoded dates rot.

Pattern for new tools:
1. Create parsing function (pure, no I/O) in `coach/parsers.py`
2. Add MCP tool with `@mcp.tool()` decorator in `coach/tools/`
3. Write tests with sample data matching Garmin structure
4. Run: `python -m pytest -v`

Key testing patterns:
- Patch `garmin_api_call` where it's **used**: `@patch('coach.tools.X.garmin_api_call')`
- Redirect `DATA_DIR` via `monkeypatch.setattr(planner, 'DATA_DIR', data_dir)` for file I/O tests
- Import modules as: `import coach.planner as planner` (preserves monkeypatch target)
- Clean install tests must monkeypatch DATA_DIR in **all** modules that use it

## When to Suggest New Tools

The coach should proactively identify gaps. Use `propose_coaching_action(action_type='new_tool', ...)` when:
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

### ~~5. Sleep score always N/A on `get_daily_metrics`~~ FIXED
- `get_user_summary()` does NOT contain `sleepScore`; the real value lives in `get_training_readiness()`
- `get_daily_metrics()` now calls `get_training_readiness()` and extracts sleep_score via `parse_training_readiness()` (falls back to `'N/A'` only if readiness is empty)
- Same fix was applied to the former `get_planning_context()` (removed in Phase 2) — that tool had the same buggy overwrite

### ~~7. HRV always null in recovery data~~ FIXED
- Garmin's `get_training_readiness()` often returns `hrv_status: null` even on devices that track HRV
- The dedicated `/hrv-service/hrv/{date}` endpoint (via `c.get_hrv_data()`) carries the real data
- `parse_training_readiness()` and `_parse_readiness_for_snapshot()` now accept an `hrv_data` kwarg and overlay its fields; snapshot + `get_training_readiness` tool both call `get_hrv_data()` as a fallback
- New fields: `hrv_last_night_avg`, `hrv_weekly_avg`, `hrv_baseline_low/high`, `hrv_feedback`

### ~~8. No bedtime/wake time or full sleep breakdown~~ FIXED
- `get_sleep_summary()` now extracts `bedtime`/`wake_time` (ISO8601 local), `light_mins`/`light_pct`, `deep_mins`, `rem_mins` per night
- Snapshot's `sleep.nights` carries last 7 full-detail nights; `persist_sleep_data()` stores them to fitness_history
- `detect_bedtime_drift()` in fitness.py flags "bedtime drifting later >15min/wk" via `sleep.bedtime_drift`

### ~~9. Rest days invisible in aggregate metrics~~ FIXED
- Aggregates (CTL, ACWR, compliance totals) hide zero-activity days
- `week_grid` top-level snapshot key: 7-day rolling window with explicit `REST` marker per day
- Each day: `day_of_week`, `activity_count`, `types`, `types_summary`, `total_duration_mins`, `total_load`, `is_rest`, `is_today`

### ~~10. No at-a-glance plan adherence by pillar~~ FIXED
- `_summarize_plan_adherence_by_pillar()` joins plan days with actual activities, returns per-pillar `{planned, completed, skipped_dates, pending_dates, deficit}`
- Surfaced as top-level `plan_adherence` in the snapshot

### ~~11. Coach accepted "I did X today" without verifying against Garmin~~ FIXED
- `SERVER_INSTRUCTIONS` now mandates checking `week_grid[today]` before confirming activity claims
- If `is_rest: true` or types don't match, the coach asks before assuming

### ~~6. Coach gives advice without grounding it in current time/day~~ FIXED
- `build_current_time_context()` helper in `coach/parsers.py` returns date, day_of_week, hour, minute, time_period, is_weekend
- `get_coaching_snapshot()` now places `current_time_context` as the FIRST key in the returned JSON
- `SERVER_INSTRUCTIONS` mandates verifying `current_time_context` before any recommendation
- New `coach://context/now` resource exposes the same data for lightweight checks
- Prompts and interactive tools (`generate_smart_brief`, `interactive_check_in`) also thread time_period through to the LLM
