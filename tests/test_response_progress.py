"""Bounded live-response diagnostics; no model, meeting or credentials."""
import json
import unittest

from teams_transcription.diagnostics import response_progress_diagnostic


class ResponseProgressTests(unittest.TestCase):
    def test_unknown_and_hostile_values_cannot_leak_or_claim_completion(self):
        private = 'PRIVATE https://private.invalid/?token=secret'
        value = {
            'state': private, 'error': private,
            'context_failure_code': private,
            'context': {'sent_bytes': private,'total_limit_bytes': private,
                        'committed_records': True,'pending': private,'text': private},
            'rx_accepted_packets': 10**100, 'rx_peer_push_packets': -1,
            'rx_dropped': True, 'tool_calls': '4',
            'transcript_notifications': {'user_delta': 2, 'user_done': False,
                                         'assistant_delta': 3, 'assistant_done': 0, private: 99},
            'input_pending': {'characters': 10**100, 'has_wake': private,
                              'has_question': True, 'text': private},
            'question_receipts': {'counts': {'received': 1, 'admitted': 0, 'rejected': 1},
                'events': [{'reason': private, 'question': private, 'request_id': private}]},
            'speech_completion': {'phase': private, 'error_code': private,
                                  'done_is_set': private, 'output_active': False,
                                  'activity_frames': -1, 'frames': 10**100,
                                  'drain_result': {'drained': private, 'truncated': False}},
        }
        result = response_progress_diagnostic(value)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('private.invalid', json.dumps(result))
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(result['rx'], {'accepted_packets': 2147483647,
                                       'peer_push_packets': 0, 'dropped_packets': 0})
        self.assertEqual(result['transcripts'], {'user_delta': 2, 'user_done': 0,
                                                'assistant_delta': 3, 'assistant_done': 0})
        self.assertFalse(result['pending_input']['has_wake'])
        self.assertTrue(result['pending_input']['has_question'])
        self.assertEqual(result['question_receipts']['last_rejection'], 'unknown')
        self.assertEqual(result['speech']['phase'], 'unknown')
        self.assertEqual(result['speech']['error_code'], 'unknown')
        self.assertFalse(result['speech']['done_is_set'])
        self.assertFalse(result['speech']['drained'])
        self.assertEqual(result['speech']['activity_frames'], 0)
        self.assertEqual(result['speech']['frames'], 2147483647)
        self.assertEqual(result['context']['failure_code'],'unknown')
        self.assertEqual(result['context']['committed_records'],0)
        self.assertFalse(result['context']['pending'])

    def test_committed_source_count_has_only_generic_integer_bounds(self):
        for count, expected in ((600, 600), (10**100, 2147483647),
                                (-1, 0), (True, 0), ('600', 0)):
            with self.subTest(count=count):
                context = response_progress_diagnostic({
                    'context': {'committed_records': count}})['context']
                self.assertEqual(context['committed_records'], expected)
                self.assertIsNone(context['total_limit_bytes'])

    def test_native_tx_receipts_reject_private_and_invalid_values_in_storage(self):
        import tempfile
        from teams_transcription.storage import Session
        fields = ('packets', 'frames')
        private = 'PRIVATE media request-id https://private.invalid'
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create({'provider':'codex', 'source':'teams'})
            for value, expected in ((17,17), (10**100,2147483647), (-1,0),
                    (True,0), (False,0), (private,0), ('17',0),
                    (float('nan'),0), (float('inf'),0), (17.0,0), (None,0)):
                with self.subTest(value=value):
                    progress = response_progress_diagnostic({
                        'pcm_admission':{'received':private},
                        'tx':{**dict.fromkeys(fields,value), 'available':True,
                            'connected':True, 'duration':1.25, 'energy':.125,
                            'counter_domain':private,'render_evidence':private,
                            'request_id':private, 'data':private}})
                    session.update(response_status={'progress':progress})
                    stored = Session(root,session.ident).status()['response_status']['progress']
                    self.assertNotIn('pcm_admission', stored)
                    self.assertEqual(stored['tx'],
                        {'available':True, 'connected':True, 'duration':1.25,
                         'energy':.125,'counter_domain':'unknown','render_evidence':None,
                         **dict.fromkeys(fields,expected)})
                    self.assertNotIn('PRIVATE',json.dumps(stored,allow_nan=False))
            for value in (None, True, -1, float('nan'), float('inf'), private):
                progress = response_progress_diagnostic({'tx':{
                    'available':value, 'connected':value, 'duration':value, 'energy':value}})
                self.assertIsNone(progress['tx']['duration'])
                self.assertIsNone(progress['tx']['energy'])
                self.assertEqual(progress['tx']['available'], value is True)
                self.assertEqual(progress['tx']['connected'], value is True)

    def test_native_local_quiet_cannot_claim_remote_drain_or_consumption(self):
        result = response_progress_diagnostic({'speech_completion':{
            'connected':True, 'quiet_received_seconds':.81,
            'drain_result':{'quiet':True, 'drained':True}},
            'tool_deliveries':[{'marker_returned':True, 'model_consumption_verified':True}]})
        self.assertTrue(result['speech']['quiet'])
        self.assertFalse(result['speech']['drained'])
        self.assertEqual(result['speech']['quiet_received_seconds'],.81)
        self.assertFalse(result['tool_deliveries'][0]['model_consumption_verified'])

    def test_host_request_counts_and_reasons_are_sanitized_without_receipt_content(self):
        private = 'PRIVATE question source request-id'
        result = response_progress_diagnostic({
            'question_receipts': {
                'counts': {'received': 10**100, 'admitted': True, 'rejected': 2},
                'events': [{'outcome': 'rejected', 'reason': 'duplicate_request',
                    'question': private, 'source': private, 'request_id': private,
                    'received_seconds': private, 'decision_seconds': private}]},
            'question_framing': {'text': private, 'request_id': private}})
        self.assertEqual(result['question_receipts'], {
            'received': 2147483647, 'admitted': 0, 'rejected': 2,
            'last_rejection': 'duplicate_request'})
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_question_receipts_are_bounded_and_cannot_leak_queries_or_claim_model_consumption(self):
        private = 'PRIVATE https://private.invalid/?token=secret'
        entry = {'question_number':1, 'phase':private, 'query':private,
            'source_version':private, 'selected_record_indexes':[0, True, -1, private, 1],
            'selected_ref_count':1, 'payload_bytes':10**100, 'sent_bytes':True,
            'marker_returned':'true', 'model_consumption_verified':True}
        progress = response_progress_diagnostic({'tool_deliveries':[entry]*40})
        self.assertEqual(len(progress['tool_deliveries']),16)
        receipt = progress['tool_deliveries'][-1]
        self.assertEqual(receipt['phase'],'unknown')
        self.assertIsNone(receipt['source_version'])
        self.assertEqual(receipt['selected_record_indexes'],[1])
        self.assertFalse(receipt['marker_returned'])
        self.assertFalse(receipt['model_consumption_verified'])
        self.assertNotIn('PRIVATE',json.dumps(progress))


class NativeResponseProgressTests(unittest.IsolatedAsyncioTestCase):
    async def test_partial_input_and_provider_final_do_not_claim_application_request(self):
        from test_b_native import NativeLiveTests
        case = NativeLiveTests('runTest')
        await case.asyncSetUp()
        try:
            await case.utterance('小白小白，PRIVATE 問題', quiet=0)
            partial = case.live.status()
            self.assertEqual(partial['transcript_notifications'],
                {'user_delta': 1, 'user_done': 0, 'assistant_delta': 0, 'assistant_done': 0})
            self.assertTrue(partial['input_pending']['has_wake'])
            self.assertNotIn('PRIVATE', json.dumps(partial))
            await case.live.handle_notification('thread/realtime/transcript/done',
                {'threadId': case.live.thread_id, 'role': 'user',
                 'text': '小白小白，PRIVATE 問題'})
            progress = response_progress_diagnostic(case.live.status())
            self.assertEqual(progress['question_receipts'],
                {'received': 0, 'admitted': 0, 'rejected': 0, 'last_rejection': None})
            self.assertEqual(progress['transcripts']['user_done'], 1)
            self.assertGreater(progress['pending_input']['characters'], 0)
            self.assertEqual(progress['speech']['phase'], 'not_started')
            self.assertEqual(progress['tool_calls'], 0)
            self.assertNotIn('PRIVATE', json.dumps(progress))
        finally:
            await case.asyncTearDown()


class RuntimeFailureProgressTests(unittest.IsolatedAsyncioTestCase):
    def controller(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from teams_transcription.b_native_output import NativeTrackOutput
        from teams_transcription.b_native import NativeLiveResponder
        from teams_transcription.b_response import MeetingMinutesTool
        from teams_transcription.response_runtime import ProductionResponseController
        sink = AsyncMock()
        sink.observe.return_value = {'connected':True, 'frames':0, 'packets':0,
            'duration':0., 'energy':0., 'source_frames':0, 'source_packets':0,
            'source_duration':0., 'source_energy':0.}
        output = NativeTrackOutput('one', sink)
        native = NativeLiveResponder(None, '/tmp/unused-runtime-failure', 'one',
            output, MeetingMinutesTool('one', lambda _: {}))
        native.thread_id = 'own-thread'
        native.started = True
        native.state = 'sleeping'
        output.start_output()
        controller = ProductionResponseController(SimpleNamespace(ident='one'),
            SimpleNamespace(), None, {}, [])
        controller.native = native
        controller.state = 'sleeping'
        return controller, native

    async def test_unavailable_native_snapshot_preserves_first_failure_and_permissions(self):
        from unittest.mock import Mock
        controller, native = self.controller()
        sink = native.output.sink
        sink.diagnostics = Mock(return_value={
            'available':True, 'connected':True, 'packets':2, 'frames':960,
            'duration':.02, 'energy':.001, 'pcm':'PRIVATE'})
        status = controller.status()
        self.assertNotIn('pcm_admission',status['progress'])
        self.assertEqual(status['progress']['tx'],{
            'available':True, 'connected':True, 'packets':2, 'frames':960,
            'duration':.02, 'energy':.001,
            'counter_domain':'legacy_render_cumulative_v1','render_evidence':None})
        self.assertNotIn('PRIVATE',json.dumps(status))
        self.assertFalse(native.output.active)
        self.assertTrue(native.output.enabled)
        await native._audio_error('Response Live connection closed')
        original_failure = native.runtime_failure_snapshot()
        sink.diagnostics.side_effect = RuntimeError('PRIVATE')
        unavailable = controller.status()
        self.assertFalse(unavailable['progress']['tx']['available'])
        self.assertEqual(unavailable['runtime_failure'],original_failure)
        self.assertFalse(unavailable['output_enabled'])

    async def test_provider_failure_keeps_first_origin_and_revokes_output_without_private_payload(self):
        controller, native = self.controller()
        await native._data_event({'type': 'error',
            'error': {'message': 'PRIVATE https://private.invalid/?token=secret'}})
        first = controller.status()
        self.assertEqual(first['state'], 'failed')
        self.assertFalse(first['output_enabled'])
        self.assertTrue(first['output_muted'])
        receipt = first['runtime_failure']
        self.assertEqual(receipt['code'], 'response_datachannel_error')
        self.assertEqual(receipt['phase'], 'datachannel')
        self.assertNotIn('PRIVATE', json.dumps(first))
        self.assertNotIn('private.invalid', json.dumps(first))
        await native._audio_error('output_backpressure')
        self.assertEqual(controller.status()['runtime_failure'], receipt)
        receipt['code'] = 'mutated-by-reader'
        self.assertEqual(controller.status()['runtime_failure']['code'],
            'response_datachannel_error')

    async def test_native_receiver_failure_mutes_without_persisting_private_payload(self):
        controller, native = self.controller()
        await native.output.begin_speech('one')
        native.output.sink.observe.return_value = {'connected':False, 'sdp':'PRIVATE'}
        await native._finish_native_speech('answering')
        status = controller.status()
        self.assertEqual(status['runtime_failure']['code'], 'response_speech_completion_failed')
        self.assertFalse(status['output_enabled'])
        self.assertFalse(native.output.active)
        self.assertNotIn('PRIVATE', json.dumps(status))

    async def test_foreign_rpc_error_cannot_set_first_failure_or_mute_own_output(self):
        import asyncio
        from types import SimpleNamespace
        controller, native = self.controller()
        native.rpc = SimpleNamespace(notifications=asyncio.Queue())
        native._answer = asyncio.get_running_loop().create_future()
        receiver = asyncio.create_task(native._receive())
        try:
            await native.rpc.notifications.put({'method': 'thread/realtime/error',
                'params': {'threadId': 'foreign', 'message': 'PRIVATE foreign error'}})
            await asyncio.sleep(0)
            self.assertIsNone(controller.status()['runtime_failure'])
            self.assertTrue(native.output.enabled)
            await native.rpc.notifications.put({'method': 'thread/realtime/error',
                'params': {'threadId': 'own-thread', 'message': 'PRIVATE own error'}})
            async with asyncio.timeout(1):
                while native.state != 'failed':
                    await asyncio.sleep(0)
            status = controller.status()
            self.assertEqual(status['runtime_failure']['code'], 'response_rpc_error')
            self.assertFalse(status['output_enabled'])
            self.assertNotIn('PRIVATE', json.dumps(status))
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            if native._answer.done() and not native._answer.cancelled():
                native._answer.exception()
            else:
                native._answer.cancel()

    def test_runtime_failure_sanitizer_rejects_hostile_labels_and_bounds_time(self):
        from teams_transcription.diagnostics import response_runtime_failure_receipt
        result = response_runtime_failure_receipt({'code': 'PRIVATE',
            'phase': 'PRIVATE', 'exception_type': 'PRIVATE', 'elapsed_ms': 10**100,
            'message': 'https://private.invalid/SDP'})
        self.assertEqual(result['code'], 'response_failed')
        self.assertEqual(result['phase'], 'unknown')
        self.assertEqual(result['exception_type'], 'Exception')
        self.assertEqual(result['elapsed_ms'], 86400000)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertNotIn('private.invalid', json.dumps(result))

    async def test_empty_timeout_message_cannot_hide_failure_or_allow_late_revival(self):
        import asyncio
        controller, native = self.controller()
        await native.output.begin_speech('one')
        native.output.sink.observe.side_effect = TimeoutError()
        native.state = 'answering'
        native._speech_task = asyncio.create_task(native._finish_native_speech('answering'))
        await asyncio.wait_for(native._speech_task, 1)
        status = controller.status()
        self.assertEqual(status['runtime_failure']['code'], 'response_speech_completion_failed')
        self.assertEqual(status['error_code'], 'response_failed')
        await native.handle_notification('thread/realtime/itemAdded', {
            'threadId': 'own-thread', 'item': {'type': 'response.cancelled'}})
        self.assertEqual(native.state, 'failed')
        self.assertFalse(native.output.enabled)


class NativeToolDeliveryReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def test_marker_pending_is_not_a_delivered_question_and_snapshot_is_private(self):
        import asyncio
        from test_b_native import NativeLiveTests
        case = NativeLiveTests('runTest')
        await case.asyncSetUp()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(event, *, capture_output=False):
            if any(item.get('text') == 'RESULT_COMPLETE' for item in event.get('content', [])):
                entered.set()
                await release.wait()
        case.live.peer.send_event.side_effect = send
        try:
            await case.utterance('小白小白，PRIVATE 問題預算多少？')
            await asyncio.wait_for(entered.wait(), 1)
            progress = response_progress_diagnostic(case.live.status())
            receipt = progress['tool_deliveries'][-1]
            self.assertEqual(receipt['phase'], 'sending')
            self.assertEqual(receipt['source_version'], 3)
            self.assertTrue(receipt['available'])
            self.assertEqual(receipt['selected_record_indexes'], [1])
            self.assertFalse(receipt['marker_returned'])
            self.assertFalse(receipt['model_consumption_verified'])
            receipt['phase'] = 'mutated'
            self.assertEqual(response_progress_diagnostic(case.live.status())['tool_deliveries'][-1]['phase'], 'sending')
            release.set()
            await case.until(lambda: case.live.status()['tool_deliveries'][-1]['phase'] == 'delivered')
            complete = response_progress_diagnostic(case.live.status())['tool_deliveries'][-1]
            self.assertEqual(complete['phase'], 'delivered')
            self.assertTrue(complete['marker_returned'])
            self.assertGreaterEqual(complete['sent_bytes'], complete['payload_bytes'])
            self.assertNotIn('PRIVATE', json.dumps(complete))
        finally:
            release.set()
            await case.asyncTearDown()

    async def test_marker_failure_keeps_selected_version_but_cannot_claim_delivery(self):
        from test_b_native import NativeLiveTests
        case = NativeLiveTests('runTest')
        await case.asyncSetUp()
        async def send(event, *, capture_output=False):
            if any(item.get('text') == 'RESULT_COMPLETE' for item in event.get('content', [])):
                raise RuntimeError('PRIVATE provider body https://private.invalid/')
        case.live.peer.send_event.side_effect = send
        try:
            await case.utterance('小白小白，預算多少？')
            await case.until(lambda: case.live.state == 'sleeping' and any(
                item['phase'] == 'failed' for item in
                response_progress_diagnostic(case.live.status())['tool_deliveries']))
            receipt = response_progress_diagnostic(case.live.status())['tool_deliveries'][-1]
            self.assertEqual(receipt['source_version'], 3)
            self.assertEqual(receipt['phase'], 'failed')
            self.assertFalse(receipt['marker_returned'])
            self.assertFalse(receipt['model_consumption_verified'])
            self.assertNotIn('PRIVATE', json.dumps(receipt))
            self.assertFalse(case.output.active)
            self.assertIsNone(case.live.error)
        finally:
            await case.asyncTearDown()
