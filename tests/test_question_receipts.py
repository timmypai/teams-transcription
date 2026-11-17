"""Bounded host-question metadata and the actual offline QA PCM receipt."""
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from teams_transcription import qa_probe
from teams_transcription.b_native import NativeLiveResponder
from teams_transcription.b_question import FramedQuestion


class QuestionReceiptTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_b_native import NativeLiveTests
        self.case = NativeLiveTests('runTest')
        await self.case.asyncSetUp()
        self.native = self.case.live
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_local_question_receipt_has_order_without_content_or_provider_ids(self):
        await self.case.utterance('小白小白，private-question？')
        await self.case.until(lambda:self.native.state == 'answering')
        receipt = self.native.question_receipts()
        self.assertEqual(receipt['counts'], {'received':1, 'admitted':1, 'rejected':0})
        self.assertEqual(len(receipt['events']), 1)
        event = receipt['events'][0]
        self.assertEqual(event['outcome'], 'admitted')
        self.assertIsNone(event['reason'])
        self.assertEqual(set(event),
            {'outcome', 'reason', 'received_seconds', 'decision_seconds'})
        self.assertGreaterEqual(event['received_seconds'], 0)
        self.assertGreaterEqual(event['decision_seconds'], event['received_seconds'])
        self.assertEqual(self.native.status()['question_receipts'], receipt)
        serialized = json.dumps(receipt)
        for private in ('private-question', 'native-thread', self.native._request_id):
            self.assertNotIn(str(private), serialized)
        self.assertEqual(self.case.reads, ['one'])

    async def test_provider_observations_never_increment_local_request_counts(self):
        await self.case.turn('turn.created', 'private-provider-turn', role='user')
        await self.native.handle_notification('thread/realtime/transcript/done', {
            'threadId':'native-thread', 'role':'user', 'text':'小白小白，private-question'})
        await self.native.handle_notification('thread/realtime/itemAdded', {
            'threadId':'native-thread', 'item':{'type':'handoff_request',
                'handoff_id':'private-provider-id', 'item_id':'private-provider-id',
                'input_transcript':'小白小白，private-question'}})
        receipt = self.native.question_receipts()
        self.assertEqual(receipt['counts'], {'received':0, 'admitted':0, 'rejected':0})
        self.assertEqual(receipt['events'], [])
        self.assertEqual(self.case.reads, [])

    async def test_same_host_request_is_rejected_without_duplicate_read(self):
        request = FramedQuestion('question', 'private-question', 1)
        await self.native._start_application_request(request)
        await self.native._tool_task
        await self.native._start_application_request(request)
        receipt = self.native.question_receipts()
        self.assertEqual(receipt['counts'], {'received':2, 'admitted':1, 'rejected':1})
        self.assertEqual(receipt['events'][-1]['reason'], 'duplicate_request')
        self.assertEqual(self.case.reads, ['one'])
        self.assertNotIn('private-question', json.dumps(receipt))

    async def test_receipts_are_bounded_saturating_and_snapshots_are_detached(self):
        for sequence in range(1, 71):
            await self.native._start_application_request(
                FramedQuestion('question', '', sequence))
        receipt = self.native.question_receipts()
        self.assertEqual(len(receipt['events']), 64)
        self.assertEqual(receipt['counts'], {'received':70, 'admitted':0, 'rejected':70})
        self.assertTrue(all(event['reason'] == 'invalid_question' for event in receipt['events']))
        receipt['counts']['received'] = -1
        receipt['events'][0]['reason'] = 'mutated'
        self.assertEqual(self.native.question_receipts()['counts']['received'], 70)
        self.assertEqual(self.native.question_receipts()['events'][0]['reason'], 'invalid_question')
        self.assertEqual(self.case.reads, [])
        self.native._request_counts.update(received=2147483647, rejected=2147483647)
        await self.native._start_application_request(FramedQuestion('question', '', 71))
        self.assertEqual(self.native.question_receipts()['counts'],
            {'received':2147483647, 'admitted':0, 'rejected':2147483647})

    async def test_probe_receipt_matches_actual_pcm_push_and_survives_question_timeout(self):
        # Real admitted clip and quiet packets, local framing and SQLite query;
        # no provider answer arrives, so the private request receipt must survive.
        pcm = bytes(range(256)) * 8 + b'\x03\x00' * 6
        chunks = []
        instances = []
        class OfflineNative(NativeLiveResponder):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                instances.append(self)
                self.rpc = SimpleNamespace(process=None, tasks=[])
                async def push(chunk):
                    chunks.append(chunk)
                    if len(chunks) == 1:
                        await self.handle_notification('thread/realtime/transcript/delta',
                            {'threadId':self.thread_id,'role':'user','delta':qa_probe.QUESTION})
                self.peer = SimpleNamespace(push=AsyncMock(side_effect=push),
                    send_event=AsyncMock(),
                    page=SimpleNamespace(
                        evaluate=AsyncMock(return_value=dict(connected=True,
                            packets=0, frames=0, energy=0., duration=0.,
                            source_packets=0, source_frames=0,
                            source_energy=0., source_duration=0.)),
                        is_closed=Mock(return_value=False)),
                    stats=AsyncMock(return_value={'connection': 'connected', 'data_channel': 'open'}))
            async def start(self, *, enable_output=False):
                # Attach the native scalar endpoint even though this provider
                # deliberately sends no answer audio or completion event.
                await self.output.attach_live(self.peer.page)
                self.thread_id = 'offline-owned'
                self.live_start_params('offline-offer')
                self.context.bind_session(self.thread_id)
                self.started = True
                self.state = 'sleeping'
                self._answer = asyncio.get_running_loop().create_future()
                self._rx_task = asyncio.create_task(self._pump_rx())
                self._question_task = asyncio.create_task(self._poll_questions())
            async def stop(self):
                self._closing = True
                tasks = [task for task in (self._rx_task, self._question_task,
                    self._tool_task, self._speech_task, self._phase_timeout) if task is not None]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if not self._answer.done():
                    self._answer.cancel()
                await self.output.close()
                self.state = 'stopped'
                self.stop_ack_received = self.stop_closed_received = True
                self.cleanup_status = 'cleaned'
                return 'stopped'
        async def inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        actual_wait = qa_probe.wait_until
        async def bounded_wait(native, condition, timeout=45):
            return await actual_wait(native, condition, timeout=2)
        root = Path(self.temp.name)
        args = SimpleNamespace(authorize_live=True, private_result=root/'private.json',
            peer_media_profile='legacy', qa_start_stage='question', config='unused.yml',
            chromium=None, ice_servers_file=None)
        guard = SimpleNamespace(prepare=AsyncMock(), adapter=Mock(return_value=object()))
        browser = AsyncMock()
        browser.browser = object()
        browser.playwright = browser._starter = None
        with patch('asyncio.to_thread', side_effect=inline), \
             patch.object(qa_probe, 'synthetic_pcm', return_value=pcm) as synthesis, \
             patch.object(qa_probe, 'NativeLiveResponder', OfflineNative), \
             patch.object(qa_probe, 'load_assistant_settings', return_value=SimpleNamespace(
                 allowed_egress_hosts=('existing.example',), codex_bin='unused')), \
             patch.object(qa_probe, 'load_ice_servers', return_value=[]), \
             patch.object(qa_probe, 'RuntimeLiveGuard', return_value=guard), \
             patch.object(qa_probe, 'Browser', return_value=browser), \
             patch.object(qa_probe, 'wait_until', side_effect=bounded_wait):
            result = await qa_probe.run_probe(args)
        synthesis.assert_called_once_with(qa_probe.QUESTION)
        self.assertEqual(chunks[:2], [pcm[:2048], pcm[2048:]])
        self.assertGreater(len(chunks), 2)
        self.assertTrue(all(chunk == bytes(960) for chunk in chunks[2:]))
        self.assertEqual(b''.join(chunks[:2]), pcm)
        private = json.loads((root/'private.json').read_text())
        round_receipt = private['rounds'][0]
        self.assertEqual(round_receipt['question_pcm'], {
            'sha256': hashlib.sha256(b''.join(chunks[:2])).hexdigest(), 'bytes': len(pcm),
            'frames': len(pcm)//2, 'sample_rate': 24000, 'channels': 1, 'format': 's16le'})
        timing = round_receipt['timing']
        self.assertGreaterEqual(timing['question_send_begin_seconds'], 0)
        self.assertGreaterEqual(timing['question_send_complete_seconds'], timing['question_send_begin_seconds'])
        self.assertEqual(result['failure']['error_code'], 'phase_wait_timeout')
        self.assertEqual(result['failure']['timeout_seconds'], 2)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['question_receipts']['counts'], {'received': 1, 'admitted': 1, 'rejected': 0})
        self.assertIsNone(result['question_receipts']['events'][0]['reason'])
        self.assertEqual(instances[0].tool_calls, 1)
        self.assertEqual(private['control_receipt']['question_receipts'], result['question_receipts'])
        self.assertEqual(instances[0].question_receipts(), result['question_receipts'])
        self.assertEqual(result['stop_status'], 'stopped')
        self.assertFalse(instances[0].output.enabled)
        self.assertFalse(instances[0].output.active)
        self.assertEqual(result['wake_validation'], 'skipped_unverified')


if __name__ == '__main__':
    unittest.main()
