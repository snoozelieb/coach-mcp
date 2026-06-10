"""ACWR comparison report: rolling primary model vs legacy EWMA reference.

HISTORICAL (cutover completed 2026-06-10). This report drove the shadow
period that ended with the owner-approved cutover: over 90 days the two
models showed mean abs diff 0.264 and 42% zone mismatch, and the rolling
model correctly flagged the May 11-13 post-stage-race danger window
(rolling 2.13-2.40 danger) that the then-primary EWMA model read as
optimal/low. The classic rolling 7d:28d model is now the PRIMARY
(``acwr`` / ``acwr_status``); the EWMA model survives only as the labeled
``acwr_ewma`` reference. The script is retained for audit and for
re-checking divergence on any window.

READ-ONLY. Reads data/fitness_history.json and writes NOTHING — no data
files, no logs, no caches. Safe to run against the live data directory.

Model definitions (both still computed by coach.fitness):
- PRIMARY: classic rolling ACWR = 7-day mean / 28-day mean daily load
  (coupled windows) — the model the 0.8/1.3/1.5 thresholds were actually
  derived against.
- LEGACY REFERENCE: ACWR = ATL/CTL where both are EWMAs with k = 2/(N+1)
  (N=7/42) — roughly double the decay of the TrainingPeaks k = 1/N
  convention.

Usage:
    python scripts/acwr_shadow_report.py
    python scripts/acwr_shadow_report.py --days 60
    python scripts/acwr_shadow_report.py --history path/to/fitness_history.json
"""
import argparse
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

# Add project root to path so coach modules resolve when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from coach.fitness import calculate_fitness_metrics, _extract_total_loads

logger = logging.getLogger(__name__)

DEFAULT_HISTORY_PATH = Path(__file__).resolve().parent.parent / "data" / "fitness_history.json"

# Zones considered "safe to train normally" (matches snapshot semantics)
SAFE_ZONES = ("low", "optimal")

# Divergence heuristics for the recommendation block
MATERIAL_MEAN_ABS_DIFF = 0.15
MATERIAL_ZONE_MISMATCH_PCT = 25.0
MODERATE_MEAN_ABS_DIFF = 0.08
MODERATE_ZONE_MISMATCH_PCT = 10.0


def load_history(path: Path) -> dict[str, Any]:
    """Load fitness history READ-ONLY (no migration is written back)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except UnicodeDecodeError:
        # Legacy files may be cp1252-encoded (known data-layer issue)
        with open(path, encoding="cp1252") as f:
            return json.load(f)


def build_shadow_report(
    history: dict[str, Any],
    days: int = 90,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Compute EWMA-ACWR vs rolling-ACWR for the last `days` days.

    Pure function: takes an in-memory history dict, returns a report dict.
    Performs no I/O, so tests can run it on synthetic data.
    """
    if as_of is None:
        as_of = date.today()

    daily_loads = history.get("daily_loads", {})
    flat_loads = _extract_total_loads(daily_loads)
    load_dates = sorted(d for d, v in flat_loads.items() if v and v > 0)
    if not load_dates:
        return {"status": "no_data", "note": "fitness_history has no recorded loads"}

    first_load = date.fromisoformat(load_dates[0])
    start = max(as_of - timedelta(days=days - 1), first_load)

    rows = []
    current = start
    while current <= as_of:
        m = calculate_fitness_metrics(flat_loads, as_of_date=current)
        # Post-cutover key shapes: acwr/acwr_status = rolling PRIMARY,
        # acwr_ewma = legacy reference block.
        ewma_acwr = m["acwr_ewma"]["value"]
        ewma_zone = m["acwr_ewma"]["zone"]
        rolling_acwr = m["acwr"]
        rolling_zone = m["acwr_status"]
        rows.append({
            "date": current.isoformat(),
            "load": round(flat_loads.get(current.isoformat(), 0.0), 1),
            "ewma_acwr": ewma_acwr,
            "ewma_zone": ewma_zone,
            "rolling_acwr": rolling_acwr,
            "rolling_zone": rolling_zone,
            "abs_diff": round(abs(ewma_acwr - rolling_acwr), 2),
            "zone_mismatch": ewma_zone != rolling_zone,
        })
        current += timedelta(days=1)

    n = len(rows)
    mean_abs_diff = round(sum(r["abs_diff"] for r in rows) / n, 3)
    max_row = max(rows, key=lambda r: r["abs_diff"])
    zone_mismatch_days = sum(1 for r in rows if r["zone_mismatch"])
    zone_mismatch_pct = round(zone_mismatch_days / n * 100, 1)
    ewma_unsafe_only = sum(
        1 for r in rows
        if r["ewma_zone"] not in SAFE_ZONES and r["rolling_zone"] in SAFE_ZONES
    )
    rolling_unsafe_only = sum(
        1 for r in rows
        if r["rolling_zone"] not in SAFE_ZONES and r["ewma_zone"] in SAFE_ZONES
    )

    if mean_abs_diff > MATERIAL_MEAN_ABS_DIFF or zone_mismatch_pct > MATERIAL_ZONE_MISMATCH_PCT:
        divergence_level = "material"
    elif mean_abs_diff > MODERATE_MEAN_ABS_DIFF or zone_mismatch_pct > MODERATE_ZONE_MISMATCH_PCT:
        divergence_level = "moderate"
    else:
        divergence_level = "low"

    return {
        "status": "ok",
        "as_of": as_of.isoformat(),
        "window_start": start.isoformat(),
        "days_compared": n,
        "rows": rows,
        "stats": {
            "mean_abs_diff": mean_abs_diff,
            "max_divergence": {
                "date": max_row["date"],
                "ewma_acwr": max_row["ewma_acwr"],
                "rolling_acwr": max_row["rolling_acwr"],
                "abs_diff": max_row["abs_diff"],
            },
            "days_in_different_zones": zone_mismatch_days,
            "zone_mismatch_pct": zone_mismatch_pct,
            "ewma_unsafe_rolling_safe_days": ewma_unsafe_only,
            "rolling_unsafe_ewma_safe_days": rolling_unsafe_only,
            "divergence_level": divergence_level,
        },
    }


def format_report(report: dict[str, Any]) -> str:
    """Render the report dict as a printable ASCII table + stats block."""
    if report.get("status") != "ok":
        return f"No report: {report.get('note', report.get('status', 'unknown'))}"

    lines = []
    lines.append("=" * 78)
    lines.append("ACWR COMPARISON REPORT  (rolling 7d:28d PRIMARY vs legacy EWMA reference)")
    lines.append("Historical: cutover to the rolling primary completed 2026-06-10.")
    lines.append(f"Window: {report['window_start']} .. {report['as_of']}"
                 f"  ({report['days_compared']} days)")
    lines.append("READ-ONLY analysis - nothing was written.")
    lines.append("=" * 78)
    lines.append(f"{'date':<12}{'load':>7}{'ewma':>7}  {'zone':<9}"
                 f"{'rolling':>8}  {'zone':<9}{'diff':>6}  flag")
    lines.append("-" * 78)
    for r in report["rows"]:
        flag = "ZONE-MISMATCH" if r["zone_mismatch"] else ""
        lines.append(
            f"{r['date']:<12}{r['load']:>7.1f}{r['ewma_acwr']:>7.2f}  "
            f"{r['ewma_zone']:<9}{r['rolling_acwr']:>8.2f}  "
            f"{r['rolling_zone']:<9}{r['abs_diff']:>6.2f}  {flag}"
        )

    s = report["stats"]
    mx = s["max_divergence"]
    lines.append("-" * 78)
    lines.append("DIVERGENCE STATS")
    lines.append(f"  mean abs diff:            {s['mean_abs_diff']}")
    lines.append(f"  days in different zones:  {s['days_in_different_zones']}"
                 f" of {report['days_compared']} ({s['zone_mismatch_pct']}%)")
    lines.append(f"  max divergence:           {mx['abs_diff']} on {mx['date']}"
                 f" (ewma {mx['ewma_acwr']} vs rolling {mx['rolling_acwr']})")
    lines.append(f"  EWMA unsafe / rolling safe days:  {s['ewma_unsafe_rolling_safe_days']}"
                 "  (legacy model would have over-restricted)")
    lines.append(f"  rolling unsafe / EWMA safe days:  {s['rolling_unsafe_ewma_safe_days']}"
                 "  (spike risk the legacy model missed)")
    lines.append("-" * 78)
    lines.append("RECOMMENDATION")
    level = s["divergence_level"]
    if level == "material":
        lines.append("  MATERIAL divergence. The two models would have coached this athlete")
        lines.append("  differently on a meaningful share of days over this window.")
    elif level == "moderate":
        lines.append("  MODERATE divergence. Models mostly agree but differ around load")
        lines.append("  spikes/tapers over this window.")
    else:
        lines.append("  LOW divergence over this window. Models track closely.")
    lines.append("")
    lines.append("  HISTORICAL NOTE: the cutover to the rolling 7d:28d primary completed")
    lines.append("  on 2026-06-10 after owner review of the 90-day shadow window (mean")
    lines.append("  abs diff 0.264, 42% zone mismatch, May 11-13 danger window). The")
    lines.append("  EWMA column above is the legacy reference (acwr_ewma) only — load")
    lines.append("  decisions key off the rolling column. Historical snapshot entries")
    lines.append("  are recomputed by scripts/recompute_acwr_history.py (supervised).")
    lines.append("=" * 78)
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only ACWR comparison (rolling primary vs legacy EWMA reference).")
    parser.add_argument("--days", type=int, default=90,
                        help="Comparison window in days (default 90)")
    parser.add_argument("--history", type=Path, default=DEFAULT_HISTORY_PATH,
                        help="Path to fitness_history.json (read-only)")
    args = parser.parse_args()

    if not args.history.exists():
        print(f"fitness_history not found: {args.history}")
        return 1

    history = load_history(args.history)
    report = build_shadow_report(history, days=args.days)
    print(format_report(report))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main())
