"""Athlete activity claims — coaching memory for "I ran this morning" when
Garmin cannot confirm it yet (watch not synced, Garmin unreachable).

A claim is never evidence of training: it counts in no compliance, adherence
or load figure. It is a promise to verify. The snapshot's ingest step and the
morning audit check the persisted history for a matching activity; a claim
still unverified after CLAIM_VERIFY_WINDOW_DAYS expires into an anomaly the
coach raises with the athlete.

Claims live in coaching_log.json under 'activity_claims' (ids
'claim:<date>:<type>', so the same claim is upserted, never duplicated).
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any

from .taxonomy import types_match

CLAIM_VERIFY_WINDOW_DAYS = 14
CLAIMS_KEY = 'activity_claims'


def _slug(value: Any) -> str:
    return re.sub(r'[^a-z0-9]+', '_', str(value).strip().lower()).strip('_')


def claim_id(date_iso: str, activity_type: str) -> str:
    return f"claim:{date_iso}:{_slug(activity_type)}"


def new_claim(*, date_iso: str, activity_type: str, duration_mins: float | None,
              note: str | None, today: date, garmin_state: str) -> dict[str, Any]:
    return {
        'id': claim_id(date_iso, activity_type),
        'date': date_iso,
        'activity_type': str(activity_type).strip().lower(),
        'duration_mins': duration_mins,
        'note': note,
        'created': today.isoformat(),
        'garmin_state_at_claim': garmin_state,   # 'present' | 'absent' | 'unavailable'
        'verified': None,                        # None pending, True confirmed, False expired
        'verified_on': None,
        'verified_by': None,
    }


def claims(log: dict[str, Any]) -> list[dict[str, Any]]:
    items = (log or {}).get(CLAIMS_KEY) or []
    return [c for c in items if isinstance(c, dict)]


def pending_claims(log: dict[str, Any]) -> list[dict[str, Any]]:
    return [c for c in claims(log) if c.get('verified') is None]


def claims_for_day(log: dict[str, Any], date_iso: str) -> list[dict[str, Any]]:
    return [c for c in claims(log) if c.get('date') == date_iso]


def pending_claim_types(log: dict[str, Any], date_iso: str) -> list[str]:
    return [c['activity_type'] for c in claims_for_day(log, date_iso)
            if c.get('verified') is None and c.get('activity_type')]


def upsert_claim(log: dict[str, Any], claim: dict[str, Any]) -> dict[str, Any]:
    """Insert or update by id; a claim Garmin already confirmed stays confirmed."""
    items = log.setdefault(CLAIMS_KEY, [])
    for i, existing in enumerate(items):
        if isinstance(existing, dict) and existing.get('id') == claim['id']:
            merged = {**existing, **claim}
            if existing.get('verified') is True:
                for key in ('verified', 'verified_on', 'verified_by'):
                    merged[key] = existing.get(key)
            items[i] = merged
            return merged
    items.append(claim)
    return claim


def activities_match_claim(claim: dict[str, Any], activities: list[dict[str, Any]] | None) -> bool:
    claimed = claim.get('activity_type') or ''
    return any(types_match(claimed, a.get('type', '')) or types_match(a.get('type', ''), claimed)
               for a in (activities or []) if isinstance(a, dict))


def verify_claims(log: dict[str, Any], daily_loads: dict[str, Any] | None,
                  today: date) -> list[dict[str, Any]]:
    """Verify pending claims against persisted daily_loads; expire stale ones.

    Mutates ``log`` in place and returns the claims whose state changed.
    """
    changed = []
    for claim in pending_claims(log):
        entry = (daily_loads or {}).get(claim.get('date'))
        activities = entry.get('activities') if isinstance(entry, dict) else None
        if activities and activities_match_claim(claim, activities):
            claim.update(verified=True, verified_on=today.isoformat(), verified_by='garmin')
            changed.append(claim)
            continue
        try:
            age_days = (today - date.fromisoformat(str(claim.get('date')))).days
        except (TypeError, ValueError):
            age_days = 0
        if age_days > CLAIM_VERIFY_WINDOW_DAYS:
            claim.update(verified=False, verified_on=today.isoformat(), verified_by='expired')
            changed.append(claim)
    return changed


def unverified_claims_view(log: dict[str, Any], today: date) -> list[dict[str, Any]]:
    """Pending claims for the snapshot core, newest first, with days_ago."""
    view = []
    for claim in pending_claims(log):
        try:
            days_ago = (today - date.fromisoformat(str(claim.get('date')))).days
        except (TypeError, ValueError):
            days_ago = None
        view.append({
            'date': claim.get('date'),
            'days_ago': days_ago,
            'activity_type': claim.get('activity_type'),
            'duration_mins': claim.get('duration_mins'),
            'note': claim.get('note'),
            'garmin_state_at_claim': claim.get('garmin_state_at_claim'),
            'status': 'unverified',
        })
    view.sort(key=lambda item: item.get('date') or '', reverse=True)
    return view


def claim_anomaly(claim: dict[str, Any]) -> dict[str, Any]:
    """Detected-anomaly dict for an expired claim (register_detected_anomalies shape)."""
    return {
        'flag': 'unverified_claim',
        'date': claim.get('date'),
        'activity_type': claim.get('activity_type'),
        'duration_mins': claim.get('duration_mins'),
    }
