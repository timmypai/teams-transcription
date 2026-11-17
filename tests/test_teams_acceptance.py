"""Single meeting acceptance: synthetic stores/RPC only; never a real Join."""
import tempfile
import unittest
import asyncio
from unittest.mock import AsyncMock
from pathlib import Path

from teams_transcription.assistant_store import AssistantStore, AssistantStoreError
from teams_transcription.storage import Session
from teams_transcription.teams_acceptance import NativeStartBudget, validate_policy, session_budget, receipt


POLICY={'version':'teams-single-v1','request_sha256':'a'*64,'maximum_seconds':None,'occurrence_revision':1}


class NativeBudgetTests(unittest.TestCase):
    def test_explicit_owner_stop_policy_has_no_fixed_work_duration(self):
        policy={**POLICY,'maximum_seconds':None}
        self.assertEqual(validate_policy(policy),policy)
        legacy={**POLICY,'maximum_seconds':120}
        self.assertEqual(validate_policy(legacy),legacy)  # Historical receipts remain readable.
        events=[]
        budget=NativeStartBudget(policy,lambda kind,data:events.append((kind,data)))
        for role in ('A','B'):
            budget.claim(role,'thread');budget.claim(role,'realtime')
            with self.assertRaises(ValueError):budget.claim(role,'thread')
        self.assertEqual(len(events),4)

    def test_each_channel_has_one_durable_thread_and_realtime_attempt(self):
        events=[]
        budget=NativeStartBudget(POLICY,lambda kind,data:events.append((kind,data)))
        budget.claim('A','thread');budget.claim('A','realtime')
        budget.claim('B','thread');budget.claim('B','realtime')
        for role,stage in (('A','thread'),('B','realtime'),('C','thread')):
            with self.subTest(role=role,stage=stage),self.assertRaises(ValueError):budget.claim(role,stage)
        self.assertEqual(len(events),4)

    def test_persistence_failure_and_out_of_order_never_allow_retry(self):
        def failed(*args):raise OSError('private fixture failure')
        budget=NativeStartBudget(POLICY,failed)
        with self.assertRaises(ValueError):budget.claim('A','realtime')
        with self.assertRaises(OSError):budget.claim('A','thread')
        with self.assertRaises(ValueError):budget.claim('B','thread')
        for value in ({**POLICY,'maximum_seconds':121},{**POLICY,'maximum_seconds':True},
                {**POLICY,'request_sha256':'not-a-digest'},{**POLICY,'url':'https://invalid.invalid'}):
            with self.subTest(value=value),self.assertRaises(ValueError):validate_policy(value)


class AcceptanceStoreTests(unittest.TestCase):
    def test_changed_room_revision_cannot_claim_any_run_or_start_either_channel(self):
        with tempfile.TemporaryDirectory() as root:
            store=AssistantStore(root)
            calendar=store.save_calendar(dict(account_id='fixture',calendar_id='fixture@example.invalid',name='Fixture',
                enabled=True,recording_authorized=True,speaking_authorized=True))
            event=dict(event_id='one',title='Synthetic',start_at='2100-01-01T00:00:00+00:00',
                end_at='2100-01-01T00:05:00+00:00',status='confirmed',link_status='valid',all_day=False,
                join_url='https://teams.microsoft.com/meet/11111')
            first=store.sync_occurrences(calendar['id'],[event])[0]
            changed=store.sync_occurrences(calendar['id'],[{**event,'join_url':'https://teams.microsoft.com/meet/22222'}])[0]
            self.assertGreater(changed['revision'],first['revision'])
            with self.assertRaisesRegex(AssistantStoreError,'teams_acceptance_revision_changed'):
                store.claim_run(first['id'],'fixture-owner',acceptance={**POLICY,'occurrence_revision':first['revision']})
            self.assertEqual(store.list_runs(),[])
            self.assertEqual(store.get_occurrence(first['id'])['state'],'pending')

    def test_claim_persists_binding_and_restart_seal_cannot_enqueue_summary(self):
        from datetime import datetime,timezone,timedelta
        with tempfile.TemporaryDirectory() as root:
            store=AssistantStore(root);now=datetime.now(timezone.utc)
            occurrence=store.create_manual_occurrence(dict(title='Synthetic acceptance',
                join_url='https://teams.microsoft.com/meet/12345',start_at=(now-timedelta(seconds=1)).isoformat(),
                end_at=(now+timedelta(minutes=5)).isoformat(),recording_authorized=True,speaking_authorized=True))
            lease=store.claim_run(occurrence['id'],'fixture-owner',acceptance=POLICY)
            self.assertEqual(store.get_run(lease['session_id'])['acceptance_policy'],POLICY)
            session=Session(root,lease['session_id'],lease=lease)
            session.create({'provider':'codex','source':'meeting',
                'prior_context':store.get_run(lease['session_id'])['prior_context']})
            session.append_text('Synthetic current fact.',0)
            first=session_budget(session,POLICY);second=session_budget(session,POLICY)
            first.claim('A','thread')
            with self.assertRaises(ValueError):second.claim('A','thread')
            first.claim('A','realtime')
            session.update(state='stopped',cleanup_status='cleaned')
            AssistantStore(root).finish_run(lease,'stopped','operator_stop')
            self.assertIsNone(AssistantStore(root).claim_summary('fixture-summary-owner'))
            with self.assertRaises(AssistantStoreError):store.claim_run(occurrence['id'],'other',acceptance=POLICY)


class ActualRPCBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_A_and_B_denied_start_cannot_issue_thread_or_native_RPC(self):
        from teams_transcription.live import LiveProvider
        from teams_transcription.b_live import LiveSpeechChannel
        from teams_transcription.b_audio import GatedPCMOutput
        with tempfile.TemporaryDirectory() as root:
            for role,blocked_stage in (('A','thread'),('B','thread'),('A','realtime'),('B','realtime')):
                calls=[]
                def denied(stage):
                    calls.append(stage)
                    if stage==blocked_stage:raise ValueError('teams_acceptance_native_limit')
                channel=(LiveProvider(None,Path(root)/role,AsyncMock(),AsyncMock(),before_native_start=denied)
                    if role=='A' else LiveSpeechChannel(None,Path(root)/role,'synthetic',
                        GatedPCMOutput('synthetic',AsyncMock()),before_native_start=denied))
                channel.rpc=AsyncMock();channel.rpc.notifications=asyncio.Queue()
                channel.rpc.thread.return_value='synthetic-thread'
                channel.rpc.request.return_value={'account':{'type':'chatgpt'}}
                channel.peer=AsyncMock()
                with self.assertRaises(ValueError):await channel.start()
                self.assertEqual(calls,['thread'] if blocked_stage=='thread' else ['thread','realtime'])
                if blocked_stage=='thread':channel.rpc.thread.assert_not_awaited()
                else:channel.rpc.thread.assert_awaited_once()
                self.assertFalse(any(c.args and c.args[0]=='thread/realtime/start'
                    for c in channel.rpc.request.await_args_list))


class AcceptanceDaemonTests(unittest.IsolatedAsyncioTestCase):
    from test_assistant_daemon import AssistantDaemonTests as _Fixture
    event=_Fixture.event
    until=_Fixture.until

    async def asyncSetUp(self):
        await self._Fixture.asyncSetUp(self)

    async def test_owner_controlled_run_remains_active_past_120_until_explicit_stop(self):
        from test_assistant_daemon import iso
        occurrence=self.store.create_manual_occurrence(dict(title='Synthetic owner-controlled meeting',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(self.now-1),end_at=iso(self.now+600),
            recording_authorized=True,speaking_authorized=True))
        seen=asyncio.Event();initial=self.now
        def clock():
            if self.now>=initial+121:seen.set()
            return self.now
        self.daemon.clock=clock
        joined=await self.daemon.join(occurrence['id'],acceptance={**POLICY,'maximum_seconds':None})
        await asyncio.wait_for(self.started.wait(),2)
        session=Session(self.root,joined['session_id'])
        self.assertIsNone(session.config()['duration'])
        from teams_transcription.rx_contract import options
        limits=options(session.config())
        self.assertIsNone(limits['capture_seconds']);self.assertIsNone(limits['total_seconds'])
        self.now=initial+121
        await asyncio.wait_for(seen.wait(),2)
        self.assertEqual(self.daemon.status()['active_session_id'],joined['session_id'])
        self.assertFalse(session.stopped())
        await self.daemon.stop(joined['session_id'])
        await self.until(lambda:self.daemon.status()['active_session_id'] is None)
        self.assertEqual(self.store.get_run(joined['session_id'])['end_reason'],'operator_stop')
        self.assertEqual(self.store.list_summary_jobs(),[])

    async def test_owner_controlled_run_still_stops_at_the_authorized_meeting_end(self):
        from test_assistant_daemon import iso
        occurrence=self.store.create_manual_occurrence(dict(title='Synthetic finite authorized meeting',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(self.now-1),end_at=iso(self.now+600),
            recording_authorized=True,speaking_authorized=True))
        joined=await self.daemon.join(occurrence['id'],acceptance={**POLICY,'maximum_seconds':None})
        await asyncio.wait_for(self.started.wait(),2)
        self.now+=601
        await self.until(lambda:self.daemon.status()['active_session_id'] is None)
        self.assertEqual(self.store.get_run(joined['session_id'])['end_reason'],'scheduled_end')


    async def test_legacy_fixed_duration_cannot_start_a_new_run(self):
        from test_assistant_daemon import iso
        occurrence=self.store.create_manual_occurrence(dict(title='Synthetic controlled meeting',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(self.now-1),end_at=iso(self.now+600),
            recording_authorized=True,speaking_authorized=True))
        with self.assertRaisesRegex(AssistantStoreError,'teams_owner_stop_policy_required'):
            await self.daemon.join(occurrence['id'],acceptance={**POLICY,'maximum_seconds':120})
        self.assertEqual(self.store.list_runs(),[])
        self.assertFalse(self.started.is_set())

    async def test_owner_mode_refuses_a_different_configured_runtime_cap(self):
        from test_assistant_daemon import iso
        self.settings.max_duration_seconds=600
        occurrence=self.store.create_manual_occurrence(dict(title='Synthetic explicit stop',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(self.now-1),end_at=iso(self.now+1800),
            recording_authorized=True,speaking_authorized=True))
        with self.assertRaisesRegex(AssistantStoreError,'teams_owner_stop_configuration_required'):
            await self.daemon.join(occurrence['id'],acceptance=POLICY)
        self.assertEqual(self.store.list_runs(),[])


class AcceptanceAdminTests(unittest.IsolatedAsyncioTestCase):
    from test_admin_web import AdminWebTests as _Fixture
    login=_Fixture.login

    async def asyncSetUp(self):await self._Fixture.asyncSetUp(self)

    async def test_owner_stop_admission_is_explicit_and_keeps_origin_csrf_guards(self):
        from test_admin_web import ORIGIN
        from unittest.mock import patch
        token=await self.login()
        values=dict(id='b'*32,acceptance_request='a'*64,acceptance_revision='1',
            acceptance_stop_policy='owner_or_meeting_end',csrf=token)
        with patch.object(self.daemon,'join',new=AsyncMock(return_value={})) as join:
            for headers in ({'Origin':'https://foreign.invalid','Accept':'application/json'},
                    {'Origin':ORIGIN,'Accept':'application/json'}):
                data=values if headers['Origin']!=ORIGIN else {**values,'csrf':'0'*64}
                self.assertEqual((await self.client.post('/actions/join',data=data,headers=headers)).status_code,403)
            for extras in ({'acceptance_seconds':'120'},{'acceptance_stop_policy':'forever'}):
                response=await self.client.post('/actions/join',data={**values,**extras},
                    headers={'Origin':ORIGIN,'Accept':'application/json'})
                self.assertEqual(response.status_code,422)
            join.assert_not_awaited()
            response=await self.client.post('/actions/join',data=values,
                headers={'Origin':ORIGIN,'Accept':'application/json'})
            self.assertEqual(response.status_code,200)
            self.assertEqual(join.await_args.kwargs,{'acceptance':{**POLICY,'maximum_seconds':None}})

    async def acceptance_fixture(self):
        from datetime import datetime,timezone,timedelta
        now=datetime.now(timezone.utc)
        occurrence=self.daemon.store.create_manual_occurrence(dict(title='PRIVATE-SYNTHETIC-TITLE',
            join_url='https://teams.microsoft.com/meet/12345',start_at=(now-timedelta(seconds=1)).isoformat(),
            end_at=(now+timedelta(minutes=5)).isoformat(),recording_authorized=True,speaking_authorized=True))
        lease=self.daemon.store.claim_run(occurrence['id'],'fixture-owner',acceptance=POLICY)
        session=Session(self.settings.data_root,lease['session_id'],lease=lease)
        session.create({'provider':'codex','source':'meeting',
            'prior_context':self.daemon.store.get_run(lease['session_id'])['prior_context']})
        await self.login()
        path='/api/meetings/'+occurrence['id']+'?view=acceptance'
        return session,lease,path

    async def test_acceptance_view_before_media_start_preserves_unknown_metrics_and_zero_attempts(self):
        session,_,path=await self.acceptance_fixture()
        for fields in ({},{'rx_audio':None},{'rx_audio':{}},
                {'rx_audio':{'live_transport':None}},{'rx_audio':{'live_transport':{}}}):
            with self.subTest(fields=fields):
                session.update(**fields)
                response=await self.client.get(path)
                self.assertEqual(response.status_code,200)
                proof=response.json()['acceptance']
                self.assertEqual((proof['thread_attempts'],proof['native_attempts']),(0,0))
                self.assertFalse(proof['joined'] or proof['count_complete'] or proof['cleanup_verified'])
                for key in ('source_rx_packets','B_tx_packets','B_tx_frames','B_tx_energy'):
                    self.assertIsNone(proof[key])
                self.assertNotIn('PRIVATE-SYNTHETIC-TITLE',response.text)
                self.assertNotIn('https://teams.microsoft.com/meet/',response.text)

    async def test_stopped_before_media_start_reports_cleanup_without_inventing_join_or_metrics(self):
        from teams_transcription.teams_acceptance import FINISHED_EVENT
        session,lease,path=await self.acceptance_fixture()
        session.update(state='stopped',cleanup_status='cleaned',rx_audio={'live_transport':None})
        session.event(FINISHED_EVENT)
        self.daemon.store.finish_run(lease,'stopped','operator_stop')
        response=await self.client.get(path)
        self.assertEqual(response.status_code,200)
        proof=response.json()['acceptance']
        self.assertTrue(proof['cleanup_verified'] and proof['count_complete'])
        self.assertEqual((proof['thread_attempts'],proof['native_attempts']),(0,0))
        self.assertFalse(proof['joined'] or proof['remote_audibility_verified'])
        for key in ('source_rx_packets','B_tx_packets','B_tx_frames','B_tx_energy'):
            self.assertIsNone(proof[key])

    async def test_present_transport_metrics_are_preserved_and_malformed_containers_still_fail(self):
        session,_,path=await self.acceptance_fixture()
        session.update(rx_audio={'live_transport':{'native_source':{'packets_received':7}}},
            response_status={'progress':{'tx':{'packets':4,'frames':960,'energy':0.125}}})
        response=await self.client.get(path)
        self.assertEqual(response.status_code,200)
        proof=response.json()['acceptance']
        self.assertEqual([proof[key] for key in ('source_rx_packets','B_tx_packets','B_tx_frames','B_tx_energy')],
            [7,4,960,0.125])
        self.assertFalse(proof['joined'] or proof['count_complete'] or proof['cleanup_verified'])
        for rx in ([],{'live_transport':[]}):
            with self.subTest(rx=rx):
                session.update(rx_audio=rx)
                with self.assertRaises(AttributeError):await self.client.get(path)

    async def test_existing_auth_csrf_origin_and_recoverable_join_reply_are_preserved(self):
        from test_admin_web import ORIGIN,csrf
        from datetime import datetime,timezone,timedelta
        from unittest.mock import patch
        now=datetime.now(timezone.utc)
        occurrence=self.daemon.store.create_manual_occurrence(dict(title='PRIVATE-SYNTHETIC-TITLE',
            join_url='https://teams.microsoft.com/meet/12345',start_at=(now-timedelta(seconds=1)).isoformat(),
            end_at=(now+timedelta(minutes=5)).isoformat(),recording_authorized=True,speaking_authorized=True))
        path='/api/meetings/'+occurrence['id']+'?view=acceptance'
        self.assertEqual((await self.client.get(path)).status_code,401)
        token=await self.login()
        response=await self.client.get(path)
        self.assertEqual(response.status_code,200)
        self.assertNotIn('PRIVATE-SYNTHETIC-TITLE',response.text)
        self.assertNotIn('https://teams.microsoft.com/meet/',response.text)
        self.assertFalse(response.json()['egress_allows_target_host'])
        values=dict(id=occurrence['id'],acceptance_request='a'*64,acceptance_stop_policy='owner_or_meeting_end',acceptance_revision='1',csrf=token)
        result=dict(session_id='b'*32,occurrence_id=occurrence['id'],acceptance_request='a'*64)
        with patch.object(self.daemon,'join',new=AsyncMock(return_value=result)) as join:
            for headers,data in (({'Origin':'https://foreign.invalid','Accept':'application/json'},values),
                    ({'Origin':ORIGIN,'Accept':'application/json'},{**values,'csrf':'0'*64})):
                denied=await self.client.post('/actions/join',data=data,headers=headers)
                self.assertEqual(denied.status_code,403)
            join.assert_not_awaited()
            accepted=await self.client.post('/actions/join',data=values,headers={'Origin':ORIGIN,'Accept':'application/json'})
            self.assertEqual(accepted.status_code,200)
            self.assertEqual(accepted.json()['result'],result)
            self.assertEqual(join.await_args.kwargs,{'acceptance':POLICY})
