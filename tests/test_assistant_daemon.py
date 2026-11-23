import asyncio
import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import httpx

from teams_transcription.assistant_config import AssistantSettings, doctor, load_assistant_settings
from teams_transcription.assistant_store import AssistantStore, AssistantStoreError
from teams_transcription.calendar_client import GoogleCalendarClient, READONLY_SCOPE
from teams_transcription.daemon import AssistantDaemon
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.storage import Session
from teams_transcription.summary import SECTIONS, source_quality
from summary_history_fixture import legacy_job


def iso(value):
    return datetime.fromtimestamp(value,timezone.utc).isoformat()


async def controlled_summary(session,path,executable,model):
    source = session.summary_source()
    value = {key:[] for key in SECTIONS}
    value['重點'] = [dict(text='Controlled source',sources=[source['entries'][0]['id']],owner='',date='')]
    value.update(model='controlled-test-codex',source_quality=source_quality(source['entries']))
    session.publish_summary(value,expected_version=source['version'],
        expected_source_sha256=source['source_sha256'],generation_mode='codex')


class AssistantDaemonTests(unittest.IsolatedAsyncioTestCase):
    async def test_device_authorization_reservation_does_not_consume_scheduled_meeting_attempt(self):
        self.daemon.auth_operation_in_progress = True
        self.store.update_settings({'scheduler_paused':False})
        # A stale scheduler snapshot must not permanently lose this occurrence.
        await asyncio.sleep(1.2)
        self.assertFalse(self.started.is_set())
        self.daemon.auth_operation_in_progress = False
        await self.until(self.started.is_set)
        self.assertIsNotNone(self.daemon.status()['active_session_id'])

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = time.time()
        self.events = {'event-one':self.event('event-one')}
        self.started = asyncio.Event()
        self.finished = asyncio.Event()
        self.requests = []
        config = self.root/'client.json'
        credentials = self.root/'google.json'
        config.write_text(json.dumps({'web':{'client_id':'fixture-client','client_secret':'fixture-secret',
            'redirect_uris':['http://localhost:8080/google/callback']}}))
        credentials.write_text(json.dumps(dict(access_token='fixture-access',refresh_token='fixture-refresh',
            expires_at=self.now+3600,scope=READONLY_SCOPE)))
        os.chmod(config,0o600)
        os.chmod(credentials,0o600)
        self.settings = AssistantSettings(data_root=str(self.root),admin_password_file=str(self.root/'password.json'),
            admin_session_secret_file=str(self.root/'secret.key'),google_credentials_file=str(credentials),
            google_client_config_file=str(config))
        self.store = AssistantStore(self.root)
        self.calendar_row = self.store.save_calendar(dict(account_id='fixture',calendar_id='primary',name='Controlled',
            enabled=True,recording_authorized=True,join_early_seconds=0,max_lateness_seconds=300))
        def handler(request):
            self.requests.append(request)
            if request.url.path.endswith('/events'):
                return httpx.Response(200,json={'items':list(self.events.values())})
            ident = request.url.path.rsplit('/',1)[-1]
            return httpx.Response(200,json=self.events[ident])
        client = GoogleCalendarClient(credentials,client_config_path=config,transport=httpx.MockTransport(handler))
        async def runner(session):
            if session.stopped():
                session.update(state='stopped',cleanup_status='cleaned')
                return 0
            session.update(state='recording')
            session.append_text('Controlled current meeting statement.',0)
            self.started.set()
            while not self.finished.is_set() and not session.stopped():
                await asyncio.sleep(.01)
            session.update(state='stopped',cleanup_status='cleaned')
            return 0
        self.daemon = AssistantDaemon(self.settings,store=self.store,calendar=client,runner=runner,
            summarizer=controlled_summary,clock=lambda:self.now)
        await self.daemon.start()
        self.addAsyncCleanup(self.daemon.close)
        await self.daemon.poll_calendar()

    def event(self,ident,start=None):
        return {'id':ident,'summary':'Controlled meeting','start':{'dateTime':iso(self.now-1 if start is None else start)},
            'end':{'dateTime':iso(self.now+600)},'description':'https://teams.microsoft.com/meet/12345'}

    async def until(self, predicate):
        async with asyncio.timeout(5):
            while not predicate():
                await asyncio.sleep(.02)

    def test_doctor_accepts_explicit_deny_all_and_rejects_malformed_policy_without_network(self):
        with patch('shutil.which',return_value=None), \
                patch('socket.getaddrinfo',side_effect=AssertionError('doctor must remain local')), \
                patch('subprocess.run',side_effect=AssertionError('no installed executable in this fixture')):
            for hosts, expected in (([],True),(['existing.example'],True),
                    (['https://existing.example'],False),(['bad..host'],False),([7],False)):
                with self.subTest(hosts=hosts):
                    settings = self.settings.model_copy(update={
                        'allowed_egress_hosts':hosts,'codex_home':str(self.root/'controlled-codex')})
                    self.assertEqual(doctor(settings)['checks']['egress_policy_configured'],expected)

    def test_duration_default_and_explicit_positive_operator_values(self):
        self.assertIsNone(self.settings.max_duration_seconds)
        for duration in (1, 14401, 24 * 3600):
            settings = load_assistant_settings(overrides={
                **self.settings.model_dump(),'max_duration_seconds':duration},environ={})
            self.assertEqual(settings.max_duration_seconds,duration)
        settings = load_assistant_settings(overrides=self.settings.model_dump(),
            environ={'ASSISTANT_MAX_DURATION_SECONDS':'null'})
        self.assertIsNone(settings.max_duration_seconds)
        settings = load_assistant_settings(overrides=self.settings.model_dump(),
            environ={'ASSISTANT_MAX_DURATION_SECONDS':'86400'})
        self.assertEqual(settings.max_duration_seconds,86400)
        for duration in (0, -1, True, 1.5):
            with self.assertRaises(ValueError):
                load_assistant_settings(overrides={
                    **self.settings.model_dump(),'max_duration_seconds':duration},environ={})

    async def test_default_two_hour_occurrence_survives_one_hour_then_stops_at_occurrence_end(self):
        self.events['event-one']['end']['dateTime'] = iso(self.now+7200)
        await self.daemon.poll_calendar()
        occurrence = self.store.list_occurrences()[0]
        joined = await self.daemon.join(occurrence['id'])
        await self.until(self.started.is_set)
        self.assertIsNone(self.daemon._session.config()['duration'])
        self.now += 3601
        # Waiting for another fenced heartbeat exercises the monitor, not a sleep guess.
        heartbeat = asyncio.Event()
        original = self.store.heartbeat_run
        loop = asyncio.get_running_loop()
        def monitored_heartbeat(*args):
            original(*args)
            loop.call_soon_threadsafe(heartbeat.set)
        with patch.object(self.store,'heartbeat_run',side_effect=monitored_heartbeat):
            await self.until(heartbeat.is_set)
            self.assertFalse(self.daemon._session.stopped())
            self.now += 3600
            await self.until(lambda:self.store.get_run(joined['session_id'])['state']!='running')
        self.assertEqual(self.store.get_run(joined['session_id'])['end_reason'],'scheduled_end')

    async def test_explicit_duration_cap_stops_before_later_occurrence_end(self):
        self.daemon.settings = self.settings.model_copy(update={'max_duration_seconds':10})
        occurrence = self.store.list_occurrences()[0]
        joined = await self.daemon.join(occurrence['id'])
        await self.until(self.started.is_set)
        self.now += 11
        await self.until(lambda:self.store.get_run(joined['session_id'])['state']!='running')
        self.assertEqual(self.store.get_run(joined['session_id'])['end_reason'],'scheduled_end')

    async def test_recording_authorization_revocation_stops_default_unlimited_capture(self):
        occurrence = self.store.list_occurrences()[0]
        joined = await self.daemon.join(occurrence['id'])
        await self.until(self.started.is_set)
        calendar = self.store.get_calendar(self.calendar_row['id'])
        self.store.save_calendar({**calendar,'recording_authorized':False})
        await self.until(lambda:self.store.get_run(joined['session_id'])['state']!='running')
        self.assertEqual(self.store.get_run(joined['session_id'])['end_reason'],'authorization_revoked')

    async def test_explicit_legacy_summary_lease_is_renewed_until_publication(self):
        original = self.store.heartbeat_summary
        renewed = asyncio.Event()
        calls = []
        lease_now = [100.0]
        loop = asyncio.get_running_loop()
        def heartbeat_summary(*args):
            # This tests renewal, not CPU/filesystem scheduling. Scheduled
            # heartbeat boundaries advance the synthetic elapsed lease clock.
            lease_now[0] += .2
            original(*args)
            calls.append(args)
            if len(calls)>=4:
                loop.call_soon_threadsafe(renewed.set)
        async def progressing_summary(*args):
            await renewed.wait()
            await controlled_summary(*args)
        self.daemon.settings = self.settings.model_copy(update={'summary_lease_seconds':.6})
        self.daemon.summarizer = progressing_summary
        occurrence = self.store.list_occurrences()[0]
        joined = await self.daemon.join(occurrence['id'])
        await self.until(self.started.is_set)
        self.finished.set()
        await self.until(lambda:self.store.get_run(joined['session_id'])['state']!='running')
        await self.daemon.close()
        legacy_job(self.store,joined['session_id'])
        # Capture has already finished: do not change its real boot/lease clock.
        with patch.object(MeetingStore,'_lease_clock',side_effect=lambda:('summary-fixture-boot',lease_now[0])), \
                patch.object(self.store,'heartbeat_summary',side_effect=heartbeat_summary):
            lease=self.store.claim_summary('explicit-legacy-publisher',lease_seconds=.6)
            initial_expiry=lease['expires_boottime']
            publisher=Session(self.root,joined['session_id'],lease=lease)
            async with asyncio.timeout(5):
                await self.daemon._summarize_owned(publisher,None,lease)
            self.assertGreater(lease_now[0],initial_expiry)
            for field,value in (('owner','different-owner'),('token','different-token'),('generation',lease['generation']+1)):
                with self.subTest(field=field),self.assertRaisesRegex(AssistantStoreError,'stale_execution_lease'):
                    original({**lease,field:value})
            self.store.finish_summary(lease)
        self.assertGreaterEqual(len(calls),4)
        self.assertTrue(self.store.summary_source(joined['session_id'])['terminal'])
        self.assertEqual(self.store.summary_state(joined['session_id'])['revision'],1)

    async def test_explicit_legacy_summary_lease_loss_cleans_without_losing_source(self):
        entered, cleaned = asyncio.Event(), asyncio.Event()
        async def stalled_summary(*args):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        original = self.store.heartbeat_summary
        def heartbeat_summary(*args):
            if entered.is_set():
                raise AssistantStoreError('lease_lost')
            original(*args)
        self.daemon.settings = self.settings.model_copy(update={'summary_lease_seconds':.6})
        self.daemon.summarizer = stalled_summary
        occurrence = self.store.list_occurrences()[0]
        joined = await self.daemon.join(occurrence['id'])
        await self.until(self.started.is_set)
        self.finished.set()
        await self.until(lambda:self.store.get_run(joined['session_id'])['state']!='running')
        await self.daemon.close()
        with patch.object(self.store,'heartbeat_summary',side_effect=heartbeat_summary):
            legacy_job(self.store,joined['session_id'])
            lease=self.store.claim_summary('explicit-legacy-publisher')
            publisher=Session(self.root,joined['session_id'],lease=lease)
            with self.assertRaisesRegex(AssistantStoreError,'lease_lost'):
                await self.daemon._summarize_owned(publisher,None,lease)
            self.store.finish_summary(lease,'lease_lost')
        self.assertTrue(cleaned.is_set())
        self.assertIsNone(self.daemon.last_error)
        self.assertIsNone(self.store.summary_state(joined['session_id']))
        self.assertTrue(Session(self.root,joined['session_id']).summary_source()['entries'])

    async def test_paused_scheduler_requires_policy_then_capture_enqueues_and_publishes_summary(self):
        await asyncio.sleep(.05)
        self.assertEqual(self.store.list_runs(),[])
        self.store.update_settings({'scheduler_paused':False})
        await self.until(self.started.is_set)
        row = self.store.list_runs()[0]
        self.assertEqual(row['status']['state'],'recording')
        self.assertEqual(row['occurrence']['event_id'],'event-one')
        self.finished.set()
        await self.until(lambda:self.store.get_run(row['session_id'])['state']!='running')
        await self.until(lambda:self.store.list_summary_jobs() and self.store.list_summary_jobs()[0]['state']=='published')
        self.assertEqual(self.store.summary_state(row['session_id'])['revision'],1)
        self.assertEqual(self.store.get_run(row['session_id'])['state'],'stopped')
        self.assertTrue(any(request.url.path.endswith('/events/event-one') for request in self.requests))
        self.assertEqual(list(self.root.rglob('*.md')),[])

    async def test_prejoin_recheck_rejects_cancelled_event_without_executing_runner(self):
        occurrence = self.store.list_occurrences()[0]
        self.events['event-one'] = {'id':'event-one','status':'cancelled'}
        with self.assertRaisesRegex(AssistantStoreError,'occurrence_outside_join_window'):
            await self.daemon.join(occurrence['id'])
        self.assertFalse(self.started.is_set())
        self.assertEqual(self.store.list_runs(),[])
        self.assertEqual(self.store.get_occurrence(occurrence['id'])['state'],'cancelled')

    async def test_reschedule_before_join_preserves_occurrence_but_defers_capture(self):
        occurrence = self.store.list_occurrences()[0]
        self.events['event-one'] = self.event('event-one',self.now+120)
        with self.assertRaisesRegex(AssistantStoreError,'occurrence_outside_join_window'):
            await self.daemon.join(occurrence['id'])
        self.assertEqual(self.store.get_occurrence(occurrence['id'])['start_at'],iso(self.now+120))
        self.assertFalse(self.started.is_set())
        self.now += 121
        joined = await self.daemon.join(occurrence['id'])
        await self.until(self.started.is_set)
        self.assertEqual(self.store.get_run(joined['session_id'])['occurrence_id'],occurrence['id'])
        self.finished.set()

    async def test_calendar_cancellation_stops_active_capture_without_rejoining(self):
        occurrence = self.store.list_occurrences()[0]
        result = await self.daemon.join(occurrence['id'])
        await self.until(self.started.is_set)
        self.events['event-one'] = {'id':'event-one','status':'cancelled'}
        await self.daemon.poll_calendar()
        await self.until(lambda:self.store.get_run(result['session_id'])['state']!='running')
        row = self.store.get_run(result['session_id'])
        self.assertEqual(row['end_reason'],'calendar_cancelled')
        self.assertTrue(row['source']['terminal'])
        with self.assertRaises(AssistantStoreError):
            await self.daemon.join(occurrence['id'])

    async def test_stop_during_claim_before_session_creation_is_not_lost(self):
        occurrence = self.store.list_occurrences()[0]
        lease = self.store.claim_run(occurrence['id'],'fixture-owner')
        self.store.request_stop(lease['session_id'])
        run = self.store.get_run(lease['session_id'])
        session = Session(self.root,lease['session_id'],lease=lease).create(dict(
            provider='codex',source='meeting',prior_context=run['prior_context']))
        self.assertTrue(session.stopped())
        self.assertTrue(self.store.stop_requested(lease))
        self.store.finish_run(lease,'stopped','operator_stop')

    async def test_speaking_revocation_during_enable_await_closes_output(self):
        occurrence = self.store.list_occurrences()[0]
        result = await self.daemon.join(occurrence['id'])
        await self.until(self.started.is_set)
        calendar = self.store.get_calendar(self.calendar_row['id'])
        self.store.save_calendar({**calendar,'speaking_authorized':True})
        entered,release = asyncio.Event(),asyncio.Event()
        class Controller:
            enabled = False
            async def enable_output(self):
                entered.set()
                await release.wait()
                self.enabled = True
            async def disable_output(self):
                self.enabled = False
            def status(self):
                return {'state':'starting','output_enabled':self.enabled}
        controller = Controller()
        self.daemon._session.response_controller = controller
        action = asyncio.create_task(self.daemon.set_output(result['session_id'],True))
        await entered.wait()
        calendar = self.store.get_calendar(self.calendar_row['id'])
        self.store.save_calendar({**calendar,'speaking_authorized':False})
        release.set()
        with self.assertRaisesRegex(AssistantStoreError,'speaking_authorization_revoked'):
            await action
        self.assertFalse(controller.enabled)
        self.finished.set()

    async def test_only_one_daemon_can_own_same_data_root(self):
        other = AssistantDaemon(self.settings,store=self.store,runner=lambda session:None)
        with self.assertRaisesRegex(AssistantStoreError,'daemon_already_running'):
            await other.start()
        await other.close()
    async def test_capture_expiring_after_start_blocks_new_meeting_and_pauses_scheduler(self):
        occurrence=self.store.list_occurrences()[0]
        lease=self.store.claim_run(occurrence['id'],'interrupted-owner')
        self.store.recover_expired(now_boottime=lease['expires_boottime']+1)
        other=self.store.create_manual_occurrence(dict(title='Next controlled occurrence',
            join_url='https://teams.microsoft.com/meet/67890',start_at=iso(self.now-1),end_at=iso(self.now+600),
            recording_authorized=True,speaking_authorized=False))
        with self.assertRaisesRegex(AssistantStoreError,'capture_unavailable'):
            await self.daemon.join(other['id'])
        self.assertTrue(self.daemon.status()['capture_interlock'])
        self.assertTrue(self.store.settings()['scheduler_paused'])
        self.assertIsNone(self.daemon.status()['active_session_id'])


    async def test_restart_preserves_historical_outbox_without_running_model_or_capture(self):
        await self.daemon.close()
        calendar = self.store.get_calendar(self.calendar_row['id'])
        self.store.save_calendar({**calendar,'enabled':False})
        occurrence = self.store.create_manual_occurrence(dict(title='Controlled restart',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(self.now-1),end_at=iso(self.now+600),
            recording_authorized=True,speaking_authorized=False))
        lease = self.store.claim_run(occurrence['id'],'interrupted-owner')
        run = self.store.get_run(lease['session_id'])
        session = Session(self.root,lease['session_id'],lease=lease).create(dict(
            provider='codex',source='meeting',prior_context=run['prior_context']))
        session.append_text('Controlled restart source.',0)
        session.update(state='stopped',cleanup_status='cleaned')
        self.store.finish_run(lease,'stopped','operator_stop')
        legacy_job(self.store,session.ident)
        summary_lease = self.store.claim_summary('interrupted-summarizer')
        publisher = Session(self.root,session.ident,lease=summary_lease)
        await controlled_summary(publisher,None,None,None)
        self.store.recover_expired(now_boottime=summary_lease['expires_boottime']+1)
        async def forbidden(*args):
            self.fail('Already-published source must not replay model or meeting')
        restarted = AssistantDaemon(self.settings,store=self.store,runner=forbidden,summarizer=forbidden)
        await restarted.start()
        try:
            await self.until(lambda:self.store.list_summary_jobs()[0]['state']=='published')
            self.assertEqual(self.store.get_run(session.ident)['state'],'stopped')
            self.assertEqual(self.store.summary_state(session.ident)['revision'],1)
            self.assertIsNone(restarted.status()['active_session_id'])
        finally:
            await restarted.close()

    async def test_restart_processes_pending_summary_and_supports_manual_retry(self):
        await self.daemon.close()
        calendar=self.store.get_calendar(self.calendar_row['id'])
        self.store.save_calendar({**calendar,'enabled':False})
        occurrence=self.store.create_manual_occurrence(dict(title='Synthetic retained history',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(self.now-1),end_at=iso(self.now+600),
            recording_authorized=True,speaking_authorized=False))
        lease=self.store.claim_run(occurrence['id'],'fixture-old-owner')
        session=Session(self.root,lease['session_id'],lease=lease).create(dict(provider='codex',source='meeting',
            prior_context=self.store.get_run(lease['session_id'])['prior_context']))
        session.append_text('Preserved synthetic transcript.',0)
        session.update(state='stopped',cleanup_status='cleaned')
        self.store.finish_run(lease,'stopped','operator_stop')
        called=[]
        async def failing_first(*args):
            called.append(True)
            if len(called)==1:
                raise AssistantStoreError('controlled_summary_failure')
            return await controlled_summary(*args)
        async def forbidden(*args):raise AssertionError('must not rejoin capture on restart')
        restarted=AssistantDaemon(self.settings,store=self.store,runner=forbidden,summarizer=failing_first)
        await restarted.start()
        try:
            await self.until(lambda:self.store.list_summary_jobs()[0]['state']=='failed')
            self.assertEqual(len(called),1)
            await restarted.retry_summary(session.ident)
            await self.until(lambda:self.store.list_summary_jobs()[0]['state']=='published')
            self.assertEqual(len(called),2)
            self.assertEqual(self.store.summary_state(session.ident)['revision'],1)
            self.assertEqual([row['text'] for row in session.entries()],['Preserved synthetic transcript.'])
        finally:
            await restarted.close()


if __name__=='__main__':
    unittest.main()
