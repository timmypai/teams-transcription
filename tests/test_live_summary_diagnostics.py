"""Fixed-label probe diagnostics with controlled RPC/peer and no real service."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from teams_transcription.b_live import ResponseLivePeer
from teams_transcription.live_summary_diagnostics import DATA_EVENTS, RPC_EVENTS, LiveSummaryDiagnostics
from tests import test_live_summary_probe
from probe_budget_fixture import blocked_phase_budget


class LiveSummaryDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    # Reuse just the factory, without importing/inheriting a discoverable suite.
    make_probe = test_live_summary_probe.LiveSummaryProbeTests.make_probe

    def assert_cleaned(self, probe, result):
        self.assertEqual(result['stop']['cleanup_state'], 'cleaned')
        self.assertTrue(probe.output.closed)
        self.assertFalse(probe.output.enabled)
        self.assertIsNone(probe.receiver)
        probe.peer.close.assert_awaited_once()
        probe.rpc.close.assert_awaited_once()

    async def test_overall_budget_identifies_each_blocked_stage_and_ack_evidence(self):
        phases = ('binary_verify', 'rpc_initialize', 'account_read', 'thread_start', 'peer_bind', 'ice_offer',
            'realtime_start_ack', 'sdp_answer', 'sdp_apply', 'media_connected', 'append_text_ack', 'wait_output')
        for phase in phases:
            with self.subTest(phase=phase):
                probe, calls, _ = self.make_probe(mode='silent', timeout=.04)
                entered = asyncio.Event()
                release = asyncio.Event()
                async def block(*args, **kwargs):
                    entered.set()
                    await release.wait()
                original_request = probe.rpc.request.side_effect
                request_phase = {'account_read':'account/read',
                    'realtime_start_ack':'thread/realtime/start',
                    'append_text_ack':'thread/realtime/appendText'}.get(phase)
                async def request(method, params):
                    if method == request_phase:
                        calls.append((method, params))
                        return await block()
                    if phase == 'sdp_answer' and method == 'thread/realtime/start':
                        # Successful start operation return, without an SDP.
                        calls.append((method, params))
                        entered.set()
                        return {}
                    return await original_request(method, params)
                probe.rpc.request.side_effect = request
                if phase == 'binary_verify': probe._verify_pinned_binary.side_effect = block
                elif phase == 'rpc_initialize': probe.rpc.start.side_effect = block
                elif phase == 'thread_start': probe.rpc.thread.side_effect = block
                elif phase == 'peer_bind': probe.peer.bind_session.side_effect = block
                elif phase == 'ice_offer': probe.peer.offer.side_effect = block
                elif phase == 'sdp_apply': probe.peer.answer.side_effect = block
                elif phase == 'media_connected': probe.peer.wait_connected.side_effect = block
                elif phase == 'wait_output':
                    probe._finished.wait = AsyncMock(side_effect=block)
                # Test the blocked stage, not how quickly earlier AsyncMocks
                # happen to run on a loaded runner. Cancellation remains real.
                with blocked_phase_budget(probe, phase):
                    result = await probe.run()
                self.assertTrue(entered.is_set())
                self.assertEqual(result['state'], 'failed')
                self.assertEqual(result['failure_code'], 'live_summary_probe_timeout')
                self.assertIsNone(result['summary'])
                diagnostic = result['diagnostics']
                self.assertEqual(diagnostic['failure_phase'], phase)
                self.assertEqual(diagnostic['failure_kind'], 'cancelled')
                self.assertEqual(diagnostic['timeout_scope'], 'overall_budget')
                self.assertEqual(diagnostic['phases'][phase]['state'], 'cancelled')
                self.assertGreaterEqual(diagnostic['phases'][phase]['duration_ms'], 0)
                self.assertGreaterEqual(diagnostic['phases'][phase]['entered_ms'], 0)
                self.assertLessEqual(diagnostic['phases'][phase]['remaining_ms'], 40)
                self.assertEqual(diagnostic['rpc_ack_scope'], 'host_operation_return_only')
                self.assertEqual(diagnostic['rpc_returns']['realtime_start'],
                    phase in {'sdp_answer', 'sdp_apply', 'media_connected', 'append_text_ack', 'wait_output'})
                self.assertEqual(diagnostic['rpc_returns']['append_text'], phase == 'wait_output')
                self.assertFalse(any(method == 'turn/start' for method, _ in calls))
                self.assert_cleaned(probe, result)

    async def test_real_overall_budget_expires_in_prerequisite_not_unentered_sdp_apply(self):
        probe, _, _ = self.make_probe(mode='silent', timeout=.04)
        prerequisite = asyncio.Event()
        target = asyncio.Event()
        async def blocked_offer():
            prerequisite.set()
            await asyncio.Event().wait()
        async def answer(*args):
            target.set()
        probe.peer.offer.side_effect = blocked_offer
        probe.peer.answer.side_effect = answer
        result = await probe.run()  # NO fixture deadline injection here.
        self.assertTrue(prerequisite.is_set())
        self.assertFalse(target.is_set())
        self.assertEqual(result['diagnostics']['failure_phase'], 'ice_offer')
        self.assertEqual(result['diagnostics']['phases']['sdp_apply']['state'], 'not_entered')
        self.assertEqual(result['diagnostics']['timeout_scope'], 'overall_budget')
        self.assert_cleaned(probe, result)

    async def test_actual_peer_branch_marks_sdp_authorization_without_starting_browser(self):
        probe, _, _ = self.make_probe(timeout=.04)
        # Keep the production peer type for its authorize branch; every owned
        # operation is injected, so no browser/context/guard/network is started.
        peer = ResponseLivePeer(None, probe._data_event, probe._pcm, probe._audio_error,
            guard=object(), meeting_id=probe.meeting_id)
        peer.bind_session = AsyncMock()
        peer.offer = AsyncMock(return_value='controlled-offer')
        peer.begin_official_start = MagicMock()
        entered = asyncio.Event()
        async def authorize(*args):
            entered.set()
            await asyncio.Event().wait()
        peer.accept_official_answer = AsyncMock(side_effect=authorize)
        peer.answer = AsyncMock()
        peer.wait_connected = AsyncMock()
        peer.close = AsyncMock()
        probe.peer = peer
        result = await probe.run()
        self.assertTrue(entered.is_set())
        self.assertEqual(result['diagnostics']['failure_phase'], 'sdp_authorize')
        self.assertEqual(result['diagnostics']['timeout_scope'], 'overall_budget')
        self.assertTrue(result['diagnostics']['rpc_returns']['realtime_start'])
        self.assertFalse(result['diagnostics']['rpc_returns']['append_text'])
        peer.answer.assert_not_awaited()
        peer.wait_connected.assert_not_awaited()
        self.assert_cleaned(probe, result)

    async def test_inner_sdp_and_media_timeouts_remain_phase_timeouts(self):
        for phase in ('sdp_answer', 'media_connected'):
            with self.subTest(phase=phase):
                probe, calls, _ = self.make_probe(timeout=.3)
                private = 'SYNTHETIC_PRIVATE_TIMEOUT_PAYLOAD'
                if phase == 'media_connected':
                    probe.peer.wait_connected.side_effect = TimeoutError(private)
                    result = await probe.run()
                else:
                    original_request = probe.rpc.request.side_effect
                    async def request(method, params):
                        if method == 'thread/realtime/start':
                            calls.append((method, params))
                            return {}
                        return await original_request(method, params)
                    probe.rpc.request.side_effect = request
                    real_wait_for = asyncio.wait_for
                    async def inner_timeout(awaitable, timeout):
                        if timeout == 35:
                            raise TimeoutError(private)
                        return await real_wait_for(awaitable, timeout)
                    with patch('teams_transcription.b_live.asyncio.wait_for', side_effect=inner_timeout):
                        result = await probe.run()
                diagnostic = result['diagnostics']
                self.assertEqual(diagnostic['failure_phase'], phase)
                self.assertEqual(diagnostic['failure_kind'], 'timeout')
                self.assertEqual(diagnostic['timeout_scope'], 'phase_timeout')
                self.assertEqual(diagnostic['phases'][phase]['state'], 'failed')
                self.assertTrue(diagnostic['rpc_returns']['realtime_start'])
                self.assertFalse(diagnostic['rpc_returns']['append_text'])
                self.assertEqual(result['failure_code'], 'live_summary_probe_timeout')
                self.assertNotIn(private, json.dumps(result))
                self.assert_cleaned(probe, result)

    async def test_pending_append_preserves_observed_background_error_after_budget_timeout(self):
        probe, calls, notify = self.make_probe(mode='silent', timeout=.04)
        original_request = probe.rpc.request.side_effect
        blocked = asyncio.Event()
        async def request(method, params):
            if method == 'thread/realtime/appendText':
                calls.append((method, params))
                notify('thread/realtime/itemAdded', item={'type':'handoff_request',
                    'input_transcript':'SYNTHETIC_PRIVATE_HANDOFF_PAYLOAD'})
                # Require the actual receiver to observe rejection before the
                # RPC operation hangs. No scheduling sleep or sample polling.
                await probe._finished.wait()
                blocked.set()
                await asyncio.Event().wait()
            return await original_request(method, params)
        probe.rpc.request.side_effect = request
        result = await probe.run()
        self.assertTrue(blocked.is_set())
        self.assertEqual(result['failure_code'], 'live_summary_background_route_observed')
        self.assertIsNone(result['summary'])
        diagnostic = result['diagnostics']
        self.assertEqual(diagnostic['failure_phase'], 'append_text_ack')
        self.assertEqual(diagnostic['failure_kind'], 'cancelled')
        self.assertEqual(diagnostic['timeout_scope'], 'overall_budget')
        self.assertTrue(diagnostic['rpc_returns']['realtime_start'])
        self.assertFalse(diagnostic['rpc_returns']['append_text'])
        self.assertEqual(diagnostic['item_type_counts']['handoff_request'], 1)
        self.assertNotIn('SYNTHETIC_PRIVATE_HANDOFF_PAYLOAD', json.dumps(result))
        self.assert_cleaned(probe, result)

    async def test_handoff_then_invalid_done_preserves_first_error_in_actual_receiver(self):
        probe, calls, notify = self.make_probe(mode='silent')
        original_request = probe.rpc.request.side_effect
        async def request(method, params):
            if method == 'thread/realtime/appendText':
                calls.append((method, params))
                notify('thread/realtime/itemAdded', item={'type':'handoff_request'})
                notify('thread/realtime/transcript/done', role='assistant',
                       text='invalid JSON\n' + probe.stream.marker)
                return {}
            return await original_request(method, params)
        probe.rpc.request.side_effect = request
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_background_route_observed')
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['summary'])
        self.assertEqual(result['diagnostics']['item_type_counts']['handoff_request'], 1)
        self.assertEqual(result['diagnostics']['transcript_role_counts']['assistant'], 1)
        self.assertEqual(probe.stream.status()['error'], 'summary_stream_invalid_json')
        self.assertTrue(result['stop']['ack_received'])
        self.assertTrue(result['stop']['closed_received'])
        self.assert_cleaned(probe, result)

    async def test_transport_then_handoff_preserves_first_error_in_actual_receiver(self):
        probe, calls, notify = self.make_probe(mode='silent')
        original_request = probe.rpc.request.side_effect
        async def request(method, params):
            if method == 'thread/realtime/appendText':
                calls.append((method, params))
                await probe._audio_error('SYNTHETIC_PRIVATE_TRANSPORT_ERROR')
                notify('thread/realtime/itemAdded', item={'type':'handoff_request'})
                return {}
            return await original_request(method, params)
        probe.rpc.request.side_effect = request
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_transport_failed')
        self.assertEqual(result['state'], 'failed')
        self.assertIsNone(result['summary'])
        self.assertEqual(result['diagnostics']['item_type_counts']['handoff_request'], 1)
        self.assertNotIn('SYNTHETIC_PRIVATE_TRANSPORT_ERROR', json.dumps(result))
        self.assertTrue(result['stop']['ack_received'])
        self.assertTrue(result['stop']['closed_received'])
        self.assert_cleaned(probe, result)

    async def test_handoff_then_startup_assistant_preserves_first_error(self):
        probe, _, notify = self.make_probe(mode='silent')
        original_request = probe.rpc.request.side_effect
        async def request(method, params):
            if method == 'thread/realtime/start':
                notify('thread/realtime/itemAdded', item={'type':'handoff_request'})
                notify('thread/realtime/transcript/delta', role='assistant', delta='startup')
            return await original_request(method, params)
        probe.rpc.request.side_effect = request
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_background_route_observed')
        self.assertIsNone(result['summary'])
        self.assertEqual(result['diagnostics']['item_type_counts']['handoff_request'], 1)
        self.assertEqual(result['diagnostics']['transcript_role_counts']['assistant'], 1)
        self.assertFalse(result['diagnostics']['rpc_returns']['append_text'])
        self.assert_cleaned(probe, result)

    async def test_first_inner_timeout_survives_cleanup_crossing_overall_deadline(self):
        probe, _, _ = self.make_probe(timeout=.04)
        probe.peer.wait_connected.side_effect = TimeoutError('SYNTHETIC_PRIVATE_MEDIA_TIMEOUT')
        cleanup_entered, release_cleanup = asyncio.Event(), asyncio.Event()
        async def close_peer():
            cleanup_entered.set()
            await release_cleanup.wait()
        probe.peer.close.side_effect = close_peer
        run = asyncio.create_task(probe.run())
        async with asyncio.timeout(1):
            await cleanup_entered.wait()
            # Explicit controlled release after the run's budget. Cleanup is
            # shielded, and its completion is observed before result checking.
            release = asyncio.get_running_loop().call_later(.08, release_cleanup.set)
            try:
                result = await run
            finally:
                release.cancel()
                release_cleanup.set()
        diagnostic = result['diagnostics']
        self.assertEqual(diagnostic['failure_phase'], 'media_connected')
        self.assertEqual(diagnostic['failure_kind'], 'timeout')
        self.assertEqual(diagnostic['phases']['media_connected']['state'], 'failed')
        self.assertEqual(diagnostic['timeout_scope'], 'overall_budget')
        self.assertEqual(diagnostic['phases']['cleanup_peer']['state'], 'returned')
        self.assertNotIn('SYNTHETIC_PRIVATE_MEDIA_TIMEOUT', json.dumps(result))
        self.assert_cleaned(probe, result)

    async def test_exception_failure_kind_uses_fixed_phase_without_private_error_text(self):
        probe, _, _ = self.make_probe()
        original_request = probe.rpc.request.side_effect
        async def request(method, params):
            if method == 'account/read':
                raise RuntimeError('SYNTHETIC_PRIVATE_ACCOUNT_ERROR')
            return await original_request(method, params)
        probe.rpc.request.side_effect = request
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_transport_failed')
        self.assertEqual(result['diagnostics']['failure_phase'], 'account_read')
        self.assertEqual(result['diagnostics']['failure_kind'], 'exception')
        self.assertIsNone(result['diagnostics']['timeout_scope'])
        self.assertNotIn('SYNTHETIC_PRIVATE_ACCOUNT_ERROR', json.dumps(result))
        self.assert_cleaned(probe, result)

    async def test_actual_notification_and_data_hooks_expose_counts_without_payloads(self):
        probe, _, notify = self.make_probe()
        private = 'SYNTHETIC_PRIVATE_EVENT_METHOD_ROLE_TYPE_FOREIGN_SDP'
        original_request = probe.rpc.request.side_effect
        async def request(method, params):
            if method == 'thread/realtime/appendText':
                notify(private, payload=private, sdp=private)
                notify('thread/realtime/itemAdded', item={'type':private, 'payload':private})
                notify('thread/realtime/transcript/delta', role=private, delta=private)
                notify('thread/realtime/transcript/done', thread=private, role='assistant', text=private)
                notify('thread/realtime/transcript/done', thread=None, role='assistant', text=private)
                await probe._data_event({'type':private, 'payload':private})
                await probe._data_event({'type':'session.updated', 'raw':private})
            return await original_request(method, params)
        probe.rpc.request.side_effect = request
        result = await probe.run()
        self.assertEqual(result['state'], 'draft_ready')
        self.assertFalse(result['service_contract_verified'])
        diagnostic = result['diagnostics']
        self.assertEqual(diagnostic['rpc_event_counts']['unknown'], 1)
        self.assertEqual(diagnostic['item_type_counts']['other'], 1)
        self.assertEqual(diagnostic['transcript_role_counts']['other'], 1)
        self.assertEqual(diagnostic['notification_scope_counts']['foreign'], 2)
        self.assertEqual(diagnostic['notification_scope_counts']['unscoped'], 1)
        self.assertEqual(diagnostic['data_channel_event_counts']['unknown'], 1)
        self.assertEqual(diagnostic['data_channel_event_counts']['session.updated'], 1)
        self.assertEqual(diagnostic['rpc_returns'], {'realtime_start':True, 'append_text':True, 'stop':True})
        self.assertNotIn(private, json.dumps(result))
        self.assertLess(len(json.dumps(diagnostic).encode()), 8192)
        self.assert_cleaned(probe, result)

    def test_fixed_event_labels_counts_privacy_and_snapshot_copy(self):
        now = [10.0]
        diagnostic = LiveSummaryDiagnostics(.3, clock=lambda:now[0])
        diagnostic.begin()
        now[0] += .125
        private = 'SYNTHETIC_PRIVATE_METHOD_ROLE_TYPE_THREAD_SDP_TEXT'
        for method in RPC_EVENTS:
            diagnostic.notification(method, {'threadId':'own', 'role':'assistant',
                'delta':private, 'text':private, 'sdp':private,
                'item':{'type':private, 'text':private}}, 'own')
        diagnostic.notification(private, {'threadId':'own', 'payload':private}, 'own')
        diagnostic.notification('turn/' + private, {'threadId':'own', 'payload':private}, 'own')
        diagnostic.notification('thread/realtime/transcript/delta',
            {'threadId':'own', 'role':private, 'delta':private}, 'own')
        diagnostic.notification('thread/realtime/itemAdded',
            {'threadId':'own', 'item':{'type':'handoff_request', 'payload':private}}, 'own')
        diagnostic.notification('thread/realtime/itemAdded',
            {'threadId':'own', 'item':{'type':'delegation', 'payload':private}}, 'own')
        diagnostic.notification('thread/realtime/transcript/done',
            {'threadId':'own', 'role':'user', 'text':private}, 'own')
        # Foreign and unscoped payloads only affect fixed scope counters.
        diagnostic.notification('thread/realtime/itemAdded',
            {'threadId':private, 'item':{'type':'handoff_request', 'payload':private}}, 'own')
        diagnostic.notification('thread/realtime/transcript/done',
            {'role':'assistant', 'text':private}, 'own')
        for kind in DATA_EVENTS:
            diagnostic.data_event({'type':kind, 'role':private, 'payload':private})
        diagnostic.data_event({'type':private, 'payload':private})
        first = diagnostic.snapshot()
        self.assertNotIn(private, json.dumps(first))
        self.assertEqual(first['notification_scope_counts']['foreign'], 1)
        self.assertEqual(first['notification_scope_counts']['unscoped'], 1)
        self.assertEqual(first['item_type_counts'], {'handoff_request':1, 'delegation':1, 'other':1})
        self.assertEqual(first['transcript_role_counts'], {'assistant':2, 'user':1, 'other':1})
        self.assertEqual(first['rpc_event_counts']['unknown'], 1)
        self.assertEqual(first['rpc_event_counts']['ordinary_turn'], 1)
        self.assertEqual(first['data_channel_event_counts']['unknown'], 2)
        self.assertEqual(first['first_assistant_ms'], 125)
        for bad_label in ([private], {'private':private}):
            diagnostic.notification(bad_label, {'threadId':'own'}, 'own')
            diagnostic.notification('thread/realtime/transcript/delta',
                {'threadId':'own', 'role':bad_label, 'delta':private}, 'own')
            diagnostic.notification('thread/realtime/itemAdded',
                {'threadId':'own', 'item':{'type':bad_label, 'payload':private}}, 'own')
            diagnostic.data_event({'type':bad_label, 'payload':private})
        malformed = diagnostic.snapshot()
        self.assertNotIn(private, json.dumps(malformed))
        self.assertEqual(malformed['rpc_event_counts']['unknown'], 3)
        self.assertEqual(malformed['transcript_role_counts']['other'], 3)
        self.assertEqual(malformed['item_type_counts']['other'], 3)
        self.assertEqual(malformed['data_channel_event_counts']['unknown'], 4)
        # Unknown dynamic labels cannot make a diagnostic grow with payloads.
        for index in range(1000):
            diagnostic.notification(private + str(index), {'threadId':'own'}, 'own')
            diagnostic.data_event({'type':private + str(index)})
        bounded = diagnostic.snapshot()
        self.assertLess(len(json.dumps(bounded).encode()), 8192)
        self.assertNotIn(private, json.dumps(bounded))
        bounded['phases']['account_read']['state'] = private
        bounded['rpc_event_counts']['unknown'] = -1
        bounded['transcript_role_counts']['assistant'] = -1
        fresh = diagnostic.snapshot()
        self.assertEqual(fresh['phases']['account_read']['state'], 'not_entered')
        self.assertEqual(fresh['rpc_event_counts']['unknown'], 1003)
        self.assertEqual(fresh['transcript_role_counts']['assistant'], 2)


if __name__ == '__main__':
    unittest.main()
