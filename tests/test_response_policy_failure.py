"""First-failure policy evidence and fail-closed B lifecycle; no external media."""
import asyncio
import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from teams_transcription.b_dual_policy import DUAL_CHECK_JS, OwnedSenderDualPolicy
from teams_transcription.response_runtime import ProductionResponseController


class PolicyFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_fault_rejects_waiting_and_future_checks_without_more_browser_work(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls, notices = [], []
        async def evaluate(*args):
            calls.append('evaluate')
            entered.set()
            await release.wait()
            raise TimeoutError('PRIVATE')
        policy = OwnedSenderDualPolicy(SimpleNamespace(evaluate=evaluate), 'synthetic',
            clock=lambda: 0., on_failure=lambda: notices.append('failed'))
        first = asyncio.create_task(policy.check())
        await entered.wait()
        waiting = [asyncio.create_task(policy.check()) for _ in range(5)]
        await asyncio.sleep(0)
        release.set()
        self.assertEqual(await asyncio.gather(first, *waiting), [False] * 6)
        before = policy.failure_snapshot()
        self.assertFalse(await policy.check())
        self.assertEqual(calls, ['evaluate'])
        self.assertEqual(notices, ['failed'])
        self.assertEqual(policy.failure_snapshot(), before)

    async def test_invalid_binding_still_revokes_after_fault_without_admitting_new_checks(self):
        frame = SimpleNamespace(evaluate=AsyncMock(side_effect=[
            TimeoutError('PRIVATE'), {'accepted': False, 'reason': 'binding_scope'}]))
        policy = OwnedSenderDualPolicy(frame, 'synthetic', clock=lambda: 0.)
        self.assertFalse(await policy.check())
        before = policy.failure_snapshot()
        self.assertFalse(await policy.authorize({}))
        self.assertEqual(frame.evaluate.await_count, 2)
        self.assertEqual(frame.evaluate.call_args.args[1]['expected'], {})
        binding = dict(meeting_id='synthetic', document_id='owned', peer_id='owned',
            sender_id='owned', owner='owned', track_id='owned')
        self.assertFalse(await policy.authorize(binding))
        self.assertFalse(await policy.check())
        self.assertEqual(frame.evaluate.await_count, 2)
        self.assertEqual(policy.failure_snapshot(), before)

    async def test_invalid_binding_faults_an_inflight_check_before_its_late_success(self):
        entered, release = asyncio.Event(), asyncio.Event()
        calls, notices = [], []
        async def evaluate(script, args):
            calls.append(args)
            if len(calls) == 1:
                entered.set()
                await release.wait()
                return {'accepted': True, 'reason': 'none'}
            return {'accepted': False, 'reason': 'binding_scope'}
        policy = OwnedSenderDualPolicy(SimpleNamespace(evaluate=evaluate), 'synthetic',
            clock=lambda: 0., on_failure=lambda: notices.append('failed'))
        checking = asyncio.create_task(policy.check())
        await entered.wait()
        revoking = asyncio.create_task(policy.authorize({}))
        await asyncio.sleep(0)
        self.assertTrue(policy.faulted)
        self.assertFalse(await policy.check())
        release.set()
        self.assertEqual(await asyncio.gather(checking, revoking), [False, False])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[-1]['expected'], {})
        self.assertEqual(notices, ['failed'])
        self.assertEqual(policy.failure_snapshot()['reason'], 'invalid_binding')

    async def test_timeout_is_preserved_after_later_rejection_and_success(self):
        now = [10.]
        frame = SimpleNamespace(evaluate=AsyncMock(side_effect=[
            TimeoutError('PRIVATE https://private.invalid/token'),
            {'accepted': False, 'reason': 'foreign_sender'},
            {'accepted': True, 'reason': 'none'}]))
        notices = []
        policy = OwnedSenderDualPolicy(frame, 'synthetic', clock=lambda: now[0],
            on_failure=lambda: notices.append('failed'))
        self.assertIsNone(policy.failure_snapshot())
        now[0] = 10.125
        self.assertFalse(await policy.check())
        first = policy.failure_snapshot()
        self.assertEqual(first, {'reason': 'evaluation_timeout',
            'exception_type': 'TimeoutError', 'elapsed_ms': 125})
        now[0] = 12.
        self.assertFalse(await policy.check())
        self.assertFalse(await policy.check())
        self.assertEqual(policy.failure_snapshot(), first)
        self.assertEqual(notices, ['failed'])
        first['reason'] = 'reader changed copy'
        self.assertEqual(policy.failure_snapshot()['reason'], 'evaluation_timeout')
        self.assertNotIn('PRIVATE', json.dumps(policy.failure_snapshot()))

    async def test_explicit_foreign_sender_reason_is_not_reported_as_timeout(self):
        frame = SimpleNamespace(evaluate=AsyncMock(return_value={
            'accepted': False, 'reason': 'foreign_sender'}))
        policy = OwnedSenderDualPolicy(frame, 'synthetic', clock=lambda: 0.)
        self.assertFalse(await policy.check())
        self.assertEqual(policy.failure_snapshot(), {'reason': 'foreign_sender',
            'exception_type': 'none', 'elapsed_ms': 0})

    async def test_actual_guard_returns_first_fixed_reason_and_mutes_foreign_track(self):
        script = 'const guard=(' + DUAL_CHECK_JS + r''');
          global.document={};global.location={href:'synthetic'};
          const track={kind:'audio',enabled:false};
          global.window={__bMeetingId:'synthetic',__meeting:{peers:[{getSenders:()=>[{track}]}]}};
          const input={policyId:'owned',meetingId:'synthetic',authorize:false,initialized:false,receipt:true};
          const first=guard(input);input.initialized=true;track.enabled=true;
          const failure=guard(input),muted=!track.enabled;
          location.href='another-synthetic-document';const later=guard(input);
          process.stdout.write(JSON.stringify({first,failure,muted,later}));
        '''
        value=json.loads(subprocess.check_output(['node','-e',script],text=True,timeout=3))
        self.assertEqual(value, {'first':{'accepted':True,'reason':'none'},
            'failure':{'accepted':False,'reason':'foreign_sender'},'muted':True,
            'later':{'accepted':False,'reason':'foreign_sender'}})

    async def test_malformed_receipt_and_exception_details_never_become_authorization(self):
        for value in [True,{}, {'accepted':1,'reason':'none'},
                {'accepted':True,'reason':'foreign_sender'},
                {'accepted':False,'reason':'PRIVATE https://private.invalid'},
                {'accepted':False,'reason':'foreign_sender','private':'PRIVATE'}]:
            with self.subTest(value=value):
                policy=OwnedSenderDualPolicy(SimpleNamespace(evaluate=AsyncMock(return_value=value)),
                    'synthetic',clock=lambda:0.)
                self.assertFalse(await policy.check())
                self.assertEqual(policy.failure_snapshot()['reason'],'invalid_result')
                self.assertNotIn('PRIVATE',json.dumps(policy.failure_snapshot()))

    async def test_concurrent_calls_never_clear_first_failure_or_notify_twice(self):
        entered,release=asyncio.Event(),asyncio.Event()
        calls=[]
        async def evaluate(*args):
            if not entered.is_set():
                entered.set();await release.wait()
                return {'accepted':False,'reason':'document_scope'}
            return {'accepted':True,'reason':'none'}
        policy=OwnedSenderDualPolicy(SimpleNamespace(evaluate=evaluate),'synthetic',
            clock=lambda:0.,on_failure=lambda:calls.append('failed'))
        first=asyncio.create_task(policy.check())
        await asyncio.wait_for(entered.wait(),1)
        second=asyncio.create_task(policy.check())
        release.set()
        self.assertEqual(await asyncio.wait_for(asyncio.gather(first,second),1),[False,False])
        self.assertEqual(calls,['failed'])
        self.assertEqual(policy.failure_snapshot()['reason'],'document_scope')

    async def test_owner_cancellation_does_not_fabricate_capability_failure(self):
        entered=asyncio.Event()
        async def evaluate(*args):
            entered.set();await asyncio.Event().wait()
        frame=SimpleNamespace(evaluate=evaluate)
        policy=OwnedSenderDualPolicy(frame,'synthetic')
        task=asyncio.create_task(policy.check())
        await asyncio.wait_for(entered.wait(),1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertFalse(policy.faulted)
        self.assertIsNone(policy.failure_snapshot())
        frame.evaluate=AsyncMock(return_value={'accepted':True,'reason':'none'})
        self.assertTrue(await policy.check())


class ResponsePolicyLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_fault_is_visible_before_one_output_revoke_and_owned_stop_waits_for_it(self):
        entered, release = asyncio.Event(), asyncio.Event()
        controller = ProductionResponseController(SimpleNamespace(ident='synthetic'),
            SimpleNamespace(), None, {}, [])
        native = SimpleNamespace(enabled=True, error=None, cleanup_timeout=5,
            status=lambda: {'state': 'sleeping', 'output_enabled': native.enabled,
                'output_muted': not native.enabled}, stop=AsyncMock(return_value='stopped'))

        async def revoke():
            entered.set()
            await release.wait()
            native.enabled = False

        native.fail_input_policy = AsyncMock(side_effect=revoke)
        controller.native, controller.state = native, 'sleeping'
        controller._release_policy = AsyncMock(return_value=True)
        frame = SimpleNamespace(evaluate=AsyncMock(return_value={
            'accepted': False, 'reason': 'foreign_sender'}))
        controller.policy = OwnedSenderDualPolicy(frame, 'synthetic', clock=lambda: 0.,
            on_failure=controller._policy_failed)
        self.assertFalse(await controller.policy.check())
        self.assertEqual(controller.status()['state'], 'unavailable')
        self.assertEqual(controller.status()['capture_policy_failure']['reason'], 'foreign_sender')
        # Unknown physical mute remains unknown until the real operation returns.
        self.assertTrue(controller.status()['output_enabled'])
        with self.assertRaisesRegex(RuntimeError, 'native_response_unavailable'):
            await controller.enable_output()
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(await controller.policy.check())
        stopping = asyncio.create_task(controller.stop())
        try:
            await asyncio.sleep(0)
            native.stop.assert_not_awaited()
        finally:
            release.set()
            self.assertEqual(await asyncio.wait_for(stopping, 1), 'stopped')
        native.fail_input_policy.assert_awaited_once()
        self.assertFalse(controller.status()['output_enabled'])
        self.assertEqual(controller.status()['state'], 'stopped')

    async def test_revoke_failure_stays_unknown_without_claiming_mute(self):
        controller=ProductionResponseController(SimpleNamespace(ident='synthetic'),
            SimpleNamespace(),None,{},[])
        controller.native=SimpleNamespace(cleanup_timeout=5,
            fail_input_policy=AsyncMock(side_effect=RuntimeError('PRIVATE')),
            status=lambda:{'state':'sleeping','output_enabled':True,'output_muted':False})
        controller.state='sleeping'
        controller.policy=OwnedSenderDualPolicy(SimpleNamespace(evaluate=AsyncMock(return_value={
            'accepted':False,'reason':'document_scope'})),'synthetic',on_failure=controller._policy_failed)
        self.assertFalse(await controller.policy.check())
        await asyncio.wait_for(controller._policy_failure_task,1)
        status=controller.status()
        self.assertEqual(status['state'],'stop_unknown')
        self.assertEqual(status['error_code'],'response_cleanup_unknown')
        self.assertFalse(status['output_muted'])
        self.assertEqual(status['cleanup_errors'][0]['phase'],'policy_revoke')
        self.assertNotIn('PRIVATE',json.dumps(status))

    async def test_revoke_timeout_is_bounded_and_does_not_claim_mute(self):
        controller=ProductionResponseController(SimpleNamespace(ident='synthetic'),
            SimpleNamespace(),None,{},[])
        async def blocked():await asyncio.Event().wait()
        controller.native=SimpleNamespace(cleanup_timeout=.01,
            fail_input_policy=blocked,
            status=lambda:{'state':'sleeping','output_enabled':True,'output_muted':False})
        controller.state='sleeping'
        controller._policy_failed()
        await asyncio.wait_for(controller._policy_failure_task,1)
        self.assertEqual(controller.status()['state'],'stop_unknown')
        self.assertFalse(controller.status()['output_muted'])
        self.assertEqual(controller.cleanup_errors[0]['exception_type'],'TimeoutError')

    async def test_cancelled_stop_does_not_cancel_revoke_or_allow_reenable(self):
        entered,release=asyncio.Event(),asyncio.Event()
        controller=ProductionResponseController(SimpleNamespace(ident='synthetic'),
            SimpleNamespace(),None,{},[])
        async def revoke():
            entered.set();await release.wait()
        native=SimpleNamespace(cleanup_timeout=5,fail_input_policy=AsyncMock(side_effect=revoke),
            status=lambda:{'state':'sleeping'},stop=AsyncMock(return_value='stopped'))
        controller.native=native;controller.state='sleeping'
        controller._release_policy=AsyncMock(return_value=True)
        controller._policy_failed()
        await asyncio.wait_for(entered.wait(),1)
        stopping=asyncio.create_task(controller.stop())
        await asyncio.sleep(0)
        stopping.cancel()
        try:
            with self.assertRaises(asyncio.CancelledError):await stopping
            self.assertFalse(controller._policy_failure_task.cancelled())
        finally:
            release.set()
            await asyncio.wait_for(controller._policy_failure_task,1)
        try:
            with self.assertRaisesRegex(RuntimeError,'native_response_unavailable'):
                await controller.enable_output()
        finally:
            self.assertEqual(await asyncio.wait_for(controller.stop(),1),'stopped')
        native.fail_input_policy.assert_awaited_once()

    async def test_failed_policy_during_native_start_cannot_emit_response_ready(self):
        frame=SimpleNamespace(evaluate=AsyncMock(side_effect=[
            {'accepted':True,'reason':'none'}, {'accepted':False,'reason':'document_scope'}]))
        meeting=SimpleNamespace(response_sender=AsyncMock(return_value=(frame,0,0)),
            bind_dual_policy=Mock())
        session=SimpleNamespace(ident='synthetic',path='/tmp/unused-synthetic',
            root='/tmp/unused-synthetic',event=Mock(),store=Mock())
        controller=ProductionResponseController(session,meeting,None,
            {'allowed_egress_hosts':[],'codex_bin':'never-called'},[])
        native=SimpleNamespace(stop=AsyncMock(return_value='stopped'))
        async def start(**kwargs):
            self.assertFalse(await controller.policy.check())
        native.start=start
        controller._release_policy=AsyncMock(return_value=True)
        with patch('teams_transcription.response_runtime.RuntimeLiveGuard') as guard, \
                patch('teams_transcription.response_runtime.BrowserTXSink',return_value=AsyncMock()), \
                patch('teams_transcription.response_runtime._AsyncStore'), \
                patch('teams_transcription.response_runtime.build_native_meeting_responder',return_value=native):
            guard.return_value.prepare=AsyncMock()
            self.assertFalse(await controller.start())
        native.stop.assert_awaited_once()
        self.assertEqual(controller.state,'unavailable')
        self.assertNotIn('response_ready',[call.args[0] for call in session.event.call_args_list])
        self.assertIsNone(controller._policy_failure_task)

    async def test_fault_after_owned_stop_begins_does_not_start_a_late_revoke(self):
        entered,release=asyncio.Event(),asyncio.Event()
        async def stop():
            entered.set();await release.wait();return 'stopped'
        native=SimpleNamespace(stop=stop,fail_input_policy=AsyncMock(),
            status=lambda:{'state':'sleeping'})
        controller=ProductionResponseController(SimpleNamespace(ident='synthetic'),
            SimpleNamespace(),None,{},[])
        controller.native=native;controller.state='sleeping'
        controller._release_policy=AsyncMock(return_value=True)
        controller.policy=OwnedSenderDualPolicy(SimpleNamespace(evaluate=AsyncMock(return_value={
            'accepted':False,'reason':'document_scope'})),'synthetic',on_failure=controller._policy_failed)
        stopping=asyncio.create_task(controller.stop())
        await asyncio.wait_for(entered.wait(),1)
        try:
            self.assertFalse(await controller.policy.check())
            self.assertIsNone(controller._policy_failure_task)
        finally:
            release.set()
            self.assertEqual(await asyncio.wait_for(stopping,1),'stopped')
        native.fail_input_policy.assert_not_awaited()

    async def test_stop_joins_revoke_scheduled_while_it_waits_for_controller_lock(self):
        native=SimpleNamespace(stop=AsyncMock(return_value='stopped'),
            fail_input_policy=AsyncMock(),status=lambda:{'state':'sleeping'})
        controller=ProductionResponseController(SimpleNamespace(ident='synthetic'),
            SimpleNamespace(),None,{},[])
        controller.native=native;controller.state='sleeping'
        controller._release_policy=AsyncMock(return_value=True)
        await controller._lock.acquire()
        stopping=asyncio.create_task(controller.stop())
        await asyncio.sleep(0)
        controller._policy_failed()
        controller._lock.release()
        self.assertEqual(await asyncio.wait_for(stopping,1),'stopped')
        self.assertTrue(controller._policy_failure_task.done())
        native.fail_input_policy.assert_not_awaited()


class NativeInputFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_mute_is_attempted_before_slow_input_cancellation_can_exhaust_budget(self):
        from teams_transcription.b_native import NativeLiveResponder
        from teams_transcription.b_native_output import NativeTrackOutput
        from teams_transcription.b_response import MeetingMinutesTool
        sink=AsyncMock()
        output=NativeTrackOutput('synthetic',sink)
        native=NativeLiveResponder(None,'/tmp/unused-input-policy','synthetic',output,
            MeetingMinutesTool('synthetic',lambda _:{}))
        entered,joining,release=asyncio.Event(),asyncio.Event(),asyncio.Event()
        async def slow_input():
            entered.set()
            try:await asyncio.Event().wait()
            finally:
                joining.set();await release.wait()
        native._rx_task=asyncio.create_task(slow_input())
        await asyncio.wait_for(entered.wait(),1)
        output.start_output();output.active=True
        task=asyncio.create_task(native.fail_input_policy())
        try:
            await asyncio.wait_for(joining.wait(),1)
            # Inspect before releasing the deliberately held cancellation join.
            sink.pause.assert_awaited_once()
            self.assertFalse(output.enabled)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):await task
        finally:
            release.set();native._rx_task.cancel();task.cancel()
            await asyncio.gather(native._rx_task,task,return_exceptions=True)

    async def test_slow_cancel_cleanup_cannot_extend_controller_revoke_budget(self):
        from teams_transcription.b_native import NativeLiveResponder
        from teams_transcription.b_native_output import NativeTrackOutput
        from teams_transcription.b_response import MeetingMinutesTool
        sink=AsyncMock();output=NativeTrackOutput('synthetic',sink)
        native=NativeLiveResponder(None,'/tmp/unused-input-policy','synthetic',output,
            MeetingMinutesTool('synthetic',lambda _:{}))
        native.cleanup_timeout=.01
        entered,release=asyncio.Event(),asyncio.Event()
        async def slow_input():
            entered.set()
            try:await asyncio.Event().wait()
            finally:await release.wait()
        native._rx_task=asyncio.create_task(slow_input())
        await asyncio.wait_for(entered.wait(),1)
        output.start_output();output.active=True
        controller=ProductionResponseController(SimpleNamespace(ident='synthetic'),
            SimpleNamespace(),None,{},[])
        controller.native=native;controller.state='sleeping'
        controller._policy_failed()
        try:
            await asyncio.wait_for(controller._policy_failure_task,1)
            self.assertEqual(controller.status()['state'],'stop_unknown')
            self.assertIsNone(controller.status()['output_muted'])
            self.assertFalse(native._rx_task.done())
            sink.pause.assert_awaited_once()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(native._rx_task,return_exceptions=True),1)

    async def test_real_native_revokes_input_request_and_output_without_a_new_channel(self):
        from teams_transcription.b_native import NativeLiveResponder
        from teams_transcription.b_native_output import NativeTrackOutput
        from teams_transcription.b_response import MeetingMinutesTool
        sink=AsyncMock()
        output=NativeTrackOutput('synthetic',sink)
        native=NativeLiveResponder(None,'/tmp/unused-input-policy','synthetic',output,
            MeetingMinutesTool('synthetic',lambda _:{}))
        native.started=True;native.thread_id='synthetic-thread';native.state='answering'
        native._request_id='synthetic-question'
        native._rx_task=asyncio.create_task(asyncio.Event().wait())
        native._question_task=asyncio.create_task(asyncio.Event().wait())
        native._tool_task=asyncio.create_task(asyncio.Event().wait())
        output.start_output();output.active=True
        try:
            await native.fail_input_policy()
            self.assertEqual(native.state,'failed')
            self.assertIsNone(native._request_id)
            self.assertTrue(all(task.cancelled() for task in
                (native._rx_task,native._question_task,native._tool_task)))
            self.assertFalse(output.enabled)
            self.assertFalse(output.active)
            sink.pause.assert_awaited_once()
            self.assertFalse(native.push_rx('synthetic',b'\x01\x00',started_at=0,ended_at=1/24000))
            self.assertEqual(native.runtime_failure_snapshot()['code'],'response_input_policy_failed')
            self.assertFalse(native.rpc.process)
        finally:
            for task in (native._rx_task,native._question_task,native._tool_task):task.cancel()
            await asyncio.gather(native._rx_task,native._question_task,native._tool_task,return_exceptions=True)


class PolicyDiagnosticTests(unittest.TestCase):
    def test_projection_bounds_counts_and_does_not_retain_hostile_labels(self):
        from teams_transcription.diagnostics import response_policy_failure_receipt,response_capture_delivery_receipt
        failure=response_policy_failure_receipt({'reason':'PRIVATE','elapsed_ms':10**100,
            'exception_type':'PRIVATE','secret':'PRIVATE'})
        self.assertEqual(failure,{'reason':'unknown','elapsed_ms':86400000,'exception_type':'Exception'})
        counts=response_capture_delivery_receipt({'received_packets':10**100,
            'policy_rejected_packets':True,'callback_error_packets':-1,'pcm':'PRIVATE'})
        self.assertEqual(counts['received_packets'],2147483647)
        self.assertIsNone(counts['policy_rejected_packets'])
        self.assertIsNone(counts['callback_error_packets'])
        self.assertNotIn('PRIVATE',json.dumps([failure,counts]))

    def test_owned_identity_detail_is_strictly_allowlisted_and_other_reasons_reject_extra_keys(self):
        from teams_transcription.diagnostics import response_policy_failure_receipt
        good = response_policy_failure_receipt({
            'reason': 'owned_identity', 'exception_type': 'none', 'elapsed_ms': 73293,
            'detail': 'revoked_guard_sender_track_changed',
        })
        self.assertEqual(good, {
            'reason': 'owned_identity', 'exception_type': 'none', 'elapsed_ms': 73293,
            'detail': 'revoked_guard_sender_track_changed',
        })
        hostile = response_policy_failure_receipt({
            'reason': 'owned_identity', 'exception_type': 'none', 'elapsed_ms': 10,
            'detail': 'PRIVATE https://secret.invalid',
        })
        self.assertEqual(hostile['detail'], 'unknown')
        self.assertNotIn('PRIVATE', json.dumps(hostile))
        non_owned = response_policy_failure_receipt({
            'reason': 'foreign_sender', 'exception_type': 'none', 'elapsed_ms': 10,
            'detail': 'sender_track_changed',
        })
        self.assertNotIn('detail', non_owned)

    def test_dual_check_js_reports_exact_owned_identity_detail_for_revoked_and_track_change(self):
        script = 'const guard=(' + DUAL_CHECK_JS + r''');
          global.document={};global.location={href:'synthetic'};
          const ownedTrack={id:'owned-1',kind:'audio',readyState:'live',enabled:false};
          const replacedTrack={id:'teams-2',kind:'audio',readyState:'live',enabled:false};
          const sender={track:ownedTrack};
          const pc={connectionState:'connected',getSenders:()=>[sender],getTransceivers:()=>[]};
          const capture={peers:[pc]};
          const binding={owner:'o1',meetingId:'m1',peerIndex:0,senderIndex:0,capture,document,
            pc,sender,transceiver:null,original:replacedTrack,href:'synthetic',
            documentId:'d1',peerId:'p1',senderId:'s1'};
          const owned={owner:'o1',binding,pc,sender,track:ownedTrack,inputTrack:{},receiver:{track:{}},
            relay:{connectionState:'connected',getReceivers:()=>[]},control:{readyState:'open'},
            decoder:{valid:()=>true},installed:true,disposed:true,muted:true,stopping:false,
            revokeReason:'source_disconnected',revoke(r){this.revokeReason ||= r;this.disposed=true;}};
          global.window={__bMeetingId:'m1',__meeting:capture,__bTX:owned,
            __bTXBindings:new Map([['o1',binding]]),
            __bTXOwnedStates:new Map([['o1',owned]])};
          const r1=guard({policyId:'p1',meetingId:'m1',authorize:false,initialized:false,receipt:true});
          // Now simulate active owned state whose sender.track was replaced before guard fired
          const owned2={...owned,disposed:false,revokeReason:null};
          owned2.receiver={track:owned2.inputTrack};
          owned2.relay={connectionState:'connected',getReceivers:()=>[owned2.receiver]};
          owned2.decoder={receiver:owned2.receiver,inputTrack:owned2.inputTrack,track:ownedTrack,valid:()=>true};
          sender.track=replacedTrack;
          window.__bTX=owned2;window.__bTXOwnedStates.set('o1',owned2);
          const pMap=window.__bDualPolicies;
          pMap.set('p2',{document,capture,href:'synthetic',meetingId:'m1',binding,trackId:'owned-1',faulted:false,failureDetail:null});
          const r2=guard({policyId:'p2',meetingId:'m1',authorize:false,initialized:true,receipt:true});
          process.stdout.write(JSON.stringify({r1,r2}));
        '''
        value = json.loads(subprocess.check_output(['node', '-e', script], text=True, timeout=3))
        self.assertEqual(value['r2'], {'accepted': False, 'reason': 'owned_identity', 'detail': 'sender_track_changed'})
        # In r1, binding was not yet authorized so policy.binding was null; test pre-authorized revoked state too:
        script2 = 'const guard=(' + DUAL_CHECK_JS + r''');
          global.document={};global.location={href:'synthetic'};
          const ownedTrack={id:'owned-1',kind:'audio',readyState:'live',enabled:false};
          const sender={track:ownedTrack};
          const pc={connectionState:'connected',getSenders:()=>[sender],getTransceivers:()=>[]};
          const capture={peers:[pc]};
          const binding={owner:'o1',meetingId:'m1',peerIndex:0,senderIndex:0,capture,document,
            pc,sender,transceiver:null,original:ownedTrack,href:'synthetic',
            documentId:'d1',peerId:'p1',senderId:'s1'};
          const owned={owner:'o1',binding,pc,sender,track:ownedTrack,installed:true,disposed:true,
            muted:true,stopping:false,revokeReason:'source_disconnected',
            revoke(r){this.revokeReason ||= r;this.disposed=true;}};
          global.window={__bMeetingId:'m1',__meeting:capture,__bTX:owned,
            __bTXBindings:new Map([['o1',binding]]),
            __bTXOwnedStates:new Map([['o1',owned]]),
            __bDualPolicies:new Map([['p3',{document,capture,href:'synthetic',meetingId:'m1',binding,trackId:'owned-1',faulted:false,failureDetail:null}]])};
          const r3=guard({policyId:'p3',meetingId:'m1',authorize:false,initialized:true,receipt:true});
          process.stdout.write(JSON.stringify(r3));
        '''
        r3 = json.loads(subprocess.check_output(['node', '-e', script2], text=True, timeout=3))
        self.assertEqual(r3, {'accepted': False, 'reason': 'owned_identity', 'detail': 'revoked_source_disconnected'})


if __name__ == '__main__':
    unittest.main()
