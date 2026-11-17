"""Timeout receipts preserve Native state; synthetic capture uses a virtual clock."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from teams_transcription import qa_probe
from teams_transcription.b_native import NativeLiveResponder


class SpeechCompletionSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_timeout_keeps_pending_media_snapshot_before_cleanup(self):
        await self.pending_media_snapshot(whole_work=False)

    async def test_whole_work_deadline_keeps_pending_media_snapshot_before_cleanup(self):
        await self.pending_media_snapshot(whole_work=True)

    async def pending_media_snapshot(self, *, whole_work):
        instances, captures, stops = [], [], []
        pending = asyncio.Event()
        capture_clock = [100.]
        class OfflineNative(NativeLiveResponder):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, clock=lambda:capture_clock[0], **kwargs)
                instances.append(self)
                self.output.clock = lambda:capture_clock[0]
                self.output.sink = AsyncMock()
                self.output.sink.observe.return_value = {
                    'connected':True, 'frames':0, 'packets':0, 'duration':0., 'energy':0.,
                    'source_frames':0, 'source_packets':0,
                    'source_duration':0., 'source_energy':0.}
                self.rpc = SimpleNamespace(process=None, tasks=[])
                async def push(pcm):
                    if any(pcm):
                        await self.handle_notification('thread/realtime/transcript/delta',
                            {'threadId':self.thread_id,'role':'user','delta':qa_probe.QUESTION})
                self.peer = SimpleNamespace(push=AsyncMock(side_effect=push),
                    send_event=AsyncMock(), set_output_capture=AsyncMock(),
                    stats=AsyncMock(return_value={'connection':'connected', 'data_channel':'open'}))
            async def start(self, *, enable_output=False):
                self.thread_id = 'offline-owned'
                self.live_start_params('offline-offer')
                self.context.bind_session(self.thread_id)
                self.started = True
                self.state = 'sleeping'
                self._answer = asyncio.get_running_loop().create_future()
                self._rx_task = asyncio.create_task(self._pump_rx())
                self._question_task = asyncio.create_task(self._poll_questions())
            async def _begin_native_speech(self, state):
                await super()._begin_native_speech(state)
                await self._data_event({'type':'turn.created', 'turn':{
                    'id':'offline-answer', 'role':'assistant', 'start_ms':0,
                    'end_ms':100, 'transcript':''}})
                await self._data_event({'type':'turn.done', 'turn':{
                    'id':'offline-answer', 'role':'assistant', 'start_ms':0,
                    'end_ms':100, 'transcript':'Synthetic answer.'}})
                # Preserve a real pending-activity barrier despite provider done;
                # arbitrary adapter details must never leak into its snapshot.
                self.drain_result = {'status':'muted', 'truncated':False,
                    'reason':'private-adapter-detail', 'payload':'private-payload'}
                await asyncio.sleep(0)
            def speech_completion_snapshot(self):
                assert not stops, 'snapshot must occur before any stop'
                snapshot = super().speech_completion_snapshot()
                captures.append(snapshot)
                return snapshot
            async def stop(self):
                stops.append(True)
                self._closing = True
                tasks = [task for task in (self._rx_task, self._question_task,
                    self._tool_task, self._speech_task, self._phase_timeout) if task is not None]
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if not self._answer.done():
                    self._answer.cancel()
                await self.output.end_speech()
                self.state = 'stopped'
                self.stop_ack_received = self.stop_closed_received = True
                self.cleanup_status = 'cleaned'
                return 'stopped'
        async def inline(fn, *args, **kwargs):
            return fn(*args, **kwargs)
        async def capture_pcm(native, pcm):
            # This fixture tests receipt state, not real-time audio pacing.
            # Scheduling delay must not fabricate a gap in synthetic capture.
            for offset in range(0, len(pcm), 960):
                chunk = pcm[offset:offset + 960]
                start = capture_clock[0]
                capture_clock[0] += len(chunk) / 48000
                self.assertTrue(native.push_rx(native.meeting_id, chunk,
                    started_at=start, ended_at=capture_clock[0]))
                await asyncio.sleep(0)
        async def timed_out(native, condition, timeout=45):
            self.assertEqual(timeout, 45)
            quiet_frames = round(native.question_framer.silence_seconds * 24000)
            await capture_pcm(native, b'\0' * (quiet_frames * 2))
            # Keep the existing state deadline. Unlike actual_wait, this wait
            # does not inject wall-receipt-timestamped PCM into the fixture.
            async with asyncio.timeout(3):
                while not (native._speech_completion_phase == 'waiting_activity'
                        and native._speech_done.is_set()):
                    await asyncio.sleep(.005)
            self.assertEqual(native._speech_completion_phase, 'waiting_activity')
            self.assertTrue(native._speech_done.is_set())
            self.assertFalse(native._speech_task.done())
            if whole_work:
                pending.set()
                await asyncio.Future()  # Whole-work supervisor cancels this task.
            raise qa_probe.PhaseWaitTimeout(timeout)
        actual_wait_tasks = asyncio.wait
        async def supervisor_wait(tasks, *, timeout):
            if whole_work and timeout == qa_probe.WORK_SECONDS:
                # Inject the supervisor timeout only after actual Native state
                # reaches its pending media barrier; no slow wall-clock race.
                await asyncio.wait_for(pending.wait(), 3)
                return set(), set(tasks)
            return await actual_wait_tasks(tasks, timeout=timeout)
        guard = SimpleNamespace(prepare=AsyncMock(), adapter=Mock(return_value=object()))
        browser = AsyncMock()
        browser.browser = object()
        browser.playwright = browser._starter = None
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = SimpleNamespace(authorize_live=True, private_result=root/'private.json',
                peer_media_profile='legacy', qa_start_stage='question', config='unused.yml',
                chromium=None, ice_servers_file=None)
            with patch('asyncio.to_thread', side_effect=inline), \
                 patch.object(qa_probe, 'synthetic_pcm', return_value=b'\x00\x10\x00\xf0'*240), \
                 patch.object(qa_probe, 'NativeLiveResponder', OfflineNative), \
                 patch.object(qa_probe, 'load_assistant_settings', return_value=SimpleNamespace(
                     allowed_egress_hosts=('existing.example',), codex_bin='unused')), \
                 patch.object(qa_probe, 'load_ice_servers', return_value=[]), \
                 patch.object(qa_probe, 'RuntimeLiveGuard', return_value=guard), \
                 patch.object(qa_probe, 'Browser', return_value=browser), \
                 patch.object(qa_probe, 'send_pcm', side_effect=capture_pcm), \
                 patch.object(qa_probe, 'wait_until', side_effect=timed_out), \
                 patch.object(qa_probe.asyncio, 'wait', side_effect=supervisor_wait):
                result = await qa_probe.run_probe(args)
            private = json.loads((root/'private.json').read_text())
        self.assertEqual(len(captures), 1 if whole_work else 2)
        self.assertEqual(len(stops), 1)
        receipt = result['speech_completion_before_stop']
        self.assertEqual(receipt, captures[-1])
        self.assertEqual(private['control_receipt']['speech_completion_before_stop'], receipt)
        if whole_work:
            self.assertIsNone(result['question_timeout_snapshot'])
        else:
            self.assertEqual(private['rounds'][0]['question_timeout_snapshot'], captures[0])
            self.assertEqual(result['question_timeout_snapshot'], captures[0])
        self.assertEqual(receipt['phase'], 'waiting_activity')
        self.assertEqual(receipt['native_state'], 'answering')
        self.assertTrue(receipt['done_is_set'])
        self.assertEqual(receipt['turn_lifecycle']['counts']['dc_turn_created'], 1)
        self.assertEqual(receipt['turn_lifecycle']['counts']['dc_turn_done'], 1)
        self.assertEqual(receipt['turn_lifecycle']['current']['open_turn_count'], 0)
        self.assertTrue(receipt['turn_lifecycle']['current']['done_is_set'])
        self.assertFalse(receipt['speech_task_done'])
        self.assertFalse(receipt['speech_task_cancelled'])
        self.assertIsNone(receipt['error_code'])
        self.assertTrue(receipt['output_active'])
        self.assertEqual(receipt['activity_frames'], 0)
        self.assertIsNone(receipt['last_active_age_seconds'])  # No effective audio yet.
        self.assertTrue(receipt['drain_result']['muted'])
        self.assertFalse(receipt['drain_result']['drained'])
        self.assertFalse(receipt['drain_result']['quiet'])
        self.assertNotIn('private-adapter-detail', json.dumps(receipt))
        self.assertNotIn('private-payload', json.dumps(receipt))
        native = instances[0]
        after = NativeLiveResponder.speech_completion_snapshot(native)
        self.assertEqual(after['phase'], 'closed')
        self.assertEqual(after['native_state'], 'stopped')
        self.assertTrue(after['speech_task_done'])
        self.assertTrue(after['speech_task_cancelled'])
        self.assertEqual(after['error_code'], 'speech_cancelled')
        self.assertFalse(after['output_active'])
        self.assertEqual(receipt['phase'], 'waiting_activity')  # Still pre-cleanup.
        self.assertEqual(result['failure']['error_code'],
            'work_deadline_exceeded' if whole_work else 'phase_wait_timeout')
        self.assertEqual(result['failure']['timeout_seconds'], 120 if whole_work else 45)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['stop_status'], 'stopped')
        self.assertEqual(result['work_limit_seconds'], 120)
        self.assertEqual(result['cleanup_limit_seconds'], 10)


if __name__ == '__main__':
    unittest.main()
