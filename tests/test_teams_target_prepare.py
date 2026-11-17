"""Bounded same-UID preparation of one approved stored target, no real meeting."""
import asyncio
import copy
import unittest

from teams_transcription.teams_local_control import LocalTeamsControl, LocalControlError
from teams_transcription.assistant_store import AssistantStoreError
import test_teams_local_control as control_fixture
from test_assistant_daemon import iso


class TargetPrepareTests(unittest.IsolatedAsyncioTestCase):
    event=control_fixture.LocalControlTests.event
    until=control_fixture.LocalControlTests.until
    call=control_fixture.LocalControlTests.call

    async def asyncSetUp(self):
        await control_fixture.LocalControlTests.asyncSetUp(self)
        first=await self.daemon.join(self.occurrence['id'])
        await asyncio.wait_for(self.started.wait(),2)
        await self.daemon.stop(first['session_id'])
        await self.until(lambda:self.daemon.status()['active_session_id'] is None)
        self.original=self.store.get_occurrence(self.occurrence['id'])
        self.assertEqual(self.original['revision'],3)
        self.root.joinpath('.deployment-maintenance').touch()
        self.prep=dict(source_id=self.original['id'],source_revision=3,request_sha256='c'*64,
            mode='owner_immediate')

    async def test_prepare_reuses_stored_link_without_login_join_or_rewriting_old_run(self):
        before=copy.deepcopy(self.original)
        async with LocalTeamsControl(self.daemon,self.settings):
            result=await self.call('prepare',self.prep)
            self.assertTrue(result['prepared'])
            target=self.store.get_occurrence(result['occurrence_id'])
            self.assertNotEqual(target['id'],before['id'])
            self.assertEqual(target['state'],'pending');self.assertEqual(target['revision'],1)
            self.assertEqual(target['join_url'],before['join_url'])
            self.assertEqual(target['series_id'],before['series_id'])
            self.assertIsNone(target['start_at']);self.assertIsNone(target['end_at'])
            self.assertEqual(target['local_preparation']['mode'],'owner_immediate')
            self.assertTrue(target['recording_authorized'] and target['speaking_authorized'])
            self.assertEqual(self.store.get_occurrence(before['id']),before)
            self.assertIsNone(self.daemon.status()['active_session_id'])
            repeated=await self.call('prepare',self.prep)
            self.assertEqual(repeated,result)
            view=await self.call('view',dict(id=target['id']))
            self.assertEqual(view['preparation_request_sha256'],'c'*64)
            self.assertNotIn('join_url',view)

    async def test_prepare_refuses_stale_source_arbitrary_url_injected_schedule_or_unpaused(self):
        async with LocalTeamsControl(self.daemon,self.settings):
            before=len(self.store.list_occurrences())
            for fields in ({**self.prep,'source_revision':1},{**self.prep,'join_url':'https://private.invalid'},
                    {k:v for k,v in self.prep.items() if k!='mode'},
                    {**self.prep,'end_at':iso(self.now+3600)}):
                with self.assertRaises(LocalControlError):await self.call('prepare',fields)
            self.store.update_settings({'scheduler_paused':False})
            with self.assertRaises(LocalControlError):await self.call('prepare',self.prep)
            self.assertEqual(len(self.store.list_occurrences()),before)

    async def test_used_preparation_never_creates_another_target(self):
        async with LocalTeamsControl(self.daemon,self.settings):
            result=await self.call('prepare',self.prep)
            self.root.joinpath('.deployment-maintenance').unlink()
            self.now+=48*3600;self.started.clear()  # preparing does not start a join/meeting timer
            with self.assertRaisesRegex(AssistantStoreError,'teams_preparation_acceptance_required'):
                await self.daemon.join(result['occurrence_id'])
            joined=await self.daemon.join(result['occurrence_id'],acceptance=dict(version='teams-single-v1',
                request_sha256='d'*64,maximum_seconds=None,occurrence_revision=1))
            await asyncio.wait_for(self.started.wait(),2)
            await self.daemon.stop(joined['session_id'])
            await self.until(lambda:self.daemon.status()['active_session_id'] is None)
            self.root.joinpath('.deployment-maintenance').touch()
            before=len(self.store.list_occurrences())
            with self.assertRaises(LocalControlError):await self.call('prepare',self.prep)
            self.assertEqual(len(self.store.list_occurrences()),before)

    async def test_skipped_source_cannot_be_reauthorized_by_preparation(self):
        source=self.store.create_manual_occurrence(dict(title='Synthetic skipped target',
            join_url=self.original['join_url'],start_at=iso(self.now),end_at=iso(self.now+3600),
            recording_authorized=True,speaking_authorized=True))
        self.store.skip_occurrence(source['id'])
        async with LocalTeamsControl(self.daemon,self.settings):
            fields={**self.prep,'source_id':source['id'],'source_revision':2}
            before=len(self.store.list_occurrences())
            with self.assertRaises(LocalControlError):await self.call('prepare',fields)
            self.assertEqual(len(self.store.list_occurrences()),before)

    async def test_immediate_run_has_no_hidden_end_or_calendar_lateness(self):
        from unittest.mock import patch
        from teams_transcription.storage import Session
        async with LocalTeamsControl(self.daemon,self.settings):
            prepared=await self.call('prepare',self.prep)
            self.now+=48*3600;self.started.clear()
            self.root.joinpath('.deployment-maintenance').unlink()
            joined=await self.call('join',dict(id=prepared['occurrence_id'],acceptance_request='d'*64,
                acceptance_revision=1,acceptance_stop_policy='owner_or_meeting_end'))
            ident=joined['result']['session_id']
            await asyncio.wait_for(self.started.wait(),2)
            self.assertIsNone(Session(self.root,ident).config()['duration'])
            observed=asyncio.Event();loop=asyncio.get_running_loop();heartbeat=self.store.heartbeat_run
            def observe(*args,**kwargs):
                result=heartbeat(*args,**kwargs)
                loop.call_soon_threadsafe(observed.set)
                return result
            self.now+=72*3600
            with patch.object(self.store,'heartbeat_run',side_effect=observe):
                await asyncio.wait_for(observed.wait(),2)
            self.assertEqual(self.daemon.status()['active_session_id'],ident)
            await self.call('stop',dict(id=ident,acceptance_request='d'*64))
            await self.until(lambda:self.daemon.status()['active_session_id'] is None)
            self.assertEqual(self.store.get_run(ident)['end_reason'],'operator_stop')

    async def test_confirmed_meeting_end_finishes_without_scheduled_deadline(self):
        from teams_transcription.teams_acceptance import FINISHED_EVENT
        ended=asyncio.Event();self.started.clear()
        async def runner(session):
            session.update(state='recording');self.started.set()
            await ended.wait()
            session.event('meeting_ended',{'reason':'confirmed_end'})
            session.update(state='stopped',cleanup_status='cleaned',end_reason='confirmed_end')
            session.event(FINISHED_EVENT,{})
        self.daemon.runner=runner
        async with LocalTeamsControl(self.daemon,self.settings):
            prepared=await self.call('prepare',self.prep)
            self.root.joinpath('.deployment-maintenance').unlink()
            joined=await self.call('join',dict(id=prepared['occurrence_id'],acceptance_request='e'*64,
                acceptance_revision=1,acceptance_stop_policy='owner_or_meeting_end'))
            await asyncio.wait_for(self.started.wait(),2)
            ended.set()
            await self.until(lambda:self.daemon.status()['active_session_id'] is None)
            run=self.store.get_run(joined['result']['session_id'])
            self.assertEqual(run['end_reason'],'confirmed_end')
            self.assertEqual(run['status']['cleanup_status'],'cleaned')

    async def test_heartbeat_fence_failure_still_stops_immediate_run(self):
        from unittest.mock import patch
        self.started.clear()
        async with LocalTeamsControl(self.daemon,self.settings):
            prepared=await self.call('prepare',self.prep)
            self.root.joinpath('.deployment-maintenance').unlink()
            joined=await self.call('join',dict(id=prepared['occurrence_id'],acceptance_request='f'*64,
                acceptance_revision=1,acceptance_stop_policy='owner_or_meeting_end'))
            await asyncio.wait_for(self.started.wait(),2)
            heartbeat=self.store.heartbeat_run
            def invalid_owner(lease,*args,**kwargs):return heartbeat({**lease,'token':'wrong'},*args,**kwargs)
            with patch.object(self.store,'heartbeat_run',side_effect=invalid_owner):
                await self.until(lambda:self.daemon.status()['active_session_id'] is None)
            run=self.store.get_run(joined['result']['session_id'])
            self.assertEqual(run['state'],'failed')
            self.assertEqual(run['status']['cleanup_status'],'cleaned')

    async def test_null_time_contract_cannot_be_used_by_calendar_or_with_injected_end(self):
        from teams_transcription.teams_acceptance import is_owner_immediate
        async with LocalTeamsControl(self.daemon,self.settings):
            prepared=await self.call('prepare',self.prep)
            occurrence=self.store.get_occurrence(prepared['occurrence_id'])
            self.assertTrue(is_owner_immediate(occurrence))
            for change in ({'calendar_ref':'calendar-fixture'},{'end_at':iso(self.now+120)},
                    {'start_at':iso(self.now)},{'max_lateness_seconds':1800}):
                with self.subTest(change=change),self.assertRaisesRegex(ValueError,'teams_immediate_contract_rejected'):
                    is_owner_immediate({**occurrence,**change})
