"""Replay recorded ninth greeting text only; no model/media completion claim."""
import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription.b_native import NativeLiveResponder
from teams_transcription.b_question import FramedQuestion
from teams_transcription.b_response import GREETING, MeetingMinutesTool


class MutedProbeGreetingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.live = self.make_live(gate=True)
        self.live.thread_id = 'offline-thread'
        self.live.context.bind_session('offline-thread')
        self.live.state = 'sleeping'
        self.live.started = True
        self.live.peer = AsyncMock()

    def make_live(self, *, gate, meeting='synthetic-qa'):
        sink = AsyncMock()
        sink.observe.return_value = dict(frames=0, packets=0, duration=0., energy=0.,
            source_frames=0, source_packets=0, source_duration=0., source_energy=0., connected=True)
        return NativeLiveResponder(None, '/tmp/greeting-unused', meeting,
            NativeTrackOutput(meeting, sink),
            MeetingMinutesTool(meeting, lambda _: {'meeting_id':meeting, 'records':[]}),
            muted_probe_text_gate=gate, question_timeout=1)

    async def asyncTearDown(self):
        if self.live._phase_timeout: self.live._phase_timeout.cancel()
        if self.live._speech_task:
            self.live._speech_task.cancel()
            await asyncio.gather(self.live._speech_task, return_exceptions=True)
        await self.live.output.close()

    async def emit(self, method, *, thread='offline-thread', **params):
        await self.live.handle_notification(method, {'threadId':thread, **params})

    async def begin(self):
        await self.live._start_application_request(FramedQuestion('greeting', '小白小白', 1))
        await self.live._tool_task
        self.assertEqual(self.live.state, 'greeting')

    async def delta(self, text, *, thread='offline-thread'):
        await self.emit('thread/realtime/transcript/delta', role='assistant', delta=text, thread=thread)

    async def turn(self, kind, ident, text=''):
        await self.live._data_event({'type':kind, 'turn':{
            'id':ident, 'role':'assistant', 'start_ms':0, 'end_ms':100,
            'transcript':text}})

    async def settle(self):
        if self.live._speech_task and self.live._greeting_text_ready.is_set():
            await asyncio.wait_for(asyncio.shield(self.live._speech_task), .2)
        else:
            await asyncio.sleep(0)

    async def test_ninth_four_deltas_only_close_local_muted_lease(self):
        fixture = json.loads((Path(__file__).parent/'fixtures/qa_ninth_greeting.json').read_text())
        self.assertFalse(fixture['assistant_done_observed'])
        await self.begin()
        for row in fixture['assistant_deltas'][:-1]:
            await self.delta(row['text']); await asyncio.sleep(0)
            self.assertEqual(self.live.state, 'greeting')
        await self.delta(fixture['assistant_deltas'][-1]['text']); await self.settle()
        self.assertEqual(self.live.state, 'listening')
        self.assertFalse(self.live._speech_done.is_set())
        self.assertFalse(self.live.output.active)
        self.assertFalse(self.live.output.enabled)
        self.assertEqual(self.live.output.activity_frames, 0)
        self.assertIsNone(self.live.drain_result)
        self.assertEqual(self.live.greeting_completion, {
            'gate':'synthetic_muted_text_only', 'text_complete':True,
            'provider_done':False, 'audio_completion':'unverified', 'local_lease_closed':True})

    async def test_partial_and_canonical_done_alone_do_not_complete_delta_gate(self):
        await self.begin(); await self.delta('我在')
        await self.emit('thread/realtime/transcript/done', role='assistant', text=GREETING)
        await asyncio.sleep(0)
        self.assertTrue(self.live.greeting_completion['provider_done'])
        self.assertFalse(self.live.greeting_completion['text_complete'])
        self.assertEqual(self.live.state, 'greeting')

    async def test_mismatched_or_extra_delta_fails_exact_gate(self):
        await self.begin(); await self.delta('我在，有什麼問題？多餘')
        await self.settle()
        self.assertEqual(self.live.state, 'failed')
        self.assertFalse(self.live.greeting_completion['text_complete'])
        self.assertFalse(self.live.greeting_completion['local_lease_closed'])

    async def test_foreign_delta_and_cancel_cannot_complete_or_revoke_own_gate(self):
        await self.begin()
        await self.delta(GREETING, thread='foreign-thread')
        await self.emit('thread/realtime/itemAdded', thread='foreign-thread', item={'type':'response.cancelled'})
        self.assertEqual(self.live.state, 'greeting')
        self.assertFalse(self.live._greeting_text_ready.is_set())
        await self.delta(GREETING); await self.settle()
        self.assertEqual(self.live.state, 'listening')

    async def test_cancelled_complete_delta_cannot_publish_listening(self):
        await self.begin(); await self.delta(GREETING)
        await self.emit('thread/realtime/itemAdded', item={'type':'response.cancelled'})
        await asyncio.sleep(0)
        self.assertEqual(self.live.state, 'sleeping')
        self.assertFalse(self.live.greeting_completion['local_lease_closed'])
        await self.emit('thread/realtime/transcript/done', role='assistant', text=GREETING)
        self.assertEqual(self.live.state, 'sleeping')

    async def test_late_greeting_final_cannot_finish_next_answer(self):
        await self.begin(); await self.delta(GREETING); await self.settle()
        await self.live._begin_native_speech('answering')
        await self.turn('turn.created', 'answer-after-muted-greeting')
        await self.emit('thread/realtime/transcript/done', role='assistant', text=GREETING)
        self.assertFalse(self.live._speech_done.is_set())
        self.assertEqual(self.live.state, 'answering')
        # Unidentified canonical finals cannot complete a media turn.
        await self.emit('thread/realtime/transcript/done', role='assistant', text='預算十萬元。')
        self.assertFalse(self.live._speech_done.is_set())
        await self.turn('turn.done', 'answer-after-muted-greeting', '預算十萬元。')
        self.assertTrue(self.live._speech_done.is_set())
        await asyncio.sleep(0)
        self.assertEqual(self.live.state, 'answering')  # No media injected.

    async def test_default_gate_off_needs_provider_and_media_completion(self):
        self.live.muted_probe_text_gate = False
        await self.begin(); await self.delta(GREETING); await asyncio.sleep(0)
        await self.turn('turn.created', 'ordinary-greeting')
        self.assertEqual(self.live.state, 'greeting')
        self.assertFalse(self.live._speech_done.is_set())
        await self.emit('thread/realtime/transcript/done', role='assistant', text=GREETING)
        self.assertFalse(self.live._speech_done.is_set())
        await self.turn('turn.done', 'ordinary-greeting', GREETING)
        await asyncio.sleep(0)
        self.assertEqual(self.live.state, 'greeting')  # Still no nonzero media.

    async def test_probe_gate_refuses_tx_and_non_synthetic_scope(self):
        with self.assertRaisesRegex(ValueError, 'synthetic QA'):
            self.make_live(gate=True, meeting='ordinary-meeting')
        with self.assertRaisesRegex(RuntimeError, 'forbids TX'):
            self.live.enable_output()
        with self.assertRaisesRegex(ValueError, 'forbids TX'):
            await self.live.start(enable_output=True)
        await self.begin()
        self.live.output.enabled = True  # Simulate bypass; gate must reject.
        await self.delta(GREETING); await self.settle()
        self.assertEqual(self.live.state, 'failed')


    async def test_error_after_exact_delta_before_transition_cannot_publish_listening(self):
        await self.begin(); await self.delta(GREETING)
        await self.live._audio_error('injected local failure')
        await self.settle()
        self.assertEqual(self.live.state, 'failed')
        self.assertFalse(self.live.greeting_completion['local_lease_closed'])

    async def test_trailing_text_after_local_gate_fails_without_provider_done_claim(self):
        await self.begin(); await self.delta(GREETING); await self.settle()
        await self.delta('多餘的話')
        self.assertEqual(self.live.state, 'failed')
        self.assertFalse(self.live.greeting_completion['text_complete'])
        self.assertFalse(self.live.greeting_completion['provider_done'])
