---
name: acwr-recompute
description: Supervised recompute of stored ACWR history in fitness_history.json under the rolling 7d:28d primary model — dry-run review, backup, then apply. Use when historical snapshot ACWR values need recomputing (e.g. after a model change or backfilled loads), never as a routine step.
---

# Recomputing stored ACWR history (supervised)

`scripts/recompute_acwr_history.py` rewrites the `acwr` field of every snapshot
entry in `fitness_history.json` — the `total` block AND every per-sport block —
from raw `daily_loads` using the rolling 7d:28d model imported from
`coach.fitness` (never a reimplementation). `ctl`/`atl`/`tsb` are left
untouched: their EWMA scale is self-consistent and targets are tuned against it.

This is a **supervised migration**, not maintenance. It was built for the
2026-06-10 ACWR cutover; reach for it again only if the primary model changes
or historical `daily_loads` were backfilled/repaired.

## Procedure — in this order

1. **Timestamped backup first** (execution rule for ANY migrating change to
   live coaching data — it is gitignored and irreplaceable):

   ```bash
   cp -r data "data-backups/backup-$(date +%Y%m%d-%H%M)"
   ```

2. **Dry-run and actually read it** (default mode; writes nothing):

   ```bash
   python scripts/recompute_acwr_history.py --check
   ```

   Review the printed change list WITH the user — zone flips
   (`optimal` ↔ `elevated`, the "sweet spot" boundary, as labeled by
   `classify_acwr_zone`) are the changes that alter coaching behavior.
   Get explicit approval before applying.

3. **Apply**:

   ```bash
   python scripts/recompute_acwr_history.py --apply
   ```

   `--apply` takes a one-time `fitness_history.json.pre-acwr-cutover.bak`
   beside the file (never overwritten — storage backup convention) and writes
   atomically/locked via `coach.storage`.

4. **Verify**: re-run `--check` (should report no changes), then
   `python -m pytest -q`, then have the running MCP server reconnected so the
   snapshot serves recomputed values.

`--data-dir path/to/data` targets a non-default data directory (e.g. a restored
backup for a rehearsal run — rehearse there when the change list is large).
