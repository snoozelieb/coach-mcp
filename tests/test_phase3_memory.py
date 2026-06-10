"""Phase 3.2 — memory that matters.

Covers the three Lane B lifecycles:

1. ANOMALY PERSISTENCE (curiosity with memory): planned-vs-actual anomalies
   register once (idempotent by id) under coaching_log.json['anomalies'],
   only open/asked entries surface, resolve_anomaly() records the athlete's
   explanation, explanations survive re-detection, and resolved anomalies
   never re-register.
2. ADAPTATION PATTERN REGISTRY: record_athlete_response normalizes its
   pattern against config.ADAPTATION_PATTERN_REGISTRY (exact / case+space /
   fuzzy contains); unknown patterns are stored but flagged; counts
   aggregate by canonical key.
3. DECISION REVIEW LIFECYCLE: active decisions past their review_date
   auto-transition to needs_review on load (persisted + idempotent), the
   due-review view carries actual summaries, and update_decision_status
   moves needs_review decisions to active/completed/superseded.
"""
import json
import pytest
from datetime import date, timedelta

import coach.planner as planner
from coach.config import ADAPTATION_PATTERN_REGISTRY
from coach.tools.decision_tools import (
    ANOMALY_ACTIVE_STATUSES,
    anomaly_id_for,
    auto_transition_due_decisions,
    get_active_decisions,
    get_response_patterns,
    normalize_adaptation_pattern,
    record_athlete_response,
    register_detected_anomalies,
    resolve_anomaly,
    summarize_decisions_due_review,
    update_decision_status,
)

TODAY = date.today()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


@pytest.fixture
def memory_dir(data_dir, monkeypatch):
    """Redirect planner.DATA_DIR to tmp_path and seed an empty coaching log."""
    monkeypatch.setattr(planner, 'DATA_DIR', data_dir)
    (data_dir / 'coaching_log.json').write_text(json.dumps({
        'decisions': [],
        'pending_approvals': [],
        'athlete_responses': [],
        'metadata': {'created': '2026-01-01'},
    }))
    return data_dir


def _read_log(data_dir):
    return json.loads((data_dir / 'coaching_log.json').read_text())


def _missing_anomaly(day=YESTERDAY):
    return {'date': day, 'flag': 'missing',
            'planned_type': 'strength_training', 'planned_mins': 45}


def _mismatch_anomaly(day=YESTERDAY):
    return {'date': day, 'flag': 'type_mismatch',
            'planned_type': 'race', 'actual_type': 'cycling'}


# ---------------------------------------------------------------------------
# Anomaly identity
# ---------------------------------------------------------------------------

class TestAnomalyIdentity:
    def test_id_format_is_date_type_slug(self):
        aid = anomaly_id_for(_missing_anomaly('2026-06-09'))
        assert aid == '2026-06-09:missing:strength_training_45'

    def test_same_detection_same_id(self):
        assert anomaly_id_for(_missing_anomaly()) == anomaly_id_for(_missing_anomaly())

    def test_different_date_or_type_different_id(self):
        a = anomaly_id_for(_missing_anomaly('2026-06-08'))
        b = anomaly_id_for(_missing_anomaly('2026-06-09'))
        c = anomaly_id_for(_mismatch_anomaly('2026-06-09'))
        assert len({a, b, c}) == 3


# ---------------------------------------------------------------------------
# Anomaly registration: persist + dedupe + surface only open/asked
# ---------------------------------------------------------------------------

class TestAnomalyRegistration:
    def test_registers_open_entry_with_full_lifecycle_shape(self, memory_dir):
        register_detected_anomalies([_missing_anomaly()])

        registry = _read_log(memory_dir)['anomalies']
        assert len(registry) == 1
        entry = registry[0]
        assert entry['id'] == anomaly_id_for(_missing_anomaly())
        assert entry['date'] == YESTERDAY
        assert entry['type'] == 'missing'
        assert 'strength_training' in entry['summary']
        assert entry['status'] == 'open'
        assert entry['athlete_explanation'] is None
        assert entry['created'] == TODAY.isoformat()
        assert entry['updated'] == TODAY.isoformat()

    def test_re_registration_is_idempotent(self, memory_dir):
        """Back-to-back snapshots detect the same anomaly — one entry, ever."""
        first = register_detected_anomalies([_missing_anomaly()])
        second = register_detected_anomalies([_missing_anomaly()])

        registry = _read_log(memory_dir)['anomalies']
        assert len(registry) == 1
        assert len(first) == len(second) == 1

    def test_surfaced_view_merges_fresh_detection_detail(self, memory_dir):
        surfaced = register_detected_anomalies([_mismatch_anomaly()])

        view = surfaced[0]
        # Lifecycle fields from the registry
        assert view['id'] == anomaly_id_for(_mismatch_anomaly())
        assert view['status'] == 'open'
        assert view['summary']
        # Fresh detection detail preserved
        assert view['planned_type'] == 'race'
        assert view['actual_type'] == 'cycling'

    def test_open_anomalies_survive_when_no_longer_detected(self, memory_dir):
        """An unexplained anomaly must not vanish when the plan window rolls."""
        register_detected_anomalies([_missing_anomaly()])

        surfaced = register_detected_anomalies([])  # nothing detected today

        assert [a['id'] for a in surfaced] == [anomaly_id_for(_missing_anomaly())]

    def test_surfaces_only_open_and_asked(self, memory_dir):
        register_detected_anomalies([_missing_anomaly(), _mismatch_anomaly()])
        resolve_anomaly(anomaly_id_for(_missing_anomaly()),
                        'Gym was closed', status='resolved')

        surfaced = register_detected_anomalies([])

        assert [a['id'] for a in surfaced] == [anomaly_id_for(_mismatch_anomaly())]
        assert all(a['status'] in ANOMALY_ACTIVE_STATUSES for a in surfaced)


# ---------------------------------------------------------------------------
# Resolve flow
# ---------------------------------------------------------------------------

class TestResolveAnomaly:
    def test_asked_keeps_surfacing_with_explanation(self, memory_dir):
        register_detected_anomalies([_missing_anomaly()])
        aid = anomaly_id_for(_missing_anomaly())

        result = json.loads(resolve_anomaly(
            aid, 'Said he felt a niggle, will confirm tomorrow', status='asked'))
        assert result['status'] == 'updated'
        assert result['anomaly']['status'] == 'asked'

        surfaced = register_detected_anomalies([])
        assert surfaced[0]['id'] == aid
        assert surfaced[0]['athlete_explanation'] == (
            'Said he felt a niggle, will confirm tomorrow')

    def test_explanation_survives_re_detection(self, memory_dir):
        """Re-detecting an asked anomaly must keep the prior explanation."""
        register_detected_anomalies([_missing_anomaly()])
        aid = anomaly_id_for(_missing_anomaly())
        resolve_anomaly(aid, 'Work trip ate the evening', status='asked')

        surfaced = register_detected_anomalies([_missing_anomaly()])  # re-detected

        assert len(surfaced) == 1
        assert surfaced[0]['status'] == 'asked'
        assert surfaced[0]['athlete_explanation'] == 'Work trip ate the evening'
        # Still a single registry entry on disk
        assert len(_read_log(memory_dir)['anomalies']) == 1

    def test_resolved_never_resurfaces_or_re_registers(self, memory_dir):
        register_detected_anomalies([_missing_anomaly()])
        aid = anomaly_id_for(_missing_anomaly())
        resolve_anomaly(aid, 'Skipped — flu', status='resolved')

        # The same anomaly keeps being detected by the comparison window
        surfaced = register_detected_anomalies([_missing_anomaly()])

        assert surfaced == []
        registry = _read_log(memory_dir)['anomalies']
        assert len(registry) == 1
        assert registry[0]['status'] == 'resolved'
        assert registry[0]['athlete_explanation'] == 'Skipped — flu'

    def test_invalid_status_rejected(self, memory_dir):
        register_detected_anomalies([_missing_anomaly()])
        result = json.loads(resolve_anomaly(
            anomaly_id_for(_missing_anomaly()), 'x', status='closed'))
        assert 'error' in result
        assert 'Invalid status' in result['error']

    def test_unknown_id_lists_open_ids(self, memory_dir):
        register_detected_anomalies([_missing_anomaly()])
        result = json.loads(resolve_anomaly('nope:missing:x', 'whatever'))
        assert 'error' in result
        assert result['open_anomaly_ids'] == [anomaly_id_for(_missing_anomaly())]

    def test_empty_explanation_rejected(self, memory_dir):
        register_detected_anomalies([_missing_anomaly()])
        result = json.loads(resolve_anomaly(
            anomaly_id_for(_missing_anomaly()), '   '))
        assert 'error' in result


# ---------------------------------------------------------------------------
# Adaptation pattern normalization
# ---------------------------------------------------------------------------

class TestPatternNormalization:
    def test_exact_canonical_key(self):
        assert normalize_adaptation_pattern('handles_volume_well') == (
            'handles_volume_well', True)

    def test_case_and_space_tolerant(self):
        assert normalize_adaptation_pattern('  Handles Volume Well ') == (
            'handles_volume_well', True)
        assert normalize_adaptation_pattern('Recovers-Quickly') == (
            'recovers_quickly', True)

    def test_fuzzy_contains_match(self):
        canonical, recognized = normalize_adaptation_pattern(
            'recovers quickly after long rides')
        assert (canonical, recognized) == ('recovers_quickly', True)

    def test_ambiguous_match_is_unrecognized(self):
        # 'volume' appears in handles_volume_well AND struggles_with_volume
        canonical, recognized = normalize_adaptation_pattern('volume')
        assert recognized is False
        assert canonical == 'volume'

    def test_unknown_pattern_returns_normalized_unrecognized(self):
        canonical, recognized = normalize_adaptation_pattern('Likes Purple Bikes')
        assert (canonical, recognized) == ('likes_purple_bikes', False)

    def test_empty_and_none(self):
        assert normalize_adaptation_pattern(None) == (None, False)
        assert normalize_adaptation_pattern('   ') == (None, False)


class TestRecordResponseNormalization:
    def test_variant_stored_under_canonical_key(self, memory_dir):
        result = json.loads(record_athlete_response(
            'big week', 'felt strong', pattern='Handles Volume Well'))

        assert result['pattern'] == 'handles_volume_well'
        assert 'unrecognized_pattern' not in result
        stored = _read_log(memory_dir)['athlete_responses'][-1]
        assert stored['pattern'] == 'handles_volume_well'

    def test_unknown_pattern_stored_but_flagged(self, memory_dir):
        result = json.loads(record_athlete_response(
            'big week', 'meh', pattern='Likes Purple Bikes'))

        assert result['unrecognized_pattern'] is True
        assert result['known_patterns'] == sorted(ADAPTATION_PATTERN_REGISTRY)
        # Still stored (normalized) — unknown data is never thrown away
        stored = _read_log(memory_dir)['athlete_responses'][-1]
        assert stored['pattern'] == 'likes_purple_bikes'

    def test_get_response_patterns_counts_by_canonical_key(self, memory_dir):
        record_athlete_response('s1', 'r1', pattern='handles_volume_well')
        record_athlete_response('s2', 'r2', pattern='Handles  Volume Well!')
        record_athlete_response('s3', 'r3', pattern='unknown_thing')

        result = json.loads(get_response_patterns())

        assert result['patterns']['handles_volume_well']['count'] == 2
        assert result['patterns']['handles_volume_well']['recognized'] is True
        assert result['patterns']['unknown_thing']['recognized'] is False
        assert result['canonical_registry'] == ADAPTATION_PATTERN_REGISTRY


# ---------------------------------------------------------------------------
# Decision review lifecycle
# ---------------------------------------------------------------------------

def _seed_decision(memory_dir, decision_id, status='active', review_date=None,
                   logged_date=None, decision_text='cut volume 10%'):
    log = _read_log(memory_dir)
    log['decisions'].append({
        'id': decision_id,
        'date': logged_date or TODAY.isoformat(),
        'type': 'load_adjustment',
        'decision': decision_text,
        'rationale': 'because data',
        'status': status,
        'outcome': None,
        'review_date': review_date,
    })
    (memory_dir / 'coaching_log.json').write_text(json.dumps(log))


class TestNeedsReviewAutoTransition:
    def test_overdue_decision_transitions_and_persists(self, memory_dir):
        _seed_decision(memory_dir, 'd_old', review_date=YESTERDAY)

        result = json.loads(get_active_decisions())

        assert result['count'] == 0
        assert result['needs_review'][0]['id'] == 'd_old'
        assert 'd_old' in result['due_for_review']
        assert result['auto_transitioned_to_needs_review'] == ['d_old']
        # Persisted to disk
        on_disk = _read_log(memory_dir)['decisions'][0]
        assert on_disk['status'] == 'needs_review'
        assert on_disk['needs_review_since'] == TODAY.isoformat()

    def test_transition_is_idempotent(self, memory_dir):
        _seed_decision(memory_dir, 'd_old', review_date=YESTERDAY)

        get_active_decisions()
        first_disk = _read_log(memory_dir)['decisions'][0]
        second = json.loads(get_active_decisions())

        assert 'auto_transitioned_to_needs_review' not in second
        assert _read_log(memory_dir)['decisions'][0] == first_disk

    def test_review_date_today_stays_active_but_is_due(self, memory_dir):
        """Transition is strictly review_date < today; due-today stays active."""
        _seed_decision(memory_dir, 'd_today', review_date=TODAY.isoformat())

        result = json.loads(get_active_decisions())

        assert result['count'] == 1
        assert 'd_today' in result['due_for_review']
        assert _read_log(memory_dir)['decisions'][0]['status'] == 'active'

    def test_future_review_date_untouched(self, memory_dir):
        future = (TODAY + timedelta(days=5)).isoformat()
        _seed_decision(memory_dir, 'd_future', review_date=future)

        result = json.loads(get_active_decisions())

        assert result['count'] == 1
        assert result['due_for_review'] == []

    def test_auto_transition_helper_returns_log_and_transitioned(self, memory_dir):
        _seed_decision(memory_dir, 'd_old', review_date=YESTERDAY)
        _seed_decision(memory_dir, 'd_ok',
                       review_date=(TODAY + timedelta(days=3)).isoformat())

        log, transitioned = auto_transition_due_decisions(TODAY)

        assert [d['id'] for d in transitioned] == ['d_old']
        statuses = {d['id']: d['status'] for d in log['decisions']}
        assert statuses == {'d_old': 'needs_review', 'd_ok': 'active'}


class TestNeedsReviewResolution:
    @pytest.mark.parametrize('target', ['completed', 'superseded'])
    def test_needs_review_to_terminal_status(self, memory_dir, target):
        _seed_decision(memory_dir, 'd_old', status='needs_review',
                       review_date=YESTERDAY)

        result = json.loads(update_decision_status('d_old', target, 'discussed'))

        assert result['new_status'] == target
        assert _read_log(memory_dir)['decisions'][0]['status'] == target

    def test_needs_review_back_to_active_rolls_review_date(self, memory_dir):
        """Reactivation must not bounce straight back to needs_review."""
        _seed_decision(memory_dir, 'd_old', status='needs_review',
                       review_date=YESTERDAY)

        result = json.loads(update_decision_status('d_old', 'active'))
        assert result['new_status'] == 'active'
        assert result['review_date'] > TODAY.isoformat()

        # Loading again keeps it active (no immediate re-transition)
        reloaded = json.loads(get_active_decisions())
        assert reloaded['count'] == 1
        assert reloaded['needs_review'] == []

    def test_needs_review_is_a_valid_manual_status(self, memory_dir):
        _seed_decision(memory_dir, 'd1', status='active',
                       review_date=(TODAY + timedelta(days=7)).isoformat())
        result = json.loads(update_decision_status('d1', 'needs_review'))
        assert result['new_status'] == 'needs_review'


class TestDueReviewSummaries:
    def test_summaries_carry_id_excerpt_review_date(self, memory_dir):
        decisions = [
            {'id': 'd_nr', 'status': 'needs_review', 'decision': 'x' * 300,
             'review_date': YESTERDAY, 'date': YESTERDAY},
            {'id': 'd_active_due', 'status': 'active', 'decision': 'due today',
             'review_date': TODAY.isoformat(), 'date': TODAY.isoformat()},
            {'id': 'd_legacy', 'status': 'active', 'decision': 'no review date',
             'review_date': None,
             'date': (TODAY - timedelta(days=10)).isoformat()},
            {'id': 'd_fresh', 'status': 'active', 'decision': 'not due',
             'review_date': (TODAY + timedelta(days=5)).isoformat(),
             'date': TODAY.isoformat()},
            {'id': 'd_done', 'status': 'completed', 'decision': 'done',
             'review_date': YESTERDAY, 'date': YESTERDAY},
        ]

        summaries = summarize_decisions_due_review(decisions, TODAY)

        assert {s['id'] for s in summaries} == {'d_nr', 'd_active_due', 'd_legacy'}
        by_id = {s['id'] for s in summaries}
        assert 'd_fresh' not in by_id and 'd_done' not in by_id
        nr = next(s for s in summaries if s['id'] == 'd_nr')
        assert len(nr['decision']) == 120          # excerpt, not the full text
        assert nr['review_date'] == YESTERDAY
        assert nr['status'] == 'needs_review'

    def test_empty_and_none_inputs(self):
        assert summarize_decisions_due_review([], TODAY) == []
        assert summarize_decisions_due_review(None, TODAY) == []
