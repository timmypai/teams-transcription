"""Owned stop receipts and bounded initial-items drafts; no real Live service."""
import asyncio
import json
import unittest
from unittest.mock import patch

from teams_transcription.live_summary_probe import LiveSummaryProbe as ActualProbe
from teams_transcription.rpc import ReceivedNotification
from tests import test_live_summary_probe


class LiveSummaryTerminalTests(unittest.IsolatedAsyncioTestCase):
    def make_probe(self, *, input_mode='append_text', clock=None, **options):
        def construct(*args, **kwargs):
            kwargs['input_mode'] = input_mode
            if clock is not None: kwargs['clock'] = clock
            return ActualProbe(*args, **kwargs)
        with patch.object(test_live_summary_probe, 'LiveSummaryProbe', side_effect=construct):
            return test_live_summary_probe.LiveSummaryProbeTests.make_probe(self, **options)

    def candidate(self, probe):
        value = {'重點':[{'text':'預算十萬元。', 'sources':['s1'], 'owner':'', 'date':''}],
            '決策':[], '待辦':[], '未決問題':[]}
        return json.dumps(value, ensure_ascii=False) + '\n' + probe.stream.marker

    def assert_cleaned(self, probe, result):
        self.assertEqual(result['stop']['cleanup_state'], 'cleaned')
        self.assertTrue(probe.output.closed)
        self.assertIsNone(probe.receiver)
        probe.peer.close.assert_awaited_once()
        probe.rpc.close.assert_awaited_once()
        self.assertFalse(result['service_contract_verified'])
        self.assertFalse(result['provider_final'])

    async def test_spontaneous_close_requires_a_new_post_stop_receipt(self):
        for requested_close in (False, True):
            with self.subTest(new_close=requested_close):
                probe, calls, notify = self.make_probe(mode='silent', stop_closed=False)
                original = probe.rpc.request.side_effect
                async def request(method, params):
                    if method == 'thread/realtime/appendText':
                        calls.append((method, params))
                        notify('thread/realtime/closed', reason='SYNTHETIC_PRIVATE_CLOSE_REASON')
                        await probe._finished.wait()
                        return {}
                    if method == 'thread/realtime/stop':
                        self.assertIsNotNone(probe.receiver)
                        self.assertFalse(probe.receiver.done())
                        self.assertFalse(probe._closed_event.is_set())
                        if requested_close:
                            notify('thread/realtime/closed', reason='requested')
                    return await original(method, params)
                probe.rpc.request.side_effect = request
                result = await probe.run()
                self.assertEqual(result['state'], 'failed')
                self.assertIsNone(result['summary'])
                self.assertEqual(result['failure_code'], 'live_summary_transport_failed')
                self.assertTrue(result['stop']['ack_received'])
                self.assertEqual(result['stop']['closed_received'], requested_close)
                self.assertEqual(result['stop']['state'], 'stopped' if requested_close else 'timeout')
                diagnostic = result['diagnostics']
                self.assertEqual(diagnostic['version'], 2)
                self.assertEqual(diagnostic['stop_confirmation_scope'], 'own_receipt_after_stop_dispatch')
                self.assertEqual(diagnostic['rpc_event_counts']['closed'], 2 if requested_close else 1)
                self.assertEqual(diagnostic['closed_before_stop']['reason'], 'unknown')
                if requested_close:
                    self.assertEqual(diagnostic['closed_after_stop']['reason'], 'requested')
                else:
                    self.assertIsNone(diagnostic['closed_after_stop'])
                self.assertEqual(diagnostic['first_transport_event']['cause'], 'rpc_closed')
                self.assertEqual(diagnostic['first_transport_event']['active_phase'], 'append_text_ack')
                self.assertNotIn('SYNTHETIC_PRIVATE_CLOSE_REASON', json.dumps(result))
                self.assert_cleaned(probe, result)

    async def test_real_queued_pre_stop_receipt_is_rejected_after_stop_dispatch(self):
        probe, _, _ = self.make_probe(stop_closed=False)
        original_request = probe.rpc.request.side_effect
        original_get = probe.rpc.notifications.get
        release = asyncio.Event()
        old_close = None
        async def get():
            message = await original_get()
            if message is old_close:
                await release.wait()
            return message
        probe.rpc.notifications.get = get
        async def request(method, params):
            nonlocal old_close
            response = await original_request(method, params)
            if method == 'thread/realtime/appendText':
                old_close = ReceivedNotification({'method':'thread/realtime/closed',
                    'params':{'threadId':'probe-thread', 'reason':'requested'}})
                probe.rpc.notifications.put_nowait(old_close)
            elif method == 'thread/realtime/stop':
                self.assertLessEqual(old_close.received_monotonic, probe._stop_issued_at)
                release.set()
            return response
        probe.rpc.request.side_effect = request
        result = await probe.run()
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['summary'])
        self.assertTrue(result['stop']['ack_received'])
        self.assertFalse(result['stop']['closed_received'])
        self.assertEqual(result['stop']['state'], 'timeout')
        self.assertEqual(result['diagnostics']['closed_before_stop']['reason'], 'requested')
        self.assertIsNone(result['diagnostics']['closed_after_stop'])
        self.assert_cleaned(probe, result)

    async def test_foreign_or_unscoped_post_stop_close_does_not_confirm_owned_stop(self):
        for scope in ('foreign-thread', None):
            with self.subTest(scope=scope):
                probe, _, notify = self.make_probe(stop_closed=False)
                original = probe.rpc.request.side_effect
                async def request(method, params):
                    if method == 'thread/realtime/stop':
                        notify('thread/realtime/closed', thread=scope, reason='requested')
                    return await original(method, params)
                probe.rpc.request.side_effect = request
                result = await probe.run()
                self.assertEqual(result['state'], 'draft_ready')
                self.assertTrue(result['stop']['ack_received'])
                self.assertFalse(result['stop']['closed_received'])
                self.assertEqual(result['stop']['state'], 'timeout')
                self.assertIsNone(result['diagnostics']['closed_after_stop'])
                self.assertIsNone(result['diagnostics']['first_transport_event'])
                self.assert_cleaned(probe, result)

    async def test_close_in_cleanup_window_invalidates_valid_sentinel_before_stop(self):
        probe, _, _ = self.make_probe()
        close_observed = asyncio.Event()
        original_audio_error = probe._audio_error
        async def audio_error(reason):
            await original_audio_error(reason)
            close_observed.set()
        probe._audio_error = audio_error
        original_close = probe.output.close
        async def close_output():
            self.assertTrue(probe._closing)
            self.assertIsNone(probe._stop_issued_at)
            self.assertTrue(probe.stream.ready)
            message = ReceivedNotification({'method':'thread/realtime/closed',
                'params':{'threadId':'probe-thread', 'reason':'transport_closed'}})
            probe.rpc.notifications.put_nowait(message)
            await close_observed.wait()
            await original_close()
        probe.output.close = close_output
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_transport_failed')
        self.assertIsNone(result['summary'])
        self.assertTrue(result['stop']['ack_received'])
        self.assertTrue(result['stop']['closed_received'])
        self.assertEqual(result['diagnostics']['rpc_event_counts']['closed'], 2)
        self.assertEqual(result['diagnostics']['closed_before_stop']['reason'], 'transport_closed')
        self.assert_cleaned(probe, result)

    async def test_first_rpc_transport_callback_precedes_later_decoder_error(self):
        now = [10.0]
        probe, _, notify = self.make_probe(mode='silent', clock=lambda:now[0])
        original = probe.rpc.request.side_effect
        async def request(method, params):
            if method == 'thread/realtime/appendText':
                now[0] = 10.125
                notify('thread/realtime/closed', reason={'SYNTHETIC_PRIVATE_REASON':[]})
                await probe._finished.wait()
                now[0] = 10.5
                await probe._audio_error('remote_decoder_failed')
                return {}
            return await original(method, params)
        probe.rpc.request.side_effect = request
        result = await probe.run()
        diagnostic = result['diagnostics']
        self.assertEqual(diagnostic['first_transport_event'],
            {'cause':'rpc_closed', 'active_phase':'append_text_ack', 'elapsed_ms':125})
        self.assertEqual(diagnostic['closed_before_stop'], {'reason':'unknown', 'elapsed_ms':125})
        self.assertNotIn('SYNTHETIC_PRIVATE_REASON', json.dumps(result))
        self.assertLess(len(json.dumps(diagnostic).encode()), 8192)
        snapshot = probe.diagnostics.snapshot()
        snapshot['first_transport_event']['cause'] = 'mutated'
        self.assertEqual(probe.diagnostics.snapshot()['first_transport_event']['cause'], 'rpc_closed')
        self.assert_cleaned(probe, result)

    async def test_closed_or_rpc_error_with_pending_answer_cannot_admit_late_sdp(self):
        for method, cause in (('thread/realtime/closed', 'rpc_closed'),
                              ('thread/realtime/error', 'rpc_error')):
            with self.subTest(method=method):
                probe, calls, notify = self.make_probe(mode='silent')
                original = probe.rpc.request.side_effect
                async def request(rpc_method, params):
                    if rpc_method == 'thread/realtime/start':
                        calls.append((rpc_method, params))
                        notify(method, reason='SYNTHETIC_PRIVATE_RPC_REASON')
                        await probe._finished.wait()
                        notify('thread/realtime/sdp', sdp='SYNTHETIC_PRIVATE_LATE_SDP')
                        return {}
                    return await original(rpc_method, params)
                probe.rpc.request.side_effect = request
                result = await probe.run()
                self.assertEqual(result['failure_code'], 'live_summary_transport_failed')
                probe.peer.answer.assert_not_awaited()
                probe.peer.wait_connected.assert_not_awaited()
                self.assertFalse(any(name == 'thread/realtime/appendText' for name, _ in calls))
                self.assertEqual(result['diagnostics']['first_transport_event']['cause'], cause)
                self.assertEqual(result['diagnostics']['rpc_event_counts']['sdp'], 1)
                self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))
                self.assert_cleaned(probe, result)

    async def test_initial_items_admits_assistant_during_start_without_append(self):
        entries = [{'id':'s1', 'text':'預算十萬元。'}]
        prior = '上次仍待確認預算。'
        probe, calls, notify = self.make_probe(input_mode='initial_items', mode='silent',
            entries=entries, prior_minutes=prior)
        entries[0]['text'] = 'mutated-source'
        original = probe.rpc.request.side_effect
        async def request(method, params):
            response = await original(method, params)
            if method == 'thread/realtime/start':
                self.assertFalse(probe.started)
                self.assertTrue(probe._input_admitted)
                notify('thread/realtime/transcript/done', role='assistant', text=self.candidate(probe))
                await probe._finished.wait()
            return response
        probe.rpc.request.side_effect = request
        result = await probe.run()
        self.assertEqual(result['state'], 'draft_ready')
        self.assertEqual(result['input_mode'], 'initial_items')
        self.assertFalse(any(name == 'thread/realtime/appendText' for name, _ in calls))
        start = next(params for name, params in calls if name == 'thread/realtime/start')
        self.assertEqual(len(start['initialItems']), 1)
        item = start['initialItems'][0]
        self.assertEqual(item['role'], 'user')
        source = item['text'].split('SOURCE_JSON=\n', 1)[1].split('\n完成標記=', 1)[0]
        self.assertEqual(json.loads(source), [{'id':'__prior_minutes__', 'text':prior},
            {'id':'s1', 'text':'預算十萬元。'}])
        self.assertIn(probe.stream.marker, item['text'])
        self.assertNotIn('mutated-source', item['text'])
        self.assertFalse(result['diagnostics']['rpc_returns']['append_text'])
        self.assertEqual(result['diagnostics']['phases']['append_text_ack']['state'], 'not_entered')
        self.assert_cleaned(probe, result)

    async def test_initial_handoff_and_invalid_response_fail_without_append(self):
        for kind in ('handoff', 'invalid'):
            with self.subTest(kind=kind):
                probe, calls, notify = self.make_probe(input_mode='initial_items', mode='silent')
                original = probe.rpc.request.side_effect
                async def request(method, params):
                    response = await original(method, params)
                    if method == 'thread/realtime/start':
                        if kind == 'handoff':
                            notify('thread/realtime/itemAdded', item={'type':'handoff_request'})
                        else:
                            notify('thread/realtime/transcript/done', role='assistant',
                                text='bad JSON\n' + probe.stream.marker)
                        await probe._finished.wait()
                    return response
                probe.rpc.request.side_effect = request
                result = await probe.run()
                self.assertEqual(result['state'], 'failed')
                self.assertIsNone(result['summary'])
                self.assertEqual(result['failure_code'], 'live_summary_background_route_observed'
                    if kind == 'handoff' else 'live_summary_stream_failed')
                self.assertFalse(any(name == 'thread/realtime/appendText' for name, _ in calls))
                self.assert_cleaned(probe, result)

    def test_complete_initial_item_utf8_boundary_includes_wrapper_and_nonce(self):
        small, _, _ = self.make_probe(input_mode='initial_items', entries=[{'id':'s1', 'text':'x'}])
        small.thread_id = 'probe-thread'
        initial = small.live_start_params('controlled-offer')['initialItems'][0]['text']
        available = 32768 - (len(initial.encode()) - 1)
        text = '字'*(available//3) + 'x'*(available%3)
        accepted, _, _ = self.make_probe(input_mode='initial_items', entries=[{'id':'s1', 'text':text}])
        accepted.thread_id = 'probe-thread'
        item = accepted.live_start_params('controlled-offer')['initialItems'][0]
        self.assertEqual(len(item['text'].encode()), 32768)
        with patch('teams_transcription.b_live.CodexRPC') as rpc:
            with self.assertRaisesRegex(ValueError, 'initial item budget exceeded'):
                self.make_probe(input_mode='initial_items', entries=[{'id':'s1', 'text':text+'x'}])
            rpc.assert_not_called()

    def test_invalid_input_mode_is_rejected_before_rpc_construction(self):
        with patch('teams_transcription.b_live.CodexRPC') as rpc:
            for mode in ('initialItems', '', None, [], {}):
                with self.subTest(mode_type=type(mode).__name__), self.assertRaises(ValueError):
                    self.make_probe(input_mode=mode)
            rpc.assert_not_called()


if __name__ == '__main__':
    unittest.main()
