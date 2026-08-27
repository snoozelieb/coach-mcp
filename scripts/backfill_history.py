"""Backfill fitness_history.json from Garmin + local loads (supervised CLI).

Thin wrapper over coach.fitness.backfill_fitness_history — the same
implementation behind the `backfill_history` MCP tool, which is the usual
way to run this (the coach takes the dates from the conversation and shows
a dry-run report first). This CLI exists for repairs outside a coaching
session.

What it repairs (ADD-ONLY — existing entries are never replaced):
1. Missing daily CTL/ACWR snapshot entries, recomputed locally from stored
   daily_loads.
2. Missing sleep nights, re-fetched per-date from Garmin.
3. Missing readiness days, re-fetched per-date from Garmin (+ HRV overlay).

Safety:
- Default (and explicit --check) is a DRY-RUN: reports what is missing.
  No network, no writes.
- --apply first copies fitness_history.json to data-backups/, then fetches
  (throttled), then writes atomically/locked via coach.storage.
- Per-date Garmin failures are skipped and counted; auth failure stops
  further fetches but keeps what was gathered.

Usage:
    python scripts/backfill_history.py --since 2026-06-01 --check
    python scripts/backfill_history.py --since 2026-06-01 [--until YYYY-MM-DD] --apply
    python scripts/backfill_history.py --since 2026-06-01 --apply --skip-garmin
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coach.config import DATA_DIR
from coach.fitness import backfill_fitness_history


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='dry-run (default)')
    mode.add_argument('--apply', action='store_true', help='backup, fetch, write')
    ap.add_argument('--since', required=True, help='range start (YYYY-MM-DD)')
    ap.add_argument('--until', default=None, help='range end (default: today)')
    ap.add_argument('--skip-garmin', action='store_true',
                    help='snapshots only — no sleep/readiness fetches')
    args = ap.parse_args()

    today = date.today()
    since = date.fromisoformat(args.since)
    until = min(date.fromisoformat(args.until), today) if args.until else today

    print(f'Range {since} .. {until} | data dir: {DATA_DIR}')
    report = backfill_fitness_history(
        since, until, today=today,
        apply=args.apply, skip_garmin=args.skip_garmin)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
