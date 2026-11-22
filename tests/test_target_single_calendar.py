"""Single Calendar grant through authenticated form, same-UID control and a synthetic runner."""
import asyncio
import time
import unittest
from unittest.mock import patch

import httpx

from deploy.target_runtime import control
from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials
from teams_transcription.assistant_store import AssistantStoreError
from teams_transcription.teams_acceptance import FINISHED_EVENT, VERSION
from teams_transcription.teams_local_control import LocalControlError, LocalTeamsControl
from test_admin_web import ORIGIN, PASSWORD
import test_admin_web as admin_fixture
from test_assistant_daemon import iso
import test_teams_local_control as fixture


class Controller:
    enabled = False

    def status(self):
        return dict(state='sleeping', output_enabled=self.enabled)

    async def enable_output(self):
        self.enabled = True

    async def disable_output(self):
        self.enabled = False


class SingleCalendarTests(unittest.IsolatedAsyncioTestCase):
    event = fixture.LocalControlTests.event
    until = fixture.LocalControlTests.until
    login = admin_fixture.AdminWebTests.login

    async def asyncSetUp(self):
        await fixture.LocalControlTests.asyncSetUp(self)
        self.calendar = self.store.save_calendar({**self.calendar_row, 'recording_authorized':False})
        first = self.events['target'] = self.event('target')
        first.update(recurringEventId='fixture-series', originalStartTime=first['start'])
        sibling = self.event('sibling')
        sibling.update(recurringEventId='fixture-series', originalStartTime={'dateTime':iso(self.now-86400)})
        self.events['sibling'] = sibling
        await self.daemon.poll_calendar()
        occurrences = {row['event_id']:row for row in self.store.list_occurrences()}
        self.target, self.sibling = occurrences['target'], occurrences['sibling']
        self.assertEqual(self.target['series_id'], self.sibling['series_id'])
        self.assertEqual(self.target['start_at'], self.sibling['start_at'])
        self.assertTrue(self.calendar['enabled'])
        self.assertFalse(self.target['recording_authorized'])
        self.assertFalse(self.calendar['speaking_authorized'])
        initialize_admin_credentials(self.settings, PASSWORD)
        app = create_admin_app(self.daemon,self.settings)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)
        self.token = await self.login()
        local = LocalTeamsControl(self.daemon,self.settings)
        await local.__aenter__()
        self.addAsyncCleanup(local.__aexit__,None,None,None)
        self.controller = Controller()
        self.prepared = asyncio.Event()
        self.cleanup = 'cleaned'
        self.daemon.runner = self.runner

    async def runner(self, session):
        session.update(state='prepared', rx_prepared=dict(generation='a'*32,meeting_id=session.ident,
            expires_at_monotonic=time.monotonic()+60))
        while not (session.path/'owner-ready.json').is_file() and not session.stopped():
            await asyncio.sleep(.01)
        if not session.stopped():
            self.prepared.set()
            session.update(state='recording')
            session.response_controller = self.controller
        while not session.stopped():
            await asyncio.sleep(.01)
        session.update(state='stopped',cleanup_status=self.cleanup)
        session.event(FINISHED_EVENT,{})

    async def post(self, row=None, *, token=None, origin=ORIGIN, **overrides):
        row = row or self.target
        form = dict(id=row['id'], revision=row['revision'], present='on', media='on')
        return await self.client.post('/actions/target-consent',data={**form,**overrides,
            'csrf':self.token if token is None else token},headers={'Origin':origin,'Accept':'application/json'})

    async def issue(self):
        response = await self.post()
        self.assertEqual(response.status_code,200,response.text)
        binding = response.json()['result']
        self.assertEqual(binding['expires_in_seconds'],300)
        return binding

    async def join(self, grant, row=None, **changes):
        return await control(self.root,'join',(row or self.target)['id'],
            changes.get('request',grant['request_sha256']),revision=changes.get('revision',grant['revision']))

    async def run_granted(self, grant):
        result = await self.join(grant)
        await asyncio.wait_for(self.prepared.wait(),3)
        await self.until(lambda:self.controller.enabled)
        return result['result']['session_id']

    async def test_authenticated_grant_claim_monitor_disable_and_stop(self):
        grant = await self.issue()
        self.assertEqual(self.store.get_calendar(self.calendar['id']),self.calendar)
        self.assertEqual(self.store.get_occurrence(self.sibling['id']),self.sibling)
        self.assertFalse(self.store.get_occurrence(self.target['id'])['speaking_authorized'])
        self.assertEqual({row['id'] for row in self.store.due_occurrences(self.now)}, {self.occurrence['id']})
        legacy = dict(version=VERSION,request_sha256=grant['request_sha256'],
            occurrence_revision=grant['revision'],maximum_seconds=None)
        with self.assertRaises(AssistantStoreError):
            self.store.claim_run(self.target['id'],'ordinary')
        with self.assertRaises(AssistantStoreError):
            self.store.claim_run(self.target['id'],'legacy-acceptance',acceptance=legacy)
        with self.assertRaises(LocalControlError):
            await self.join(grant,self.sibling,revision=self.sibling['revision'])
        with self.assertRaises(LocalControlError):
            await self.join(grant,revision=grant['revision']-1)
        ident = await self.run_granted(grant)
        request = grant['request_sha256']
        with self.assertRaisesRegex(ValueError,'authorized_occurrence_required'):
            await control(self.root,'view',self.sibling['id'])
        observed = await control(self.root,'view',self.target['id'])
        self.assertEqual(observed['session_id'],ident)
        self.assertEqual(observed['acceptance']['request_sha256'],request)
        self.assertTrue(observed['acceptance']['target_calendar_claimed'])
        self.assertTrue(observed['response_ready'])
        self.assertFalse(observed['recording_authorized'])
        self.assertFalse(observed['speaking_authorized'])
        with self.assertRaises(LocalControlError):
            await control(self.root,'output-disable',ident,'b'*64)
        await control(self.root,'output-disable',ident,request)
        await self.daemon.poll_calendar()
        with self.assertRaisesRegex(AssistantStoreError,'speaking_authorization_required'):
            await self.daemon.set_output(ident,True)
        self.assertFalse(self.controller.enabled)
        self.assertEqual(self.store.get_run(ident)['state'],'running')
        with self.assertRaises(LocalControlError):
            await control(self.root,'stop',ident,'b'*64)
        self.assertEqual(await control(self.root,'stop',ident,request),dict(ok=True,cleanup_verified=True))
        observed = await control(self.root,'view',self.target['id'])
        self.assertTrue(observed['acceptance']['cleanup_verified'])
        with self.assertRaises(LocalControlError):
            await self.join(grant)
        self.assertEqual(self.store.list_summary_jobs(),[])
        self.assertEqual(self.store.get_calendar(self.calendar['id']),self.calendar)

    async def test_form_guards_and_expired_grant(self):
        for options in [dict(present=''),dict(media=''),dict(revision=0),dict(token='a'*64),
                dict(origin='http://foreign.invalid'),dict(recording_authorized='on')]:
            with self.subTest(options=options):
                self.assertGreaterEqual((await self.post(**options)).status_code,400)
        no_media = self.store.create_manual_occurrence(dict(title='Synthetic manual without grants',
            join_url=self.occurrence['join_url'],start_at=self.target['start_at'],end_at=self.target['end_at']))
        self.assertEqual((await self.post(no_media)).status_code,422)
        self.store.update_settings({'scheduler_paused':False})
        try:
            self.assertEqual((await self.post()).status_code,422)
        finally:
            self.store.update_settings({'scheduler_paused':True})
        grant = await self.issue()
        self.assertEqual((await self.post()).status_code,422)
        boot, elapsed = self.store._lease_clock()
        with patch.object(self.store,'_lease_clock',return_value=(boot,elapsed+301)):
            with self.assertRaises(LocalControlError):
                await self.join(grant)
        self.assertEqual(self.store.list_runs(),[])

    async def test_after_provider_await_disabled_then_enabled_calendar_cannot_restore_grant(self):
        grant = await self.issue()

        async def admission():
            current = self.store.save_calendar({**self.calendar,'enabled':False})
            self.store.save_calendar({**current,'enabled':True})
            await self.daemon.poll_calendar()

        with patch.object(self.daemon,'_admission',side_effect=admission) as invoked:
            with self.assertRaises(LocalControlError):
                await self.join(grant)
            invoked.assert_awaited_once()
        self.assertEqual(self.store.list_runs(),[])

    async def test_reschedule_cancel_and_wrong_fresh_identity_denied_before_admission(self):
        for mutation in ({'id':'unrelated'}, {'status':'cancelled'},
                {'start':{'dateTime':iso(self.now+30)}}):
            with self.subTest(mutation=mutation):
                original = self.events['target']
                grant = await self.issue()
                self.events['target'] = {**original,**mutation}
                try:
                    with patch.object(self.daemon,'_admission',side_effect=AssertionError('provider not reached')):
                        with self.assertRaises(LocalControlError):
                            await self.join(grant)
                finally:
                    self.events['target'] = original
                    await self.daemon.poll_calendar()
                    self.target = self.store.get_occurrence(self.target['id'])
        self.assertEqual(self.store.list_runs(),[])

    async def test_revocation_during_output_await_stays_muted_after_refresh(self):
        grant = await self.issue()
        entered, release = asyncio.Event(), asyncio.Event()

        async def stalled_enable():
            entered.set()
            await release.wait()
            self.controller.enabled = True

        with patch.object(self.controller,'enable_output',side_effect=stalled_enable):
            ident = (await self.join(grant))['result']['session_id']
            try:
                await asyncio.wait_for(entered.wait(),3)
                disabling = asyncio.create_task(control(self.root,'output-disable',ident,grant['request_sha256']))
                try:
                    await self.until(lambda:self.store.get_occurrence(self.target['id']).get('target_speaking_revoked'))
                    await self.daemon.poll_calendar()
                finally:
                    release.set()
                    await disabling
            finally:
                release.set()
        self.assertFalse(self.controller.enabled)
        self.assertEqual(self.store.get_run(ident)['state'],'running')
        with self.assertRaisesRegex(AssistantStoreError,'speaking_authorization_required'):
            await self.daemon.set_output(ident,True)
        await control(self.root,'stop',ident,grant['request_sha256'])

    async def test_active_lease_and_unknown_cleanup_prevent_a_second_grant(self):
        self.cleanup = 'unknown'
        grant = await self.issue()
        ident = await self.run_granted(grant)
        with self.assertRaises(AssistantStoreError):
            self.store.issue_target_consent(self.sibling['id'],self.sibling['revision'],present=True,media=True)
        with self.assertRaisesRegex(ValueError,'stop_cleanup_unverified'):
            await control(self.root,'stop',ident,grant['request_sha256'])
        self.assertTrue(self.store.capture_interlocks())
        self.assertEqual((await self.post(self.sibling)).status_code,422)
        self.assertTrue(self.store.settings()['scheduler_paused'])

    async def test_calendar_reconciliation_stops_capture_without_restoring_grant(self):
        grant = await self.issue()
        ident = await self.run_granted(grant)
        # A reschedule invalidates the grant even if a later refresh restores
        # the previous time, before the monitor has observed the edit.
        original = self.events['target']
        self.events['target'] = {**original,'start':{'dateTime':iso(self.now+30)}}
        await self.daemon.poll_calendar()
        self.events['target'] = original
        await self.daemon.poll_calendar()
        await self.until(lambda:self.daemon.status()['active_session_id'] is None)
        self.assertFalse(self.controller.enabled)
        self.assertEqual(self.store.get_run(ident)['end_reason'],'authorization_revoked')
        with self.assertRaises(LocalControlError):
            await self.join(grant)
