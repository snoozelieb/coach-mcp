"""
Single I/O choke point for the coach data files.

Every JSON read/write in the codebase funnels through here (via
coach.planner.load_json_file / save_json_file and
coach.fitness.save_fitness_history), which gives all call sites:

- **UTF-8 encoding** on both read and write (output stays ASCII-escaped via
  json's default ensure_ascii so any legacy reader that still opens files
  without an explicit encoding cannot mis-decode them).
- **Atomic writes**: unique tempfile in the same directory + os.replace.
- **Cross-process locking**: stdlib lockfile (os.open with O_CREAT|O_EXCL),
  retry loop, and a stale-lock timeout (~30s) so a crashed process can never
  wedge the system. No third-party dependencies.
- **Schema validation** (flag-only) via coach.schemas for known filenames.
  Validation failures log a warning naming the file and the data is returned/
  written anyway — validation never blocks reads or destroys data.
- **schema_version stamping + central migration registry**. Migrations are
  conservative by contract: they may ADD canonical keys or mirrors, but never
  delete or rewrite athlete-authored content. Reads apply migrations
  in-memory only (so consumers always see canonical shape); disk content is
  upgraded either by a normal save or by the supervised CLI below. Before the
  first migrating write to a file, a one-time ``<name>.v<N>.bak`` copy is
  placed next to it (N = the on-disk version being replaced).

CLI:
    python -m coach.storage --check               # dry-run: validate + report, never writes
    python -m coach.storage --migrate             # supervised migration (writes .bak first)
    python -m coach.storage --check --data-dir D  # operate on a different data dir
"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Any, Callable, Iterator

from pydantic import ValidationError

from .config import DATA_DIR
from .schemas import SCHEMA_MODELS

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cross-process lockfile (stdlib only)
# ---------------------------------------------------------------------------

LOCK_TIMEOUT_SECONDS = 10.0      # how long a caller waits to acquire
LOCK_STALE_SECONDS = 30.0        # locks older than this are considered dead
LOCK_RETRY_INTERVAL = 0.05


@contextmanager
def file_lock(
    target: Path,
    timeout: float = LOCK_TIMEOUT_SECONDS,
    stale_after: float = LOCK_STALE_SECONDS,
) -> Iterator[None]:
    """Cross-process advisory lock for ``target`` via a ``<name>.lock`` file.

    Acquisition is atomic (os.open with O_CREAT|O_EXCL works on both POSIX
    and Windows). If the lockfile is older than ``stale_after`` seconds it is
    treated as left behind by a crashed process and broken. Raises
    TimeoutError if the lock cannot be acquired within ``timeout`` seconds.
    """
    lock_path = target.with_name(target.name + '.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd: int | None = None

    while fd is None:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid={os.getpid()} t={time.time():.0f}".encode('utf-8'))
        except FileExistsError:
            # Held by someone else — break it only if stale.
            try:
                age = time.time() - lock_path.stat().st_mtime
            except OSError:
                age = 0.0  # lock vanished between open and stat — just retry
            if age > stale_after:
                logger.warning("Breaking stale lock %s (age %.0fs)", lock_path, age)
                try:
                    lock_path.unlink()
                except OSError:
                    pass  # another process beat us to it
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Could not acquire lock {lock_path} within {timeout}s"
                )
            time.sleep(LOCK_RETRY_INTERVAL)

    try:
        yield
    finally:
        try:
            os.close(fd)
        finally:
            try:
                lock_path.unlink()
            except OSError:
                logger.warning("Could not remove lock file %s", lock_path)


# ---------------------------------------------------------------------------
# Migration registry
# ---------------------------------------------------------------------------

# Target schema_version per known file. fitness_history's lineage (v2 =
# sport-aware daily_loads) is owned by coach.fitness and respected here.
# exercise_library.json is intentionally absent: it is a root mapping of
# exercise names, and stamping a schema_version key into it would pollute
# the namespace that consumers iterate.
SCHEMA_TARGET_VERSIONS: dict[str, int] = {
    'athlete.json': 1,
    'athlete_baseline.json': 1,
    'training_config.json': 1,
    'methodology.json': 1,
    'weekly_plan.json': 1,
    'coaching_log.json': 1,
    'fitness_history.json': 2,
}

KNOWN_DATA_FILES: tuple[str, ...] = tuple(SCHEMA_MODELS.keys())


def _is_iso_date(value: Any) -> bool:
    try:
        date.fromisoformat(value)
        return True
    except (ValueError, TypeError):
        return False


def _stamp_only(data: dict[str, Any]) -> dict[str, Any]:
    """No structural change — the framework stamps schema_version after us."""
    return data


def _migrate_weekly_plan_v1(plan: dict[str, Any]) -> dict[str, Any]:
    """v0 -> v1: derive week_start/week_end from day keys when absent."""
    days = plan.get('days')
    if isinstance(days, dict):
        valid_keys = sorted(k for k in days if _is_iso_date(k))
        if valid_keys:
            if not plan.get('week_start'):
                plan['week_start'] = valid_keys[0]
            if not plan.get('week_end'):
                plan['week_end'] = valid_keys[-1]
    return plan


def _migrate_athlete_v1(athlete: dict[str, Any]) -> dict[str, Any]:
    """v0 -> v1: mirror no_training_days <-> blocked_days (keep BOTH keys).

    Conservative: nothing is removed or renamed — whichever spelling exists
    is copied to the missing one so every consumer finds the key it reads.
    """
    constraints = athlete.get('life_constraints')
    if isinstance(constraints, dict):
        no_training = constraints.get('no_training_days')
        blocked = constraints.get('blocked_days')
        if no_training is not None and blocked is None:
            constraints['blocked_days'] = list(no_training)
        elif blocked is not None and no_training is None:
            constraints['no_training_days'] = list(blocked)
    return athlete


def _migrate_fitness_history_v2(history: dict[str, Any]) -> dict[str, Any]:
    """v0/v1 -> v2: delegate to the existing (idempotent, tested) migration
    in coach.fitness so the daily_loads/snapshot conversion has one owner."""
    from .fitness import migrate_fitness_history  # lazy: fitness imports storage
    return migrate_fitness_history(history)


# {filename: [(from_version, to_version, migration_fn), ...]}
# Migration fns take the loaded dict and return the (possibly mutated) dict.
# Contract: ADD canonical keys/mirrors only — never delete or rewrite
# athlete-authored content. The framework stamps schema_version=to_version
# after each step.
MIGRATIONS: dict[str, list[tuple[int, int, Callable[[dict[str, Any]], dict[str, Any]]]]] = {
    'athlete.json': [(0, 1, _migrate_athlete_v1)],
    'athlete_baseline.json': [(0, 1, _stamp_only)],
    'training_config.json': [(0, 1, _stamp_only)],
    'methodology.json': [(0, 1, _stamp_only)],
    'weekly_plan.json': [(0, 1, _migrate_weekly_plan_v1)],
    'coaching_log.json': [(0, 1, _stamp_only)],
    'fitness_history.json': [
        (0, 2, _migrate_fitness_history_v2),
        (1, 2, _migrate_fitness_history_v2),
    ],
}


def get_schema_version(data: Any) -> int:
    """Current schema_version of a loaded file (0 = unversioned legacy)."""
    if isinstance(data, dict):
        version = data.get('schema_version')
        if isinstance(version, int):
            return version
    return 0


def pending_migrations(filename: str, data: Any) -> list[tuple[int, int]]:
    """Which registered migration steps would apply to this data."""
    if not isinstance(data, dict):
        return []
    pending = []
    version = get_schema_version(data)
    for from_v, to_v, _fn in sorted(MIGRATIONS.get(filename, [])):
        if version == from_v and to_v > version:
            pending.append((from_v, to_v))
            version = to_v
    return pending


def apply_migrations(filename: str, data: Any) -> tuple[Any, list[tuple[int, int]]]:
    """Run all applicable registered migrations for ``filename`` in order.

    Returns (migrated_data, applied_steps). Never downgrades; unknown
    filenames and non-dict payloads pass through untouched.
    """
    if not isinstance(data, dict):
        return data, []
    applied: list[tuple[int, int]] = []
    for from_v, to_v, fn in sorted(MIGRATIONS.get(filename, [])):
        version = get_schema_version(data)
        if version == from_v and to_v > version:
            data = fn(data)
            data['schema_version'] = to_v
            applied.append((from_v, to_v))
    return data, applied


# ---------------------------------------------------------------------------
# Validation (flag-only — never blocks, never rewrites)
# ---------------------------------------------------------------------------

def validate_data(filename: str, data: Any) -> list[str]:
    """Validate ``data`` against the schema registered for ``filename``.

    Returns a list of human-readable problems (empty = valid). Problems are
    logged as warnings naming the offending file. Unknown filenames are not
    validated. The caller's data is NEVER modified or rejected.
    """
    model = SCHEMA_MODELS.get(filename)
    if model is None:
        return []
    try:
        model.model_validate(data)
        return []
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(part) for part in err['loc'])}: {err['msg']}"
            for err in exc.errors()
        ]
        logger.warning(
            "Schema validation failed for %s (%d issue(s)): %s",
            filename, len(problems), '; '.join(problems[:5]),
        )
        return problems


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _resolve(filename: str, data_dir: Path | str | None) -> Path:
    base = Path(data_dir) if data_dir is not None else DATA_DIR
    return base / filename


def _read_raw(path: Path) -> Any:
    return json.loads(path.read_text(encoding='utf-8'))


def read_json(filename: str, data_dir: Path | str | None = None) -> Any:
    """Load a JSON data file.

    - Missing file -> {} (matching historical planner.load_json_file).
    - Registered migrations are applied IN MEMORY so consumers always see the
      canonical shape; plain reads never write to disk (disk upgrades happen
      on the next save, or supervised via ``python -m coach.storage --migrate``).
    - Validation problems are logged (naming the file) and the raw data is
      returned anyway — reads are never blocked by validation.
    """
    path = _resolve(filename, data_dir)
    if not path.exists():
        return {}
    with file_lock(path):
        data = _read_raw(path)
    data, applied = apply_migrations(filename, data)
    if applied:
        logger.info(
            "Applied in-memory migration(s) %s to %s (disk unchanged; "
            "persists on next save or via 'python -m coach.storage --migrate')",
            applied, filename,
        )
    validate_data(filename, data)
    return data


def _backup_path(path: Path, disk_version: int) -> Path:
    return path.with_name(f"{path.name}.v{disk_version}.bak")


def _ensure_backup_before_upgrade(path: Path, filename: str, new_data: Any) -> None:
    """One-time ``<name>.v<N>.bak`` copy before the first migrating write.

    Triggered when the on-disk file's schema_version is lower than the
    version about to be written. The backup is never overwritten, so the
    pre-migration original is always recoverable. Caller must hold the lock.
    """
    if filename not in SCHEMA_TARGET_VERSIONS or not path.exists():
        return
    new_version = get_schema_version(new_data)
    if new_version <= 0:
        return
    try:
        disk_version = get_schema_version(_read_raw(path))
    except (json.JSONDecodeError, OSError):
        logger.warning("Could not read %s to check schema version before write", path)
        return
    if disk_version >= new_version:
        return
    backup = _backup_path(path, disk_version)
    if backup.exists():
        return  # one-time only
    shutil.copy2(path, backup)
    logger.info(
        "Created pre-migration backup %s (v%d -> v%d)",
        backup.name, disk_version, new_version,
    )


def _atomic_write(path: Path, data: Any) -> None:
    """Write JSON atomically: unique tempfile in the same dir + os.replace.

    Unique tempfile (not a fixed '.tmp' suffix) so two processes writing the
    same file can never trample each other's temp data. Output is UTF-8 with
    ASCII escapes (json default), so legacy readers that open files without
    an explicit encoding still decode correctly.
    """
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + '.', suffix='.tmp', dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def write_json(filename: str, data: Any, data_dir: Path | str | None = None) -> None:
    """Save a JSON data file (atomic, locked, UTF-8).

    Registered migrations run on the outgoing data first (stamping
    schema_version and adding canonical mirrors), validation problems are
    logged but never block the write, and a one-time ``.v<N>.bak`` copy of
    the on-disk file is taken before its first migrating write.
    """
    path = _resolve(filename, data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data, _applied = apply_migrations(filename, data)
    validate_data(filename, data)
    with file_lock(path):
        _ensure_backup_before_upgrade(path, filename, data)
        _atomic_write(path, data)


# ---------------------------------------------------------------------------
# CLI: --check (dry-run) / --migrate (supervised)
# ---------------------------------------------------------------------------

def check_data_files(data_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Dry-run validation + migration report for every known data file.

    NEVER writes. Returns {filename: {exists, parse_error, schema_version,
    target_version, pending_migrations, validation_problems}}.
    """
    report: dict[str, dict[str, Any]] = {}
    for filename in KNOWN_DATA_FILES:
        path = _resolve(filename, data_dir)
        entry: dict[str, Any] = {
            'exists': path.exists(),
            'parse_error': None,
            'schema_version': None,
            'target_version': SCHEMA_TARGET_VERSIONS.get(filename),
            'pending_migrations': [],
            'validation_problems': [],
        }
        if path.exists():
            try:
                data = _read_raw(path)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                entry['parse_error'] = str(exc)
                report[filename] = entry
                continue
            entry['schema_version'] = get_schema_version(data)
            entry['pending_migrations'] = pending_migrations(filename, data)
            # Validate the post-migration shape on a COPY — --check must not
            # mutate anything, in memory or on disk.
            migrated, _ = apply_migrations(filename, copy.deepcopy(data))
            entry['validation_problems'] = validate_data(filename, migrated)
        report[filename] = entry
    return report


def migrate_data_files(data_dir: Path | str | None = None) -> dict[str, dict[str, Any]]:
    """Apply pending migrations to every known data file (supervised path).

    For each file with pending migrations: take the one-time .v<N>.bak copy,
    then atomically write the migrated content. Files already at their target
    version (or absent) are untouched. Returns a per-file action report.
    """
    report: dict[str, dict[str, Any]] = {}
    for filename in KNOWN_DATA_FILES:
        path = _resolve(filename, data_dir)
        entry: dict[str, Any] = {'migrated': False, 'applied': [], 'backup': None}
        if not path.exists():
            report[filename] = entry
            continue
        with file_lock(path):
            try:
                data = _read_raw(path)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                entry['error'] = str(exc)
                report[filename] = entry
                continue
            migrated, applied = apply_migrations(filename, data)
            if applied:
                _ensure_backup_before_upgrade(path, filename, migrated)
                backup = _backup_path(path, applied[0][0])
                entry['backup'] = backup.name if backup.exists() else None
                _atomic_write(path, migrated)
                entry['migrated'] = True
                entry['applied'] = applied
        report[filename] = entry
    return report


def _print_check_report(report: dict[str, dict[str, Any]]) -> bool:
    """Render the --check report. Returns True if everything is healthy."""
    ok = True
    for filename, entry in report.items():
        if not entry['exists']:
            print(f"  {filename:<28} MISSING (ok on a fresh install)")
            continue
        if entry['parse_error']:
            ok = False
            print(f"  {filename:<28} PARSE ERROR: {entry['parse_error']}")
            continue
        problems = entry['validation_problems']
        pending = entry['pending_migrations']
        status = 'OK' if not problems else f"{len(problems)} VALIDATION ISSUE(S)"
        if problems:
            ok = False
        version = entry['schema_version']
        target = entry['target_version']
        migration_note = f", pending migrations: {pending}" if pending else ''
        print(f"  {filename:<28} {status} (v{version} -> target v{target}{migration_note})")
        for problem in problems[:10]:
            print(f"      - {problem}")
    return ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog='python -m coach.storage',
        description='Validate and migrate coach data files.',
    )
    parser.add_argument(
        '--check', action='store_true',
        help='Dry-run: validate every data file and report pending migrations. Never writes.',
    )
    parser.add_argument(
        '--migrate', action='store_true',
        help='Apply pending migrations (one-time .v<N>.bak copies are written first).',
    )
    parser.add_argument(
        '--data-dir', default=None,
        help=f'Data directory to operate on (default: {DATA_DIR})',
    )
    args = parser.parse_args(argv)

    data_dir = args.data_dir or DATA_DIR

    if args.migrate:
        print(f"Migrating data files in {data_dir}")
        report = migrate_data_files(data_dir)
        for filename, entry in report.items():
            if entry.get('error'):
                print(f"  {filename:<28} ERROR: {entry['error']}")
            elif entry['migrated']:
                print(f"  {filename:<28} migrated {entry['applied']} (backup: {entry['backup']})")
            else:
                print(f"  {filename:<28} up to date")
        print("Post-migration check:")
        return 0 if _print_check_report(check_data_files(data_dir)) else 1

    # Default (and explicit --check): dry-run report, never writes.
    print(f"Checking data files in {data_dir} (dry-run, no writes)")
    return 0 if _print_check_report(check_data_files(data_dir)) else 1


if __name__ == '__main__':
    raise SystemExit(main())
