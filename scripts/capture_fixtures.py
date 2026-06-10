"""
Capture REAL Garmin API responses into test_fixtures.json (OWNER-RUN ONLY).

Fetches one live response per endpoint the test suite exercises and writes
them to <repo root>/test_fixtures.json — which is GITIGNORED because even
after redaction it contains personal health data (sleep, HRV, weight,
training load). The committed, fully synthetic fallback lives at
tests/fixtures/garmin_sample.json; the test suite's `garmin_fixtures`
fixture (tests/conftest.py) loads the sample first and overlays this real
capture when present, so real-shape regressions are caught on the owner's
machine while clean checkouts still run every test.

Obvious PII is redacted before writing:
- names / display names / email-like fields  -> "REDACTED"
- profile / device / owner identifiers       -> 0
- GPS coordinates                            -> removed

Requires a valid Garmin session (.garth/ tokens or GARMIN_EMAIL +
GARMIN_PASSWORD in .env). If auth fails, run: python scripts/garmin_login.py

Usage (run it yourself — agents and CI must NEVER run this):
    python scripts/capture_fixtures.py
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from coach.garmin_client import garmin_api_call

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_PATH = REPO_ROOT / "test_fixtures.json"

# Keys whose VALUES are personal text — replaced with "REDACTED".
REDACT_TEXT_KEYS = {
    "fullname", "displayname", "username", "firstname", "lastname",
    "emailaddress", "ownerfullname", "ownerdisplayname", "location",
    "locationname",
}

# Keys whose VALUES are personal identifiers — replaced with 0.
REDACT_ID_KEYS = {
    "userprofileid", "userprofilepk", "profileid", "deviceid",
    "userdailysummaryid", "ownerid", "uuid", "samplepk",
}

# Keys removed entirely (GPS traces pinpoint the athlete's home).
DROP_KEYS = {
    "startlatitude", "startlongitude", "endlatitude", "endlongitude",
    "ownerprofileimageurllarge", "ownerprofileimageurlmedium",
    "ownerprofileimageurlsmall",
}


def redact(value):
    """Recursively redact obvious PII in a Garmin response (returns a copy)."""
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            lowered = key.lower()
            if lowered in DROP_KEYS:
                continue
            if lowered in REDACT_TEXT_KEYS:
                cleaned[key] = "REDACTED" if item is not None else None
            elif lowered in REDACT_ID_KEYS:
                cleaned[key] = 0 if item is not None else None
            else:
                cleaned[key] = redact(item)
        return cleaned
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


def main() -> None:
    today = date.today()
    today_iso = today.isoformat()
    week_ago_iso = (today - timedelta(days=7)).isoformat()
    month_ago_iso = (today - timedelta(days=30)).isoformat()

    # One representative call per endpoint shape the suite relies on.
    captures = {
        "user_summary": lambda c: c.get_user_summary(today_iso),
        "body_battery": lambda c: c.get_body_battery(today_iso),
        "training_readiness": lambda c: c.get_training_readiness(today_iso),
        "hrv": lambda c: c.get_hrv_data(today_iso),
        "sleep": lambda c: c.get_sleep_data(today_iso),
        "body_composition": lambda c: c.get_body_composition(
            month_ago_iso, today_iso),
        "user_profile": lambda c: c.get_user_profile(),
        "personal_records": lambda c: c.get_personal_record(),
        "activities": lambda c: c.get_activities_by_date(
            week_ago_iso, today_iso),
    }

    fixtures = {"test_date": today_iso}
    failures = {}
    for name, fetch in captures.items():
        try:
            print(f"Capturing {name}...")
            fixtures[name] = redact(garmin_api_call(fetch))
        except Exception as e:  # capture what we can, report the rest
            failures[name] = str(e)
            print(f"  FAILED: {e}")

    if len(failures) == len(captures):
        print("\nEvery capture failed — check Garmin auth "
              "(python scripts/garmin_login.py) and try again.")
        sys.exit(1)

    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(fixtures, f, indent=2)
        f.write("\n")

    print(f"\nWrote {len(fixtures) - 1} endpoint capture(s) to {OUTPUT_PATH}")
    if failures:
        print(f"Skipped (failed): {', '.join(sorted(failures))}")
        print("The committed sample (tests/fixtures/garmin_sample.json) "
              "still covers the missing keys.")
    print("Reminder: this file is gitignored — NEVER commit it; it still "
          "contains personal health data after redaction.")


if __name__ == "__main__":
    main()
