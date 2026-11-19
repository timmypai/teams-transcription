"""Current-target fixed helper -> real socket/daemon/SQLite; injected runner only."""
import asyncio
import json
import unittest
from unittest.mock import patch

from deploy import target_runtime as runtime
from teams_transcription.teams_local_control import LocalTeamsControl, LocalControlError
import test_teams_local_control as fixture


class TargetControlPathTests(unittest.IsolatedAsyncioTestCase):
    event=fixture.LocalControlTests.event
    until=fixture.LocalControlTests.until

    async def asyncSetUp(self):
        await fixture.LocalControlTests.asyncSetUp(self)

    async def test_join_bridge_reaches_real_daemon(self):
        # RED on real main's helper, not an import/API-presence assertion.
        fields=self.fields
        issue=getattr(self.store,'issue_target_consent',None)
        if issue:
            consent=issue(self.occurrence['id'],1,present=True,media=True)
            fields={**fields,'acceptance_request':consent['request_sha256'],
                'acceptance_revision':consent['revision']}
        async with LocalTeamsControl(self.daemon,self.settings):
            try:
                import inspect
                extra=dict(revision=fields['acceptance_revision']) if 'revision' in inspect.signature(runtime.control).parameters else {}
                result=await runtime.control(self.root,'join',self.occurrence['id'],
                    fields['acceptance_request'],**extra)
            except (ValueError,TypeError):
                self.fail('current-target Join bridge rejected before daemon claim')
            await asyncio.wait_for(self.started.wait(),2)
            self.assertEqual(self.daemon.status()['active_session_id'],result['result']['session_id'])
            with self.assertRaises((ValueError,LocalControlError)):
                await runtime.control(self.root,'join',self.occurrence['id'],
                    fields['acceptance_request'],revision=fields['acceptance_revision'])

    async def test_missing_expired_wrong_binding_and_changed_revision_fail_before_claim(self):
        async with LocalTeamsControl(self.daemon,self.settings):
            with self.assertRaises((ValueError,LocalControlError)):
                await runtime.control(self.root,'join',self.occurrence['id'],'a'*64,revision=1)
            consent=self.store.issue_target_consent(self.occurrence['id'],1,present=True,media=True)
            for request,revision in [('b'*64,consent['revision']),
                    (consent['request_sha256'],consent['revision']+1)]:
                with self.assertRaises((ValueError,LocalControlError)):
                    await runtime.control(self.root,'join',self.occurrence['id'],request,revision=revision)
            boot,now=self.store._lease_clock()
            with patch.object(self.store,'_lease_clock',return_value=(boot,now+301)):
                with self.assertRaises((ValueError,LocalControlError)):
                    await runtime.control(self.root,'join',self.occurrence['id'],
                        consent['request_sha256'],revision=consent['revision'])
            self.assertIsNone(self.daemon.status()['active_session_id'])
            self.assertEqual(self.store.list_runs(),[])

    async def test_disable_stays_revoked_and_stop_requires_durable_clean_receipt(self):
        from teams_transcription.teams_acceptance import FINISHED_EVENT
        class Controller:
            def __init__(self):self.enabled=False
            def status(self):return dict(state='sleeping',output_enabled=self.enabled)
            async def enable_output(self):self.enabled=True
            async def disable_output(self):self.enabled=False
        controller=Controller()
        async def runner(session):
            session.response_controller=controller
            session.update(state='recording');self.started.set()
            while not session.stopped():await asyncio.sleep(.01)
            session.update(state='stopped',cleanup_status='cleaned')
            session.event(FINISHED_EVENT,{})
        self.daemon.runner=runner
        consent=self.store.issue_target_consent(self.occurrence['id'],1,present=True,media=True)
        request=consent['request_sha256']
        async with LocalTeamsControl(self.daemon,self.settings):
            joined=await runtime.control(self.root,'join',self.occurrence['id'],request,revision=consent['revision'])
            ident=joined['result']['session_id'];await asyncio.wait_for(self.started.wait(),2)
            await self.until(lambda:controller.enabled)
            with self.assertRaises((ValueError,LocalControlError)):
                await runtime.control(self.root,'output-disable',ident,'b'*64)
            await runtime.control(self.root,'output-disable',ident,request)
            await asyncio.sleep(.6)  # a full original monitor interval
            self.assertFalse(controller.enabled)
            self.assertFalse(self.store.get_occurrence(self.occurrence['id'])['speaking_authorized'])
            result=await runtime.control(self.root,'stop',ident,request)
            self.assertTrue(result['cleanup_verified'])
            self.assertIsNone(self.daemon.status()['active_session_id'])
            self.assertTrue((await runtime.control(self.root,'stop',ident,request))['cleanup_verified'])
            with self.assertRaises((ValueError,LocalControlError)):
                await runtime.control(self.root,'stop',ident,'b'*64)

    async def test_unknown_cleanup_never_becomes_success_or_allows_another_join(self):
        from teams_transcription.teams_acceptance import FINISHED_EVENT
        async def runner(session):
            session.update(state='recording');self.started.set()
            while not session.stopped():await asyncio.sleep(.01)
            session.update(state='stopped',cleanup_status='unknown')
            session.event(FINISHED_EVENT,{})
        self.daemon.runner=runner
        consent=self.store.issue_target_consent(self.occurrence['id'],1,present=True,media=True)
        async with LocalTeamsControl(self.daemon,self.settings):
            result=await runtime.control(self.root,'join',self.occurrence['id'],consent['request_sha256'],revision=consent['revision'])
            await asyncio.wait_for(self.started.wait(),2)
            with self.assertRaisesRegex(ValueError,'stop_cleanup_unverified'):
                await runtime.control(self.root,'stop',result['result']['session_id'],consent['request_sha256'])
            self.assertTrue(self.store.capture_interlocks())
            self.assertTrue(self.store.settings()['scheduler_paused'])

    async def test_recheck_after_provider_await_and_cross_occurrence_or_boot_refuses(self):
        from test_assistant_daemon import iso
        consent=self.store.issue_target_consent(self.occurrence['id'],1,present=True,media=True)
        other=self.store.create_manual_occurrence(dict(title='Another synthetic occurrence',
            join_url=self.occurrence['join_url'],start_at=iso(self.now-1),end_at=iso(self.now+600),
            recording_authorized=True,speaking_authorized=True))
        async with LocalTeamsControl(self.daemon,self.settings):
            with self.assertRaises((ValueError,LocalControlError)):
                await runtime.control(self.root,'join',other['id'],consent['request_sha256'],revision=1)
            boot,clock=self.store._lease_clock()
            with patch.object(self.store,'_lease_clock',return_value=('changed-boot',clock)):
                with self.assertRaises((ValueError,LocalControlError)):
                    await runtime.control(self.root,'join',self.occurrence['id'],consent['request_sha256'],revision=consent['revision'])
            async def admission():
                # Invalidate after preflight, at the real provider await seam.
                self.store.assign_series(self.occurrence['id'],other['series_id'])
            with patch.object(self.daemon,'_admission',side_effect=admission):
                with self.assertRaises((ValueError,LocalControlError)):
                    await runtime.control(self.root,'join',self.occurrence['id'],consent['request_sha256'],revision=consent['revision'])
            self.assertEqual(self.store.list_runs(),[])

    async def test_deny_all_and_unpaused_or_malformed_inputs_refuse_without_admission(self):
        consent=self.store.issue_target_consent(self.occurrence['id'],1,present=True,media=True)
        async with LocalTeamsControl(self.daemon,self.settings):
            self.settings.allowed_egress_hosts=[]
            with patch.object(self.daemon,'_admission',side_effect=AssertionError('no provider for rejected control')):
                with self.assertRaises((ValueError,LocalControlError)):
                    await runtime.control(self.root,'join',self.occurrence['id'],consent['request_sha256'],revision=consent['revision'])
                self.settings.allowed_egress_hosts=['teams.microsoft.com']
                self.store.update_settings({'scheduler_paused':False})
                with self.assertRaises((ValueError,LocalControlError)):
                    await runtime.control(self.root,'join',self.occurrence['id'],consent['request_sha256'],revision=consent['revision'])
            self.store.update_settings({'scheduler_paused':True})
            for action,request,revision in [('join','',2),('join',consent['request_sha256'],True),
                    ('output-disable',consent['request_sha256'],2),('view',consent['request_sha256'],None),
                    ('prepare','',None),('output-enable',consent['request_sha256'],None)]:
                with self.assertRaises(ValueError):
                    await runtime.control(self.root,action,self.occurrence['id'],request,revision=revision)
            self.assertEqual(self.store.list_runs(),[])

    async def test_consent_is_not_a_grant_and_owner_confirmation_is_required(self):
        for present,media in [(False,True),(True,False),('true',True)]:
            with self.assertRaises(ValueError):
                self.store.issue_target_consent(self.occurrence['id'],1,present=present,media=media)
        with self.store._locked() as db,db:
            row=db.execute('SELECT value FROM calendar_occurrences WHERE id=?',(self.occurrence['id'],)).fetchone()
            value=json.loads(row[0]);value['speaking_authorized']=False
            db.execute('UPDATE calendar_occurrences SET value=? WHERE id=?',(json.dumps(value),self.occurrence['id']))
        with self.assertRaises(ValueError):
            self.store.issue_target_consent(self.occurrence['id'],1,present=True,media=True)
        self.assertIsNone(self.daemon.status()['active_session_id'])
