"""Date semantics step one: the device clock beside the server clock."""
from datetime import datetime, timedelta, timezone

from coach.parsers import build_current_time_context, device_utc_offset_minutes


def test_offset_from_the_newest_activity():
    raw = [
        {'startTimeLocal': '2026-10-20 07:00:00', 'startTimeGMT': '2026-10-20 05:00:00'},
        {'startTimeLocal': '2026-10-25 18:30:00', 'startTimeGMT': '2026-10-25 09:30:00'},  # newest: +9 h
        {'startTimeLocal': None, 'startTimeGMT': '2026-10-26 09:30:00'},
        'garbage',
    ]
    assert device_utc_offset_minutes(raw) == 540
    assert device_utc_offset_minutes([]) is None
    assert device_utc_offset_minutes(None) is None


def test_context_without_offset_is_server_local():
    ctx = build_current_time_context(datetime(2026, 10, 26, 9, 15))
    assert ctx['source'] == 'server_local'
    assert ctx['garmin_date'] is None
    assert ctx['date_mismatch'] is False
    assert ctx['date'] == '2026-10-26'


def test_same_day_device_offset_has_no_mismatch():
    now = datetime(2026, 10, 26, 9, 15, tzinfo=timezone(timedelta(hours=2)))
    ctx = build_current_time_context(now, garmin_utc_offset_minutes=120)
    assert ctx['source'] == 'device_offset'
    assert ctx['garmin_date'] == '2026-10-26'
    assert ctx['date_mismatch'] is False


def test_device_on_another_calendar_day_is_a_mismatch():
    # Server (UTC+2) at 23:30 on the 26th; the watch is on UTC+9: 06:30 on the 27th
    now = datetime(2026, 10, 26, 23, 30, tzinfo=timezone(timedelta(hours=2)))
    ctx = build_current_time_context(now, garmin_utc_offset_minutes=540)
    assert ctx['date'] == '2026-10-26'
    assert ctx['garmin_date'] == '2026-10-27'
    assert ctx['date_mismatch'] is True
