"""Date-anchoring + matcher-honesty regression tests.

Pins the fixes for the REAL coaching failure of 2026-06-11 06:27:

1. CROSSWIRE (defect 1): on 2026-06-10 the plan was [mobility 30', padel 90'];
   the athlete skipped padel and instead did a 34.5' mobility session (Garmin
   type 'other', name "Cape Town Mobility") plus an unplanned hard 39.9' ride
   (avg HR 152, load 172.8). The old matcher paired mobility<->cycling and
   padel<->other, fabricating two type_mismatch + two duration_delta
   anomalies. Required: mobility=completed, padel=MISSING, ride=UNPLANNED.
2. TODAY IS PENDING (defect 2): at 06:27 the snapshot had already emitted
   "2026-06-11:missing:cycling_75" / "...:missing:strength_10" persistent
   anomalies and put today in plan_adherence.skipped_dates. Required: today's
   planned sessions are pending until the day is over; registration never
   persists a missing-anomaly for the current date; wrongly-registered open
   today-missing entries are cleaned up.
3. TEMPORAL SELF-ANCHORING (defect 3): the coach, anchored on yesterday in a
   long conversation, combined the above into "padel was today". Required:
   days_ago on week_grid entries and anomalies, week_grid_today,
   planned_vs_actual.as_of, and anomaly summaries that embed the absolute
   date plus a relative phrase ("2026-06-10 (yesterday): ...").

All date logic is driven by an explicitly threaded `today` (clock
discipline) — no wall-clock reads in these tests.
"""
import json
from datetime import date

import pytest

from coach.taxonomy import (
    is_known_type,
    is_mobility_by_name,
    types_match_with_name,
)
from coach.tools.coaching_tools import (
    _build_week_grid,
    _compare_planned_actual,
    _summarize_plan_adherence_by_pillar,
)
from coach.tools.decision_tools import (
    anomaly_id_for,
    register_detected_anomalies,
    relative_day_phrase,
)

TODAY = date(2026, 6, 11)  # the morning the failure happened (a Thursday)
PADEL_DAY = '2026-06-10'

JUNE_PLAN = {
    'week_start': '2026-06-08',
    'week_end': '2026-06-14',
    'days': {
        PADEL_DAY: {'planned': [
            {'type': 'mobility', 'duration_mins': 30, 'purpose': 'Hip mobility'},
            {'type': 'padel', 'duration_mins': 90, 'purpose': 'Weekly match'},
        ]},
        TODAY.isoformat(): {'planned': [
            {'type': 'cycling', 'duration_mins': 75, 'purpose': 'Z2 base'},
            {'type': 'strength', 'duration_mins': 10, 'purpose': 'Core finisher'},
        ]},
    },
}

JUNE_ACTUALS = [
    {'date': PADEL_DAY, 'type': 'other', 'name': 'Cape Town Mobility',
     'duration_mins': 34.5, 'avg_hr': 88, 'load': 18.4},
    {'date': PADEL_DAY, 'type': 'cycling', 'name': 'Lunch Ride',
     'duration_mins': 39.9, 'avg_hr': 152, 'load': 172.8},
]


def _flags(result, flag):
    return [a for a in result['anomalies'] if a['flag'] == flag]


@pytest.fixture
def memory_dir(sandbox_data_dir):
    """Empty coaching log in the per-test sandbox DATA_DIR."""
    (sandbox_data_dir / 'coaching_log.json').write_text(json.dumps({
        'decisions': [],
        'pending_approvals': [],
        'athlete_responses': [],
        'metadata': {'created': '2026-01-01'},
    }), encoding='utf-8')
    return sandbox_data_dir


def _read_anomaly_registry(data_dir):
    log = json.loads((data_dir / 'coaching_log.json').read_text(encoding='utf-8'))
    return log.get('anomalies', [])


# ---------------------------------------------------------------------------
# Defect 1 — the exact June 10/11 scenario (no crosswire)
# ---------------------------------------------------------------------------

class TestJune10Regression:
    def test_padel_missing_cycling_unplanned_mobility_completed(self):
        result = _compare_planned_actual(JUNE_PLAN, JUNE_ACTUALS, TODAY)

        # The crosswire is dead: no fabricated type_mismatch pairs
        assert _flags(result, 'type_mismatch') == []

        # Padel is honestly MISSING
        missing = _flags(result, 'missing')
        assert len(missing) == 1
        assert missing[0]['planned_type'] == 'padel'
        assert missing[0]['planned_mins'] == 90
        assert missing[0]['date'] == PADEL_DAY

        # The hard ride is honestly UNPLANNED, with its load visible
        unplanned = _flags(result, 'unplanned')
        assert len(unplanned) == 1
        assert unplanned[0]['activity_type'] == 'cycling'
        assert unplanned[0]['duration_mins'] == 39.9
        assert unplanned[0]['load'] == 172.8

        # Mobility paired with the 'other'-typed session by name hint:
        # 30 planned vs 34.5 actual is within tolerance — no anomaly
        assert _flags(result, 'duration_delta') == []
        assert {'date': PADEL_DAY, 'status': 'matched'} in result['details']
        assert result['sessions_missed'] == 1

    def test_completed_counts_only_real_matches(self):
        result = _compare_planned_actual(JUNE_PLAN, JUNE_ACTUALS, TODAY)
        # mobility completed; padel missed; today's two sessions pending
        assert result['sessions_planned'] == 4
        assert result['sessions_completed'] == 1
        assert result['sessions_missed'] == 1
        assert result['sessions_pending'] == 2

    def test_anomalies_carry_days_ago(self):
        result = _compare_planned_actual(JUNE_PLAN, JUNE_ACTUALS, TODAY)
        assert result['anomalies'], 'regression scenario must produce anomalies'
        for anomaly in result['anomalies']:
            assert anomaly['date'] == PADEL_DAY
            assert anomaly['days_ago'] == 1

    def test_closest_duration_wins_among_type_matches(self):
        plan = {'days': {PADEL_DAY: {'planned': {
            'type': 'cycling', 'duration_mins': 60}}}}
        actuals = [
            {'date': PADEL_DAY, 'type': 'cycling', 'duration_mins': 25},
            {'date': PADEL_DAY, 'type': 'cycling', 'duration_mins': 58},
        ]
        result = _compare_planned_actual(plan, actuals, TODAY)
        paired = [d for d in result['details'] if d.get('status') == 'matched']
        assert paired and paired[0]['duration_actual'] == 58
        unplanned = _flags(result, 'unplanned')
        assert len(unplanned) == 1 and unplanned[0]['duration_mins'] == 25

    def test_unknown_plan_type_substitute_still_surfaces_mismatch(self):
        """The narrow exception survives: planned 'race' (unknown to the
        taxonomy) + exactly one actual = a plausible substitute to ask about."""
        assert not is_known_type('race')
        plan = {'days': {PADEL_DAY: {'planned': {
            'type': 'race', 'duration_mins': 240}}}}
        actuals = [{'date': PADEL_DAY, 'type': 'cycling', 'duration_mins': 235}]
        result = _compare_planned_actual(plan, actuals, TODAY)
        mismatches = _flags(result, 'type_mismatch')
        assert len(mismatches) == 1
        assert mismatches[0]['planned_type'] == 'race'
        assert mismatches[0]['actual_type'] == 'cycling'
        assert _flags(result, 'missing') == []
        assert _flags(result, 'unplanned') == []


# ---------------------------------------------------------------------------
# Defect 1 — taxonomy name-hint fallback
# ---------------------------------------------------------------------------

class TestMobilityNameHint:
    @pytest.mark.parametrize('plan_type,garmin_type,name,expected', [
        ('mobility', 'other', 'Cape Town Mobility', True),
        ('mobility', 'other', 'Post-ride Stretch', True),
        ('mobility', 'other', 'Evening Workout', False),
        ('mobility', 'other', None, False),
        ('mobility', 'yoga', None, True),          # plain types_match
        ('padel', 'other', 'Cape Town Mobility', False),
        ('padel', 'paddelball', None, True),        # plain types_match
        ('cycling', 'other', 'Mobility-ish ride', False),
    ])
    def test_types_match_with_name(self, plan_type, garmin_type, name, expected):
        assert types_match_with_name(plan_type, garmin_type, name) is expected

    def test_known_types_never_hijacked_by_name(self):
        # A known Garmin type always speaks for itself
        assert is_mobility_by_name('walking', 'Mobility walk') is False
        assert is_mobility_by_name('other', 'Morning yoga flow') is True


# ---------------------------------------------------------------------------
# Defect 2 — today is PENDING, not missing
# ---------------------------------------------------------------------------

class TestTodayIsPending:
    def test_six_am_snapshot_today_sessions_pending(self):
        """No activities logged yet today -> pending, zero missing anomalies."""
        result = _compare_planned_actual(JUNE_PLAN, JUNE_ACTUALS, TODAY)
        today_iso = TODAY.isoformat()
        assert all(a['date'] != today_iso for a in result['anomalies'])
        pending = [d for d in result['details']
                   if d.get('status') == 'pending' and d['date'] == today_iso]
        assert {d['planned'] for d in pending} == {'cycling', 'strength'}

    def test_today_completed_still_counts(self):
        """A session already done today is matched, not left pending."""
        actuals = JUNE_ACTUALS + [
            {'date': TODAY.isoformat(), 'type': 'cycling', 'name': 'Dawn Z2',
             'duration_mins': 74, 'load': 60.0},
        ]
        result = _compare_planned_actual(JUNE_PLAN, actuals, TODAY)
        assert result['sessions_completed'] == 2
        assert result['sessions_pending'] == 1  # strength still pending
        assert all(a['date'] != TODAY.isoformat() for a in result['anomalies'])

    def test_yesterday_genuinely_missed_is_still_missing(self):
        plan = {'days': {PADEL_DAY: {'planned': {
            'type': 'strength', 'duration_mins': 45}}}}
        result = _compare_planned_actual(plan, [], TODAY)
        missing = _flags(result, 'missing')
        assert len(missing) == 1 and missing[0]['date'] == PADEL_DAY

    def test_plan_adherence_today_pending_not_skipped(self):
        adherence = _summarize_plan_adherence_by_pillar(
            JUNE_PLAN, JUNE_ACTUALS, TODAY)
        today_iso = TODAY.isoformat()
        for pillar in ('strength', 'mobility', 'long_effort'):
            assert today_iso not in adherence[pillar]['skipped_dates']
        assert adherence['strength']['pending_dates'] == [today_iso]
        assert adherence['long_effort']['pending_dates'] == [today_iso]
        # Mobility on June 10 completed via the name hint, not skipped
        assert adherence['mobility']['completed'] == 1
        assert adherence['mobility']['skipped_dates'] == []


class TestTodayPendingRegistration:
    MISSING_TODAY = {'date': TODAY.isoformat(), 'flag': 'missing',
                     'planned_type': 'cycling', 'planned_mins': 75}
    MISSING_YESTERDAY = {'date': PADEL_DAY, 'flag': 'missing',
                         'planned_type': 'padel', 'planned_mins': 90}

    def test_missing_today_never_registers(self, memory_dir):
        surfaced = register_detected_anomalies([self.MISSING_TODAY], today=TODAY)
        assert surfaced == []
        assert _read_anomaly_registry(memory_dir) == []

    def test_missing_yesterday_registers_with_stable_id(self, memory_dir):
        surfaced = register_detected_anomalies([self.MISSING_YESTERDAY],
                                               today=TODAY)
        assert len(surfaced) == 1
        # Id format <date>:<type>:<slug> is unchanged
        assert surfaced[0]['id'] == '2026-06-10:missing:padel_90'
        assert anomaly_id_for(self.MISSING_YESTERDAY) == surfaced[0]['id']

    def test_cleanup_drops_wrongly_registered_today_missing(self, memory_dir):
        """The live log contained open '2026-06-11:missing:*' entries written
        at 06:27 — they must be dropped (and may re-register tomorrow)."""
        seeded = [
            {'id': '2026-06-11:missing:cycling_75', 'date': '2026-06-11',
             'type': 'missing', 'summary': 'x', 'status': 'open',
             'athlete_explanation': None,
             'created': '2026-06-11', 'updated': '2026-06-11'},
            {'id': '2026-06-11:missing:strength_10', 'date': '2026-06-11',
             'type': 'missing', 'summary': 'x', 'status': 'open',
             'athlete_explanation': None,
             'created': '2026-06-11', 'updated': '2026-06-11'},
            # A real past miss must survive the cleanup
            {'id': '2026-06-09:missing:yoga_30', 'date': '2026-06-09',
             'type': 'missing', 'summary': 'x', 'status': 'open',
             'athlete_explanation': None,
             'created': '2026-06-10', 'updated': '2026-06-10'},
            # An 'asked' today-missing was already discussed — keep it
            {'id': '2026-06-11:missing:swim_30', 'date': '2026-06-11',
             'type': 'missing', 'summary': 'x', 'status': 'asked',
             'athlete_explanation': 'pool closed',
             'created': '2026-06-11', 'updated': '2026-06-11'},
        ]
        log = json.loads((memory_dir / 'coaching_log.json').read_text(
            encoding='utf-8'))
        log['anomalies'] = seeded
        (memory_dir / 'coaching_log.json').write_text(json.dumps(log),
                                                      encoding='utf-8')

        surfaced = register_detected_anomalies([], today=TODAY)

        registry_ids = {e['id'] for e in _read_anomaly_registry(memory_dir)}
        assert '2026-06-11:missing:cycling_75' not in registry_ids
        assert '2026-06-11:missing:strength_10' not in registry_ids
        assert '2026-06-09:missing:yoga_30' in registry_ids
        assert '2026-06-11:missing:swim_30' in registry_ids
        surfaced_ids = [a['id'] for a in surfaced]
        assert '2026-06-11:missing:cycling_75' not in surfaced_ids
        assert '2026-06-09:missing:yoga_30' in surfaced_ids

    def test_dropped_today_missing_can_re_register_tomorrow(self, memory_dir):
        register_detected_anomalies([self.MISSING_TODAY], today=TODAY)
        assert _read_anomaly_registry(memory_dir) == []
        # Next morning the session genuinely never happened
        tomorrow = date(2026, 6, 12)
        surfaced = register_detected_anomalies([self.MISSING_TODAY],
                                               today=tomorrow)
        assert [a['id'] for a in surfaced] == ['2026-06-11:missing:cycling_75']

    def test_unplanned_today_still_registers(self, memory_dir):
        """Only missing is gated — an activity that HAPPENED today is real."""
        unplanned = {'date': TODAY.isoformat(), 'flag': 'unplanned',
                     'activity_type': 'cycling', 'duration_mins': 39.9,
                     'load': 172.8}
        surfaced = register_detected_anomalies([unplanned], today=TODAY)
        assert len(surfaced) == 1
        assert surfaced[0]['days_ago'] == 0


# ---------------------------------------------------------------------------
# Defect 3 — temporal self-anchoring of the payload
# ---------------------------------------------------------------------------

class TestRelativeDayPhrase:
    @pytest.mark.parametrize('iso,expected', [
        ('2026-06-11', 'today'),
        ('2026-06-10', 'yesterday'),
        ('2026-06-09', '2 days ago'),
        ('2026-06-04', '7 days ago'),
        ('2026-06-12', 'tomorrow'),
        ('2026-06-13', 'in 2 days'),
    ])
    def test_phrases(self, iso, expected):
        assert relative_day_phrase(iso, TODAY) == expected

    def test_garbage_returns_none(self):
        assert relative_day_phrase('not-a-date', TODAY) is None
        assert relative_day_phrase(None, TODAY) is None


class TestWeekGridAnchoring:
    def test_days_ago_zero_is_today(self):
        grid = _build_week_grid([], TODAY)
        assert grid[TODAY.isoformat()]['days_ago'] == 0
        assert grid[TODAY.isoformat()]['is_today'] is True
        assert grid[PADEL_DAY]['days_ago'] == 1
        for day_iso, entry in grid.items():
            assert entry['days_ago'] == (TODAY - date.fromisoformat(day_iso)).days


class TestComparisonAnchoring:
    def test_as_of_present(self):
        result = _compare_planned_actual(JUNE_PLAN, JUNE_ACTUALS, TODAY)
        assert result['as_of'] == TODAY.isoformat()

    def test_as_of_present_even_without_plan(self):
        result = _compare_planned_actual(None, [], TODAY)
        assert result['status'] == 'no_plan'
        assert result['as_of'] == TODAY.isoformat()


class TestAnomalySummaryAnchoring:
    def test_summary_embeds_date_and_relative_phrase(self, memory_dir):
        detected = _compare_planned_actual(JUNE_PLAN, JUNE_ACTUALS,
                                           TODAY)['anomalies']
        surfaced = register_detected_anomalies(detected, today=TODAY)

        assert surfaced, 'June 10 anomalies must surface'
        for view in surfaced:
            assert view['summary'].startswith('2026-06-10 (yesterday): ')
            assert view['days_ago'] == 1
        summaries = ' | '.join(a['summary'] for a in surfaced)
        assert 'padel' in summaries
        assert 'cycling' in summaries
        assert '172.8' in summaries  # the unplanned ride's load is visible

    def test_relative_phrase_recomputed_each_snapshot(self, memory_dir):
        detected = [{'date': PADEL_DAY, 'flag': 'missing',
                     'planned_type': 'padel', 'planned_mins': 90}]
        first = register_detected_anomalies(detected, today=TODAY)
        assert first[0]['summary'].startswith('2026-06-10 (yesterday):')

        # Two days later the same open anomaly is re-detected: the absolute
        # date is unchanged but the phrase must move on
        later = register_detected_anomalies(detected, today=date(2026, 6, 13))
        assert later[0]['summary'].startswith('2026-06-10 (3 days ago):')
        assert later[0]['days_ago'] == 3
