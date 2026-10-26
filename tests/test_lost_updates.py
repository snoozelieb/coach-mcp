"""Lost-update protection for the files several server processes share.

Claude Desktop runs two coach-mcp processes, Claude Code a third, the 06:30
audit a fourth. Every "load, mutate in memory, save the whole file" path let
the slowest one overwrite the others' writes — the athlete saw a 5.6 h day
vanish from fitness_history. These tests pin the merge semantics.
"""
import json
from datetime import date, timedelta


import coach.planner as planner
import coach.fitness as fitness_mod
import coach.tools.decision_tools as decision_mod
from coach.planner import NoChange

TODAY = date.today()


def _day(offset):
    return (TODAY - timedelta(days=offset)).isoformat()


# ---------------------------------------------------------------------------
# coaching_log: structural merge
# ---------------------------------------------------------------------------

class TestCoachingLogMerge:
    def test_stale_copy_cannot_drop_entries_added_meanwhile(self):
        planner.save_coaching_log({'decisions': [{'id': 'a', 'status': 'active'}]})
        stale = planner.load_coaching_log()           # "another process" loaded here
        planner.save_coaching_log({'decisions': [{'id': 'b', 'status': 'active'}]})
        stale['decisions'][0]['status'] = 'completed'  # the stale copy edits a
        planner.save_coaching_log(stale)

        log = planner.load_coaching_log()
        by_id = {d['id']: d for d in log['decisions']}
        assert set(by_id) == {'a', 'b'}
        assert by_id['a']['status'] == 'completed'

    def test_anomalies_and_proposals_upsert_by_id_responses_union(self):
        planner.save_coaching_log({
            'anomalies': [{'id': 'x', 'status': 'open'}],
            'pending_approvals': [{'id': 'p1', 'proposal': 'one'}],
            'athlete_responses': [{'date': _day(1), 'stimulus': 's', 'response': 'r'}],
            'auto_proposal_tags': {'t1': 'p1'},
        })
        planner.save_coaching_log({
            'anomalies': [{'id': 'x', 'status': 'resolved'}, {'id': 'y', 'status': 'open'}],
            'pending_approvals': [{'id': 'p2', 'proposal': 'two'}],
            'athlete_responses': [{'date': _day(1), 'stimulus': 's', 'response': 'r'},
                                  {'date': _day(2), 'stimulus': 's2', 'response': 'r2'}],
            'auto_proposal_tags': {'t2': 'p2'},
        })
        log = planner.load_coaching_log()
        assert {a['id']: a['status'] for a in log['anomalies']} == {'x': 'resolved', 'y': 'open'}
        assert {p['id'] for p in log['pending_approvals']} == {'p1', 'p2'}
        assert len(log['athlete_responses']) == 2
        assert log['auto_proposal_tags'] == {'t1': 'p1', 't2': 'p2'}

    def test_update_coaching_log_removes_under_lock(self):
        planner.save_coaching_log({'pending_approvals': [{'id': 'p1'}, {'id': 'p2'}]})

        def drop_p1(log):
            log['pending_approvals'] = [p for p in log['pending_approvals'] if p['id'] != 'p1']

        planner.update_coaching_log(drop_p1)
        assert [p['id'] for p in planner.load_coaching_log()['pending_approvals']] == ['p2']

    def test_nochange_leaves_file_untouched(self):
        planner.save_coaching_log({'decisions': [{'id': 'a'}]})
        before = planner.load_coaching_log()['metadata']['last_updated']

        def abort(log):
            log['decisions'] = []
            raise NoChange()

        planner.update_coaching_log(abort)
        after = planner.load_coaching_log()
        assert [d['id'] for d in after['decisions']] == ['a']
        assert after['metadata']['last_updated'] == before

    def test_approve_removes_only_its_proposal(self):
        planner.save_coaching_log({'pending_approvals': [
            {'id': 'p1', 'proposal': 'one', 'rationale': 'r1', 'action_type': 'load_adjustment'},
            {'id': 'p2', 'proposal': 'two', 'rationale': 'r2', 'action_type': 'load_adjustment'},
        ]})
        result = json.loads(decision_mod.approve_proposal('p1'))
        assert result['status'] == 'approved'
        log = planner.load_coaching_log()
        assert [p['id'] for p in log['pending_approvals']] == ['p2']
        assert log['decisions'][-1]['approved_from'] == 'p1'
        # unknown id: no write, clear error
        assert 'error' in json.loads(decision_mod.approve_proposal('nope'))
        assert [p['id'] for p in planner.load_coaching_log()['pending_approvals']] == ['p2']

    def test_reject_keeps_a_record_and_the_other_proposals(self):
        planner.save_coaching_log({'pending_approvals': [
            {'id': 'p1', 'proposal': 'one', 'rationale': 'r1'},
            {'id': 'p2', 'proposal': 'two', 'rationale': 'r2'},
        ]})
        result = json.loads(decision_mod.reject_proposal('p1', reason='no'))
        assert result['status'] == 'rejected'
        log = planner.load_coaching_log()
        assert [p['id'] for p in log['pending_approvals']] == ['p2']
        assert log['rejected_proposals'][0]['id'] == 'p1'


# ---------------------------------------------------------------------------
# weekly_plan: one lock, carry-over, per-day merges, prune-after-archive
# ---------------------------------------------------------------------------

class TestWeeklyPlanMerges:
    def test_resend_without_audit_state_keeps_it(self):
        d1 = _day(1)
        planner.save_weekly_plan({'days': {d1: {'planned': {'type': 'running', 'purpose': 'x'}}}},
                                 today=TODAY)
        planner.merge_plan_days({d1: {'status': 'completed', 'actual': [{'type': 'running'}]}})
        planner.merge_plan_fields({'pushed_workout_ids': [11, 12]})

        # The coach resends the plan rebuilt from get_weekly_plan, minus the verdict
        planner.save_weekly_plan({'days': {d1: {'planned': {'type': 'running', 'purpose': 'y'}}}},
                                 today=TODAY)

        plan = planner.get_current_plan()
        assert plan['days'][d1]['planned']['purpose'] == 'y'
        assert plan['days'][d1]['status'] == 'completed'
        assert plan['days'][d1]['actual'] == [{'type': 'running'}]
        assert plan['pushed_workout_ids'] == [11, 12]

    def test_merge_plan_days_skips_days_no_longer_in_plan(self):
        d1, d2 = _day(1), _day(2)
        planner.save_weekly_plan({'days': {d1: {'planned': None}}}, today=TODAY)
        planner.merge_plan_days({d2: {'status': 'missed'}, d1: {'status': 'rest_taken'}})
        plan = planner.get_current_plan()
        assert set(plan['days']) == {d1}
        assert plan['days'][d1]['status'] == 'rest_taken'

    def test_prune_keeps_days_when_the_archive_write_fails(self, monkeypatch):
        old = _day(planner.PLAN_RETENTION_DAYS + 5)
        d1 = _day(1)
        real_save = planner.save_json_file

        def failing_save(filename, data):
            if filename == planner.PLAN_HISTORY_FILE:
                raise OSError('disk full')
            return real_save(filename, data)

        monkeypatch.setattr(planner, 'save_json_file', failing_save)
        planner.save_weekly_plan({'days': {old: {'planned': None, 'status': 'rest_taken'},
                                           d1: {'planned': None}}}, today=TODAY)
        plan = planner.get_current_plan()
        assert old in plan['days'], 'a failed archive must not drop the day'

    def test_prune_archives_then_drops(self):
        old = _day(planner.PLAN_RETENTION_DAYS + 5)
        planner.save_weekly_plan({'days': {old: {'planned': None, 'status': 'rest_taken'},
                                           _day(1): {'planned': None}}}, today=TODAY)
        plan = planner.get_current_plan()
        assert old not in plan['days']
        archive = planner.load_json_file(planner.PLAN_HISTORY_FILE)
        assert [e['date'] for e in archive['archived_days']] == [old]


# ---------------------------------------------------------------------------
# fitness_history: merges instead of whole-file saves
# ---------------------------------------------------------------------------

def _activity(day_offset, activity_id, load=50.0):
    return {'date': _day(day_offset), 'activity_id': activity_id, 'type': 'cycling',
            'duration_mins': 60.0, 'garmin_training_load': load}


class TestFitnessHistoryMerges:
    def test_two_ingests_of_different_days_both_survive(self):
        fitness_mod.update_fitness_history([_activity(1, 'a1')], TODAY)
        fitness_mod.update_fitness_history([_activity(2, 'a2')], TODAY)
        loads = fitness_mod.load_fitness_history()['daily_loads']
        assert set(loads) == {_day(1), _day(2)}

    def test_recovery_merge_keeps_a_day_ingested_meanwhile(self):
        fitness_mod.update_fitness_history([_activity(1, 'a1')], TODAY)
        stale = fitness_mod.load_fitness_history()      # the snapshot's step-0 copy
        fitness_mod.update_fitness_history([_activity(5, 'a5')], TODAY)  # another process

        # The snapshot persists its readiness via the merge, not via save(stale)
        fitness_mod.merge_recovery_records(
            readiness_record={'date': _day(0), 'score': 70, 'level': 'HIGH',
                              'hrv_status': 'BALANCED', 'body_battery': 60},
            sleep_need={'date': _day(0), 'hours': 8.0}, today=TODAY)
        history = fitness_mod.load_fitness_history()
        assert set(history['daily_loads']) == {_day(1), _day(5)}
        assert history['readiness_history'][-1]['date'] == _day(0)
        assert history['sleep_need']['hours'] == 8.0
        assert _day(5) not in stale['daily_loads']  # the stale copy really was stale

    def test_day_shrinking_is_logged_and_moved_ids_are_deduplicated(self, caplog):
        fitness_mod.update_fitness_history([_activity(1, 'a1'), _activity(1, 'b1')], TODAY)
        with caplog.at_level('WARNING', logger='coach.fitness'):
            fitness_mod.update_fitness_history([_activity(1, 'a1')], TODAY)
        assert any('removed' in r.getMessage() and "'b1'" in r.getMessage()
                   for r in caplog.records)
        # a1 edited onto another date: it must not live under two days
        fitness_mod.update_fitness_history([_activity(2, 'a1')], TODAY)
        loads = fitness_mod.load_fitness_history()['daily_loads']
        assert fitness_mod.activity_ids(loads[_day(2)]) == {'a1'}
        assert fitness_mod.activity_ids(loads[_day(1)]) == set()
        assert loads[_day(1)]['total'] == 0

    def test_ingest_watermark_never_regresses(self):
        fitness_mod.update_fitness_history([_activity(1, 'a1')], TODAY)
        fitness_mod.update_fitness_history([_activity(1, 'a1')], TODAY - timedelta(days=3))
        assert fitness_mod.load_fitness_history()['last_activity_ingest_date'] == TODAY.isoformat()

    def test_empty_payload_wipes_nothing(self):
        fitness_mod.update_fitness_history([_activity(1, 'a1')], TODAY)
        fitness_mod.update_fitness_history([], TODAY, window_start=TODAY - timedelta(days=14))
        assert _day(1) in fitness_mod.load_fitness_history()['daily_loads']

    def test_snapshot_computed_from_merged_loads(self):
        fitness_mod.update_fitness_history([_activity(1, 'a1', load=100.0)], TODAY)
        history = fitness_mod.update_fitness_history([_activity(2, 'a2', load=100.0)], TODAY)
        todays = [s for s in history['snapshots'] if s['date'] == TODAY.isoformat()]
        assert len(todays) == 1
        assert todays[0]['total']['ctl'] > 0

