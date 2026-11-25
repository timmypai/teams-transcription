"""Phase 1 (on_demand) and Phase 2 (watch_and_cache) ScreenContextController seam tests."""
import asyncio
import hashlib
import unittest
from pathlib import Path

from teams_transcription.screen_capture import CapturedFrame, ScreenFrameCache
from teams_transcription.screen_vision import ScreenVisionError
from teams_transcription.screen_watcher import ScreenContextController


def _frame(tag, dhash, *, width=1280, height=720, method='webrtc_video_frame'):
    raw = f'JPEG-{tag}'.encode()
    return CapturedFrame(
        image_bytes=raw, mime='image/jpeg', width=width, height=height,
        dhash=dhash, frame_sha256=hashlib.sha256(raw).hexdigest(),
        capture_method=method, captured_at='2026-11-24T13:50:00+00:00',
    )


class FakeCapturer:
    def __init__(self, frames):
        self.frames = list(frames)
        self.calls = 0

    async def capture(self):
        self.calls += 1
        if not self.frames:
            return None
        return self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]


class FakeAnalyzer:
    def __init__(self, *, fail=False, uncertain=None, delay=0.0):
        self.fail = fail
        self.uncertain = list(uncertain or [])
        self.delay = delay
        self.frame_calls = []
        self.question_calls = []
        self.closed = False

    async def analyze_frame(self, image_path, *, detail='high'):
        self.frame_calls.append(image_path)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ScreenVisionError('vision_turn_failed')
        idx = len(self.frame_calls)
        return {
            'screen_kind': 'slide',
            'title': f'Slide {idx}',
            'summary_text': f'第 {idx} 頁簡報摘要內容。',
            'key_items': [f'重點 {idx}'],
            'metrics_or_numbers': [f'{idx * 100} USD'],
            'visible_text_excerpt': f'Slide {idx} text',
            'uncertain_areas': list(self.uncertain),
            'vision_model': 'gpt-5.6-sol',
            'analyzed_at': '2026-11-24T13:50:05+00:00',
        }

    async def analyze_question(self, image_path, question, *, detail='high', prior_summary=None):
        self.question_calls.append((image_path, question))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise ScreenVisionError('vision_turn_failed')
        return {
            'answer_relevant_findings': f'針對「{question}」的細節觀察。',
            'visible_evidence': ['Row 2: 85,500 USD'],
            'uncertain_areas': [],
            'vision_model': 'gpt-5.6-sol',
            'analyzed_at': '2026-11-24T13:50:06+00:00',
        }

    async def close(self):
        self.closed = True
        return True


class ScreenWatcherTests(unittest.IsolatedAsyncioTestCase):
    async def test_phase1_on_demand_never_polls_background_and_persists_on_question(self):
        slide1 = _frame('s1', '0000000000000000')
        capturer = FakeCapturer([slide1])
        analyzer = FakeAnalyzer()
        saved = []
        cache = ScreenFrameCache(max_frames=4)
        ctrl = ScreenContextController(
            'm-p1', mode='on_demand', capturer=capturer, analyzer=analyzer,
            persist_observation=saved.append, frame_cache=cache,
            poll_seconds=0.02, min_analyze_interval_seconds=0.0,
        )
        await ctrl.start()
        try:
            await asyncio.sleep(0.08)
            self.assertEqual(capturer.calls, 0)
            self.assertEqual(len(saved), 0)

            res = await ctrl.resolve_for_question('這張簡報重點是什麼？')
            self.assertTrue(res['available'])
            self.assertTrue(res['share_active_now'])
            self.assertEqual(res['mode'], 'on_demand')
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]['capture_seq'], 1)
            self.assertEqual(len(analyzer.frame_calls), 1)
            self.assertEqual(len(analyzer.question_calls), 1)
            self.assertIn('細節觀察', res['question_focused_vision']['answer_relevant_findings'])

            # Diagnostics never leak OCR text or titles
            st = ctrl.status()
            self.assertEqual(st['mode'], 'on_demand')
            self.assertEqual(st['observations_saved'], 1)
            self.assertNotIn('Slide 1', str(st))
            self.assertNotIn('細節觀察', str(st))
        finally:
            await ctrl.close()
        self.assertTrue(analyzer.closed)
        self.assertFalse(Path(cache.directory).exists())

    async def test_phase2_watch_and_cache_preanalyzes_stable_slide_change_for_zero_latency_hit(self):
        # Poll 1: slide1 (candidate), Poll 2: slide1 (stable -> analyze!),
        # Poll 3: slide2 (candidate), Poll 4: slide2 (stable -> analyze!)
        slide1 = _frame('s1', '0000000000000000')
        slide2 = _frame('s2', 'ffffffffffffffff')
        capturer = FakeCapturer([slide1, slide1, slide2, slide2, slide2])
        analyzer = FakeAnalyzer()
        saved = []
        now = [100.0]

        def clock():
            now[0] += 10.0
            return now[0]

        cache = ScreenFrameCache(max_frames=4)
        ctrl = ScreenContextController(
            'm-p2', mode='watch_and_cache', capturer=capturer, analyzer=analyzer,
            persist_observation=saved.append, frame_cache=cache,
            poll_seconds=0.01, min_analyze_interval_seconds=2.0, clock=clock,
        )
        await ctrl.start()
        try:
            for _ in range(40):
                if len(saved) >= 2:
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(len(saved), 2)
            self.assertEqual([s['capture_seq'] for s in saved], [1, 2])

            before_calls = len(analyzer.frame_calls) + len(analyzer.question_calls)
            res = await ctrl.resolve_for_question('解釋目前畫面')
            after_calls = len(analyzer.frame_calls) + len(analyzer.question_calls)
            self.assertTrue(res['available'])
            self.assertTrue(res['cache_hit'])
            self.assertEqual(before_calls, after_calls)
            self.assertEqual(res['current_capture_seq'], 2)
        finally:
            await ctrl.close()

    async def test_phase2_deep_look_triggers_when_cached_slide_has_uncertain_areas_or_wake_flipped(self):
        slide1 = _frame('s1', '0000000000000000')
        slide2 = _frame('s2', 'ffffffffffffffff')
        current = [slide1]

        class _SwitchableCapturer:
            async def capture(self):
                return current[0]

        analyzer = FakeAnalyzer(uncertain=['右下角小字模糊'])
        saved = []
        cache = ScreenFrameCache(max_frames=4)
        ctrl = ScreenContextController(
            'm-p2-deep', mode='watch_and_cache', capturer=_SwitchableCapturer(), analyzer=analyzer,
            persist_observation=saved.append, frame_cache=cache,
            poll_seconds=10.0, min_analyze_interval_seconds=0.0,
        )
        await ctrl.start()
        try:
            # Wake captures slide1, then presenter flips to slide2 before question ends
            await ctrl.note_wake()
            current[0] = slide2
            res = await ctrl.resolve_for_question('這張表右下角的數字是多少？')
            self.assertTrue(res['available'])
            self.assertTrue(res['wake_slide_changed'])
            self.assertEqual(len(saved), 2)
            self.assertIsNotNone(res['question_focused_vision'])
            self.assertEqual(len(analyzer.question_calls), 1)
        finally:
            await ctrl.close()

    async def test_vision_error_degrades_gracefully_and_cancellation_propagates(self):
        slide1 = _frame('s1', '0000000000000000')
        capturer = FakeCapturer([slide1])
        analyzer = FakeAnalyzer(fail=True)
        cache = ScreenFrameCache(max_frames=4)
        ctrl = ScreenContextController(
            'm-err', mode='on_demand', capturer=capturer, analyzer=analyzer,
            persist_observation=lambda obs: None, frame_cache=cache,
        )
        await ctrl.start()
        try:
            res = await ctrl.resolve_for_question('幫我看畫面')
            self.assertFalse(res['available'])
            self.assertEqual(res['error_code'], 'vision_turn_failed')
            self.assertEqual(ctrl.status()['vision_failures'], 1)
        finally:
            await ctrl.close()

        # Cancellation must propagate immediately (never swallowed)
        slow_analyzer = FakeAnalyzer(delay=5.0)
        ctrl2 = ScreenContextController(
            'm-cancel', mode='on_demand', capturer=FakeCapturer([slide1]),
            analyzer=slow_analyzer, persist_observation=lambda obs: None,
        )
        await ctrl2.start()
        try:
            task = asyncio.create_task(ctrl2.resolve_for_question('取消測試'))
            await asyncio.sleep(0.05)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            await ctrl2.close()

    async def test_evicted_frame_a_b_a_re_caches_exact_a_and_never_substitutes_b(self):
        slide_a = _frame('SLIDE_A', '0000000000000000')
        slide_b = _frame('SLIDE_B', 'ffffffffffffffff')
        current = [slide_a]

        class _SwitchableCapturer:
            async def capture(self):
                return current[0]

        class _ContentIdentifyingAnalyzer(FakeAnalyzer):
            async def analyze_frame(self, image_path, *, detail='high'):
                content = Path(image_path).read_bytes().decode('utf-8')
                res = await super().analyze_frame(image_path, detail=detail)
                res['title'] = content
                res['summary_text'] = f'SUMMARY:{content}'
                return res

            async def analyze_question(self, image_path, question, *, detail='high', prior_summary=None):
                content = Path(image_path).read_bytes().decode('utf-8')
                res = await super().analyze_question(image_path, question, detail=detail, prior_summary=prior_summary)
                res['answer_relevant_findings'] = f'DEEP:{content}'
                return res

        analyzer = _ContentIdentifyingAnalyzer()
        saved = []
        cache = ScreenFrameCache(max_frames=1)
        ctrl = ScreenContextController(
            'm-evict-aba', mode='watch_and_cache', capturer=_SwitchableCapturer(),
            analyzer=analyzer, persist_observation=saved.append, frame_cache=cache,
            poll_seconds=10.0, min_analyze_interval_seconds=0.0, max_frames=1,
        )
        await ctrl.start()
        try:
            # 1) Ask on slide A
            r1 = await ctrl.resolve_for_question('請看這頁細節')
            self.assertEqual(r1['current_capture_seq'], 1)
            self.assertEqual(r1['question_focused_vision']['answer_relevant_findings'], 'DEEP:JPEG-SLIDE_A')

            # 2) Switch to slide B -> evicts A from max_frames=1 ring cache
            current[0] = slide_b
            r2 = await ctrl.resolve_for_question('請看這頁細節')
            self.assertEqual(r2['current_capture_seq'], 2)
            self.assertEqual(r2['question_focused_vision']['answer_relevant_findings'], 'DEEP:JPEG-SLIDE_B')
            self.assertIsNone(cache.get(slide_a.frame_sha256))
            self.assertIsNotNone(cache.get(slide_b.frame_sha256))

            # 3) Switch back to slide A -> hits metadata for A (seq=1), MUST re-cache & analyze A, never B
            current[0] = slide_a
            r3 = await ctrl.resolve_for_question('請看這頁細節')
            self.assertTrue(r3['cache_hit'])
            self.assertEqual(r3['current_capture_seq'], 1)
            self.assertEqual(r3['question_focused_vision']['answer_relevant_findings'], 'DEEP:JPEG-SLIDE_A')
            self.assertNotIn('SLIDE_B', r3['question_focused_vision']['answer_relevant_findings'])
            self.assertIsNotNone(cache.get(slide_a.frame_sha256))
        finally:
            await ctrl.close()

    async def test_evicted_older_slide_never_falls_back_to_other_sha_in_cache(self):
        slide_a = _frame('SLIDE_A', '0000000000000000')
        slide_b = _frame('SLIDE_B', 'ffffffffffffffff')
        current = [slide_a]

        class _SwitchableCapturer:
            async def capture(self):
                return current[0]

        analyzer = FakeAnalyzer()
        cache = ScreenFrameCache(max_frames=1)
        ctrl = ScreenContextController(
            'm-evict-no-fallback', mode='watch_and_cache', capturer=_SwitchableCapturer(),
            analyzer=analyzer, persist_observation=lambda obs: None, frame_cache=cache,
            poll_seconds=10.0, min_analyze_interval_seconds=0.0, max_frames=1,
        )
        await ctrl.start()
        try:
            await ctrl.resolve_for_question('先看第一頁')
            current[0] = slide_b
            await ctrl.resolve_for_question('再看第二頁')
            # A is evicted; only B remains in cache
            meta_a = ctrl._analyzed_by_sha[slide_a.frame_sha256]
            self.assertIsNone(cache.get(slide_a.frame_sha256))
            self.assertIsNotNone(cache.latest())
            self.assertEqual(cache.latest().frame_sha256, slide_b.frame_sha256)

            # Without matching live frame A, helper MUST refuse cross-SHA fallback to B
            self.assertIsNone(ctrl._ensure_target_frame_cached(meta_a, live_frame=None))
            self.assertIsNone(ctrl._ensure_target_frame_cached(meta_a, live_frame=slide_b))

            # If deep look target frame cannot be obtained (evicted & not live), report unavailable and skip analyze_question
            calls_before = len(analyzer.question_calls)
            ctrl._ensure_target_frame_cached = lambda meta, live_frame=None: None
            r = await ctrl.resolve_for_question('請看這頁細節')
            self.assertEqual(len(analyzer.question_calls), calls_before)
            self.assertIsNone(r['question_focused_vision'])
            self.assertEqual(r['deep_look_unavailable'], 'target_frame_evicted')
        finally:
            await ctrl.close()


if __name__ == '__main__':
    unittest.main()
