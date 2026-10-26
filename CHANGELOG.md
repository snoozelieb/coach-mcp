# Changelog

All notable changes to coach-mcp are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- **Distance-based workout steps reached the watch as lap-button steps**:
  `END_DISTANCE` was sent with `conditionTypeId` 1, which is Garmin's
  lap-button id (distance is 3). Garmin stored every distance step that way
  and dropped the distance, so every structured swim and every run step
  given in `distance_m` (e.g. 6×800m) waited for a lap press instead of
  ending at the distance. Confirmed with a live upload round-trip before
  and after the fix. Tests now pin the end-condition ids and require them
  to be unique, a check that would have caught this.
- **Pillar compliance diagnostics never saw a single activity.** The week
  builder filtered persisted activities on `act['date']`, but they have no
  date of their own (the day is the `daily_loads` key). So every pillar was
  scored a chronic miss in both the snapshot and `get_week_constraints`,
  which pushed the planner to add work the athlete was already doing.
  - Weeks are now the last four *completed* weeks, rebuilt with
    `_activities_from_history`.
  - Pillar types match through the taxonomy: `padel` counts Garmin
    `paddelball`, and `running` counts trail and treadmill runs.
  - The live `athlete.json` target keys are scored: session minimums,
    km/week and km ranges.
  - Targets that can't be scored from activities (e.g. `daily`) report
    `not_tracked` instead of a fake miss.
- **Mobility sessions Garmin logs as type `other`** (e.g. "Cape Town
  Mobility") now count toward weekly mobility compliance, matched by name.
  Activity names are now persisted in `daily_loads` so pillars can be scored
  offline.
- **Personal records all parsed as `unknown`.** Real Garmin records leave
  `prTypeLabelKey` empty and carry only a `typeId`. They are now labelled
  from Garmin's PR-type catalog (`parsers.PR_TYPES`), with a sport and a unit
  (seconds, metres or watts, checked against the linked activities).
- **Every activity type from a real 10-year Garmin history is now known to
  the taxonomy.** This includes Garmin's renamed `_v2` keys: `rowing_v2`,
  `tennis_v2`, `kayaking_v2`, `stand_up_paddleboarding_v2` and `surfing_v2`.
  A planned "rowing" session now matches `rowing_v2` instead of raising a
  false type-mismatch anomaly.
- `PADEL_SPORT` paired the racket-sports category id (219) with padel's key.
  It is now padel's own id (226). Behaviour is unchanged: Garmin's workout
  service has no padel sport, so padel workouts are stored without one (see
  CLAUDE.md Known Issues).
- **History retention no longer deletes the training diary**: the fixed
  rolling prunes (sleep 30 days, readiness 60 days, CTL/ACWR snapshots
  90 days) silently erased the athlete's history — after a year of coaching
  only 14 sleep nights and 4 readiness entries survived. Retention is now
  config-driven (`SLEEP_HISTORY_RETENTION_DAYS` /
  `READINESS_HISTORY_RETENTION_DAYS` / `SNAPSHOT_RETENTION_DAYS`) and
  defaults to keeping everything.
- The snapshot read `athlete.json` directly, bypassing the
  `athlete_baseline.json` merge that `load_athlete()` performs, so
  Garmin-derived capacity never reached compliance scoring. It now uses
  `load_athlete()`.

### Added
- **Capacity grounding — the fourth hard mandate, VERIFY BEFORE PRESCRIBING.**
  Aggregate load (CTL/ACWR) says how much load is safe, not what session
  durations the athlete has actually demonstrated, so every prescription now
  anchors to completed training rather than a template:
  - `snapshot.capacity` (core payload, no Garmin call) — per-sport
    demonstrated capacity derived from persisted `daily_loads`: longest
    session (28 days + full window), typical session, sessions/week,
    weekly-minutes trend and the last session with distance/pace
    (`fitness.capacity_by_sport` / `derive_sport_capacity`; buckets from
    `taxonomy.capacity_bucket_for` — cycling, running, strength, swimming;
    games and mobility are never duration-progressed).
  - `update_weekly_plan` returns advisory `grounding_warnings`
    (`capacity_regression`, `session_spike`, `no_demonstrated_history`)
    comparing the plan's longest session per sport with demonstrated
    capacity. The save succeeds; every warning must be addressed with the
    athlete before pushing.
  - `get_week_constraints` and `get_weekly_prescription` carry
    `recent_capacity` beside their generic template ranges;
    `refresh_athlete_baseline` stores per-sport `by_sport` capacity.
  - `query_metrics(kind='sport_history', sport=..., days=...)` — the
    per-session list (durations, distances, paces) and capacity behind
    those numbers, from local history. `get_activities_range` gained a
    taxonomy-aware `activity_type` filter. Persisted activity details now
    keep `distance_km`, `avg_pace_min_km` and `name` so both work offline.
  - `SERVER_INSTRUCTIONS` states the mandate; the doctrine resource gained
    "Grounding Prescriptions" and "Multi-Week Programs" sections.
- **`training_program` MCP tool** (50th tool) — the multi-week macro plan
  the rolling weekly plan executes, stored in `training_program.json`
  (`action='get'|'set'`; prior programs are archived in the file's history).
  A program is validated against a typed schema (`schemas.TrainingProgram`)
  and **gated in code**: the required `capacity_anchor` must agree within
  25% with the server-derived demonstrated longest session
  (`capacity_anchor_mismatch`); a week 1 below 60% of demonstrated capacity
  must state `rationale_if_below` (`capacity_anchor`); the sport must be
  duration-prescribed; the injury gate runs on the program sport
  (`override_injury_gate` mirrors `update_weekly_plan`); start dates are
  sanity-checked; week-over-week jumps above 25% come back as
  `progression_rate` warnings. The snapshot carries a `training_program`
  pointer (name, sport, status, current week) whenever a program exists.
- `race_program` prompt (6th prompt) — capacity-anchored multi-week program
  builder that ends in `training_program(action='set')`.
- Methodology: a `road_running` race template (5k to marathon) whose session
  guidance anchors to demonstrated capacity. Taxonomy: `strides` plan alias;
  `tennis`, `basketball`, `kayaking`, `stand_up_paddleboarding`, `surfing`,
  `meditation`, `multi_sport` and `incident_detected` activity types.
- **`backfill_history` MCP tool** (49th tool) — repairs holes in the
  training diary from the conversation: the coach takes the date range from
  chat, reports a dry run first (default), then applies after the athlete
  confirms (backup to `data-backups/` before writing). Add-only and
  idempotent: rebuilds missing daily CTL/ACWR snapshot entries locally from
  stored `daily_loads` and re-fetches missing sleep nights / readiness days
  per-date from Garmin. `scripts/backfill_history.py` is a thin CLI over
  the same implementation (`--since` is required — no hardcoded dates).
- Coaching doctrine resource (`coach://coaching/doctrine`): multi-sport
  guidance (shared sessions benefit all sports; never spike total-body ACWR
  chasing a sport-specific CTL target) and the bedtime-drift overtraining
  signal — both previously lived only in dev docs.

### Changed
- Dev docs restructured: CLAUDE.md slimmed to development-only facts (585 →
  191 lines) with coaching doctrine single-sourced in the server surfaces;
  project Claude Code skills added under `.claude/skills/` (release,
  new-tool, capture-fixtures, acwr-recompute) and — together with the
  pre-existing mcp-builder — un-ignored so all five ship with the repo.
- **garminconnect 0.3.5 → 0.3.20** (exact pin). 0.3.17 picked up upstream's
  token-storage and auth security hardening (atomic, symlink-safe token
  writes; fail-closed MFA resume), the `display_name` retry fix that kept
  sleep/RHR/PR endpoints working after a transient profile fetch, and capped
  activity pagination. 0.3.18–0.3.20 add the fix for Garmin's 429 on every
  fresh mobile login (the credential fallback and `scripts/garmin_login.py`
  path had been blocked upstream since April), cross-process MFA resume,
  HTTP 427 handled as rate limiting, and profile-timezone date handling
  (`tzdata` on Windows). Verified before each bump: full suite, a diff of
  every workout payload the coach pushes serialized through the library's
  own models (0.3.5 → 0.3.17: HR/power targets serialize as floats, e.g.
  `120.0`; 0.3.17 → 0.3.20: byte-identical), and a read-only live run of
  every Garmin read the coach makes. The token file format is unchanged, so
  rolling back is a plain re-pin. The contract test now pins the token file
  name by behavior instead of library source text.
- FastMCP stays on `>=3.4,<4`; the project is developed and released
  against 3.4.11 (the 3.x line still receives security backports). FastMCP 4
  (MCP SDK 2, httpx2) is deferred to a dedicated release.
- Dev toolchain: pytest 9.0.3 → 9.1.1, pytest-asyncio 1.3.0 → 1.4.1; `ruff`
  0.15.11 pinned in `requirements-dev.txt` with its rule set in
  `pyproject.toml` (`[tool.ruff]`). CI gained a lint job (`ruff check .`)
  and now runs the suite on Python 3.12, 3.13 and 3.14 — the versions
  `pyproject.toml` advertises — instead of 3.14 only. Unused imports,
  placeholder-less f-strings and dead assignments were cleaned up; no
  behaviour change.
- `.gitignore` now treats everything under `data/` as private by default
  (only the shipped `methodology.json` template is tracked), so new data
  files such as `training_program.json` can't be committed by accident.

## [1.1.0] - 2026-08-27

A reliability and coaching-correctness release. The ACWR injury gate now runs
on the validated rolling 7d:28d model instead of the EWMA approximation, and a
class of date/anomaly bugs that made the coach misread *what happened when* is
fixed at the root (with the live failure that surfaced them captured as a
regression test). No new features and no breaking changes for MCP clients —
the tool surface, names, and config are identical to 1.0.0.

### Fixed
- **Morning-audit crash loop**: `scripts/daily_loop.py` no longer dies at
  import when `daily_loop.log` is held by another writer. The Task Scheduler
  action's cmd `>>` redirect into the same file the script's own
  `logging.FileHandler` opens held the log without write sharing (Windows),
  so every scheduled run since the task's 2026-06-10 registration crashed
  with `PermissionError` before doing any work. The file handler is now
  best-effort (console-only fallback) and the shipped task guidance redirects
  scheduler output to a separate `daily_loop_task.log`.
- **Planned-vs-actual matcher no longer crosswires types** (live coaching
  failure, 2026-06-10): pairing now happens ONLY between a planned session
  and an actual whose types satisfy `taxonomy.types_match`, with a new
  name-hint fallback so plan "mobility" matches Garmin type `other` when the
  activity name contains mobility/stretch/yoga ("Cape Town Mobility"); when
  several actuals match by type the closest duration wins. Unmatched planned
  sessions are `missing`, unmatched actuals are `unplanned` (load included)
  — never paired just because both were left over. The skipped-padel day now
  reads mobility=completed, padel=MISSING, hard ride=UNPLANNED instead of
  two fabricated type_mismatches. `type_mismatch` survives only as a narrow
  substitute signal: one unmatched planned session whose type the taxonomy
  can't classify (e.g. `race`) plus exactly one leftover actual.
- **Today is pending, never missed**: planned sessions dated today no longer
  produce `missing` anomalies or land in `plan_adherence.skipped_dates` — a
  06:27 snapshot was flagging the day's sessions as already skipped. The
  comparison cutoff is now date < today for missing; today belongs in
  `pending_dates`. Persistent-anomaly registration refuses missing-anomalies
  dated today, and cleanup drops previously mis-registered open
  missing-anomalies for the current date (they re-register tomorrow if the
  session genuinely never happened). Anomaly ids keep the
  `<date>:<type>:<slug>` format.
- **Snapshot payload is temporally self-anchoring** (the coach combined
  week-old anomalies into "padel was today"): `week_grid` entries and all
  anomalies carry `days_ago` (0 = today), the snapshot carries
  `week_grid_today`, `planned_vs_actual` carries `as_of`, and every anomaly
  summary embeds its absolute date plus a relative phrase
  ("2026-06-10 (yesterday): ..."), recomputed against the threaded today on
  every snapshot. `SERVER_INSTRUCTIONS` now mandates trusting
  `current_time_context` over any date impression from earlier conversation
  and stating the current date/day at session start; the full date-discipline
  doctrine lives in `coach://coaching/doctrine`.

### Changed
- **ACWR cutover to the classic rolling 7d:28d model** (owner-approved after
  the 90-day shadow period — mean abs diff 0.264, 42% zone mismatch, and the
  May 11-13 post-stage-race danger window the EWMA model missed). The rolling
  coupled-window ratio (Hulin/Gabbett — the model the 0.8/1.3/1.5 thresholds
  were derived against) is now the PRIMARY `acwr`/`acwr_status` everywhere:
  snapshot `fitness_metrics`, `load_hierarchy`, `acwr_warnings`, the weekly
  prescription volume adjustment, the coaching score health component, and
  sport-specific ACWR (the load hierarchy never mixes models). The legacy
  EWMA ratio is demoted to `fitness_metrics.acwr_ewma`
  `{value, zone, safe, note}` — reference only. The shadow keys
  (`acwr_rolling`, `acwr_rolling_status`, `acwr_shadow`) are removed.
  CTL/ATL/TSB math, the 0.8/1.3/1.5 thresholds, and the [10, 15, 25] volume
  steps are unchanged.
- **Docs clarify that Cloudflare handling is a library dependency, not custom
  code in this repo** — Garmin's SSO sits behind Cloudflare and the
  `garminconnect` library handles it natively via its `curl_cffi` dependency;
  this project's old garth + Playwright login fallback was deleted during the
  1.0 rebuild.
- **Docs aligned with MCP spec 2026-07-28** (the stateless-core release):
  `COACH_TRANSPORT=sse` is documented as legacy-only (the HTTP+SSE transport
  was formally deprecated upstream), and the deferred elicitation re-add is
  redesigned around the MRTR pattern instead of the removed server-initiated
  `ctx.elicit()` flow. No behavior changes — this stdio server needs no
  migration.

### Added
- `scripts/recompute_acwr_history.py`: supervised one-off migration that
  recomputes the `acwr` field of every stored `fitness_history.json` snapshot
  (total + per-sport) under the rolling model — `--check` dry-run by default,
  `--apply` writes after a one-time `.pre-acwr-cutover.bak` backup;
  ctl/atl/tsb are never touched.

## [1.0.0] - 2026-06-10

First publishable release — the end of a five-phase modernization
(see [docs/UPGRADE_ROADMAP.md](docs/UPGRADE_ROADMAP.md) for the full rationale).

### Added
- **Packaging**: installable via `pip install garmin-coach-mcp` /
  `uvx garmin-coach-mcp` with a `garmin-coach-mcp` console entry point
  (`coach-mcp` ships as an alias); `COACH_DATA_DIR` env var with
  data-dir resolution (env var → checkout `data/` → per-user directory);
  published to PyPI and the official MCP Registry.
- **Hard coaching gates, code-enforced**: `update_weekly_plan` /
  `push_plan_to_garmin` reject sessions violating an active injury's
  restricted activities (taxonomy-aware); the purpose gate refuses to save
  any non-rest session without a stated purpose; plan date validation
  (no all-historical plans, 21-day fat-finger guard).
- **Persistent curiosity**: planned-vs-actual anomalies register once in
  coaching memory with an open → asked → resolved lifecycle
  (`resolve_anomaly`); resolved anomalies never resurface.
- **Season lifecycle**: auto-proposals (idempotent by event tag) for A/B-race
  debriefs after race day and overdue phase transitions; season-layer
  `data_quality` flags (`a_race_in_past`, `phase_overdue`, invalid block
  dates).
- **Sectioned snapshot**: `get_coaching_snapshot()` returns a compact core
  payload (~2-3K tokens) with named drill-down sections
  (plan/activities/fitness/sleep/recovery/strength/memory/goals/patterns/
  sport_priorities); per-section failure isolation — one Garmin error
  degrades to a `data_quality` flag instead of aborting the snapshot.
- **Structured run builder**: pushed workouts carry pace/power/HR targets to
  the watch instead of losing all intensity targets.
- **Tool annotations**: readOnly/destructive/idempotent/openWorld hints on
  all 48 tools, contract-tested.

### Changed
- **Garmin auth rebuilt** on native garminconnect 0.3.5: token-first login,
  non-interactive failure with actionable `AUTH_REQUIRED` errors, a
  fail-fast auth latch, and `scripts/garmin_login.py` as the documented
  MFA/recovery path. The garth/Playwright/browser fallback stack is deleted.
  A contract test pins every garminconnect call shape the codebase uses.
- **Typed schemas + storage layer**: pydantic models for all data files,
  per-file schema versions with a migration registry, cross-process locked
  atomic writes, UTF-8 everywhere.
- **Activity taxonomy**: one canonical sport-group mapping ends the
  plan-type vs Garmin-type drift (`strength` vs `strength_training`) that
  produced false anomalies and zero adherence counts.
- **Tool surface rationalized** to 48 annotated tools: race CRUD + research
  consolidated into `races(action=...)`, five metric lookups into
  `query_metrics(kind=...)`; `SERVER_INSTRUCTIONS` cut under the 2KB client
  truncation limit with long-form doctrine moved to the
  `coach://coaching/doctrine` resource.
- **ACWR math corrected** (EWMA decay constants and thresholds aligned with
  the cited research model, with a shadow comparison report).

### Fixed
- Silent data-pipeline death: activity ingestion staleness check, fitness
  metrics fed wrong-shaped daily loads, bedtime-drift epoch crash, coaching
  memory surfacing oldest-instead-of-recent decisions.
- Plan lifecycle: stale plans pruned to a rolling window with explicit
  `plan_expired` / `days_uncoached` signals instead of anomaly floods.

### Testing
- 1,292-test suite (37 files) with GitHub Actions CI (push/PR + weekly
  date-rot cron), a canonical FakeGarminClient, committed sanitized
  fixtures so clean checkouts run everything, an autouse live-data sandbox
  guard, and an AST clock-discipline lint.

---

Pre-1.0 history (the 0.x checkout-only era) lives in the git log.

[Unreleased]: https://github.com/snoozelieb/coach-mcp/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/snoozelieb/coach-mcp/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/snoozelieb/coach-mcp/releases/tag/v1.0.0
