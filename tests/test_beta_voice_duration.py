"""Virtual application contracts, not ASR, audible playback or provider longevity.

Only the existing fixture's injected monotonic clock advances. Event-loop waits
retain its normal bounded deadlines; no real-hour or real-model run is claimed.
"""
import unittest

import test_b_native as native_fixture


class BetaVoiceDurationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Hold the fixture instead of importing/subclassing its TestCase, which
        # would make discovery rerun its unrelated test methods in this module.
        self.case = native_fixture.NativeLiveTests('runTest')
        await self.case.asyncSetUp()

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    def assert_one_model_start(self):
        self.assertEqual(sum(method == 'thread/realtime/start'
            for method, _ in self.case.calls), 1)
        self.case.live.rpc.start.assert_awaited_once()
        self.case.live.rpc.thread.assert_awaited_once()

    async def next_output_poll(self):
        count = self.case.sink.observe.await_count
        await self.case.until(lambda: self.case.sink.observe.await_count > count)

    async def test_virtual_hour_idle_then_three_woken_questions_reuse_one_session(self):
        case = self.case
        rpc = case.live.rpc
        case.live.enable_output()
        case.now[0] += 3601
        for index in range(3):
            with self.subTest(question=index + 1):
                await case.utterance('小白小白，請問目前預算多少？')
                await case.until(lambda: case.live.state == 'answering')
                self.assertTrue(case.output.active)
                self.assertEqual(len(case.reads), index + 1)
                await case.emit_speech('目前預算十萬元。')
                await case.until(lambda: case.live.state == 'sleeping')
                self.assertFalse(case.output.active)
                self.assertTrue(case.output.enabled)
                self.assertIs(case.live.rpc, rpc)
                self.assertIsNone(case.live.error)
                self.assert_one_model_start()
        self.assertEqual(case.live.question_receipts()['counts']['admitted'], 3)
        self.assertEqual(case.reads, ['one'] * 3)

    async def test_long_answer_crosses_old_limits_but_requires_matching_done_and_media_quiet(self):
        case = self.case
        case.live.enable_output()
        await case.utterance('小白小白，請完整說明目前預算。')
        await case.until(lambda: case.live.state == 'answering')
        await case.turn('turn.created', 'synthetic-long-answer')
        began = case.now[0]
        for elapsed in (45.1, 120.1, 300.1):
            with self.subTest(virtual_elapsed=elapsed):
                case.now[0] = began + elapsed - .1
                await case.receive_media(.1, active=True)
                await self.next_output_poll()
                self.assertTrue(case.output.active)
                self.assertEqual(case.live.state, 'answering')
                self.assertIsNone(case.live.error)
        # Quiet alone and a different turn ID cannot finish the admitted turn.
        await case.receive_media(.81)
        await self.next_output_poll()
        self.assertTrue(case.output.active)
        await case.turn('turn.done', 'synthetic-unmatched-answer')
        self.assertFalse(case.live._speech_done.is_set())
        self.assertTrue(case.output.active)
        # Matching done alone also cannot finish while real media remains active.
        await case.receive_media(.1, active=True)
        await case.turn('turn.done', 'synthetic-long-answer')
        await self.next_output_poll()
        self.assertTrue(case.output.active)
        self.assertEqual(case.output.quiet_received_seconds, 0)
        await case.receive_media(.81)
        await case.until(lambda: case.live.state == 'sleeping')
        self.assertFalse(case.output.active)
        self.assertEqual(case.live.drain_result['status'], 'quiet')
        self.assertFalse(case.live.drain_result['drained'])
        self.assert_one_model_start()

    async def test_provider_cancel_mutes_old_turn_and_fresh_wake_can_answer(self):
        case = self.case
        case.live.enable_output()
        await case.utterance('小白小白，請說明目前預算。')
        await case.until(lambda: case.live.state == 'answering')
        await case.turn('turn.created', 'synthetic-cancelled-answer')
        await case.receive_media(.1, active=True)
        await case.live.handle_notification('thread/realtime/itemAdded', {
            'threadId': 'native-thread', 'item': {'type': 'response.cancelled'}})
        self.assertFalse(case.output.active)
        self.assertIsNone(case.live._answer_scope)
        case.sink.pause.assert_awaited()
        await case.turn('turn.created', 'synthetic-cancelled-answer')
        await case.turn('turn.done', 'synthetic-cancelled-answer')
        self.assertFalse(case.output.active)
        self.assertIsNone(case.live._answer_scope)
        await case.utterance('小白小白，現在預算是多少？')
        await case.until(lambda: case.live.state == 'answering')
        await case.turn('turn.created', 'synthetic-cancelled-answer')
        self.assertFalse(case.live._speech_done.is_set())
        await case.emit_speech('現在預算十萬元。')
        await case.until(lambda: case.live.state == 'sleeping')
        self.assertEqual(case.reads, ['one', 'one'])
        self.assertIsNone(case.live.error)
        self.assertFalse(case.output.active)
        self.assert_one_model_start()

    async def test_expired_bare_wake_followup_rejects_unwoken_text_but_accepts_fresh_wake(self):
        case = self.case
        case.live.enable_output()
        await case.wake()
        self.assertTrue(case.live.question_framer.status()['followup_active'])
        self.assertEqual(case.live.question_timeout, 20)
        case.now[0] += 21
        self.assertFalse(case.live.question_framer.status()['followup_active'])
        await case.utterance('請問目前預算多少？')
        await case.until(lambda: case.live.question_framer.pending_status()['characters'] == 0)
        self.assertEqual(case.reads, [])
        self.assertEqual(case.live.question_receipts()['counts']['admitted'], 1)
        self.assertFalse(case.output.active)
        await case.utterance('小白小白，請問目前預算多少？')
        await case.until(lambda: case.live.state == 'answering')
        await case.emit_speech('目前預算十萬元。')
        await case.until(lambda: case.live.state == 'sleeping')
        self.assertEqual(case.reads, ['one'])
        self.assert_one_model_start()

    async def test_explicit_provider_session_expiry_is_not_hidden_by_virtual_idle_contract(self):
        case = self.case
        case.live.enable_output()
        case.notify('thread/realtime/closed', reason='session_expired')
        await case.until(lambda: case.live.state == 'failed')
        self.assertTrue(case.live.error)
        self.assertFalse(case.output.active)
        case.now[0] += 3601
        start = case.now[0]
        self.assertFalse(case.live.push_rx('one', b'\x00\x10' * 480,
            started_at=start, ended_at=start + .02))
        await case.delta('小白小白，請問目前預算多少？')
        self.assertEqual(case.reads, [])
        self.assertEqual(case.live.question_receipts()['counts']['admitted'], 0)
        self.assertEqual(case.live.state, 'failed')
        self.assert_one_model_start()
