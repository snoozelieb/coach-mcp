"""Recompute stored snapshot ACWR values under the rolling 7d:28d primary.

One-off supervised migration for the 2026-06-10 ACWR cutover. Every entry in
fitness_history.json -> snapshots stores the ``acwr`` that was primary at
write time; entries written before the cutover carry legacy EWMA values.
This script recomputes the ``acwr`` field of every snapshot entry — the
``total`` block AND every per-sport block (cycling/running/strength/...) —
from raw ``daily_loads`` using the rolling model now used by coach.fitness
(imported, not duplicated). ``ctl``/``atl``/``tsb`` are left UNTOUCHED:
their EWMA scale is self-consistent and CTL targets are tuned against it.

Safety:
- Default (and explicit ``--check``) is a DRY-RUN: prints every would-be
  change, writes nothing.
- ``--apply`` first takes a one-time ``fitness_history.json.pre-acwr-cutover.bak``
  copy next to the file (never overwritten — storage backup convention),
  then writes atomically/locked via coach.storage.
- Designed to be run SUPERVISED by the orchestrator — do not run --apply
  against live data without a fresh backup.

Usage:
    python scripts/recompute_acwr_history.py --check
    python scripts/recompute_acwr_history.py --apply
    python scripts/recompute_acwr_history.py --check --data-dir path/to/data
"""
import argparse
import shutil
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Add project root to path so coach modules resolve when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coach import storage
from coach.config import DATA_DIR, FITNESS_HISTORY_FILE
from coach.fitness import (
    _extract_sport_loads,
    _extract_total_loads,
    calculate_fitness_metrics,
    classify_acwr_zone,
)

BACKUP_SUFFIX = '.pre-acwr-cutover.bak'


def recompute_snapshot_acwr(history: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Recompute every stored snapshot ``acwr`` under the rolling primary.

    Pure function — no I/O. Mutates (and returns) ``history`` in place,
    plus a change list for reporting. Each snapshot entry is recomputed
    as of ITS OWN stored date from the raw daily_loads, using
    coach.fitness.calculate_fitness_metrics (the rolling primary lives
    there — this script deliberately does not reimplement the math).
    Only the ``acwr`` field changes; ctl/atl/tsb are never touched.
    """
    daily_loads = history.get('daily_loads', {})
    total_loads = _extract_total_loads(daily_loads)
    sport_loads_cache: dict[str, dict[str, float]] = {}
    changes: list[dict[str, Any]] = []

    for snap in history.get('snapshots', []):
        snap_date = snap.get('date')
        try:
            as_of = date.fromisoformat(snap_date)
        except (TypeError, ValueError):
            continue  # malformed entry — leave untouched

        for scope, block in snap.items():
            if scope == 'date' or not isinstance(block, dict) or 'acwr' not in block:
                continue
            if scope == 'total':
                loads = total_loads
            else:
                # Per-sport block (cycling/running/strength/...): same
                # rolling model on the sport-filtered loads — the load
                # hierarchy must not mix models.
                if scope not in sport_loads_cache:
                    sport_loads_cache[scope] = _extract_sport_loads(daily_loads, scope)
                loads = sport_loads_cache[scope]

            old = block.get('acwr')
            new = calculate_fitness_metrics(loads, as_of)['acwr']
            if new != old:
                block['acwr'] = new
                changes.append({
                    'date': snap_date,
                    'scope': scope,
                    'old': old,
                    'new': new,
                    'old_zone': classify_acwr_zone(old)[0]
                                if isinstance(old, (int, float)) else 'unknown',
                    'new_zone': classify_acwr_zone(new)[0],
                })

    return history, changes


def _print_changes(changes: list[dict[str, Any]]) -> None:
    if not changes:
        print("All stored snapshot acwr values already match the rolling model.")
        return
    print(f"{'date':<12}{'scope':<10}{'old':>8}{'new':>8}  zone change")
    print('-' * 60)
    for c in changes:
        old = f"{c['old']:.2f}" if isinstance(c['old'], (int, float)) else str(c['old'])
        zone_note = (f"{c['old_zone']} -> {c['new_zone']}"
                     if c['old_zone'] != c['new_zone'] else c['new_zone'])
        print(f"{c['date']:<12}{c['scope']:<10}{old:>8}{c['new']:>8.2f}  {zone_note}")
    print('-' * 60)
    zone_flips = sum(1 for c in changes if c['old_zone'] != c['new_zone'])
    print(f"{len(changes)} value(s) to recompute, {zone_flips} zone change(s).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python scripts/recompute_acwr_history.py',
        description='Recompute stored snapshot ACWR values under the '
                    'rolling 7d:28d primary model (2026-06-10 cutover). '
                    'ctl/atl/tsb are never touched.',
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true',
                      help='Dry-run (default): report every change, write nothing.')
    mode.add_argument('--apply', action='store_true',
                      help='Write the recomputed history '
                           '(one-time .pre-acwr-cutover.bak copy is taken first).')
    parser.add_argument('--data-dir', default=None,
                        help=f'Data directory to operate on (default: {DATA_DIR})')
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir) if args.data_dir else DATA_DIR
    path = data_dir / FITNESS_HISTORY_FILE
    if not path.exists():
        print(f"fitness_history not found: {path}")
        return 1

    history = storage.read_json(FITNESS_HISTORY_FILE, data_dir=data_dir)
    snapshots = history.get('snapshots', [])
    print(f"Loaded {path} ({len(snapshots)} snapshot(s))")

    history, changes = recompute_snapshot_acwr(history)
    _print_changes(changes)

    if not args.apply:
        print("DRY-RUN - nothing written. Re-run with --apply to write.")
        return 0

    if not changes:
        print("Nothing to write.")
        return 0

    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if backup.exists():
        print(f"Backup already exists (kept, never overwritten): {backup.name}")
    else:
        shutil.copy2(path, backup)
        print(f"Backup written: {backup.name}")

    storage.write_json(FITNESS_HISTORY_FILE, history, data_dir=data_dir)
    print(f"Wrote {len(changes)} recomputed acwr value(s) to {path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
