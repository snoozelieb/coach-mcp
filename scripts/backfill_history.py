"""Backfill fitness_history.json from Garmin + local loads (supervised).

The morning-audit crash loop (2026-06-10 -> 2026-08-26) plus the old rolling
retention prunes (sleep 30d / readiness 60d / snapshots 90d, retired
2026-08-27) left holes in the training diary: 14 sleep nights, 4 readiness
entries, and a 6-point CTL/ACWR trajectory. This script repairs them:

1. SNAPSHOTS (local, no network): for every date in range missing from
   fitness_history.snapshots, recompute the v2 entry (total + per-sport
   ctl/atl/tsb/acwr) as of THAT date from stored daily_loads — same math and
   entry shape as coach.fitness.update_fitness_history (imported, not
   duplicated). Existing entries are never replaced.
2. SLEEP (Garmin): per-date get_sleep_data for nights missing from
   sleep_history, parsed by coach.fitness.parse_sleep_payload, persisted via
   persist_sleep_data.
3. READINESS (Garmin): per-date get_training_readiness (+ get_hrv_data
   overlay) for days missing from readiness_history, parsed by
   coach.parsers.parse_training_readiness.

Safety:
- Default (and explicit --check) is a DRY-RUN: reports what is missing.
  No network, no writes.
- --apply first copies fitness_history.json to
  data-backups/fitness_history-<stamp>.bak.json, then fetches (throttled
  ~0.3s/call), then writes atomically/locked via coach.storage.
- Per-date Garmin failures degrade gracefully: the date is skipped and
  counted; the run continues.
- Designed to be run SUPERVISED — review --check output before --apply.

Usage:
    python scripts/backfill_history.py --check [--since 2026-06-01]
    python scripts/backfill_history.py --apply [--since 2026-06-01] [--until YYYY-MM-DD]
    python scripts/backfill_history.py --apply --skip-garmin   # snapshots only
"""
import argparse
import shutil
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coach.config import DATA_DIR, FITNESS_HISTORY_FILE
from coach.fitness import (
    _extract_sport_loads,
    _extract_total_loads,
    calculate_fitness_metrics,
    load_fitness_history,
    parse_sleep_payload,
    persist_readiness_data,
    persist_sleep_data,
    save_fitness_history,
)
from coach.garmin_client import GarminAuthRequiredError, garmin_api_call
from coach.parsers import parse_training_readiness

THROTTLE_SECS = 0.3
SNAPSHOT_SPORTS = ('cycling', 'running', 'strength')


def daterange(since: date, until: date):
    d = since
    while d <= until:
        yield d
        d += timedelta(days=1)


def build_snapshot_entry(daily_loads: dict, as_of: date) -> dict:
    """Build one v2 snapshot entry as of `as_of` — mirrors the shape written
    by coach.fitness.update_fitness_history (math imported, not duplicated)."""
    metrics = calculate_fitness_metrics(_extract_total_loads(daily_loads), as_of)
    entry = {
        'date': metrics['as_of_date'],
        'total': {
            'ctl': metrics['ctl'],
            'atl': metrics['atl'],
            'tsb': metrics['tsb'],
            'acwr': metrics['acwr'],
        },
    }
    for sport in SNAPSHOT_SPORTS:
        sport_loads = _extract_sport_loads(daily_loads, sport)
        if any(v > 0 for v in sport_loads.values()):
            sm = calculate_fitness_metrics(sport_loads, as_of)
            entry[sport] = {
                'ctl': sm['ctl'], 'atl': sm['atl'],
                'tsb': sm['tsb'], 'acwr': sm['acwr'],
            }
    return entry


def compact_ranges(dates: list[str]) -> str:
    """Render sorted ISO dates as 'a..b, c, d..e' for readable reports."""
    if not dates:
        return '(none)'
    out, run_start, prev = [], dates[0], dates[0]
    for ds in dates[1:]:
        if date.fromisoformat(ds) - date.fromisoformat(prev) == timedelta(days=1):
            prev = ds
            continue
        out.append(run_start if run_start == prev else f'{run_start}..{prev}')
        run_start = prev = ds
    out.append(run_start if run_start == prev else f'{run_start}..{prev}')
    return ', '.join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='dry-run (default)')
    mode.add_argument('--apply', action='store_true', help='backup, fetch, write')
    ap.add_argument('--since', default='2026-06-01', help='range start (ISO)')
    ap.add_argument('--until', default=None, help='range end (ISO, default today)')
    ap.add_argument('--skip-garmin', action='store_true',
                    help='snapshots only — no sleep/readiness fetches')
    args = ap.parse_args()

    today = date.today()
    since = date.fromisoformat(args.since)
    until = date.fromisoformat(args.until) if args.until else today
    apply_mode = args.apply

    history = load_fitness_history()
    daily_loads = history.get('daily_loads', {})
    have_snapshots = {s.get('date') for s in history.get('snapshots', [])}
    have_sleep = {r.get('date') for r in history.get('sleep_history', [])}
    have_readiness = {r.get('date') for r in history.get('readiness_history', [])}

    all_dates = [d.isoformat() for d in daterange(since, until)]
    missing_snapshots = [ds for ds in all_dates if ds not in have_snapshots]
    missing_sleep = [ds for ds in all_dates if ds not in have_sleep]
    missing_readiness = [ds for ds in all_dates if ds not in have_readiness]

    print(f'Range {since} .. {until} | data dir: {DATA_DIR}')
    print(f'daily_loads coverage: {len(daily_loads)} days (snapshot math source)')
    print(f'missing snapshots: {len(missing_snapshots)} -> {compact_ranges(missing_snapshots)}')
    print(f'missing sleep nights: {len(missing_sleep)} -> {compact_ranges(missing_sleep)}')
    print(f'missing readiness days: {len(missing_readiness)} -> {compact_ranges(missing_readiness)}')
    if not apply_mode:
        garmin_calls = 0 if args.skip_garmin else len(missing_sleep) + 2 * len(missing_readiness)
        print(f'DRY-RUN: no writes. --apply would make ~{garmin_calls} Garmin calls '
              f'(throttled {THROTTLE_SECS}s) and add the entries above.')
        return 0

    # ---- apply ----
    backups_dir = Path(DATA_DIR).parent / 'data-backups'
    backups_dir.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d-%H%M')
    backup_path = backups_dir / f'fitness_history-{stamp}.bak.json'
    shutil.copy2(Path(DATA_DIR) / FITNESS_HISTORY_FILE, backup_path)
    print(f'Backup written: {backup_path}')

    # 1. Snapshots — local recompute, add-only
    added_snapshots = 0
    snapshots = history.get('snapshots', [])
    for ds in missing_snapshots:
        snapshots.append(build_snapshot_entry(daily_loads, date.fromisoformat(ds)))
        added_snapshots += 1
    snapshots.sort(key=lambda s: s.get('date', ''))
    history['snapshots'] = snapshots
    print(f'snapshots: +{added_snapshots}')

    sleep_added = sleep_empty = readiness_added = readiness_empty = 0
    auth_dead = False
    if not args.skip_garmin:
        # 2. Sleep
        fetched_sleep = []
        for ds in missing_sleep:
            try:
                payload = garmin_api_call(lambda c, _ds=ds: c.get_sleep_data(_ds))
            except GarminAuthRequiredError as e:
                print(f'AUTH failure — stopping Garmin fetches: {e}')
                auth_dead = True
                break
            except Exception as e:
                print(f'  sleep {ds}: fetch failed ({e.__class__.__name__}) — skipped')
                sleep_empty += 1
                continue
            record, _need = parse_sleep_payload(payload, ds)
            if record:
                fetched_sleep.append(record)
                sleep_added += 1
            else:
                sleep_empty += 1
            time.sleep(THROTTLE_SECS)
        if fetched_sleep:
            history = persist_sleep_data(fetched_sleep, history, today=today)
        print(f'sleep: +{sleep_added} nights, {sleep_empty} unavailable/empty')

        # 3. Readiness (+ HRV overlay)
        if not auth_dead:
            for ds in missing_readiness:
                try:
                    readiness = garmin_api_call(
                        lambda c, _ds=ds: c.get_training_readiness(_ds))
                    time.sleep(THROTTLE_SECS)
                    try:
                        hrv = garmin_api_call(lambda c, _ds=ds: c.get_hrv_data(_ds))
                    except Exception:
                        hrv = None
                except GarminAuthRequiredError as e:
                    print(f'AUTH failure — stopping Garmin fetches: {e}')
                    break
                except Exception as e:
                    print(f'  readiness {ds}: fetch failed ({e.__class__.__name__}) — skipped')
                    readiness_empty += 1
                    continue
                parsed = parse_training_readiness(readiness or {}, hrv_data=hrv)
                if parsed.get('score') is None and parsed.get('hrv_status') is None:
                    readiness_empty += 1
                else:
                    history = persist_readiness_data({
                        'date': ds,
                        'score': parsed.get('score'),
                        'level': parsed.get('level'),
                        'hrv_status': parsed.get('hrv_status'),
                        'body_battery': None,
                    }, history, today=today)
                    readiness_added += 1
                time.sleep(THROTTLE_SECS)
            print(f'readiness: +{readiness_added} days, {readiness_empty} unavailable/empty')

    save_fitness_history(history)
    print('Saved. Totals now: '
          f"snapshots={len(history.get('snapshots', []))}, "
          f"sleep={len(history.get('sleep_history', []))}, "
          f"readiness={len(history.get('readiness_history', []))}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
