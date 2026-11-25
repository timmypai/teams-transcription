"""B-channel auto-injection, Phase 1/2 screen context, interruption & isolation tests."""
import asyncio
import hashlib
import json
import tempfile
import unittest

from teams_transcription.b_context import MeetingQuestionContext
from teams_transcription.b_native import NativeLiveResponder
from teams_transcription.b_response import MeetingMinutesTool, MeetingStoreMinutesReader
from teams_transcription.diagnostics import screen_context_diagnostic
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.screen_capture import CapturedFrame, ScreenFrameCache
from teams_transcription.screen_watcher import ScreenContextController


def _frame(tag, dhash):
    raw = f'IMG-{tag}'.encode()
    return CapturedFrame(
        image_bytes=raw, mime='image/jpeg', width=1280, height=720,
        dhash=dhash, frame_sha256=hashlib.sha256(raw).hexdigest(),
        capture_method='webrtc_video_frame', captured_at='2026-11-24T13:50:00+00:00',
    )


class _StubCapturer:
    def __init__(self, frames):
        self.frames = list(frames)

    async def capture(self):
        if not self.frames:
            return None
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


class _StubAnalyzer:
    def __init__(self, *, delay=0.0):
        self.delay = delay
        self.calls = 0
        self.question_calls = 0

    async def analyze_frame(self, image_path, *, detail='high'):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return {
            'screen_kind': 'slide',
            'title': f'第 {self.calls} 頁架構圖',
            'summary_text': f'第 {self.calls} 頁顯示預算為 {self.calls * 50000} 元。',
            'key_items': [f'模組 {self.calls}'],
            'metrics_or_numbers': [f'{self.calls * 50000} 元'],
            'visible_text_excerpt': f'Budget {self.calls * 50000}',
            'uncertain_areas': [],
            'vision_model': 'gpt-5.6-sol',
            'analyzed_at': '2026-11-24T13:50:05+00:00',
        }

    async def analyze_question(self, image_path, question, *, detail='high', prior_summary=None):
        self.question_calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return {
            'answer_relevant_findings': f'畫面回應：{question}',
            'visible_evidence': ['Budget 50000'],
            'uncertain_areas': [],
            'vision_model': 'gpt-5.6-sol',
            'analyzed_at': '2026-11-24T13:50:06+00:00',
        }

    async def close(self):
        return True


class _LocalNativeOutput:
    def __init__(self, meeting_id, *, clock=lambda: 10.0):
        self.meeting_id = meeting_id
        self.clock = clock
        self.enabled = False
        self.active = False
        self.closed = False
        self.connected = True
        self.frames = 0
        self.activity_frames = 0
        self.last_active = 0.0
        self.received_duration = 0.0
        self.received_energy = 0.0
        self.quiet_received_seconds = 0.0
        self.last_delivery = None
        self.sink = None

    def start_output(self):
        self.enabled = True

    async def stop_output(self):
        self.enabled = False
        self.active = False

    async def begin_speech(self, meeting_id):
        if meeting_id != self.meeting_id:
            raise ValueError('wrong_meeting')
        self.active = True

    async def end_speech(self, *, quiet=False):
        self.active = False
        return {'status': 'quiet' if quiet else 'muted', 'truncated': False}

    async def observe(self):
        return True

    async def close(self):
        self.closed = True
        self.active = False
        self.enabled = False


class _LocalPeer:
    def __init__(self):
        self.events = []

    async def send_event(self, event, *, capture_output=False):
        self.events.append(event)

    async def close(self):
        pass


class _LocalRPC:
    def __init__(self):
        self.process = None
        self.notifications = asyncio.Queue()

    async def close(self):
        pass


class ScreenBIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_b_context_auto_injects_sqlite_screen_observations_separately_from_speech(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('m-ctx')
            store.append_final('m-ctx', {
                'event_id': 'live:1', 'revision': 0, 'text': '大家看一下這張投影片。',
                'timestamp': '2026-11-24T13:50:00+00:00', 'sequence': 1,
            })
            f1 = _frame('p1', '0000000000000000')
            f2 = _frame('p2', 'ffffffffffffffff')
            for seq, fr, title in ((1, f1, '舊頁：第一季'), (2, f2, '現頁：第四季')):
                store.record_screen_observation('m-ctx', {
                    'observation_id': f'screen:{seq:04d}:{fr.frame_sha256[:8]}',
                    'capture_seq': seq, 'frame_sha256': fr.frame_sha256, 'dhash': fr.dhash,
                    'capture_method': 'webrtc_video_frame', 'width': 1280, 'height': 720,
                    'captured_at': '2026-11-24T13:50:00+00:00',
                    'analyzed_at': '2026-11-24T13:50:05+00:00',
                    'vision_model': 'gpt-5.6-sol', 'screen_kind': 'slide',
                    'title': title, 'summary_text': f'{title} 預算表',
                    'structured': {'screen_kind': 'slide', 'title': title,
                        'summary_text': f'{title} 預算表', 'key_items': [],
                        'metrics_or_numbers': [], 'visible_text_excerpt': title, 'uncertain_areas': []},
                })

            reader = MeetingStoreMinutesReader(store)
            ctx = MeetingQuestionContext(MeetingMinutesTool('m-ctx', reader))
            ctx.bind_session('thread-1')
            plan = await ctx.prepare(
                '剛剛上一頁跟這一頁差在哪？',
                session_id='thread-1',
                request_id='question-1',
                screen_live={'mode': 'watch_and_cache', 'available': True, 'share_active_now': True,
                             'current_capture_seq': 2, 'freshness_seconds': 0.5, 'cache_hit': True,
                             'wake_slide_changed': False, 'question_focused_vision': None},
            )
            wire = json.loads(plan.payload)
            # Speech source stays clean (only 1 speech record, no screen mixed in)
            self.assertEqual(len(wire['selected_source_reading']), 1)
            self.assertEqual(wire['selected_source_reading'][0]['text'], '大家看一下這張投影片。')
            # Screen context auto-injected with is_current flags for multi-slide comparison
            sc = wire['screen_context']
            self.assertTrue(sc['available'])
            self.assertEqual(sc['provenance'], 'derived_screen_share_observation')
            self.assertEqual(len(sc['observations']), 2)
            self.assertFalse(sc['observations'][0]['is_current'])
            self.assertTrue(sc['observations'][1]['is_current'])
            self.assertEqual(sc['observations'][0]['title'], '舊頁：第一季')
            self.assertEqual(sc['observations'][1]['title'], '現頁：第四季')

    async def test_native_responder_phase1_and_interruption_during_vision_never_opens_tx(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('m-b-live')
            slide = _frame('live-1', '0f0f0f0f0f0f0f0f')
            cache = ScreenFrameCache(max_frames=4)
            ctrl = ScreenContextController(
                'm-b-live', mode='on_demand',
                capturer=_StubCapturer([slide]),
                analyzer=_StubAnalyzer(),
                persist_observation=lambda obs: store.record_screen_observation('m-b-live', obs),
                frame_cache=cache,
            )
            await ctrl.start()
            now = [10.0]
            output = _LocalNativeOutput('m-b-live', clock=lambda: now[0])
            reader = MeetingStoreMinutesReader(store)
            responder = NativeLiveResponder(
                None, root, 'm-b-live', output, MeetingMinutesTool('m-b-live', reader),
                clock=lambda: now[0], screen_controller=ctrl,
            )
            responder.rpc = _LocalRPC()
            responder.peer = _LocalPeer()
            responder.started = True
            responder.thread_id = 'thread-b-1'
            responder.context.bind_session('thread-b-1')
            responder.state = 'sleeping'
            responder.output.start_output()

            try:
                responder._request_id = 'question-1'
                responder._request_sequence = 1
                responder.state = 'thinking'
                responder._answer_scope = {'request_id': 'question-1', 'thread_id': 'thread-b-1', 'delivered': False}
                await responder._return_tool('question-1', '這張畫面顯示多少預算？')

                # Complete payload chunks + RESULT_COMPLETE delivered, including auto-injected screen_context
                chunks = [ev['content'][0]['text'] for ev in responder.peer.events
                          if ev.get('type') == 'session.context.append']
                self.assertEqual(chunks[-1], 'RESULT_COMPLETE')
                payload = json.loads(''.join(chunks[:-1]))
                self.assertIn('screen_context', payload)
                self.assertEqual(len(payload['screen_context']['observations']), 1)
                self.assertIn('50000 元', payload['screen_context']['observations'][0]['summary_text'])
                self.assertIsNotNone(payload['screen_context']['question_focused_vision'])

                # Verify diagnostics projection strips sensitive content
                diag = screen_context_diagnostic(responder.status()['screen_context'])
                self.assertEqual(diag['mode'], 'on_demand')
                self.assertEqual(diag['observations_saved'], 1)
                self.assertNotIn('50000', str(diag))
            finally:
                await responder.stop()
                await ctrl.close()

        # Interruption while slow vision is awaiting must NOT open TX or send RESULT_COMPLETE
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('m-b-int')
            ctrl = ScreenContextController(
                'm-b-int', mode='on_demand',
                capturer=_StubCapturer([_frame('slow', 'aaaaaaaaaaaaaaaa')]),
                analyzer=_StubAnalyzer(delay=2.0),
                persist_observation=lambda obs: store.record_screen_observation('m-b-int', obs),
            )
            await ctrl.start()
            output = _LocalNativeOutput('m-b-int')
            responder = NativeLiveResponder(
                None, root, 'm-b-int', output, MeetingMinutesTool('m-b-int', MeetingStoreMinutesReader(store)),
                screen_controller=ctrl,
            )
            responder.rpc = _LocalRPC()
            responder.peer = _LocalPeer()
            responder.started = True
            responder.thread_id = 'thread-b-2'
            responder.context.bind_session('thread-b-2')
            responder.state = 'thinking'
            responder._request_id = 'question-1'
            responder._answer_scope = {'request_id': 'question-1', 'thread_id': 'thread-b-2', 'delivered': False}
            responder.output.start_output()
            try:
                task = asyncio.create_task(responder._return_tool('question-1', '幫我看畫面'))
                await asyncio.sleep(0.05)
                responder._request_id = None
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertFalse(output.active)
                self.assertEqual(responder.peer.events, [])
            finally:
                await responder.stop()
                await ctrl.close()

    async def test_native_responder_phase2_zero_latency_cached_slide_delivery(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('m-b-p2')
            slide1 = _frame('p2-s1', '0000000000000000')
            slide2 = _frame('p2-s2', 'ffffffffffffffff')
            analyzer = _StubAnalyzer()
            now = [100.0]

            def clock():
                now[0] += 10.0
                return now[0]

            ctrl = ScreenContextController(
                'm-b-p2', mode='watch_and_cache',
                capturer=_StubCapturer([slide1, slide1, slide2, slide2, slide2]),
                analyzer=analyzer,
                persist_observation=lambda obs: store.record_screen_observation('m-b-p2', obs),
                poll_seconds=0.01, min_analyze_interval_seconds=2.0, clock=clock,
            )
            await ctrl.start()
            output = _LocalNativeOutput('m-b-p2')
            responder = NativeLiveResponder(
                None, root, 'm-b-p2', output, MeetingMinutesTool('m-b-p2', MeetingStoreMinutesReader(store)),
                screen_controller=ctrl,
            )
            responder.rpc = _LocalRPC()
            responder.peer = _LocalPeer()
            responder.started = True
            responder.thread_id = 'thread-b-p2'
            responder.context.bind_session('thread-b-p2')
            responder.output.start_output()
            try:
                for _ in range(40):
                    if len(store.list_screen_observations('m-b-p2')) >= 2:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(len(store.list_screen_observations('m-b-p2')), 2)

                calls_before = analyzer.calls + analyzer.question_calls
                responder._request_id = 'question-1'
                responder._request_sequence = 1
                responder.state = 'thinking'
                responder._answer_scope = {'request_id': 'question-1', 'thread_id': 'thread-b-p2', 'delivered': False}
                await responder._return_tool('question-1', '幫我解釋現在分享的內容')
                self.assertEqual(analyzer.calls + analyzer.question_calls, calls_before)

                chunks = [ev['content'][0]['text'] for ev in responder.peer.events
                          if ev.get('type') == 'session.context.append']
                self.assertEqual(chunks[-1], 'RESULT_COMPLETE')
                payload = json.loads(''.join(chunks[:-1]))
                sc = payload['screen_context']
                self.assertEqual(len(sc['observations']), 2)
                self.assertTrue(sc['observations'][1]['is_current'])
                self.assertFalse(sc['observations'][0]['is_current'])
            finally:
                await responder.stop()
                await ctrl.close()


if __name__ == '__main__':
    unittest.main()
