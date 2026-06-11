# Changelog

All notable changes to coach-mcp are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
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
