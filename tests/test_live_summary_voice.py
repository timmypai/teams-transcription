"""Synthetic PCM dispatch evidence; no model, browser, device or audio file."""
import asyncio
import json
import struct
import unittest
from unittest.mock import patch

from teams_transcription.live_summary_probe import LiveSummaryProbe as ActualProbe
from teams_transcription.rpc import ReceivedNotification
from tests import test_live_summary_probe


class LiveSummaryVoiceTests(unittest.IsolatedAsyncioTestCase):
    def make_probe(self, *, voice_pcm=None, input_mode='initial_items', **options):
        def construct(*args, **kwargs):
            return ActualProbe(*args, **kwargs, voice_pcm=voice_pcm, input_mode=input_mode)
        with patch.object(test_live_summary_probe, 'LiveSummaryProbe', side_effect=construct):
            return test_live_summary_probe.LiveSummaryProbeTests.make_probe(self, **options)

    def candidate(self, probe):
        value = {'重點':[{'text':'預算十萬元。', 'sources':['s1'], 'owner':'', 'date':''}],
            '決策':[], '待辦':[], '未決問題':[]}
        return json.dumps(value, ensure_ascii=False) + '\n' + probe.stream.marker

    def assert_cleaned(self, probe, result):
        self.assertEqual(result['stop']['cleanup_state'], 'cleaned')
        self.assertTrue(probe.output.closed)
        self.assertFalse(probe.output.enabled)
        self.assertIsNone(probe.receiver)
        self.assertIsNone(probe._voice_pcm)
        probe.peer.close.assert_awaited_once()
        probe.rpc.close.assert_awaited_once()
        self.assertFalse(result['service_contract_verified'])
        if result['voice_input'] is not None:
            self.assertFalse(result['voice_input']['backend_consumption_verified'])
            self.assertFalse(result['voice_input']['voice_trigger_verified'])

    def test_pcm_validation_and_mode_rejection_precede_rpc_construction(self):
        invalid = (b'', b'\x01', b'\0\0', b'\0\0'*2400,
            b'\1\0'*240001, bytearray(b'\1\0'), 'synthetic-pcm', True)
        with patch('teams_transcription.b_live.CodexRPC') as rpc:
            for pcm in invalid:
                with self.subTest(kind=type(pcm).__name__, size=len(pcm) if hasattr(pcm, '__len__') else None), \
                        self.assertRaises(ValueError):
                    self.make_probe(voice_pcm=pcm)
            with self.assertRaises(ValueError):
                self.make_probe(voice_pcm=b'\1\0', input_mode='append_text')
            rpc.assert_not_called()

    def test_one_frame_and_ten_second_pcm_boundaries_construct_without_starting(self):
        for pcm in (b'\1\0', b'\1\0'*240000):
            with self.subTest(frames=len(pcm)//2):
                probe, calls, _ = self.make_probe(voice_pcm=pcm)
                self.assertEqual(probe._voice_pcm, pcm)
                self.assertEqual(calls, [])
                probe.rpc.start.assert_not_awaited()
                probe.peer.push.assert_not_awaited()

    async def test_default_without_voice_preserves_append_text_fixture_behavior(self):
        probe, calls, _ = self.make_probe(input_mode='append_text')
        result = await probe.run()
        self.assertEqual(result['state'], 'draft_ready')
        self.assertTrue(any(method == 'thread/realtime/appendText' for method, _ in calls))
        probe.peer.push.assert_not_awaited()
        probe.peer.stats.assert_not_awaited()
        self.assertIsNone(result['voice_input'])
        self.assert_cleaned(probe, result)

    async def test_exact_pcm_chunks_order_and_local_evidence_do_not_claim_consumption(self):
        pcm = struct.pack('<h', 123)*2400 + struct.pack('<h', -321)*2400 + b'\0\0'*3 + b'\1\0'*14
        probe, calls, notify = self.make_probe(voice_pcm=pcm, mode='silent', timeout=.8)
        chunks = []
        async def push(piece):
            chunks.append(piece)
            if sum(map(len, chunks)) == len(pcm):
                notify('thread/realtime/transcript/done', role='assistant', text=self.candidate(probe))
                await probe._finished.wait()
        probe.peer.push.side_effect = push
        private = 'SYNTHETIC_PRIVATE_RTP_IDENTIFIER'
        probe.peer.stats.side_effect = [
            {'reports':[{'packetsSent':2, 'bytesSent':100, 'ssrc':private}]},
            {'reports':[{'packetsSent':5, 'bytesSent':700, 'id':private}]},
            {'reports':[{'packetsSent':6, 'bytesSent':900, 'remoteAddress':private}]},
        ]
        result = await probe.run()
        self.assertEqual(result['state'], 'draft_ready')
        self.assertEqual(chunks, [pcm[:4800], pcm[4800:9600], pcm[9600:]])
        self.assertEqual(b''.join(chunks), pcm)
        self.assertTrue(all(len(chunk) <= 4800 and len(chunk)%2 == 0 for chunk in chunks))
        self.assertEqual([len(chunk)/48000 for chunk in chunks], [.1, .1, 17/24000])
        voice = result['voice_input']
        self.assertEqual(voice['requested_frames'], 4817)
        self.assertEqual(voice['nonzero_requested_frames'], 4814)
        self.assertEqual(voice['dispatch_attempt_frames'], 4817)
        self.assertEqual(voice['push_returned_frames'], 4817)
        self.assertEqual(voice['push_calls_started'], 3)
        self.assertEqual(voice['push_calls_returned'], 3)
        self.assertEqual((voice['sample_rate'], voice['channels'], voice['format']), (24000, 1, 'pcm16le'))
        self.assertEqual(voice['duration_ms'], 4817/24)
        self.assertEqual(voice['nonzero_dispatch_frames'], 4814)
        self.assertEqual(voice['nonzero_returned_frames'], 4814)
        self.assertEqual(voice['push_return_scope'], 'host_media_schedule_return_only')
        self.assertEqual(voice['rtp_scope'], 'browser_outbound_audio_rtp_cumulative')
        for stage, packets, count in (('before',2,100), ('after',5,700), ('final',6,900)):
            self.assertEqual(voice['rtp'][stage],
                {'state':'available', 'reports':1, 'packets_sent':packets, 'bytes_sent':count})
        self.assertFalse(any(method in {'turn/start', 'thread/realtime/appendText',
            'thread/realtime/appendSpeech', 'thread/realtime/appendAudio'} for method, _ in calls))
        probe.peer.send_event.assert_not_awaited()
        self.assertNotIn(private, json.dumps(result))
        self.assert_cleaned(probe, result)

    async def test_push_failure_stops_later_chunks_and_retains_attempt_return_distinction(self):
        for failure_call in (1, 2):
            with self.subTest(failure_call=failure_call):
                probe, _, _ = self.make_probe(voice_pcm=b'\1\0'*6000, mode='silent')
                pieces = []
                async def push(piece):
                    pieces.append(piece)
                    if len(pieces) == failure_call:
                        raise RuntimeError('SYNTHETIC_PRIVATE_PCM_PUSH_ERROR')
                probe.peer.push.side_effect = push
                result = await probe.run()
                self.assertEqual(result['failure_code'], 'live_summary_voice_input_failed')
                self.assertIsNone(result['summary'])
                self.assertEqual(len(pieces), failure_call)
                self.assertTrue(all(piece == b'\1\0'*2400 for piece in pieces))
                voice = result['voice_input']
                self.assertEqual(voice['push_calls_started'], failure_call)
                self.assertEqual(voice['push_calls_returned'], failure_call-1)
                self.assertEqual(voice['dispatch_attempt_frames'], 2400*failure_call)
                self.assertEqual(voice['push_returned_frames'], 2400*(failure_call-1))
                self.assertNotIn('SYNTHETIC_PRIVATE_PCM_PUSH_ERROR', json.dumps(result))
                self.assert_cleaned(probe, result)

    async def test_hanging_push_uses_same_overall_budget_and_owned_cleanup(self):
        probe, _, _ = self.make_probe(voice_pcm=b'\1\0'*6000, mode='silent', timeout=.04)
        entered = asyncio.Event()
        async def push(piece):
            entered.set()
            await asyncio.Event().wait()
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertTrue(entered.is_set())
        self.assertEqual(result['failure_code'], 'live_summary_probe_timeout')
        self.assertEqual(result['diagnostics']['timeout_scope'], 'overall_budget')
        self.assertEqual(result['voice_input']['push_calls_started'], 1)
        self.assertEqual(result['voice_input']['push_calls_returned'], 0)
        self.assertEqual(result['voice_input']['rtp']['final']['state'], 'not_observed')
        self.assert_cleaned(probe, result)

    async def test_transport_failure_during_push_precedes_pcm_error_and_cannot_borrow_close(self):
        probe, _, notify = self.make_probe(voice_pcm=b'\1\0'*6000, mode='silent', stop_closed=False)
        async def push(piece):
            notify('thread/realtime/closed', reason='SYNTHETIC_PRIVATE_PRE_STOP_CLOSE')
            await probe._finished.wait()
            raise RuntimeError('SYNTHETIC_PRIVATE_PUSH_AFTER_CLOSE')
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_transport_failed')
        self.assertTrue(result['stop']['ack_received'])
        self.assertFalse(result['stop']['closed_received'])
        self.assertEqual(result['stop']['state'], 'timeout')
        probe.peer.push.assert_awaited_once()
        self.assertEqual(result['diagnostics']['first_transport_event']['cause'], 'rpc_closed')
        self.assertNotIn('SYNTHETIC_PRIVATE', json.dumps(result))
        self.assert_cleaned(probe, result)

    async def test_assistant_before_voice_dispatch_is_rejected_without_pushing_pcm(self):
        probe, calls, notify = self.make_probe(voice_pcm=b'\1\0'*20, mode='silent')
        original_request = probe.rpc.request.side_effect
        async def request(method, params):
            response = await original_request(method, params)
            if method == 'thread/realtime/start':
                notify('thread/realtime/transcript/done', role='assistant', text=self.candidate(probe))
                await probe._finished.wait()
            return response
        probe.rpc.request.side_effect = request
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_unexpected_pre_voice_output')
        self.assertIsNone(result['summary'])
        probe.peer.push.assert_not_awaited()
        self.assertEqual(result['voice_input']['dispatch_attempt_frames'], 0)
        self.assertFalse(any(method == 'thread/realtime/appendText' for method, _ in calls))
        self.assert_cleaned(probe, result)

    async def test_own_rpc_and_bound_data_channel_roles_count_after_dispatch_without_payloads(self):
        probe, _, _ = self.make_probe(voice_pcm=b'\1\0'*20, mode='silent')
        private = 'SYNTHETIC_PRIVATE_TRANSCRIPT_DATA_CHANNEL'
        async def push(piece):
            for thread, role in (('foreign', 'assistant'), ('probe-thread', 'user')):
                probe.rpc.notifications.put_nowait(ReceivedNotification({
                    'method':'thread/realtime/transcript/done',
                    'params':{'threadId':thread, 'role':role, 'text':private}}))
            await probe._data_event({'type':'input_transcript.added', 'text':private})
            await probe._data_event({'type':'output_transcript.added', 'text':private})
            await probe._data_event({'type':'turn.done', 'turn':{'role':'unknown-private-role', 'text':private}})
            probe.rpc.notifications.put_nowait(ReceivedNotification({
                'method':'thread/realtime/transcript/done', 'params':{'threadId':'probe-thread',
                    'role':'assistant', 'text':self.candidate(probe)}}))
            await probe._finished.wait()
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertEqual(result['state'], 'draft_ready')
        voice = result['voice_input']
        self.assertEqual(voice['received_after_dispatch'],
            {'rpc_user':1, 'rpc_assistant':1, 'data_user':1, 'data_assistant':1})
        self.assertGreaterEqual(voice['first_user_ms'], voice['first_dispatch_ms'])
        self.assertGreaterEqual(voice['first_assistant_ms'], voice['first_dispatch_ms'])
        self.assertNotIn(private, json.dumps(result))
        self.assertNotIn('unknown-private-role', json.dumps(result))
        self.assert_cleaned(probe, result)

    async def test_queued_pre_dispatch_ingress_is_not_relabelled_by_late_receiver_processing(self):
        for role in ('assistant', 'user'):
            with self.subTest(role=role):
                probe, _, _ = self.make_probe(voice_pcm=b'\1\0'*20, mode='silent')
                original_request = probe.rpc.request.side_effect
                original_get = probe.rpc.notifications.get
                release = asyncio.Event()
                old_message = None
                async def get():
                    message = await original_get()
                    if message is old_message:
                        await release.wait()
                    return message
                probe.rpc.notifications.get = get
                async def request(method, params):
                    nonlocal old_message
                    response = await original_request(method, params)
                    if method == 'thread/realtime/start':
                        old_message = ReceivedNotification({'method':'thread/realtime/transcript/done',
                            'params':{'threadId':'probe-thread', 'role':role,
                                'text':self.candidate(probe) if role == 'assistant'
                                    else 'SYNTHETIC_PRIVATE_QUEUED_PRE_VOICE_USER'}})
                        probe.rpc.notifications.put_nowait(old_message)
                    return response
                async def push(piece):
                    self.assertLessEqual(old_message.received_monotonic, probe._voice_issued_at)
                    release.set()
                    if role == 'user':
                        probe.rpc.notifications.put_nowait(ReceivedNotification({
                            'method':'thread/realtime/transcript/done', 'params':{'threadId':'probe-thread',
                                'role':'assistant', 'text':self.candidate(probe)}}))
                    await probe._finished.wait()
                probe.rpc.request.side_effect = request
                probe.peer.push.side_effect = push
                result = await probe.run()
                voice = result['voice_input']
                self.assertEqual(voice['received_after_dispatch']['rpc_user'], 0)
                self.assertIsNone(voice['first_user_ms'])
                if role == 'assistant':
                    self.assertEqual(result['failure_code'], 'live_summary_unexpected_pre_voice_output')
                    self.assertIsNone(result['summary'])
                    self.assertEqual(voice['received_after_dispatch']['rpc_assistant'], 0)
                    self.assertIsNone(voice['first_assistant_ms'])
                else:
                    self.assertEqual(result['state'], 'draft_ready')
                    self.assertEqual(voice['received_after_dispatch']['rpc_assistant'], 1)
                    self.assertGreaterEqual(voice['first_assistant_ms'], voice['first_dispatch_ms'])
                self.assertNotIn('SYNTHETIC_PRIVATE_QUEUED_PRE_VOICE_USER', json.dumps(result))
                self.assert_cleaned(probe, result)

    async def test_malformed_rtp_snapshots_are_unavailable_without_private_payloads(self):
        private = 'SYNTHETIC_PRIVATE_STATS_ADDRESS_IDENTIFIER'
        cases = (None, {}, {'reports':[]}, {'reports':[private]},
            {'reports':[{'bytesSent':True, 'packetsSent':1}]},
            {'reports':[{'bytesSent':-1, 'packetsSent':1}]},
            {'reports':[{'bytesSent':2**53, 'packetsSent':1}]},
            {'reports':[{'bytesSent':float('nan'), 'packetsSent':1}]},
            {'reports':[{'bytesSent':'123', 'packetsSent':1}]},
            {'reports':[{'bytesSent':1, 'packetsSent':1}]*9},
            {'reports':[{'bytesSent':1, 'packetsSent':1}, {'private':private}]})
        for value in cases:
            with self.subTest(shape=type(value).__name__):
                probe, _, notify = self.make_probe(voice_pcm=b'\1\0'*20, mode='silent')
                probe.peer.stats.return_value = value
                async def push(piece):
                    notify('thread/realtime/transcript/done', role='assistant', text=self.candidate(probe))
                    await probe._finished.wait()
                probe.peer.push.side_effect = push
                result = await probe.run()
                self.assertEqual(result['state'], 'draft_ready')
                for snapshot in result['voice_input']['rtp'].values():
                    self.assertEqual(snapshot, {'state':'unavailable', 'reports':0,
                        'bytes_sent':None, 'packets_sent':None})
                self.assertNotIn(private, json.dumps(result))
                self.assert_cleaned(probe, result)

    async def test_optional_stats_exceptions_do_not_fail_draft_or_expose_errors(self):
        probe, _, notify = self.make_probe(voice_pcm=b'\1\0'*20, mode='silent')
        probe.peer.stats.side_effect = RuntimeError('SYNTHETIC_PRIVATE_STATS_ERROR')
        async def push(piece):
            notify('thread/realtime/transcript/done', role='assistant', text=self.candidate(probe))
            await probe._finished.wait()
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertEqual(result['state'], 'draft_ready')
        self.assertIsNone(result['failure_code'])
        self.assertTrue(all(snapshot['state'] == 'unavailable' for snapshot in result['voice_input']['rtp'].values()))
        self.assertNotIn('SYNTHETIC_PRIVATE_STATS_ERROR', json.dumps(result))
        self.assert_cleaned(probe, result)

    async def test_stats_hang_consumes_overall_budget_without_pcm_or_final_resample(self):
        probe, _, _ = self.make_probe(voice_pcm=b'\1\0'*20, mode='silent', timeout=.04)
        entered = asyncio.Event()
        async def stats():
            entered.set()
            await asyncio.Event().wait()
        probe.peer.stats.side_effect = stats
        result = await probe.run()
        self.assertTrue(entered.is_set())
        self.assertEqual(result['failure_code'], 'live_summary_probe_timeout')
        self.assertEqual(result['diagnostics']['timeout_scope'], 'overall_budget')
        probe.peer.push.assert_not_awaited()
        probe.peer.stats.assert_awaited_once()
        self.assertEqual(result['voice_input']['rtp']['final']['state'], 'not_observed')
        self.assert_cleaned(probe, result)

    async def test_final_stats_remain_inside_budget_after_a_complete_candidate(self):
        probe, _, notify = self.make_probe(voice_pcm=b'\1\0'*20, mode='silent', timeout=.04)
        calls = 0
        final_entered = asyncio.Event()
        async def stats():
            nonlocal calls
            calls += 1
            if calls == 3:
                final_entered.set()
                await asyncio.Event().wait()
            return {'reports':[{'bytesSent':10, 'packetsSent':1}]}
        async def push(piece):
            notify('thread/realtime/transcript/done', role='assistant', text=self.candidate(probe))
            await probe._finished.wait()
        probe.peer.stats.side_effect = stats
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertTrue(final_entered.is_set())
        self.assertEqual(calls, 3)
        self.assertTrue(probe.stream.ready)
        self.assertEqual(result['failure_code'], 'live_summary_probe_timeout')
        self.assertIsNone(result['summary'])
        self.assertEqual(result['voice_input']['rtp']['final']['state'], 'not_observed')
        self.assertEqual(result['voice_input']['push_returned_frames'], 20)
        self.assertEqual(result['diagnostics']['timeout_scope'], 'overall_budget')
        self.assert_cleaned(probe, result)

    async def test_bound_data_assistant_during_before_stats_prevents_voice_dispatch(self):
        probe, _, _ = self.make_probe(voice_pcm=b'\1\0'*20, mode='silent')
        async def stats():
            await probe._data_event({'type':'output_transcript.added',
                'text':'SYNTHETIC_PRIVATE_PRE_VOICE_DATA_OUTPUT'})
            return {'reports':[{'bytesSent':0, 'packetsSent':0}]}
        probe.peer.stats.side_effect = stats
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_unexpected_pre_voice_output')
        self.assertIsNone(result['summary'])
        probe.peer.push.assert_not_awaited()
        self.assertEqual(result['voice_input']['dispatch_attempt_frames'], 0)
        self.assertEqual(result['voice_input']['received_after_dispatch'],
            {'rpc_user':0, 'rpc_assistant':0, 'data_user':0, 'data_assistant':0})
        self.assertNotIn('SYNTHETIC_PRIVATE_PRE_VOICE_DATA_OUTPUT', json.dumps(result))
        self.assert_cleaned(probe, result)

    async def test_pending_push_handoff_preserves_first_failure_after_timeout(self):
        probe, _, notify = self.make_probe(voice_pcm=b'\1\0'*6000, mode='silent', timeout=.04)
        observed = asyncio.Event()
        async def push(piece):
            notify('thread/realtime/itemAdded', item={'type':'handoff_request'})
            await probe._finished.wait()
            observed.set()
            await asyncio.Event().wait()
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertTrue(observed.is_set())
        self.assertEqual(result['failure_code'], 'live_summary_background_route_observed')
        self.assertEqual(result['diagnostics']['timeout_scope'], 'overall_budget')
        self.assertEqual(result['voice_input']['push_calls_started'], 1)
        self.assertEqual(result['voice_input']['push_calls_returned'], 0)
        probe.peer.push.assert_awaited_once()
        self.assert_cleaned(probe, result)

    async def test_cancellation_finishes_owned_cleanup_and_releases_pcm(self):
        probe, _, _ = self.make_probe(voice_pcm=b'\1\0'*6000, mode='silent', timeout=.8)
        entered = asyncio.Event()
        async def push(piece):
            entered.set()
            await asyncio.Event().wait()
        probe.peer.push.side_effect = push
        run = asyncio.create_task(probe.run())
        async with asyncio.timeout(1):
            await entered.wait()
            run.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await run
        self.assertIsNone(probe._voice_pcm)
        self.assertEqual(probe.cleanup_status, 'cleaned')
        self.assertIsNone(probe.receiver)
        self.assertTrue(probe.output.closed)
        probe.peer.close.assert_awaited_once()
        probe.rpc.close.assert_awaited_once()
        probe.peer.push.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
