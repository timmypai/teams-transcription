import asyncio
import hashlib
import os
import shutil
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.assistant_store import AssistantStore, AssistantStoreError
from teams_transcription.daemon import AssistantDaemon
from teams_transcription.meeting_store import MeetingStoreError
from teams_transcription.storage import Session
from summary_history_fixture import legacy_job
from teams_transcription.summary import SECTIONS, source_quality


PID = 987654321
SIGNATURE = '12345'
CURRENT_STAMP = object()


def iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class CleanupFixture:
    def make_store(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AssistantStore(self.tmp.name)
        self.now = time.time()

    def occurrence(self, title='Controlled cleanup review', *, authorized=True):
        return self.store.create_manual_occurrence(dict(title=title,
            join_url='https://teams.microsoft.com/meet/12345',
            start_at=iso(self.now-1), end_at=iso(self.now+600),
            recording_authorized=authorized, speaking_authorized=False))

    def capture(self, *, source=True, **status):
        occurrence = self.occurrence()
        lease = self.store.claim_run(occurrence['id'], 'interrupted-fixture')
        session = Session(self.tmp.name, lease['session_id'], lease=lease).create(dict(
            provider='codex', source='meeting',
            prior_context=self.store.get_run(lease['session_id'])['prior_context']))
        session.update(**{**dict(state='recording', pid=PID, process_signature=SIGNATURE,
            cleanup_status='unknown', provider_stop_status='stop_unknown'), **status})
        if source:
            session.save_provisional(dict(provisional_id='provisional:'+'a'*32,
                revision=1, text='Controlled unfinalized source.', truncated=False))
        return session, lease

    def interrupted(self, *, source=True):
        session, lease = self.capture(source=source)
        self.store.recover_expired(now_boottime=lease['expires_boottime']+1)
        return session, lease

    def stamp(self, session):
        return self.store.get_run(session.ident)['cleanup_review']['stamp']

    def review(self, session, stamp=CURRENT_STAMP, **changes):
        confirmations = dict(resources_stopped=True, assistant_left=True)
        confirmations.update(changes)
        return self.store.review_capture_cleanup(session.ident,
            self.stamp(session) if stamp is CURRENT_STAMP else stamp, **confirmations)

    def reviews(self):
        return [row for row in self.store.audit() if row['action']=='capture_cleanup_reviewed']

    def publish(self, session):
        legacy_job(self.store,session.ident)
        lease = self.store.claim_summary('controlled-summary')
        publisher = Session(self.tmp.name, session.ident, lease=lease)
        source = publisher.summary_source()
        value = {key:[] for key in SECTIONS}
        value.update(model='controlled-test-codex', source_quality=source_quality(source['entries']))
        publisher.publish_summary(value, expected_version=source['version'],
            expected_source_sha256=source['source_sha256'], generation_mode='codex')
        self.store.finish_summary(lease)


class CaptureCleanupReviewStoreTests(CleanupFixture, unittest.TestCase):
    def setUp(self):
        self.make_store()
        self.gone = patch('teams_transcription.assistant_store.os.kill', side_effect=ProcessLookupError)
        self.gone.start()
        self.addCleanup(self.gone.stop)

    def test_review_is_audited_idempotent_durable_and_does_not_rewrite_history(self):
        session, lease = self.interrupted()
        self.publish(session)
        before = self.store.get_run(session.ident)
        summary = self.store.summary_state(session.ident)
        source = self.store.read(session.ident)
        status = session.status()
        occurrence = self.store.get_occurrence(before['occurrence_id'])
        pending = self.store.list_summary_jobs()
        self.store.update_settings({'scheduler_paused':False})
        stamp = before['cleanup_review']['stamp']
        self.assertRegex(stamp, r'^[0-9a-f]{64}$')
        self.assertEqual(before['cleanup_review']['worker_status'], 'gone')
        self.assertTrue(before['cleanup_review']['required'])

        result = self.review(session, stamp)
        self.assertFalse(result['required'])
        self.assertEqual(result['stamp'], stamp)
        self.assertEqual(result['actor'], 'admin')
        self.assertIsNotNone(result['reviewed_at'])
        self.assertEqual(self.review(session, stamp), result)
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.reviews()[0]['target'], session.ident)
        self.assertEqual(self.store.capture_interlocks(), [])
        self.assertTrue(self.store.settings()['scheduler_paused'])

        self.store = AssistantStore(self.tmp.name)
        after = self.store.get_run(session.ident)
        self.assertEqual(after['cleanup_review'], result)
        self.assertEqual({key:value for key,value in after.items() if key!='cleanup_review'},
            {key:value for key,value in before.items() if key!='cleanup_review'})
        self.assertEqual(self.store.read(session.ident), source)
        self.assertEqual(self.store.summary_state(session.ident), summary)
        self.assertEqual(self.store.get_occurrence(before['occurrence_id']), occurrence)
        self.assertEqual(session.status(), status)
        self.assertEqual(self.store.list_summary_jobs(), pending)
        self.assertEqual(self.store.capture_interlocks(), [])
        with self.assertRaisesRegex(MeetingStoreError, 'stale_execution_lease'):
            session.append_text('Forbidden stale source write.', 1)
        with self.assertRaisesRegex(MeetingStoreError, 'meeting_finalized'):
            Session(self.tmp.name, session.ident).append_text('Forbidden sealed source write.', 1)
        with self.assertRaisesRegex(AssistantStoreError, 'recording_authorization_required'):
            self.store.claim_run(self.occurrence(authorized=False)['id'], 'new-owner')
        next_lease = self.store.claim_run(self.occurrence()['id'], 'new-owner')
        self.assertNotEqual(next_lease['session_id'], lease['session_id'])

    def test_confirmations_must_both_be_literal_true_and_stamp_must_match(self):
        session, _ = self.interrupted()
        stamp = self.stamp(session)
        for field in ('resources_stopped', 'assistant_left'):
            for value in (False, None, 0, 1, 'true', 'on', [], {}):
                with self.subTest(field=field, value=value):
                    with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_confirmations_required'):
                        self.review(session, stamp, **{field:value})
        with self.assertRaises(TypeError):
            self.store.review_capture_cleanup(session.ident, stamp, resources_stopped=True)
        for wrong in ('', '0'*64, None, 123, stamp+'0'):
            with self.subTest(stamp=wrong):
                with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_stale'):
                    self.review(session, wrong)
        self.assertEqual(self.store.capture_interlocks(), [session.ident])
        self.assertEqual(self.reviews(), [])

    def test_live_unknown_and_missing_worker_proof_all_block(self):
        session, _ = self.interrupted()
        observations = (
            (None, SIGNATURE, 'alive'),
            (PermissionError(), SIGNATURE, 'unknown'),
            (OSError(), SIGNATURE, 'unknown'),
            (None, None, 'unknown'),
            (None, 'unreadable', 'unknown'),
        )
        for liveness_error, signature, expected in observations:
            with self.subTest(expected=expected, signature=signature):
                with patch('teams_transcription.assistant_store.os.kill', side_effect=liveness_error), \
                        patch('teams_transcription.assistant_store.process_signature', return_value=signature):
                    self.assertEqual(self.store.get_run(session.ident)['cleanup_review']['worker_status'], expected)
                    with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_worker_'+expected):
                        self.review(session)
        for error in (PermissionError(), UnicodeError()):
            with self.subTest(error=type(error).__name__):
                with patch('teams_transcription.assistant_store.os.kill'), \
                        patch('teams_transcription.assistant_store.process_signature', side_effect=error):
                    with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_worker_unknown'):
                        self.review(session)
        for changes in ({'pid':None}, {'pid':True}, {'pid':-1},
                {'process_signature':None}, {'process_signature':''}):
            with self.subTest(changes=changes):
                Session(self.tmp.name, session.ident).update(**{
                    'pid':PID, 'process_signature':SIGNATURE, **changes})
                self.assertEqual(self.store.get_run(session.ident)['cleanup_review']['worker_status'], 'unknown')
                with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_worker_unknown'):
                    self.review(session)
        self.assertEqual(self.reviews(), [])
        self.assertEqual(self.store.capture_interlocks(), [session.ident])

    def test_interrupted_claim_without_worker_identity_cannot_be_attested(self):
        lease = self.store.claim_run(self.occurrence()['id'], 'never-started-worker')
        self.store.recover_expired(now_boottime=lease['expires_boottime']+1)
        session = Session(self.tmp.name, lease['session_id'])
        self.assertEqual(self.store.get_run(session.ident)['cleanup_review']['worker_status'], 'unknown')
        with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_worker_unknown'):
            self.review(session)
        self.assertEqual(self.store.capture_interlocks(), [session.ident])
        self.assertEqual(self.reviews(), [])

    def test_reused_pid_and_process_disappearing_during_probe_are_confirmed_gone(self):
        session, _ = self.interrupted()
        with patch('teams_transcription.assistant_store.os.kill'), \
                patch('teams_transcription.assistant_store.process_signature', return_value='99999'):
            self.assertEqual(self.store.get_run(session.ident)['cleanup_review']['worker_status'], 'gone')
            self.review(session)
        Session(self.tmp.name, session.ident).update(cleanup_detail='changed')
        stamp = self.stamp(session)
        with patch('teams_transcription.assistant_store.os.kill', side_effect=[None, ProcessLookupError()]), \
                patch('teams_transcription.assistant_store.process_signature', return_value=None):
            self.review(session, stamp)
        self.assertEqual(self.store.capture_interlocks(), [])
        self.assertEqual(len(self.reviews()), 2)

    def test_only_terminal_affected_run_is_eligible_and_any_active_lease_blocks(self):
        session, lease = self.capture()
        with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_not_eligible'):
            self.review(session)
        session.update(cleanup_status='cleaned')
        self.store.finish_run(lease, 'stopped', 'operator_stop')
        with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_not_eligible'):
            self.review(session)
        self.assertFalse(self.store.get_run(session.ident)['cleanup_review']['required'])
        self.assertEqual(self.reviews(), [])
        next_session, _ = self.interrupted()
        self.review(next_session)
        active, _ = self.capture()
        Session(self.tmp.name, next_session.ident).update(cleanup_detail='late persisted observation')
        with self.assertRaisesRegex(AssistantStoreError, 'meeting_slot_busy'):
            self.review(next_session)
        self.assertEqual(set(self.store.capture_interlocks()), {active.ident, next_session.ident})

    def test_unknown_cleanup_terminal_without_interruption_is_reviewable(self):
        session, lease = self.capture()
        self.store.finish_run(lease, 'failed', 'capture_failed', 'capture_cleanup_unknown')
        before = self.store.get_run(session.ident)
        self.review(session)
        after = self.store.get_run(session.ident)
        self.assertEqual((after['state'], after['end_reason'], after['error_code']),
            (before['state'], before['end_reason'], before['error_code']))
        self.assertEqual(self.store.capture_interlocks(), [])

    def test_changed_unknown_marker_cannot_silently_bypass_an_old_snapshot_review(self):
        session, lease = self.capture()
        self.store.finish_run(lease, 'stopped', 'operator_stop')
        stamp = self.stamp(session)
        self.review(session, stamp)
        Session(self.tmp.name, session.ident).update(cleanup_status='cleaned')
        new_status = session.status()
        detail = self.store.get_run(session.ident)['cleanup_review']
        self.assertTrue(detail['required'])
        self.assertNotEqual(detail['stamp'], stamp)
        self.assertIsNone(detail['reviewed_at'])
        self.assertEqual(self.store.capture_interlocks(), [session.ident])
        next_occurrence = self.occurrence()
        with self.assertRaisesRegex(AssistantStoreError, 'capture_cleanup_review_required'):
            self.store.claim_run(next_occurrence['id'], 'new-owner')
        with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_stale'):
            self.review(session, stamp)
        self.review(session, detail['stamp'])
        self.assertEqual(session.status(), new_status)
        self.assertEqual(self.store.capture_interlocks(), [])
        self.store.claim_run(next_occurrence['id'], 'new-owner')

    def test_new_runtime_state_invalidates_review_and_each_blocker_needs_its_own_review(self):
        first, _ = self.interrupted()
        first_stamp = self.stamp(first)
        self.review(first, first_stamp)
        second, _ = self.interrupted()
        with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_stale'):
            self.review(second, first_stamp)
        Session(self.tmp.name, first.ident).update(cleanup_detail={'late':'observation'})
        self.assertNotEqual(self.stamp(first), first_stamp)
        invalidated = self.store.get_run(first.ident)['cleanup_review']
        self.assertTrue(invalidated['required'])
        self.assertIsNone(invalidated['reviewed_at'])
        self.assertIsNone(invalidated['actor'])
        with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_stale'):
            self.review(first, first_stamp)
        self.assertEqual(set(self.store.capture_interlocks()), {first.ident, second.ident})
        self.review(first)
        next_occurrence = self.occurrence()
        with self.assertRaisesRegex(AssistantStoreError, 'capture_cleanup_review_required'):
            self.store.claim_run(next_occurrence['id'], 'new-owner')
        self.assertEqual(self.store.capture_interlocks(), [second.ident])
        self.review(second)
        self.store.claim_run(next_occurrence['id'], 'new-owner')
        self.assertEqual(len(self.reviews()), 3)

    def test_terminal_outcome_and_end_timestamp_are_bound_into_the_review(self):
        session, _ = self.interrupted()
        for field,value in (('ended_at','2026-10-14T04:00:00+00:00'), ('state','stopped'),
                ('end_reason','capture_failed'), ('error_code','capture_cleanup_unknown')):
            with self.subTest(field=field):
                stamp = self.stamp(session)
                self.review(session, stamp)
                # Model an external persisted-state repair; assertions stay on the
                # public guard and review interface rather than the table layout.
                with sqlite3.connect(Path(self.tmp.name)/'.meeting-store.sqlite3') as db:
                    db.execute(f'UPDATE assistant_runs SET {field}=? WHERE session_id=?', (value,session.ident))
                self.assertNotEqual(self.stamp(session), stamp)
                self.assertEqual(self.store.capture_interlocks(), [session.ident])
                with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_stale'):
                    self.review(session, stamp)

    def test_stamp_and_worker_proof_are_checked_in_the_writing_transaction(self):
        session, _ = self.interrupted()
        stamp = self.stamp(session)
        attempted = []
        def concurrent_observation(pid):
            with sqlite3.connect(Path(self.tmp.name)/'.meeting-store.sqlite3', timeout=0) as db:
                try:
                    db.execute('UPDATE session_runtime SET status_json=status_json||? WHERE meeting_id=?',
                        (' ', session.ident))
                except sqlite3.OperationalError as error:
                    attempted.append(str(error))
            return '99999'
        with patch('teams_transcription.assistant_store.os.kill'), \
                patch('teams_transcription.assistant_store.process_signature', side_effect=concurrent_observation):
            self.review(session, stamp)
        self.assertEqual(attempted, ['database is locked'])
        self.assertEqual(self.store.capture_interlocks(), [])
        Session(self.tmp.name, session.ident).update(cleanup_detail='after commit')
        self.assertEqual(self.store.capture_interlocks(), [session.ident])
        with self.assertRaisesRegex(AssistantStoreError, 'cleanup_review_stale'):
            self.review(session, stamp)

    def test_review_and_audit_rollback_together(self):
        session, _ = self.interrupted()
        with patch.object(self.store, '_audit', side_effect=RuntimeError('controlled audit failure')):
            with self.assertRaisesRegex(RuntimeError, 'controlled audit failure'):
                self.review(session)
        self.assertEqual(self.store.capture_interlocks(), [session.ident])
        self.assertTrue(self.store.get_run(session.ident)['cleanup_review']['required'])
        self.assertEqual(self.reviews(), [])


class LocalCalendar:
    def status(self):
        return {'connected':False}

    async def close(self):
        pass


class CaptureCleanupReviewDaemonTests(CleanupFixture, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.make_store()
        self.gone = patch('teams_transcription.assistant_store.os.kill', side_effect=ProcessLookupError)
        self.gone.start()
        self.addCleanup(self.gone.stop)
        self.calls = []
        self.settings = AssistantSettings(data_root=self.tmp.name,
            admin_password_file=str(Path(self.tmp.name)/'password'),
            admin_session_secret_file=str(Path(self.tmp.name)/'secret'),
            google_credentials_file=str(Path(self.tmp.name)/'google'))
        async def forbidden(*args):
            self.calls.append('unexpected execution')
            raise AssertionError('Review must not execute capture or summary')
        self.daemon = AssistantDaemon(self.settings, store=self.store, calendar=LocalCalendar(),
            runner=forbidden, summarizer=forbidden)
        self.addAsyncCleanup(self.daemon.close)

    async def test_daemon_review_recomputes_cache_without_join_output_or_schedule_resume(self):
        first, _ = self.interrupted(source=False)
        self.review(first)
        second, _ = self.interrupted(source=False)
        Session(self.tmp.name, first.ident).update(cleanup_detail='later observation')
        await self.daemon.start()
        self.assertTrue(self.daemon.status()['capture_interlock'])
        self.assertEqual(self.daemon.status()['last_error'], 'capture_cleanup_review_required')
        await self.daemon.review_capture_cleanup(first.ident, self.stamp(first),
            resources_stopped=True, assistant_left=True)
        self.assertTrue(self.daemon.status()['capture_interlock'])
        self.assertEqual(self.daemon.status()['last_error'], 'capture_cleanup_review_required')
        result = await self.daemon.review_capture_cleanup(second.ident, self.stamp(second),
            resources_stopped=True, assistant_left=True)
        self.assertFalse(result['required'])
        await asyncio.sleep(.02)
        state = self.daemon.status()
        self.assertFalse(state['capture_interlock'])
        self.assertIsNone(state['last_error'])
        self.assertIsNone(state['active_session_id'])
        self.assertIsNone(state['response'])
        self.assertTrue(self.store.settings()['scheduler_paused'])
        self.assertEqual(self.calls, [])
        Session(self.tmp.name, second.ident).update(cleanup_detail='changed after review')
        with self.assertRaisesRegex(AssistantStoreError, 'capture_unavailable'):
            await self.daemon.join(self.occurrence()['id'])
        self.assertTrue(self.daemon.status()['capture_interlock'])
        self.assertTrue(self.store.settings()['scheduler_paused'])
        self.assertEqual(self.calls, [])

    async def test_active_in_memory_capture_rejects_review_without_touching_it(self):
        session, _ = self.interrupted(source=False)
        entered = asyncio.Event()
        calls = []
        async def controlled_runner(active):
            calls.append(active.ident)
            active.update(state='recording', cleanup_status='cleaned')
            entered.set()
            while not active.stopped():
                await asyncio.sleep(.01)
            active.update(state='stopped', cleanup_status='cleaned')
        daemon = AssistantDaemon(self.settings, store=self.store, calendar=LocalCalendar(),
            runner=controlled_runner)
        self.addAsyncCleanup(daemon.close)
        runtime = Path('/dev/shm')/('teams-assistant-'+str(os.getuid())+'-'+hashlib.sha256(
            self.settings.data_root.encode()).hexdigest()[:16])
        self.addCleanup(shutil.rmtree, runtime, ignore_errors=True)
        await daemon.start()
        await daemon.review_capture_cleanup(session.ident, self.stamp(session),
            resources_stopped=True, assistant_left=True)
        result = await daemon.join(self.occurrence()['id'])
        async with asyncio.timeout(5):
            await entered.wait()
        with self.assertRaisesRegex(AssistantStoreError, 'meeting_slot_busy'):
            await daemon.review_capture_cleanup(session.ident, self.stamp(session),
                resources_stopped=True, assistant_left=True)
        self.assertEqual(daemon.status()['active_session_id'], result['session_id'])
        self.assertEqual(calls, [result['session_id']])
        self.assertFalse(Session(self.tmp.name, result['session_id']).stopped())
        self.assertTrue(self.store.settings()['scheduler_paused'])
        await daemon.stop(result['session_id'])
        async with asyncio.timeout(5):
            while daemon.status()['active_session_id'] is not None:
                await asyncio.sleep(.01)

    async def test_failed_guard_reload_stays_closed_until_a_successful_recomputation(self):
        session, _ = self.interrupted(source=False)
        await self.daemon.start()
        stamp = self.stamp(session)
        with patch.object(self.store, 'capture_interlocks', side_effect=RuntimeError('controlled guard read failure')):
            with self.assertRaisesRegex(RuntimeError, 'controlled guard read failure'):
                await self.daemon.review_capture_cleanup(session.ident, stamp,
                    resources_stopped=True, assistant_left=True)
        self.assertTrue(self.daemon.status()['capture_interlock'])
        self.assertEqual(self.store.capture_interlocks(), [])
        self.assertEqual(len(self.reviews()), 1)
        await self.daemon.review_capture_cleanup(session.ident, stamp,
            resources_stopped=True, assistant_left=True)
        self.assertFalse(self.daemon.status()['capture_interlock'])
        self.assertTrue(self.store.settings()['scheduler_paused'])
        self.assertEqual(len(self.reviews()), 1)
        self.assertEqual(self.calls, [])

    async def test_review_preserves_other_error_and_closed_daemon_cannot_review(self):
        session, _ = self.interrupted(source=False)
        await self.daemon.start()
        self.daemon.last_error = 'controlled_other_error'
        await self.daemon.review_capture_cleanup(session.ident, self.stamp(session),
            resources_stopped=True, assistant_left=True)
        self.assertEqual(self.daemon.status()['last_error'], 'controlled_other_error')
        self.assertFalse(self.daemon.status()['capture_interlock'])
        await self.daemon.close()
        Session(self.tmp.name, session.ident).update(cleanup_detail='changed')
        with self.assertRaisesRegex(AssistantStoreError, 'capture_unavailable'):
            await self.daemon.review_capture_cleanup(session.ident, self.stamp(session),
                resources_stopped=True, assistant_left=True)
        self.assertEqual(self.store.capture_interlocks(), [session.ident])


if __name__=='__main__':
    unittest.main()
