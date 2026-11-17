import json
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from teams_transcription.assistant_store import AssistantStore, AssistantStoreError
from teams_transcription.meeting_store import MeetingStore, MeetingStoreError
from teams_transcription.storage import Session
from teams_transcription.history_context import history_initial_items
from teams_transcription.summary import SECTIONS, source_quality
from summary_history_fixture import legacy_job


class AssistantStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AssistantStore(self.tmp.name)
        self.calendar = self.store.save_calendar(dict(account_id='one',calendar_id='fixture@example.invalid',
            name='Fixture',enabled=True,recording_authorized=True))

    def occurrence(self, ident='event-one', *, start='2026-10-14T01:00:00+00:00', calendar=None, **changes):
        value = dict(event_id=ident,title='Fixture',start_at=start,end_at='2026-10-14T04:00:00+00:00',
            status='confirmed',link_status='valid',all_day=False,join_url='https://teams.microsoft.com/meet/12345')
        value.update(changes)
        return self.store.sync_occurrences((calendar or self.calendar)['id'],[value])[0]

    def capture(self, occurrence):
        lease = self.store.claim_run(occurrence['id'],'fixture-owner')
        row = self.store.get_run(lease['session_id'])
        session = Session(self.tmp.name,lease['session_id'],lease=lease).create(dict(
            provider='codex',source='meeting',prior_context=row['prior_context']))
        session.append_text('Review the report.',0)
        return session, lease

    def finish(self, session, lease):
        self.store.finish_run(lease,'stopped','operator_stop')

    def test_finished_capture_preserves_source_without_automatic_summary_job(self):
        session,lease=self.capture(self.occurrence())
        self.finish(session,lease)
        source=self.store.read(session.ident)
        self.assertTrue(source['terminal'])
        self.assertEqual([row['text'] for row in source['sources']],['Review the report.'])
        self.assertEqual(self.store.list_summary_jobs(),[])
        with self.assertRaisesRegex(AssistantStoreError,'summary_service_disabled'):
            self.store.retry_summary(session.ident)

    def test_direct_source_finalize_also_cannot_enqueue_summary(self):
        session,lease=self.capture(self.occurrence())
        source=session.store.finalize(session.ident,'stopped')
        self.assertTrue(source['terminal'])
        self.assertEqual([row['text'] for row in self.store.read(session.ident)['sources']],['Review the report.'])
        self.assertEqual(self.store.list_summary_jobs(),[])

    def publish(self, session, *, tasks=True):
        legacy_job(self.store,session.ident)
        lease = self.store.claim_summary('fixture-summary')
        self.assertEqual(lease['session_id'],session.ident)
        publisher = Session(self.tmp.name,session.ident,lease=lease)
        source = publisher.summary_source()
        value = {key:[] for key in SECTIONS}
        if tasks:
            value['待辦'] = [dict(text='Review the report',owner='',date='',sources=[source['entries'][0]['id']])]
        value.update(model='controlled-test-codex',source_quality=source_quality(source['entries']))
        publisher.publish_summary(value,expected_version=source['version'],
            expected_source_sha256=source['source_sha256'],generation_mode='codex')
        self.store.finish_summary(lease)
        return value

    def test_recurring_reschedule_keeps_identity_but_shared_url_does_not_merge_series(self):
        first = self.occurrence(recurring_event_id='parent',original_start='2026-10-14T01:00:00Z')
        moved = self.occurrence(start='2026-10-14T02:00:00+00:00',recurring_event_id='parent',
            original_start='2026-10-14T01:00:00Z',join_url='https://teams.microsoft.com/meet/67890')
        self.assertEqual((first['id'],first['series_id']),(moved['id'],moved['series_id']))
        other = self.occurrence('different-series')
        self.assertNotEqual(other['series_id'],first['series_id'])

    def test_pending_cancellation_and_cross_account_grouping_fail_closed(self):
        first = self.occurrence()
        other_calendar = self.store.save_calendar(dict(account_id='other',calendar_id='other@example.invalid',
            name='Other',enabled=True,recording_authorized=True))
        other = self.occurrence('other',calendar=other_calendar)
        with self.assertRaisesRegex(AssistantStoreError,'series_scope_mismatch'):
            self.store.assign_series(first['id'],other['series_id'])
        self.occurrence(status='cancelled',start_at=None,end_at=None,join_url=None,link_status='missing')
        with self.assertRaisesRegex(AssistantStoreError,'occurrence_not_ready'):
            self.store.claim_run(first['id'],'fixture')

    def test_valid_lease_cannot_write_another_session_and_only_one_capture_runs(self):
        first = self.occurrence()
        session, lease = self.capture(first)
        other = self.occurrence('other')
        with self.assertRaisesRegex(AssistantStoreError,'meeting_slot_busy'):
            self.store.claim_run(other['id'],'another-owner')
        unrelated = Session(self.tmp.name).create(dict(provider='mock',source='synthetic'))
        forged = Session(self.tmp.name,unrelated.ident,lease=lease)
        with self.assertRaisesRegex(MeetingStoreError,'stale_execution_lease'):
            forged.update(state='stopped')
        self.assertEqual(unrelated.status()['state'],'starting')
        self.finish(session,lease)
        with self.assertRaisesRegex(MeetingStoreError,'stale_execution_lease'):
            session.append_text('Late write',1)

    def test_wall_clock_adjustments_do_not_expire_live_capture_or_source_writes(self):
        session, lease = self.capture(self.occurrence())
        wall = time.time()
        for index, offset in enumerate((34.642246, -3600), 1):
            with self.subTest(offset=offset), patch('time.time', return_value=wall+offset):
                self.store.heartbeat_run(lease)
                session.append_text(f'After clock adjustment {index}.', index)
                restarted = AssistantStore(self.tmp.name)
                self.assertEqual(restarted.recover_expired(), 0)
                self.assertEqual(restarted.get_run(session.ident)['state'], 'running')
        self.assertEqual([item['text'] for item in session.entries()],
            ['Review the report.', 'After clock adjustment 1.', 'After clock adjustment 2.'])
        self.finish(session, lease)

    def test_elapsed_expiry_is_not_extended_by_backward_wall_clock(self):
        with patch.object(MeetingStore, '_lease_clock', return_value=('boot-one', 100)):
            session, lease = self.capture(self.occurrence())
        with patch.object(MeetingStore, '_lease_clock', return_value=('boot-one', 131)), \
                patch('time.time', return_value=time.time()-3600):
            with self.assertRaisesRegex(AssistantStoreError, 'stale_execution_lease'):
                self.store.heartbeat_run(lease)
            with self.assertRaisesRegex(MeetingStoreError, 'stale_execution_lease'):
                session.append_text('Must not be written.', 1)
            self.assertEqual(self.store.recover_expired(), 1)
        self.assertEqual([item['text'] for item in session.entries()], ['Review the report.'])
        self.assertEqual(self.store.get_run(session.ident)['state'], 'interrupted')
        self.assertEqual(self.store.capture_interlocks(), [session.ident])

    def test_new_boot_fences_capture_even_when_elapsed_clock_restarts_lower(self):
        with patch.object(MeetingStore, '_lease_clock', return_value=('boot-one', 100)):
            session, lease = self.capture(self.occurrence())
        with patch.object(MeetingStore, '_lease_clock', return_value=('boot-two', 1)):
            restarted = AssistantStore(self.tmp.name)
            with self.assertRaisesRegex(MeetingStoreError, 'stale_execution_lease'):
                session.update(state='recording')
            with self.assertRaisesRegex(AssistantStoreError, 'stale_execution_lease'):
                restarted.heartbeat_run(lease)
            self.assertEqual(restarted.recover_expired(), 1)
            self.assertEqual(restarted.recover_expired(), 0)
        self.assertEqual(restarted.get_run(session.ident)['state'], 'interrupted')
        self.assertEqual(restarted.list_summary_jobs(), [])

    def test_summary_publication_survives_wall_clock_correction(self):
        session, capture = self.capture(self.occurrence())
        self.finish(session, capture)
        legacy_job(self.store,session.ident)
        lease = self.store.claim_summary('summary-owner')
        publisher = Session(self.tmp.name, session.ident, lease=lease)
        source = publisher.summary_source()
        value = {key:[] for key in SECTIONS}
        value['重點'] = [dict(text='Review the report.', owner='', date='',
            sources=[source['entries'][0]['id']])]
        value.update(model='controlled-test-codex', source_quality=source_quality(source['entries']))
        with patch('time.time', return_value=time.time()+3600):
            self.store.heartbeat_summary(lease)
            self.assertEqual(self.store.recover_expired(), 0)
            publisher.publish_summary(value, expected_version=source['version'],
                expected_source_sha256=source['source_sha256'], generation_mode='codex')
            self.store.finish_summary(lease)
        self.assertEqual(self.store.summary_state(session.ident)['revision'], 1)
        self.assertEqual(self.store.list_summary_jobs()[0]['state'], 'published')

    def test_legacy_wall_lease_migration_preserves_source_without_reviving_owner(self):
        session, lease = self.capture(self.occurrence())
        # Recreate the previous schema, retaining its real source and live wall deadline.
        with sqlite3.connect(f'{self.tmp.name}/.meeting-store.sqlite3') as db:
            db.execute('ALTER TABLE execution_leases DROP COLUMN boot_id')
            db.execute('ALTER TABLE execution_leases DROP COLUMN expires_boottime')
        restarted = AssistantStore(self.tmp.name)
        with self.assertRaisesRegex(MeetingStoreError, 'stale_execution_lease'):
            session.append_text('Legacy owner must be fenced.', 1)
        with self.assertRaisesRegex(AssistantStoreError, 'stale_execution_lease'):
            restarted.heartbeat_run(lease)
        self.assertEqual(restarted.recover_expired(), 1)
        self.assertEqual(AssistantStore(self.tmp.name).recover_expired(), 0)
        self.assertEqual([item['text'] for item in session.entries()], ['Review the report.'])
        self.assertEqual(restarted.capture_interlocks(), [session.ident])
        self.assertEqual(restarted.list_summary_jobs(), [])

    def test_source_seal_is_independent_of_canceled_summary_generation(self):
        session, lease = self.capture(self.occurrence())
        with patch.object(self.store,'_summary_source',side_effect=AssertionError('summary must stay disabled')):
            self.finish(session,lease)
        self.assertEqual(self.store.get_run(session.ident)['state'],'stopped')
        self.assertTrue(self.store.read(session.ident)['terminal'])
        self.assertEqual(self.store.list_summary_jobs(),[])

    def test_restart_recovers_source_without_summary_or_rejoining(self):
        occurrence = self.occurrence()
        session, lease = self.capture(occurrence)
        restarted = AssistantStore(self.tmp.name)
        restarted.recover_expired(now_boottime=lease['expires_boottime']+1)
        row = restarted.get_run(session.ident)
        self.assertEqual(row['state'],'interrupted')
        self.assertEqual(row['end_reason'],'service_interrupted')
        self.assertTrue(row['source']['terminal'])
        self.assertEqual(restarted.list_summary_jobs(),[])
        with self.assertRaises(AssistantStoreError):
            restarted.claim_run(occurrence['id'],'new-owner')
        with self.assertRaises(MeetingStoreError):
            session.append_text('Stale capture',1)
        other = self.occurrence('next-occurrence')
        with self.assertRaisesRegex(AssistantStoreError,'capture_cleanup_review_required'):
            restarted.claim_run(other['id'],'new-owner')

    def test_glossary_only_approved_relevant_entries_and_snapshot_remains_frozen(self):
        occurrence = self.occurrence()
        active = self.store.save_glossary(dict(kind='term',canonical='RTO',aliases=['recovery time objective'],status='active'))
        expected = {'RTO'}
        for index in range(50):
            canonical = f'Approved global vocabulary {index}'
            self.store.save_glossary(dict(kind='term',canonical=canonical,
                definition='Preserve this approved definition. '*15,status='active'))
            expected.add(canonical)
        self.store.save_glossary(dict(kind='person',canonical='Synthetic Person',status='pending'))
        other = self.occurrence('other')
        self.store.save_glossary(dict(kind='term',canonical='Other Project',scope='series',series_id=other['series_id'],status='active'))
        session, lease = self.capture(occurrence)
        frozen = self.store.get_run(session.ident)['glossary_snapshot']
        self.assertEqual({entry['canonical'] for entry in frozen['entries']},expected)
        self.store.set_glossary_status(active['id'],'disabled')
        self.assertEqual(self.store.get_run(session.ident)['glossary_snapshot'],frozen)
        self.assertEqual({entry['canonical'] for entry in self.store.glossary_snapshot(occurrence['series_id'])['entries']},
            expected-{'RTO'})

    def test_summary_and_tasks_publish_atomically_and_omission_does_not_close_old_tasks(self):
        first = self.occurrence()
        session, lease = self.capture(first)
        self.finish(session,lease)
        self.publish(session)
        task = self.store.list_tasks(first['series_id'])[0]
        second = self.occurrence('next',start='2026-10-14T02:00:00+00:00')
        self.store.assign_series(second['id'],first['series_id'])
        session2, lease2 = self.capture(self.store.get_occurrence(second['id']))
        prior = self.store.get_run(session2.ident)['prior_context']
        self.assertEqual(prior['session_id'],session.ident)
        self.assertEqual(prior['open_tasks'][0]['id'],task['id'])
        self.finish(session2,lease2)
        self.publish(session2,tasks=False)
        preserved = self.store.list_tasks(first['series_id'])[0]
        self.assertEqual((preserved['id'],preserved['status']),(task['id'],'open'))
        self.store.update_task(task['id'],'completed')
        self.assertEqual(self.store.list_tasks(first['series_id'])[0]['status'],'completed')

    def test_stale_summary_lease_cannot_publish_after_restart(self):
        session, lease = self.capture(self.occurrence())
        self.finish(session,lease)
        legacy_job(self.store,session.ident)
        first = self.store.claim_summary('first-summary')
        publisher = Session(self.tmp.name,session.ident,lease=first)
        source = publisher.summary_source()
        value = {key:[] for key in SECTIONS}
        value.update(model='controlled-test-codex',source_quality=source_quality(source['entries']))
        self.store.recover_expired(now_boottime=first['expires_boottime']+1)
        second = self.store.claim_summary('second-summary')
        self.assertEqual(second['generation'],first['generation']+1)
        with self.assertRaisesRegex(MeetingStoreError,'stale_execution_lease'):
            publisher.publish_summary(value,expected_version=source['version'],
                expected_source_sha256=source['source_sha256'],generation_mode='codex')
        self.assertIsNone(self.store.summary_state(session.ident))

    def test_late_older_summary_never_replaces_newer_meeting_memory(self):
        old = self.occurrence('old',start='2026-10-14T01:00:00+00:00')
        older,lease = self.capture(old)
        self.finish(older,lease)
        legacy_job(self.store,older.ident)
        failed = self.store.claim_summary('older-summary')
        self.store.finish_summary(failed,'controlled_summary_failure')
        recent = self.occurrence('recent',start='2026-10-14T02:00:00+00:00')
        self.store.assign_series(recent['id'],old['series_id'])
        newer,lease = self.capture(self.store.get_occurrence(recent['id']))
        self.finish(newer,lease)
        self.publish(newer,tasks=False)
        # Explicit imported historical retry fixture; the service API stays disabled.
        legacy_job(self.store,older.ident)
        self.publish(older)
        context = self.store.prior_context(old['series_id'],'2026-10-14T03:00:00+00:00')
        self.assertEqual(context['session_id'],newer.ident)
        self.assertEqual(context['occurred_at'],recent['start_at'])
        self.assertEqual(context['open_tasks'][0]['origin_session_id'],older.ident)

    def test_operator_completed_task_status_overrides_old_summary_todo(self):
        occurrence = self.occurrence()
        session,lease = self.capture(occurrence)
        self.finish(session,lease)
        self.publish(session)
        task = self.store.list_tasks(occurrence['series_id'])[0]
        self.store.update_task(task['id'],'completed')
        context = self.store.prior_context(occurrence['series_id'],'2026-10-14T03:00:00+00:00')
        self.assertEqual(context['open_tasks'],[])
        historical = context['summary']['待辦'][0]
        self.assertEqual((historical['task_id'],historical['task_status']),(task['id'],'completed'))

    def test_large_published_history_and_tasks_freeze_intact_before_later_changes(self):
        first = self.occurrence()
        session, lease = self.capture(first)
        self.finish(session,lease)
        legacy_job(self.store,session.ident)
        summary_lease = self.store.claim_summary('large-history-publisher')
        publisher = Session(self.tmp.name,session.ident,lease=summary_lease)
        source = publisher.summary_source()
        expected_texts = [f'Saved task {index}: '+'Preserve every approved detail. '*100 for index in range(24)]
        value = {key:[] for key in SECTIONS}
        value['待辦'] = [dict(text=text,owner='Fixture owner',date='',
            sources=[source['entries'][0]['id']]) for text in expected_texts]
        value.update(model='controlled-test-codex',source_quality=source_quality(source['entries']))
        publisher.publish_summary(value,expected_version=source['version'],
            expected_source_sha256=source['source_sha256'],generation_mode='codex')
        self.store.finish_summary(summary_lease)
        second = self.occurrence('next-large',start='2026-10-14T02:00:00+00:00')
        self.store.assign_series(second['id'],first['series_id'])
        current, lease = self.capture(self.store.get_occurrence(second['id']))
        frozen = self.store.get_run(current.ident)['prior_context']
        memory = json.loads(history_initial_items(frozen)[0]['text'].split('\n',1)[1])
        self.assertEqual([item['text'] for item in memory['summary']['待辦']],expected_texts)
        self.assertEqual({item['text'] for item in memory['open_tasks']},set(expected_texts))
        self.assertEqual(memory['sha256'],self.store.summary_state(session.ident)['sha256'])
        self.assertEqual((memory['session_id'],memory['revision']),(session.ident,1))
        self.assertFalse(memory['truncated'])
        for task in memory['open_tasks']:
            self.assertEqual(task['origin_session_id'],session.ident)
            self.assertEqual(task['source_refs'],[{'session_id':session.ident,'source_id':source['entries'][0]['id']}])
        self.store.update_task(memory['open_tasks'][0]['id'],'completed')
        self.finish(current,lease)
        self.publish(current,tasks=False)
        self.assertEqual(self.store.get_run(current.ident)['prior_context'],frozen)
        self.assertEqual(self.store.read(current.ident)['prior_context'],frozen)


if __name__=='__main__':
    unittest.main()
