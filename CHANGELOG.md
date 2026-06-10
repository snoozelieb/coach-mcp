# Changelog

All notable changes to coach-mcp are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[Semantic Versioning](https://semver.org/).

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
