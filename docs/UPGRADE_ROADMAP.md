# Coach-MCP Upgrade Roadmap (FINAL — post intent-guardian + red-team review)

Project: C:\Users\schoonie\Documents\personal\coach-mcp — adaptive AI training coach MCP server.
Basis: 45-agent review (7 dimensions, 32 adversarially-verified critical/high findings, all confirmed), then roadmap reviews by project-intent-guardian (verdict: aligned-with-changes) and red-team-reviewer (verdict: proceed-with-changes). All required changes incorporated.

## Diagnosis (why it "gives lots of errors")

1. **Garmin auth stack is dead.** garminconnect 0.3.2 dropped garth (not installed, deprecated upstream); the entire fallback stack (playwright_auth.py, garmin_browser_login.py, the 429 branch) crashes with ModuleNotFoundError. System survives only while the current DI token stays valid — NO working recovery path exists.
2. **Data pipeline silently died 2026-05-23.** Snapshot sleep/readiness saves bump last_updated, defeating the activity-ingestion staleness check — ingestion permanently stopped. CTL/ATL/ACWR run on 18-day-old data; the ACWR injury gate is fiction. calculate_fitness_metrics is also fed v2-dict daily_loads at 2 sites (TypeError swallowed) so ACWR volume adjustment never fires.
3. **Schema/vocabulary drift family** (the df536c1 bug class): plan types vs Garmin types ('strength' vs 'strength_training') → plan_adherence always 0 + false anomalies; 'target_mins_per_week' vs 'target_minutes_per_week'; injury 'restrictions' vs 'restricted_activities' (hard-gate reads keys that never exist); 'no_training_days' vs 'blocked_days'; broken `from tools.coaching_tools` import; 17-day stale plan, no week_start → anomaly floods; week_grid marks unfetched days REST.
4. **Philosophy enforcement channel broken**: SERVER_INSTRUCTIONS is 4,908 chars; Claude Code truncates at 2KB — injury gate + canonical flow silently dropped (reproduced live). Live plan: 0 purpose fields in 34 sessions, 6 runs prescribed against an active running restriction, expired 7 days unflagged, athlete dark 17 days, learning loop ran ~3x in 150 days, snapshot surfaces the 5 OLDEST decisions.
5. **ACWR math wrong** (k=2/(N+1) ≈ double decay vs cited model, rolling-average thresholds applied to EWMA values). **Snapshot SPOF** (one unguarded call aborts all; ~11+N sequential calls; 45-65KB response). **MCP surface dated** (54 tools ≈ 11-12K tokens fixed cost; no annotations; stringly params; update_weekly_plan schema docs silently dropped by FastMCP Args parsing). **Elicitation now supported by Claude Code** (removal rationale stale; sampling still unsupported). **daily_loop --llm**: anthropic SDK not installed + model retires 2026-06-15; audit_yesterday has a NameError that fires whenever the plan is current. **Tests**: suite RED (date rot), no CI, clean checkout errors, zero happy-path tests on the two tools that shipped the last 5 production bugs, mocks insulate from garminconnect. **Data layer**: no schemas/versioning (7 of 8 files), derived view written back into training_config.json, concurrent-writer races, unbounded growth, cp1252 encoding.

---

## Phase 0 — Stabilize + RESUME COACHING (the kernel)
Exit criteria: athlete is actually coached again; suite + CI green; no live deadline; no unrecoverable failure mode.

0. **Backup discipline first (non-negotiable)**: timestamped copy of data/ → data-backups/backup-YYYYMMDD-HHMM/ before ANY change; repeat before every migration step in later phases.
1. **daily_loop**: add anthropic>=0.109; model via env var (default claude-sonnet-4-6, ID verified against the models API at implementation); fix audit_yesterday NameError (daily_loop.py:290) + normalize list-vs-dict 'planned' via one shared helper.
2. **Minimal auth recovery script on the INSTALLED 0.3.2** (~30 lines, native login) — insurance so a token expiry mid-upgrade can't strand us. Full rebuild is Phase 1.
3. **Silent-correctness fixes** (small diffs, immediate effect):
   - _extract_total_loads() at planning_tools.py:84 and :201 (ACWR volume adjustment revives)
   - `from tools.coaching_tools` → `from .coaching_tools` (planning_tools.py:816)
   - injury key normalizer ('restrictions'/'name' → actual record keys) in get_week_constraints
   - pillar key normalizer (target_mins_per_week vs target_minutes_per_week)
   - ingestion staleness: separate last_activity_ingest_date + idempotent trailing-3-day re-ingest per snapshot
   - power/pace zones path fix (pushed workouts currently lose all intensity targets — prescription never reaches the wrist)
   - detect_bedtime_drift epoch-ms crash fix (latent snapshot killer)
   - coaching memory recency: [:5] oldest-first → most-recent-first (one line; restores the memory pillar)
4. **Plan lifecycle quick fix + lapse signaling together**: derive/require week_start, prune to rolling window, plan_stale flag suppressing the false-anomaly flood AND plan_expired/days_uncoached flags so the snapshot signals the lapse instead of going quiet.
5. **Interim injury write-gate** (~30 lines): update_weekly_plan/push_plan_to_garmin reject sessions whose type intersects active/improving restricted_activities (string-intersection via the same normalizer as 3c).
6. **Tests/CI green on day one**: fix 2 red date-rot tests + 2027-02-05 time bomb (relative dates); garmin_fixtures skip-if-absent; GitHub Actions pytest on push + weekly cron; import-everything smoke test (would have caught the garth break).
7. **SERVER_INSTRUCTIONS < 2,000 chars**: hard mandates first (snapshot-first, injury gate, verify-before-confirm); long-form doctrine → coach://doctrine resource.
8. **Live-data repair session (with the athlete)**: archive the 17-day stale plan; debrief + clear the past A-race; fix corrupt block dates; one-time 18-day load backfill under OLD math (sanity-checked against Garmin's own load figures); then build a fresh week plan — purpose on every non-rest session, honoring the active running restriction.
9. **Schedule daily_loop** via Task Scheduler with a basic notification (rich push channel deferred).

## Phase 1 — Foundations: auth + schema + storage + load model
1. **Garmin auth rebuild — prove-before-delete protocol**: on a branch, pin garminconnect==0.3.5; contract test FIRST (assert every method/attr the code uses exists); full cycle on THIS machine (token load → forced expiry → fresh login → MFA via resume_login); write scripts/garmin_login.py (new documented recovery entry point); tag rollback commit; only then delete playwright_auth.py + browser script + 429 branch. Non-interactive failures return {'error': {'code': 'AUTH_REQUIRED', 'remediation': '<the real script>'}}; auth latch (one expired session ≠ N login attempts per snapshot).
2. **Pydantic schema layer**: models for all 8 data files; per-file schema_version + central migration registry; migrations dry-runnable (--check) and keep <name>.v<N>.bak on first upgrade; validation errors name the offending file.
3. **coach/storage.py** as a repository-style interface (domain operations, not raw load/save): atomic writes, utf-8, cross-process filelock, stored-state vs derived-view separation (fixes training_config pollution). SQLite stays a backend-swap option behind this interface — explicit triggers: a verified lost write under filelock, fitness_history > ~5MB or snapshot-save latency degrading the first call, or a third concurrent writer.
4. **Canonical activity-type taxonomy**: one registry (canonical type, aliases, pillar, sport group, Garmin type) used by classify_activity, planned-vs-actual matcher, plan_adherence, workout_builder, race-type→sport maps.
5. **Load model rebuild — shadow then cutover**: idempotent ingestion separated from computation; new validated ACWR model (classic 7d/28d rolling, or EWMA k=1/N) computed IN SHADOW alongside old math with a 90-day comparison report the owner eyeballs; cutover = recompute historical snapshots from raw daily_loads + recalibrate every consumer constant (CTL_TARGETS, [10,15,25] volume steps, 0.8/1.3/1.5 thresholds) in the same commit; fix check_safety_rules to count DAYS not activities (the consecutive-hard-day safety gate); golden-value unit tests pin the math.
6. **Structured error envelope** {'error': {'code', 'message', 'remediation'}} across all tools.

## Phase 2 — MCP surface (slimmed scope per red-team)
1. **Sectioned snapshot**: sections=['core'] default (~2-3K tokens) including current_time_context, flags, week_grid, acwr_status, injuries, plan_adherence, today/tomorrow plan, RECENT coaching decisions, OPEN anomalies, sleep-gate signal, data_quality (per intent-guardian: core must carry memory + curiosity or they die by default). CRITICAL: trailing-3-day ingest + sleep persistence run on EVERY snapshot call regardless of sections — with a regression test asserting daily_loads advances after a core-only call. Per-section try/except → data_quality flags; ranged sleep fetch; lazy HR-zone enrichment; short-TTL cache; drop indent=2.
2. **Typed parameters + structured output**: pydantic WeeklyPlan/PlanDay/Session/StructurePhase (fixes the dropped-schema bug + protocol-level validation). Session model includes purpose AND intensity:'discretion' + constraints (athlete-discretion days stay truthful). Real outputSchema instead of JSON strings.
3. **Tool annotations** (readOnlyHint/destructiveHint/idempotentHint) + FastMCP 3.4.x pin.
4. **Cheap consolidations only**: merge race CRUD + drill-down families; keep destructive ops (push_plan_to_garmin, remove_race) standalone (annotations are per-tool); demote to resources only data also reachable via snapshot; renames atomic with CLAUDE.md/prompts/settings updates + old→new table. Don't chase "22".

## Phase 3 — Coaching intelligence (lean)
1. **Full typed injury + purpose code gates** in mutating tools (override=True + logged rationale escape hatch); plan date validation.
2. **Memory that matters**: decisions_due_review in snapshot; anomaly persistence (asked/answered/resolved — curiosity with memory); canonical adaptation-pattern keys so patterns can trigger; auto-transition overdue decisions to needs_review.
3. **Periodization lifecycle**: block-date validation; race-date-passed → auto-propose "debrief + re-plan season"; sleep_gate signal in flags (status + basis).

## Phase 4 — Test depth
1. Canonical FakeGarminClient + end-to-end golden-schema tests for get_coaching_snapshot and push_plan_to_garmin happy paths.
2. Committed sanitized test_fixtures.json (list-vs-dict fidelity) + scripts/capture_fixtures.py; real capture stays optional override.
3. Clock injection (ban naked date.today() outside tool boundaries; CI grep lint).
4. Autouse DATA_DIR sandbox guard (tests can never touch live coaching data).
5. pytest-cov ratchet; close untested-tool gap in risk order; delete tautological tests; pin full dependency stack in pyproject.toml.

## Deferred backlog (explicit, with triggers)
- Post-session debrief auto-prefill loop; living athlete dossier (record_athlete_insight); proactive push daemon (needs filelock + non-crashing daily_loop first) — revisit after Phase 3.
- Elicitation re-add — REDESIGN REQUIRED (noted 2026-08-27): MCP spec 2026-07-28 removed server-initiated `elicitation/create`; elicitation now works via the MRTR pattern (tool returns `resultType: "input_required"`, client retries with `inputResponses`). Re-add only when on a 2026-07-28-capable framework (FastMCP 4 GA or mcp SDK v2); the data-first fallback (question-set returns) is already the right app-layer shape.
- SQLite migration — only on the explicit triggers in Phase 1.3.
- MCP Tasks and sampling — spec 2026-07-28 moved tasks out of core into the `io.modelcontextprotocol/tasks` extension and Deprecated Sampling wholesale (SEP-2577; migration is direct LLM provider APIs — which `daily_loop --llm` already does). Do not build on either.

## Execution discipline (red-team mandates)
- Backup before every migrating step; migrations dry-runnable; .bak retention.
- Every phase item independently shippable; commit-per-item; phases have done-criteria.
- The single biggest failure mode to avoid: Phase 1 big-bang (migration + storage rewrite + math swap landing together on unbacked-up live data). Sequence: backup → backfill under old math → schema migration with dry-run → shadow math → cutover with recalibration.
