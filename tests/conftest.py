"""Shared pytest fixtures and sample data for coach-mcp test suite."""
import hashlib
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Live-data sandbox guard (Phase 4.4)
#
# Two layers, both autouse, so tests can NEVER touch the live coaching data
# in <repo>/data/:
#
# 1. live_data_guard (session): hashes every file under data/ at session
#    start and fails LOUDLY at session end, naming the files, if anything
#    changed. This is the tripwire — it catches any write that escapes the
#    sandbox, whatever the path.
# 2. sandbox_data_dir (function): redirects DATA_DIR in every loaded
#    coach.* module (the binding is discovered dynamically, so new modules
#    are covered automatically) to a per-test empty tmp dir. Tests that
#    monkeypatch DATA_DIR themselves still win: autouse fixtures run first,
#    so the test's own setattr lands on top.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
LIVE_DATA_DIR = REPO_ROOT / 'data'


def _hash_live_data_files() -> dict:
    """{relative_path: sha256} for every file under the live data/ dir."""
    if not LIVE_DATA_DIR.is_dir():
        return {}
    return {
        path.relative_to(LIVE_DATA_DIR).as_posix():
            hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(LIVE_DATA_DIR.rglob('*'))
        if path.is_file()
    }


@pytest.fixture(scope='session', autouse=True)
def live_data_guard():
    """Fail the session loudly if any live data/ file changed during tests."""
    before = _hash_live_data_files()
    yield
    after = _hash_live_data_files()
    if after == before:
        return
    created = sorted(set(after) - set(before))
    deleted = sorted(set(before) - set(after))
    modified = sorted(
        name for name in set(before) & set(after)
        if before[name] != after[name]
    )
    pytest.fail(
        'LIVE COACHING DATA WAS TOUCHED BY THE TEST SUITE.\n'
        f'  data dir:  {LIVE_DATA_DIR}\n'
        f'  created:   {created or "-"}\n'
        f'  deleted:   {deleted or "-"}\n'
        f'  modified:  {modified or "-"}\n'
        'A test wrote through to the real DATA_DIR. Find the write path and '
        'route it through the sandbox (see sandbox_data_dir in conftest.py). '
        'Restore the files from git/backup before re-running.'
    )


@pytest.fixture(autouse=True)
def sandbox_data_dir(tmp_path, monkeypatch):
    """Redirect DATA_DIR in every coach.* module to a per-test empty tmp dir.

    The module list is built dynamically: any loaded coach module that binds
    a DATA_DIR attribute (via `from .config import DATA_DIR` or otherwise)
    gets patched, including coach.config itself — so modules imported later
    in the test also resolve to the sandbox. Tests that monkeypatch DATA_DIR
    themselves simply override this (their setattr applies after).

    Yields the sandbox path so tests can seed files into it directly.
    """
    sandbox = tmp_path / 'data'
    sandbox.mkdir(exist_ok=True)
    for name, module in list(sys.modules.items()):
        if name != 'coach' and not name.startswith('coach.'):
            continue
        if module is not None and hasattr(module, 'DATA_DIR'):
            monkeypatch.setattr(module, 'DATA_DIR', sandbox)
    # The snapshot's Garmin fetch cache keys embed str(DATA_DIR); clear it
    # anyway so no test ever sees another test's cached fetches.
    coaching = sys.modules.get('coach.tools.coaching_tools')
    if coaching is not None and hasattr(coaching, '_garmin_fetch_cache'):
        coaching._garmin_fetch_cache.clear()
    yield sandbox


# ---------------------------------------------------------------------------
# Sample data constants (importable by any test file via: from conftest import X)
# ---------------------------------------------------------------------------

SAMPLE_RUNNING_ACTIVITY = {
    'activityId': 12345678901,
    'activityName': 'Morning Run',
    'startTimeLocal': '2025-12-01T06:30:00.0',
    'activityType': {
        'typeId': 1,
        'typeKey': 'running',
        'parentTypeId': 17,
    },
    'duration': 2700,
    'distance': 8000,
    'averageHR': 145,
    'maxHR': 168,
    'calories': 520,
}

SAMPLE_STRENGTH_ACTIVITY = {
    'activityId': 12345678902,
    'activityName': 'Strength Training',
    'startTimeLocal': '2025-12-02T17:00:00.0',
    'activityType': {
        'typeId': 13,
        'typeKey': 'strength_training',
        'parentTypeId': 29,
    },
    'duration': 3600,
    'distance': None,
    'averageHR': 110,
    'maxHR': 135,
    'calories': 380,
}

SAMPLE_PR_DATA = {
    'personalRecords': [
        {
            'prTypeLabelKey': 'pr_running_fastest_5k_time',
            'value': 1320,
            'unitKey': 'time',
            'prStartTimeGmtFormatted': '2025-06-15T08:30:00.0',
            'activityId': 11111111111,
        },
        {
            'prTypeLabelKey': 'pr_running_fastest_10k_time',
            'value': 2820,
            'unitKey': 'time',
            'prStartTimeGmtFormatted': '2025-09-22T07:00:00.0',
            'activityId': 22222222222,
        },
        {
            'prTypeLabelKey': 'pr_running_longest_distance',
            'value': 21100,
            'unitKey': 'meter',
            'prStartTimeGmtFormatted': '2025-10-10T06:00:00.0',
            'activityId': 33333333333,
        },
    ]
}

SAMPLE_TRAINING_READINESS = {
    'calendarDate': '2025-12-01',
    'score': 72,
    'level': 'HIGH',
    'sleepScore': 85,
    'recoveryTimeInHours': 12,
    'hrvStatus': 'BALANCED',
    'acuteLoad': 450.5,
    'feedbackPhrase': 'Your body is well recovered and ready for a hard workout.',
}

SAMPLE_PARSED_ACTIVITIES = [
    {'date': '2025-11-25', 'type': 'running', 'duration_mins': 45.0},
    {'date': '2025-11-26', 'type': 'strength_training', 'duration_mins': 60.0},
    {'date': '2025-11-28', 'type': 'running', 'duration_mins': 30.0},
    {'date': '2025-12-01', 'type': 'running', 'duration_mins': 60.0},
    {'date': '2025-12-02', 'type': 'cycling', 'duration_mins': 90.0},
    {'date': '2025-12-03', 'type': 'strength_training', 'duration_mins': 45.0},
    {'date': '2025-12-05', 'type': 'running', 'duration_mins': 75.0},
]


# ---------------------------------------------------------------------------
# Garmin response shape builders (synthetic values, REAL response shapes)
#
# These mirror the exact list-vs-dict shapes the live Garmin API returns —
# the fidelity matters because parsers branch on it (e.g. training readiness
# is a LIST of dicts, sleep is a dict wrapping 'dailySleepDTO' with epoch-ms
# timestamps). Used by FakeGarminClient defaults and importable by any test.
# ---------------------------------------------------------------------------

def local_iso_to_epoch_ms(iso_str: str) -> int:
    """Garmin '...TimestampLocal' fields: epoch-ms offset so that reading
    them as UTC yields local wall-clock time (see parsers.epoch_ms_to_local_iso)."""
    dt = datetime.fromisoformat(iso_str)
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


def make_garmin_activity(day, type_key='cycling', activity_id=None,
                         duration_secs=3600, distance_m=20000.0, avg_hr=128,
                         max_hr=152, load=55.0, name='Synthetic Session',
                         event_type='training', **extra):
    """One raw activity as returned inside get_activities_by_date()'s list."""
    day_iso = day.isoformat() if isinstance(day, date) else str(day)
    activity = {
        'activityId': activity_id or int(day_iso.replace('-', '')) * 100,
        'activityName': name,
        'startTimeLocal': f'{day_iso} 08:00:00',
        'activityType': {'typeId': 2, 'typeKey': type_key, 'parentTypeId': 17},
        'eventType': {'typeKey': event_type},
        'duration': duration_secs,
        'movingDuration': duration_secs * 0.95,
        'distance': distance_m,
        'elevationGain': 120.0,
        'elevationLoss': 118.0,
        'averageHR': avg_hr,
        'maxHR': max_hr,
        'calories': 600,
        'activityTrainingLoad': load,
        'aerobicTrainingEffect': 2.8,
        'anaerobicTrainingEffect': 0.4,
    }
    activity.update(extra)
    return activity


def make_sleep_payload(day, score=82, duration_hrs=7.5, avg_hr=48,
                       bedtime='22:30:00', wake='06:00:00'):
    """get_sleep_data() response: {'dailySleepDTO': {...}} with epoch-ms
    '...TimestampLocal' fields (the live endpoint's current shape)."""
    day = day if isinstance(day, date) else date.fromisoformat(str(day))
    secs = int(duration_hrs * 3600)
    prev = day - timedelta(days=1)
    return {
        'dailySleepDTO': {
            'id': local_iso_to_epoch_ms(f'{prev.isoformat()}T{bedtime}'),
            'calendarDate': day.isoformat(),
            'sleepTimeSeconds': secs,
            'napTimeSeconds': 0,
            'sleepStartTimestampLocal': local_iso_to_epoch_ms(
                f'{prev.isoformat()}T{bedtime}'),
            'sleepEndTimestampLocal': local_iso_to_epoch_ms(
                f'{day.isoformat()}T{wake}'),
            'deepSleepSeconds': int(secs * 0.20),
            'lightSleepSeconds': int(secs * 0.50),
            'remSleepSeconds': int(secs * 0.22),
            'awakeSleepSeconds': int(secs * 0.08),
            'awakeCount': 2,
            'avgSleepStress': 14.0,
            'avgHeartRate': avg_hr,
            'averageRespirationValue': 14.2,
            'sleepScores': {
                'overall': {'value': score, 'qualifierKey': 'GOOD'},
                'deepPercentage': {'qualifierKey': 'GOOD'},
                'remPercentage': {'qualifierKey': 'GOOD'},
                'stress': {'qualifierKey': 'GOOD'},
                'restlessness': {'qualifierKey': 'GOOD'},
            },
            'sleepNeed': {'actual': 480, 'baseline': 450,
                          'feedback': 'INCREASED',
                          'trainingFeedback': 'CHRONIC'},
        },
        'restingHeartRate': avg_hr + 2,
    }


def make_training_readiness(day, score=72, level='HIGH', sleep_score=85):
    """get_training_readiness() response — a LIST of dicts."""
    day_iso = day.isoformat() if isinstance(day, date) else str(day)
    return [{
        'userProfilePK': 0,
        'calendarDate': day_iso,
        'timestamp': f'{day_iso}T06:10:00.0',
        'deviceId': 0,
        'score': score,
        'level': level,
        'sleepScore': sleep_score,
        'sleepScoreFactorPercent': 70,
        'recoveryTime': 720,
        'recoveryTimeInHours': 12,
        'recoveryTimeFactorPercent': 60,
        'acwrFactorPercent': 80,
        'acuteLoad': 410,
        'hrvFactorPercent': 90,
        'hrvStatus': None,  # Garmin commonly returns null here; HRV endpoint has it
        'stressHistoryFactorPercent': 85,
        'feedbackShort': 'READY_FOR_ANYTHING',
        'feedbackPhrase': 'Synthetic readiness — well recovered.',
    }]


def make_hrv_payload(day, status='BALANCED', last_night=58, weekly=55):
    """get_hrv_data() response: {'hrvSummary': {...}, 'hrvReadings': [...]}."""
    day_iso = day.isoformat() if isinstance(day, date) else str(day)
    return {
        'userProfilePk': 0,
        'hrvSummary': {
            'calendarDate': day_iso,
            'weeklyAvg': weekly,
            'lastNightAvg': last_night,
            'lastNight5MinHigh': last_night + 14,
            'baseline': {
                'lowUpper': 45,
                'balancedLow': 50,
                'balancedUpper': 70,
                'markerValue': last_night,
            },
            'status': status,
            'feedbackPhrase': 'Your HRV is within your baseline range.',
            'createTimeStamp': f'{day_iso}T06:10:00.0',
        },
        'hrvReadings': [
            {'hrvValue': last_night - 3,
             'readingTimeLocal': f'{day_iso}T02:00:00.0'},
            {'hrvValue': last_night + 3,
             'readingTimeLocal': f'{day_iso}T04:00:00.0'},
        ],
        'sleepStartTimestampLocal': None,
        'sleepEndTimestampLocal': None,
    }


def make_body_battery(day, values=(35, 52, 70, 64)):
    """get_body_battery() response — a LIST of per-day dicts whose
    bodyBatteryValuesArray holds [epoch_ms, value] pairs."""
    day = day if isinstance(day, date) else date.fromisoformat(str(day))
    base_ms = local_iso_to_epoch_ms(f'{day.isoformat()}T06:00:00')
    return [{
        'date': day.isoformat(),
        'charged': 45,
        'drained': 28,
        'startTimestampGMT': f'{day.isoformat()}T00:00:00.0',
        'endTimestampGMT': f'{day.isoformat()}T23:59:59.0',
        'startTimestampLocal': f'{day.isoformat()}T02:00:00.0',
        'endTimestampLocal': f'{(day + timedelta(days=1)).isoformat()}T01:59:59.0',
        'bodyBatteryValuesArray': [
            [base_ms + i * 3600_000, v] for i, v in enumerate(values)
        ],
        'bodyBatteryValueDescriptorDTOList': [
            {'bodyBatteryValueDescriptorIndex': 0,
             'bodyBatteryValueDescriptorKey': 'timestamp'},
            {'bodyBatteryValueDescriptorIndex': 1,
             'bodyBatteryValueDescriptorKey': 'bodyBatteryLevel'},
        ],
    }]


def make_user_summary(day, resting_hr=52):
    """get_user_summary() response (realistic subset — note: NO sleepScore;
    the live endpoint does not carry it, readiness does)."""
    day_iso = day.isoformat() if isinstance(day, date) else str(day)
    return {
        'userProfileId': 0,
        'calendarDate': day_iso,
        'totalKilocalories': 2600.0,
        'activeKilocalories': 700.0,
        'bmrKilocalories': 1900.0,
        'totalSteps': 9500,
        'dailyStepGoal': 8000,
        'totalDistanceMeters': 7600,
        'minHeartRate': resting_hr - 2,
        'maxHeartRate': 142,
        'restingHeartRate': resting_hr,
        'lastSevenDaysAvgRestingHeartRate': resting_hr + 1,
        'averageStressLevel': 28,
        'maxStressLevel': 86,
        'stressQualifier': 'BALANCED',
        'bodyBatteryChargedValue': 45,
        'bodyBatteryDrainedValue': 28,
        'bodyBatteryHighestValue': 70,
        'bodyBatteryLowestValue': 30,
        'bodyBatteryMostRecentValue': 64,
        'averageSpo2': 95.0,
        'avgWakingRespirationValue': 14.0,
        'sleepingSeconds': 27000,
        'moderateIntensityMinutes': 35,
        'vigorousIntensityMinutes': 20,
        'intensityMinutesGoal': 150,
        'floorsAscended': 8.0,
        'floorsDescended': 7.0,
        'includesWellnessData': True,
        'includesActivityData': True,
        'privacyProtected': False,
        'source': 'GARMIN',
    }


def make_body_composition(day, weight_grams=70000.0):
    """get_body_composition() response — weights in GRAMS."""
    day_iso = day.isoformat() if isinstance(day, date) else str(day)
    return {
        'startDate': day_iso,
        'endDate': day_iso,
        'dateWeightList': [{
            'samplePk': 1,
            'date': local_iso_to_epoch_ms(f'{day_iso}T07:00:00'),
            'calendarDate': day_iso,
            'weight': weight_grams,
            'bmi': 22.9,
            'bodyFat': None,
            'sourceType': 'MANUAL',
        }],
        'totalAverage': {
            'from': local_iso_to_epoch_ms(f'{day_iso}T00:00:00'),
            'until': local_iso_to_epoch_ms(f'{day_iso}T23:59:59'),
            'weight': weight_grams,
            'bmi': 22.9,
            'bodyFat': None,
            'bodyWater': None,
            'boneMass': None,
            'muscleMass': None,
        },
    }


def make_user_profile(birth_date='1990-01-15', max_hr=188):
    """get_user_profile() response (userData wrapper)."""
    return {
        'id': 0,
        'userData': {
            'gender': 'MALE',
            'weight': 70000.0,
            'height': 178.0,
            'birthDate': birth_date,
            'maxHeartRate': max_hr,
            'measurementSystem': 'metric',
            'handedness': 'RIGHT',
            'vo2MaxRunning': 48.0,
            'vo2MaxCycling': 50.0,
        },
        'userSleep': {'sleepTime': 81000, 'wakeTime': 21600},
    }


def make_personal_records():
    """get_personal_record() response — a LIST of record dicts."""
    return [
        {
            'id': 1,
            'prTypeLabelKey': 'pr_running_fastest_5k_time',
            'typeId': 3,
            'value': 1500.0,
            'unitKey': 'time',
            'prStartTimeGmtFormatted': '2025-06-15T08:30:00.0',
            'activityId': 11111111111,
        },
        {
            'id': 2,
            'prTypeLabelKey': 'pr_running_longest_distance',
            'typeId': 7,
            'value': 21100.0,
            'unitKey': 'meter',
            'prStartTimeGmtFormatted': '2025-10-10T06:00:00.0',
            'activityId': 22222222222,
        },
    ]


def make_hr_in_timezones():
    """get_activity_hr_in_timezones() response — a LIST of zone dicts."""
    return [
        {'zoneNumber': 1, 'secsInZone': 600.0, 'zoneLowBoundary': 95},
        {'zoneNumber': 2, 'secsInZone': 2200.0, 'zoneLowBoundary': 118},
        {'zoneNumber': 3, 'secsInZone': 500.0, 'zoneLowBoundary': 140},
        {'zoneNumber': 4, 'secsInZone': 80.0, 'zoneLowBoundary': 156},
        {'zoneNumber': 5, 'secsInZone': 20.0, 'zoneLowBoundary': 172},
    ]


def make_hr_zones():
    """/biometric-service/heartRateZones response — a LIST of sport configs."""
    return [{
        'sport': 'DEFAULT',
        'trainingMethod': 'HR_RESERVE',
        'restingHrAutoUpdateUsed': True,
        'maxHeartRateUsed': 188,
        'restingHrUsed': 52,
        'lactateThresholdHeartRateUsed': 165,
        'zone1Floor': 95,
        'zone2Floor': 118,
        'zone3Floor': 140,
        'zone4Floor': 156,
        'zone5Floor': 172,
    }]


def make_exercise_sets(activity_id):
    """get_activity_exercise_sets() response."""
    return {
        'activityId': activity_id,
        'exerciseSets': [
            {
                'exercises': [{'category': 'SQUAT',
                               'name': 'BARBELL_BACK_SQUAT',
                               'probability': 100.0}],
                'duration': 52.0,
                'repetitionCount': 8,
                'weight': 60000.0,
                'setType': 'ACTIVE',
                'startTime': '2026-01-15T17:05:00.0',
            },
            {
                'exercises': [],
                'duration': 90.0,
                'repetitionCount': None,
                'weight': None,
                'setType': 'REST',
                'startTime': '2026-01-15T17:06:00.0',
            },
        ],
    }


def make_activity_splits(activity_id):
    """get_activity_splits() response."""
    return {
        'activityId': activity_id,
        'lapDTOs': [
            {'lapIndex': 1, 'distance': 1000.0, 'duration': 330.0,
             'movingDuration': 328.0, 'averageHR': 142, 'maxHR': 150,
             'averageSpeed': 3.03, 'startTimeGMT': '2026-01-15T06:30:00.0'},
            {'lapIndex': 2, 'distance': 1000.0, 'duration': 325.0,
             'movingDuration': 324.0, 'averageHR': 148, 'maxHR': 155,
             'averageSpeed': 3.08, 'startTimeGMT': '2026-01-15T06:35:30.0'},
        ],
    }


# ---------------------------------------------------------------------------
# Canonical FakeGarminClient
# ---------------------------------------------------------------------------

class _FakeTransport:
    """The Garmin().client transport attribute (raw post/delete/connectapi)."""

    def __init__(self, owner):
        self._owner = owner

    def post(self, subdomain, url, json=None, **kwargs):
        self._owner._log('client.post', (subdomain, url), {'json': json, **kwargs})
        if '/workout-service/schedule/' in url:
            workout_id = int(url.rstrip('/').rsplit('/', 1)[-1])
            self._owner.scheduled.append(
                (workout_id, (json or {}).get('date')))
            return {'workoutScheduleId': 7000 + len(self._owner.scheduled)}
        return {}

    def delete(self, subdomain, url, **kwargs):
        self._owner._log('client.delete', (subdomain, url), kwargs)
        if '/workout-service/workout/' in url:
            workout_id = int(url.rstrip('/').rsplit('/', 1)[-1])
            self._owner.deleted_workout_ids.append(workout_id)
            self._owner.workout_library = [
                w for w in self._owner.workout_library
                if w.get('workoutId') != workout_id
            ]
        return None

    def connectapi(self, path, **kwargs):
        self._owner._log('client.connectapi', (path,), kwargs)
        return self._owner._connectapi(path)

    def dump(self, token_dir):
        self._owner._log('client.dump', (token_dir,), {})

    def load(self, token_dir):
        self._owner._log('client.load', (token_dir,), {})


class FakeGarminClient:
    """Canonical fake Garmin client for the whole suite (Phase 4).

    One fake, realistic response SHAPES for every garminconnect endpoint the
    codebase calls. List-vs-dict fidelity is deliberate and load-bearing:

    - get_training_readiness  -> LIST of dicts
    - get_body_battery        -> LIST of per-day dicts with bodyBatteryValuesArray
    - get_sleep_data          -> dict {'dailySleepDTO': {...}} with epoch-ms
                                 '...TimestampLocal' ints
    - get_hrv_data            -> dict {'hrvSummary': {...}}
    - get_activities_by_date  -> LIST of activities with nested
                                 activityType.typeKey / eventType.typeKey
    - get_personal_record     -> LIST of record dicts
    - upload_*_workout        -> dict {'workoutId': int}

    All seed values are synthetic ("Test Athlete", 70 kg) — never real
    personal data.

    Args:
        today: anchor date for generated defaults (default date.today()).
        activities: raw activity list served by get_activities_by_date
            (filtered to the requested range). Defaults to a 3-day
            cycling/strength/cycling mix ending today.
        overrides: {method_name: value} per-endpoint behaviour override.
            value may be a plain response (returned as-is), an Exception
            instance/class (raised), or a callable (invoked with the call's
            args). e.g. {'get_training_readiness': Exception('down')}.

    Call log (for assertions):
        calls               list of (method, args, kwargs), in order
        call_counts         {method_name: count}
        uploaded            list of (kind, workout) from upload_* calls
        scheduled           list of (workout_id, date_iso) from schedule POSTs
        deleted_workout_ids list of ids deleted via the transport DELETE
        workout_library     current get_workouts() view (uploads append here)
    """

    def __init__(self, today=None, activities=None, overrides=None):
        self.today = today or date.today()
        self.activities = activities if activities is not None else [
            make_garmin_activity(self.today, 'cycling'),
            make_garmin_activity(self.today - timedelta(days=1),
                                 'strength_training', distance_m=None,
                                 duration_secs=2700, load=35.0),
            make_garmin_activity(self.today - timedelta(days=2), 'cycling'),
        ]
        self.overrides = dict(overrides or {})
        self.calls = []
        self.call_counts = defaultdict(int)
        self.uploaded = []
        self.scheduled = []
        self.deleted_workout_ids = []
        self.workout_library = []
        self._next_workout_id = 90001

        # Plain attributes the codebase reads off Garmin instances
        self.display_name = 'test-athlete-uuid'
        self.full_name = 'Test Athlete'
        self.unit_system = 'metric'
        self.garmin_workouts_schedule_url = '/workout-service/schedule'
        self.garmin_connect_user_settings_url = (
            '/userprofile-service/userprofile/user-settings')
        self.client = _FakeTransport(self)

    # -- plumbing ----------------------------------------------------------

    def _log(self, method, args, kwargs):
        self.calls.append((method, args, kwargs))
        self.call_counts[method] += 1

    def total_calls(self):
        return sum(self.call_counts.values())

    def _respond(self, method, default, *args, **kwargs):
        self._log(method, args, kwargs)
        if method in self.overrides:
            value = self.overrides[method]
            if isinstance(value, BaseException):
                raise value
            if isinstance(value, type) and issubclass(value, BaseException):
                raise value()
            if callable(value):
                return value(*args, **kwargs)
            return value
        return default(*args, **kwargs)

    def _connectapi(self, path):
        if 'heartRateZones' in path:
            return make_hr_zones()
        if 'socialProfile' in path:
            return {'id': 0, 'displayName': 'test-athlete-uuid',
                    'fullName': 'Test Athlete'}
        return {}

    # -- Garmin API surface (one method per endpoint the codebase calls) ----

    def get_activities_by_date(self, start, end):
        return self._respond(
            'get_activities_by_date',
            lambda s, e: [a for a in self.activities
                          if s <= a['startTimeLocal'][:10] <= e],
            start, end)

    def get_activity_splits(self, activity_id):
        return self._respond('get_activity_splits',
                             make_activity_splits, activity_id)

    def get_activity_exercise_sets(self, activity_id):
        return self._respond('get_activity_exercise_sets',
                             make_exercise_sets, activity_id)

    def get_activity_hr_in_timezones(self, activity_id):
        return self._respond('get_activity_hr_in_timezones',
                             lambda aid: make_hr_in_timezones(), activity_id)

    def get_personal_record(self):
        return self._respond('get_personal_record', make_personal_records)

    def get_user_summary(self, for_date):
        return self._respond('get_user_summary', make_user_summary, for_date)

    def get_body_battery(self, for_date):
        return self._respond('get_body_battery', make_body_battery, for_date)

    def get_training_readiness(self, for_date):
        return self._respond('get_training_readiness',
                             make_training_readiness, for_date)

    def get_hrv_data(self, for_date):
        return self._respond('get_hrv_data', make_hrv_payload, for_date)

    def get_sleep_data(self, for_date):
        return self._respond('get_sleep_data', make_sleep_payload, for_date)

    def get_user_profile(self):
        return self._respond('get_user_profile', make_user_profile)

    def get_full_name(self):
        return self._respond('get_full_name', lambda: 'Test Athlete')

    def get_body_composition(self, start, end=None):
        return self._respond('get_body_composition',
                             lambda s, e=None: make_body_composition(e or s),
                             start, end)

    def get_workouts(self):
        return self._respond('get_workouts', lambda: list(self.workout_library))

    def _upload(self, kind, workout):
        workout_id = self._next_workout_id
        self._next_workout_id += 1
        self.uploaded.append((kind, workout))
        if isinstance(workout, dict):
            name = workout.get('workoutName', 'Workout')
        else:
            name = getattr(workout, 'workoutName', 'Workout')
        self.workout_library.append(
            {'workoutId': workout_id, 'workoutName': name})
        return {'workoutId': workout_id, 'workoutName': name}

    def upload_workout(self, workout):
        return self._respond('upload_workout',
                             lambda w: self._upload('generic', w), workout)

    def upload_cycling_workout(self, workout):
        return self._respond('upload_cycling_workout',
                             lambda w: self._upload('cycling', w), workout)

    def upload_running_workout(self, workout):
        return self._respond('upload_running_workout',
                             lambda w: self._upload('running', w), workout)

    def connectapi(self, path, **kwargs):
        return self._respond('connectapi', self._connectapi, path)


def patch_garmin_everywhere(monkeypatch, client):
    """Route every garmin_api_call in the codebase through `client`.

    Patches the name in each module that imported it (patching the source
    module alone would miss the `from ..garmin_client import garmin_api_call`
    copies). Returns the fake call for further use.
    """
    import coach.garmin_client as garmin_client_mod
    import coach.fitness as fitness_mod
    import coach.tools.athlete_tools as athlete_mod
    import coach.tools.coaching_tools as coaching_mod
    import coach.tools.data_tools as data_mod
    import coach.tools.fitness_tools as fitness_tools_mod
    import coach.tools.planning_tools as planning_mod
    import coach.tools.strength_tools as strength_mod

    fake_call = lambda fn, *args, **kwargs: fn(client, *args, **kwargs)
    for mod in (garmin_client_mod, fitness_mod, athlete_mod, coaching_mod,
                data_mod, fitness_tools_mod, planning_mod, strength_mod):
        monkeypatch.setattr(mod, 'garmin_api_call', fake_call)
    return fake_call


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _MockContext:
    """Lightweight mock for fastmcp.Context used in direct tool calls."""
    def __init__(self):
        self.report_progress = AsyncMock()
        self.info = AsyncMock()
        self.debug = AsyncMock()
        self.warning = AsyncMock()
        self.error = AsyncMock()
        self.log = AsyncMock()


@pytest.fixture
def mock_ctx():
    """Provide a mock fastmcp Context for async tool tests."""
    return _MockContext()


@pytest.fixture
def fake_garmin():
    """A default canonical FakeGarminClient (synthetic, realistic shapes)."""
    return FakeGarminClient()


@pytest.fixture(scope="session")
def garmin_fixtures():
    """Garmin API fixture data (session-scoped, loaded once).

    Base layer: the committed sanitized sample (tests/fixtures/
    garmin_sample.json — synthetic values, real response shapes), so a clean
    checkout runs every fixture-driven test instead of skipping.

    Overlay: when the gitignored real capture exists at the project root
    (test_fixtures.json, written by scripts/capture_fixtures.py), its keys
    take precedence — real responses are preferred wherever captured.
    """
    sample_path = Path(__file__).parent / "fixtures" / "garmin_sample.json"
    with open(sample_path, encoding="utf-8") as f:
        data = json.load(f)

    real_path = Path(__file__).parent.parent / "test_fixtures.json"
    if real_path.exists():
        with open(real_path, encoding="utf-8") as f:
            data.update(json.load(f))
    return data


@pytest.fixture
def data_dir(tmp_path):
    """Provide a temp directory for tool tests that do file I/O."""
    return tmp_path


@pytest.fixture
def sample_athlete():
    """Minimal athlete profile for tool tests."""
    return {
        'personal': {
            'name': 'Test Athlete',
            'age': 30,
            'max_hr': 190,
            'resting_hr': 45,
            'weight_kg': 75,
        },
        'injury_history': [],
        'life_constraints': {},
        'preferences': {},
    }
