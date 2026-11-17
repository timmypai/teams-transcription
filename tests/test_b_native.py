"""Offline host-framed questions and owned Native media completion."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription.b_native import NativeLiveResponder, utf8_chunks
from teams_transcription.b_response import MeetingMinutesTool, GREETING
from teams_transcription.b_question import FramedQuestion
from context_fixtures import context_record,context_snapshot


class NativeLiveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reads = []
        self.turn_number = 0
        self.now = [100.]
        def read(meeting):
            self.reads.append(meeting)
            return context_snapshot(meeting,[context_record('budget','預算十萬元。')],version=3)
        self.sink = AsyncMock()
        self.media = dict(frames=0, packets=0, energy=0., duration=0., connected=True)
        self.sink.observe.side_effect = lambda:{
            **self.media, **{f'source_{key}':self.media[key]
                            for key in ('frames','packets','energy','duration')}}
        self.output = NativeTrackOutput("one", self.sink, clock=lambda:self.now[0])
        self.live = NativeLiveResponder(None,"/tmp/native-unused","one",self.output,
            MeetingMinutesTool("one",read), executable="codex", question_timeout=20,
            stop_timeout=.05, clock=lambda:self.now[0])
        self.live.rpc = AsyncMock()
        self.live.rpc.executable = "codex"
        self.live.rpc.thread.return_value = "native-thread"
        self.live.rpc.notifications = asyncio.Queue()
        self.live.rpc.process = SimpleNamespace(returncode=None)
        self.live.peer = AsyncMock()
        self.live.peer.offer.return_value = "controlled-offer"
        self.calls = []
        async def request(method,p):
            self.calls.append((method,p))
            if method == "account/read":
                return {"account":{"type":"chatgpt"}}
            if method == "thread/realtime/start":
                self.notify("thread/realtime/sdp",sdp="controlled-answer")
            if method == "thread/realtime/stop":
                self.notify("thread/realtime/closed",reason="stopped")
            return {}
        self.live.rpc.request.side_effect = request
        version = SimpleNamespace(returncode=0,
            communicate=AsyncMock(return_value=(b"codex-cli 0.159.0-alpha.3\n", b"")))
        with patch("teams_transcription.b_native.asyncio.create_subprocess_exec",
                   new=AsyncMock(return_value=version)):
            await self.live.start()

    def notify(self, method, **params):
        self.live.rpc.notifications.put_nowait({"method":method,
            "params":{"threadId":"native-thread",**params}})

    async def until(self, condition):
        async with asyncio.timeout(3):
            while not condition():
                await asyncio.sleep(.005)

    async def turn(self, kind, ident, *, role="assistant", text=""):
        await self.live._data_event({"type":kind, "turn":{
            "id":ident, "role":role, "start_ms":0, "end_ms":100,
            "transcript":text}})

    async def emit_speech(self, text):
        self.turn_number += 1
        ident = "assistant-"+str(self.turn_number)
        await self.turn("turn.created", ident)
        await self.receive_media(.1, active=True)
        self.notify("thread/realtime/transcript/done",role="assistant",text=text)
        await self.turn("turn.done", ident, text=text)
        await self.receive_media(.81)

    async def receive_media(self, seconds, *, active=False):
        self.media['frames'] += round(seconds * 48000)
        self.media['duration'] += seconds
        self.media['packets'] += 1
        self.media['energy'] += seconds * (.01 if active else 0)
        self.now[0] += seconds
        await self.output.observe()

    async def input_pcm(self, *, quiet=False, seconds=.02):
        frames = round(seconds * 24000)
        while frames:
            count = min(frames, 1200)
            started = self.now[0]
            self.now[0] += count / 24000
            pcm = bytes(count*2) if quiet else b"\x00\x10\x00\xf0"*(count//2)
            self.assertTrue(self.live.push_rx("one", pcm,
                started_at=started, ended_at=self.now[0]))
            frames -= count
            await asyncio.sleep(0)

    async def delta(self, text, *, thread="native-thread"):
        await self.live.handle_notification("thread/realtime/transcript/delta", {
            "threadId":thread, "role":"user", "delta":text})

    async def utterance(self, text, *, quiet=1.6):
        self.now[0] += .2  # A new physical utterance starts beyond the owned echo tail.
        await self.input_pcm()
        await self.delta(text)
        if quiet:
            await self.input_pcm(quiet=True, seconds=quiet)

    async def wake(self):
        await self.utterance("小白小白")
        await self.until(lambda:self.live.state=="greeting")
        await self.emit_speech(GREETING)
        self.now[0] += 1
        await self.until(lambda:self.live.state=="listening")

    def context_payload(self, start=0):
        events = [call.args[0] for call in self.live.peer.send_event.await_args_list[start:]]
        texts = [event["content"][0]["text"] for event in events]
        complete = "".join(text for text in texts if text != "RESULT_COMPLETE")
        decoder, values = json.JSONDecoder(), []
        while complete.strip():
            value, end = decoder.raw_decode(complete.lstrip())
            values.append(value)
            complete = complete.lstrip()[end:]
        return next(value for value in values if "result" in value), events

    async def asyncTearDown(self):
        await self.live.close()

    async def test_woken_question_without_provider_final_or_delegation_reads_once_muted(self):
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state=="answering")
        self.assertEqual(self.reads,["one"])
        payload, events = self.context_payload()
        self.assertEqual(payload["result"]["version"],3)
        self.assertIn("預算十萬元",str(payload["selected_source_reading"]))
        self.assertTrue(all(event["type"]=="session.context.append"
            and event["channel"]=="commentary"
            and "delegation_item_id" not in event for event in events))
        self.assertEqual(events[-1]["content"][0]["text"],"RESULT_COMPLETE")
        await self.emit_speech("依目前部分紀錄，預算十萬元。")
        self.now[0] += 1
        await self.until(lambda:self.live.state=="sleeping")
        self.assertEqual(self.reads,["one"])
        self.assertFalse(self.output.enabled)
        self.sink.start.assert_awaited_once()
        self.assertFalse(any(call.args == (False,) for call in self.sink.set_muted.await_args_list))
        self.assertEqual(self.live.drain_result["status"],"quiet")
        self.assertFalse(self.live.drain_result["drained"])

    def test_live_prompt_distinguishes_verbatim_format_from_normal_current_fact_answers(self):
        prompt = self.live.live_start_params('synthetic-offer')['prompt']
        self.assertNotIn('先回答本次准用資料能支持的內容，再說明紀錄範圍。', prompt)
        self.assertIn('逐字引用時，只說本題當前來源中支援問題的原文字句', prompt)
        self.assertIn('不加引言、前綴、引號或額外說明', prompt)
        self.assertIn('一般問答依本題當前來源自然作答', prompt)
        self.assertIn('前次回答不是本題來源', prompt)

    async def test_three_physical_questions_in_one_provider_turn_query_current_sqlite_each_time(self):
        import tempfile
        from teams_transcription.meeting_store import MeetingStore
        from teams_transcription.b_response import MeetingStoreMinutesReader
        request_ids = []
        reads = self.reads
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create("one")
            class Source:
                def read(self, meeting):
                    reads.append(meeting)
                    return store.read(meeting)
            self.live.tool.reader = MeetingStoreMinutesReader(Source())
            await self.turn("turn.created","one-long-provider-turn",role="user")
            for revision, amount in ((1,"十"), (2,"十二"), (2,"十二")):
                store.append_final("one", {"event_id":"budget", "revision":revision,
                    "text":"預算"+amount+"萬元。", "sequence":1,
                    "timestamp":"2026-10-11T00:00:00Z"})
                version = store.read("one")["version"]
                start = self.live.peer.send_event.await_count
                await self.utterance("小白小白，預算多少？")
                await self.until(lambda:self.live.state=="answering")
                request_ids.append(self.live._answer_scope["request_id"])
                payload, _ = self.context_payload(start)
                self.assertEqual(payload["result"]["version"],version)
                readings = payload["selected_source_reading"]
                self.assertEqual([row["text"] for row in readings], ["預算"+amount+"萬元。"])
                await self.emit_speech("預算"+amount+"萬元。")
                self.now[0] += 1
                await self.until(lambda:self.live.state=="sleeping")
        self.assertEqual(self.reads,["one"]*3)
        self.assertEqual(len(set(request_ids)),3)

    async def test_provider_events_alone_cannot_query_or_authorize_output(self):
        self.output.start_output()
        for _ in range(2):
            await self.turn("turn.created","provider-user",role="user")
            await self.live.handle_notification("thread/realtime/transcript/done", {
                "threadId":"native-thread","role":"user","text":"小白小白，預算多少？"})
            await self.turn("turn.done","provider-user",role="user",text="小白小白，預算多少？")
            await self.live.handle_notification("thread/realtime/itemAdded", {
                "threadId":"native-thread","item":{"type":"handoff_request",
                    "handoff_id":"provider-only","item_id":"provider-only",
                    "input_transcript":"小白小白，預算多少？"}})
        self.assertFalse(self.output.active)
        self.assertEqual(self.reads,[])
        self.assertEqual(self.live.question_receipts()["counts"]["received"],0)
        self.live.peer.send_event.assert_not_awaited()

    async def test_short_pause_joins_question_and_ignores_early_provider_final(self):
        await self.utterance("小白小白，",quiet=.5)
        await self.live.handle_notification("thread/realtime/transcript/done", {
            "threadId":"native-thread","role":"user","text":"小白小白"})
        self.assertEqual(self.reads,[])
        self.assertFalse(self.output.active)
        await self.utterance("預算多少？")
        await self.until(lambda:self.live.state=="answering")
        self.assertEqual(self.reads,["one"])
        self.assertEqual(self.live.question_receipts()["counts"]["admitted"],1)

    async def test_no_packets_is_not_observed_silence(self):
        await self.utterance("小白小白，預算多少？",quiet=0)
        self.now[0] += 10
        await asyncio.sleep(.1)
        self.assertEqual(self.reads,[])
        self.assertFalse(self.output.active)
        self.live.peer.send_event.assert_not_awaited()

    async def test_unwoken_utterance_neither_queries_nor_speaks(self):
        self.output.start_output()
        await self.utterance("預算多少？")
        await asyncio.sleep(.1)
        self.assertEqual(self.reads,[])
        self.live.peer.send_event.assert_not_awaited()
        self.assertFalse(any(call.args == (False,) for call in self.sink.set_muted.await_args_list))

    async def test_foreign_transcript_cannot_attach_wake_to_admitted_pcm(self):
        await self.input_pcm()
        await self.delta("小白小白，預算多少？",thread="foreign-thread")
        await self.input_pcm(quiet=True,seconds=1.6)
        await asyncio.sleep(.1)
        self.assertEqual(self.reads,[])
        self.assertFalse(self.output.active)

    async def test_natural_greeting_arms_one_followup_after_output_completion(self):
        await self.utterance("小白小白")
        await self.until(lambda:self.live.state=="greeting")
        self.assertEqual(self.reads,[])
        await self.emit_speech("您好，請說。")
        self.now[0] += 1
        await self.until(lambda:self.live.state=="listening")
        await self.utterance("預算多少？")
        await self.until(lambda:self.live.state=="answering")
        await self.emit_speech("預算十萬元。")
        self.now[0] += 1
        await self.until(lambda:self.live.state=="sleeping")
        await self.utterance("誰負責？")
        await asyncio.sleep(.1)
        self.assertEqual(self.reads,["one"])
        self.assertIsNone(self.live.error)

    async def test_greeting_has_no_implicit_total_deadline(self):
        self.output.start_output()
        await self.utterance("小白小白")
        await self.until(lambda:self.live.state=="greeting")
        self.now[0] += 61
        self.assertTrue(self.output.active)
        await self.emit_speech(GREETING)
        self.now[0] += 1
        await self.until(lambda:self.live.state=="listening")
        self.assertEqual(self.reads,[])
        self.assertIsNone(self.live.error)

    async def test_provider_cancellation_after_old_limits_mutes_and_rejects_stale_audio(self):
        self.output.start_output()
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state=="answering")
        self.now[0] += 61
        await self.turn("turn.created","long-cancelled-answer")
        await self.receive_media(.1, active=True)
        async with asyncio.timeout(.2):
            await self.live.handle_notification("thread/realtime/itemAdded", {
                "threadId":"native-thread", "item":{"type":"response.cancelled"}})
        self.assertFalse(self.output.active)
        self.sink.pause.assert_awaited()
        self.assertIsNone(self.live._answer_scope)
        self.assertIsNone(self.live._request_id)
        await self.turn("turn.created", "stale-answer")
        self.assertFalse(self.output.active)
        self.sink.pause.assert_awaited()
        self.assertIsNone(self.live.error)

    async def test_late_provider_cancellation_cannot_revive_failed_responder(self):
        await self.utterance("小白小白，預算多少？",quiet=0)
        await self.live._audio_error("controlled_failure")
        await self.live.handle_notification("thread/realtime/itemAdded", {
            "threadId":"native-thread", "item":{"type":"response.cancelled"}})
        await self.delta("小白小白，預算多少？")
        self.assertEqual(self.live.state,"failed")
        self.assertFalse(self.output.active)
        self.assertEqual(self.reads,[])

    async def test_audio_before_turn_created_and_marker_return_is_not_dropped(self):
        self.output.start_output()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(event):
            if event['content'][0]['text'] == 'RESULT_COMPLETE':
                self.assertTrue(self.output.active)
                self.assertEqual(self.sink.set_muted.await_args.args, (False,))
                entered.set()
                await release.wait()
        self.live.peer.send_event.side_effect = send
        await self.utterance("小白小白，預算多少？")
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await self.receive_media(.1, active=True)
            self.assertGreater(self.output.activity_frames, 0)
            self.assertIsNone(self.live._output_turn_id)
            await self.turn("turn.created", "early-answer")
            await self.turn("turn.done", "early-answer")
            await self.receive_media(.81)
            await asyncio.sleep(.1)
            self.assertTrue(self.output.active)
            release.set()
            await self.live._tool_task
            await self.until(lambda:self.live.state == "sleeping")
            receipt = self.live.status()['tool_deliveries'][-1]
            self.assertTrue(receipt['marker_returned'])
            self.assertFalse(receipt['model_consumption_verified'])
            self.live.peer.set_output_capture.assert_not_awaited()
        finally:
            release.set()

    async def test_late_audio_after_matching_done_restarts_received_quiet(self):
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        await self.turn("turn.created", "answer")
        await self.receive_media(.1, active=True)
        await self.turn("turn.done", "answer")
        await self.receive_media(.4)
        await self.receive_media(.1, active=True)
        await self.receive_media(.4)
        await asyncio.sleep(.1)
        self.assertTrue(self.output.active)
        await self.receive_media(.41)
        await self.until(lambda:self.live.state == "sleeping")
        self.assertEqual(self.live.drain_result['status'], 'quiet')
        self.assertFalse(self.live.drain_result['drained'])

    async def test_no_packets_and_concealment_cannot_count_as_quiet(self):
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        await self.turn("turn.created", "answer")
        await self.receive_media(.1, active=True)
        await self.turn("turn.done", "answer")
        self.now[0] += 1000
        self.media['frames'] += 48000
        self.media['duration'] += 1
        await asyncio.sleep(.1)
        self.assertTrue(self.output.active)
        self.assertEqual(self.output.quiet_received_seconds, 0)
        await self.receive_media(.81)
        await self.until(lambda:self.live.state == "sleeping")

    async def test_silence_without_answer_activity_does_not_complete(self):
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        await self.turn("turn.created", "answer")
        await self.turn("turn.done", "answer")
        await self.receive_media(2)
        await asyncio.sleep(.1)
        self.assertTrue(self.output.active)

    async def test_overlapping_turns_wait_for_all_done_with_one_relay(self):
        self.output.start_output()
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        for ident in ('prefix', 'answer'):
            await self.turn("turn.created", ident)
            await self.receive_media(.1, active=True)
        await self.turn("turn.done", 'answer')
        await self.receive_media(.81)
        await asyncio.sleep(.1)
        self.assertTrue(self.output.active)
        await self.turn("turn.done", 'prefix')
        await self.until(lambda:self.live.state == "sleeping")
        self.sink.attach_live.assert_awaited_once()
        self.sink.start.assert_awaited_once()
        self.assertEqual(self.reads, ['one'])

    async def test_next_turn_before_quiet_keeps_same_authorization(self):
        self.output.start_output()
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        await self.turn("turn.created", 'prefix')
        await self.receive_media(.1, active=True)
        await self.turn("turn.done", 'prefix')
        await self.receive_media(.4)
        await self.turn("turn.created", 'answer')
        await self.receive_media(.1, active=True)
        await self.turn("turn.done", 'prefix')
        await self.receive_media(.81)
        await asyncio.sleep(.1)
        self.assertTrue(self.output.active)
        await self.turn("turn.done", 'answer')
        await self.until(lambda:self.live.state == "sleeping")
        self.assertEqual(sum(call.args == (False,) for call in self.sink.set_muted.await_args_list), 1)

    async def test_plain_assistant_transcript_cannot_complete_media_turn(self):
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        await self.turn("turn.created", 'answer')
        await self.receive_media(.1, active=True)
        await self.live.handle_notification("thread/realtime/transcript/done", {
            "threadId":"native-thread", "role":"assistant", "text":"預算十萬元"})
        await self.receive_media(.81)
        await asyncio.sleep(.1)
        self.assertTrue(self.output.active)
        await self.turn("turn.done", 'answer')
        await self.until(lambda:self.live.state == "sleeping")

    async def test_cancellation_during_marker_send_mutes_immediately(self):
        self.output.start_output()
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(event):
            if event['content'][0]['text'] == 'RESULT_COMPLETE':
                entered.set()
                await release.wait()
        self.live.peer.send_event.side_effect = send
        await self.utterance("小白小白，預算多少？")
        await asyncio.wait_for(entered.wait(), 1)
        await self.receive_media(.1, active=True)
        await self.live.handle_notification("thread/realtime/itemAdded", {
            "threadId":"native-thread", "item":{"type":"response.cancelled"}})
        self.assertFalse(self.output.active)
        self.sink.pause.assert_awaited()
        self.assertIsNone(self.live._answer_scope)
        self.assertEqual(self.live.context.status()['committed_records'], 0)
        release.set()

    async def test_new_turn_during_quiet_pause_invalidates_completion(self):
        self.output.start_output()
        entered, release = asyncio.Event(), asyncio.Event()
        async def pause():
            entered.set()
            await release.wait()
        self.sink.pause.side_effect = pause
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == 'answering')
        await self.emit_speech('好的。')
        await asyncio.wait_for(entered.wait(), 1)
        try:
            await self.turn('turn.created', 'answer-during-quiet-close')
            release.set()
            await self.until(lambda:self.output.active)
            self.assertEqual(self.live.state, 'answering')
            self.assertIsNone(self.live.drain_result)
            await self.receive_media(.1, active=True)
            await self.turn('turn.done', 'answer-during-quiet-close')
            await self.receive_media(.81)
            await self.until(lambda:self.live.state == 'sleeping')
            self.sink.start.assert_awaited_once()
            self.sink.attach_live.assert_awaited_once()
            self.assertEqual(self.reads, ['one'])
        finally:
            release.set()

    async def test_identified_late_same_request_turn_reuses_muted_track(self):
        self.output.start_output()
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == 'answering')
        await self.emit_speech('好的。')
        await self.until(lambda:self.live.state == 'sleeping')
        self.now[0] += 120
        await self.turn('turn.created', 'late-same-request-answer')
        await self.until(lambda:self.output.active)
        await self.receive_media(.1, active=True)
        await self.turn('turn.done', 'late-same-request-answer')
        await self.receive_media(.81)
        await self.until(lambda:self.live.state == 'sleeping')
        self.sink.start.assert_awaited_once()
        self.sink.attach_live.assert_awaited_once()
        self.assertEqual(self.reads, ['one'])

    async def test_cancellation_during_unmute_never_sends_final_marker(self):
        self.output.start_output()
        entered = asyncio.Event()
        async def unmute(muted):
            if not muted:
                entered.set()
                await asyncio.Event().wait()
        self.sink.set_muted.side_effect = unmute
        await self.utterance("小白小白，預算多少？")
        await asyncio.wait_for(entered.wait(), 1)
        self.assertFalse(any(call.args[0]['content'][0]['text'] == 'RESULT_COMPLETE'
            for call in self.live.peer.send_event.await_args_list))
        await self.live.handle_notification("thread/realtime/itemAdded", {
            "threadId":"native-thread", "item":{"type":"response.cancelled"}})
        self.assertFalse(self.output.active)
        self.assertTrue(self.output.enabled)
        self.sink.pause.assert_awaited()
        self.assertEqual(self.live.context.status()['committed_records'], 0)

    async def test_marker_failure_revokes_authorized_track(self):
        self.output.start_output()
        async def send(event):
            if event['content'][0]['text'] == 'RESULT_COMPLETE':
                self.assertTrue(self.output.active)
                raise RuntimeError('controlled marker failure')
        self.live.peer.send_event.side_effect = send
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.tool_calls == 1)
        await self.live._tool_task
        self.assertFalse(self.output.active)
        self.assertTrue(self.output.enabled)
        self.assertEqual(self.live.state, 'sleeping')
        self.assertIsNone(self.live.error)
        self.sink.pause.assert_awaited()
        self.assertEqual(self.live.context.status()['committed_records'], 0)

    async def test_scope_change_during_marker_send_mutes_without_commit(self):
        self.output.start_output()
        async def send(event):
            if event['content'][0]['text'] == 'RESULT_COMPLETE':
                self.live.thread_id = 'replacement-thread'
        self.live.peer.send_event.side_effect = send
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.tool_calls == 1)
        await self.live._tool_task
        self.assertFalse(self.output.active)
        self.assertIsNone(self.live._answer_scope)
        self.assertEqual(self.live.context.status()['committed_records'], 0)
        self.live.thread_id = 'native-thread'

    async def test_active_answer_has_no_total_duration_limit(self):
        self.output.start_output()
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        await self.turn("turn.created", "long-answer")
        for elapsed in (46, 61, 120):
            self.now[0] += elapsed
            await self.receive_media(.1, active=True)
            self.assertTrue(self.output.active)
        await self.turn("turn.done", "long-answer")
        await self.receive_media(.81)
        await self.until(lambda:self.live.state == "sleeping")

    async def test_disconnect_fails_closed_without_quiet_receipt(self):
        self.output.start_output()
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        self.media['connected'] = False
        await self.until(lambda:self.live.error is not None)
        self.assertFalse(self.output.active)
        self.assertFalse(self.output.enabled)
        self.assertFalse(self.live.speech_completion_snapshot()['drain_result']['quiet'])
        self.sink.pause.assert_awaited()

    async def test_statistics_reset_fails_closed(self):
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        await self.receive_media(.1, active=True)
        self.media['frames'] = 0
        await self.until(lambda:self.live.error is not None)
        self.assertFalse(self.output.active)

    async def test_disable_enable_cannot_reauthorize_unsolicited_turn(self):
        self.output.start_output()
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        await self.live.disable_output()
        self.live.enable_output()
        await self.turn("turn.created", "unsolicited")
        self.assertFalse(self.output.active)
        self.assertIsNone(self.live._answer_scope)

    async def test_new_answer_does_not_reuse_previous_quiet_receipt(self):
        await self.wake()
        self.assertTrue(self.live.speech_completion_snapshot()['drain_result']['quiet'])
        await self.utterance("預算多少？")
        await self.until(lambda:self.live.state == "answering")
        current = self.live.speech_completion_snapshot()
        self.assertFalse(current['drain_result']['present'])
        self.assertFalse(current['drain_result']['drained'])
        self.assertEqual(current['activity_frames'], 0)

    async def test_same_thread_continues_answering_beyond_old_cumulative_context_limit(self):
        clock = self.now
        self.live.clock = self.output.clock = lambda:clock[0]
        self.output.start_output()
        def growing_source(meeting):
            self.reads.append(meeting)
            return context_snapshot(meeting,[context_record(str(i),
                '預算十萬元。'+'說明'*300+str(len(self.reads))) for i in range(12)],
                version=len(self.reads))
        self.live.tool.reader = growing_source
        for _ in range(5):
            await self.utterance('小白小白，預算多少？')
            await self.until(lambda:self.live.state in {'answering','failed'})
            self.assertEqual(self.live.state,'answering')
            await self.emit_speech('依部分紀錄，預算十萬元。')
            clock[0] += 1
            await self.until(lambda:self.live._speech_task.done())
            self.assertEqual(self.live.state,'sleeping')
        self.assertEqual(self.live.thread_id,'native-thread')
        self.assertEqual(self.live.tool_calls,5)
        self.assertGreater(self.live.context.status()['sent_bytes'],32768)
        self.assertIsNone(self.live.error)
        self.assertTrue(self.output.enabled)


    async def test_no_data_is_returned_to_live_not_fabricated_answer(self):
        self.live.tool.reader=lambda m:context_snapshot(m,[])
        await self.wake()
        await self.utterance("What is decided?")
        await self.until(lambda:self.live.state=="answering")
        payload, _ = self.context_payload()
        self.assertFalse(payload["result"]["available"])
        self.assertFalse(payload["selected_source_reading"])

    async def test_tool_foreign_store_or_failure_revokes_tx(self):
        self.live.tool.reader=lambda m:context_snapshot('other',[context_record('secret','secret')])
        self.output.start_output()
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state=="failed")
        self.live.peer.send_event.assert_not_awaited()
        self.assertFalse(self.output.enabled)

    async def test_rx_intervals_reject_delayed_echo_and_accept_next_question(self):
        now = self.now[0]
        self.live.intervals.intervals.append((now-.8,now-.5))
        pcm=b"\0\0"*2048
        self.assertFalse(self.live.push_rx("one",pcm,started_at=now-.7,ended_at=now-.7+2048/24000))
        self.assertTrue(self.live.push_rx("one",pcm,started_at=now-.2,ended_at=now-.2+2048/24000))
        self.assertFalse(self.live.push_rx("other",pcm,started_at=now-.2,ended_at=now-.2+2048/24000))
        await self.until(lambda:self.live.peer.push.await_count==1)
        self.assertEqual(self.live.peer.push.await_args.args[0],pcm)

    async def test_stop_mutes_persistent_relay_and_closes_owned_resources(self):
        self.output.start_output()
        await self.utterance("小白小白，預算多少？")
        await self.until(lambda:self.live.state == "answering")
        await self.live.close()
        self.assertFalse(self.output.active)
        self.sink.pause.assert_awaited()
        self.sink.stop.assert_awaited_once()

    async def test_stop_missing_ack_or_closed_reports_timeout_and_cleans(self):
        self.live.rpc.request.side_effect=AsyncMock(return_value={})
        await self.live.close()
        self.assertEqual(self.live.stop_status,"timeout")
        self.assertEqual(self.live.state,"stop_unknown")
        self.assertIsNone(self.live.receiver)
        self.live.peer.close.assert_awaited_once()
        self.live.rpc.close.assert_awaited_once()

    async def test_partial_bare_wake_waits_for_local_quiet_not_provider_final(self):
        self.output.start_output()
        await self.utterance("小白小白",quiet=.5)
        self.assertEqual(self.live.state,"sleeping")
        self.assertFalse(self.output.active)
        self.assertFalse(any(call.args == (False,) for call in self.sink.set_muted.await_args_list))
        await self.live.handle_notification("thread/realtime/transcript/done", {
            "threadId":"native-thread","role":"user","text":"小白小白"})
        self.assertEqual(self.live.state,"sleeping")
        await self.input_pcm(quiet=True,seconds=1.1)
        await self.until(lambda:self.live.state=="greeting")
        await self.emit_speech("您好，請說。")
        self.now[0] += 1
        await self.until(lambda:self.live.state=="listening")
        self.assertEqual(self.output.activity_frames,4800)

    async def test_admitted_application_question_and_observed_answer_are_private_activity(self):
        import tempfile
        from teams_transcription.meeting_store import MeetingStore
        from teams_transcription.qa_activity import QuestionActivityRecorder
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('one')
            before = store.read('one')
            self.live.activity_recorder = QuestionActivityRecorder(store, 'one')
            await self.utterance("小白小白，預算多少？")
            await self.until(lambda: self.live.state == "answering"
                and len(store.list_question_activity('one')) == 1)
            rows = store.list_question_activity('one')
            self.assertEqual(rows[0]['kind'], 'question')
            self.assertIn('預算多少', rows[0]['text'])
            self.assertEqual(rows[0]['request_id'], 'question-1')
            for delta in ('合成', '答案'):
                await self.live.handle_notification('thread/realtime/transcript/delta',
                    {'threadId': 'native-thread', 'role': 'assistant', 'delta': delta,
                     'itemId': 'answer-item', 'turnId': 'provider-turn'})
            await self.live.handle_notification('thread/realtime/transcript/done',
                {'threadId': 'native-thread', 'role': 'assistant', 'text': '合成答案。',
                 'itemId': 'answer-item', 'turnId': 'provider-turn'})
            await self.live.handle_notification('thread/realtime/transcript/done',
                {'threadId': 'foreign-thread', 'role': 'assistant', 'text': '不得寫入'})
            await self.live.activity_recorder.flush()
            rows = store.list_question_activity('one')
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[1]['text'], '合成答案。')
            self.assertIsNone(rows[1]['request_id'])
            self.assertEqual(rows[1]['turn_id'], 'provider-turn')
            self.assertFalse(self.live.speech_completion_snapshot()['done_is_set'])
            self.assertEqual(self.live.state, 'answering')
            self.assertEqual(store.read('one'), before)
            self.assertNotIn('合成答案', str(self.live.status()))
            await self.live.close()
            self.assertEqual(MeetingStore(root).list_question_activity('one'), rows)

    async def test_activity_fence_failure_does_not_fail_audio_or_leak_question(self):
        import tempfile
        from teams_transcription.meeting_store import MeetingStore
        from teams_transcription.qa_activity import QuestionActivityRecorder
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('one')
            store.finalize('one', 'stopped')
            self.live.activity_recorder = QuestionActivityRecorder(store, 'one')
            await self.utterance("小白小白，預算多少？")
            await self.until(lambda: self.live.state == 'answering'
                and self.live.status()['question_activity']['state'] == 'unavailable')
            self.assertEqual(store.list_question_activity('one'), [])
            self.assertIsNone(self.live.error)
            self.assertEqual(self.reads, ['one'])
            self.assertNotIn('預算多少', str(self.live.status()))
            await self.live.close()

    async def test_slow_journal_does_not_delay_native_media_close(self):
        import tempfile
        import threading
        from teams_transcription.meeting_store import MeetingStore
        from teams_transcription.qa_activity import QuestionActivityRecorder
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('one')
            self.live.activity_recorder = QuestionActivityRecorder(store, 'one')
            entered, release = threading.Event(), threading.Event()
            original = store._save_question_activity
            def delayed(*args, **kwargs):
                entered.set()
                release.wait(5)
                return original(*args, **kwargs)
            with patch.object(store, '_save_question_activity', side_effect=delayed):
                await self.live.handle_notification('thread/realtime/transcript/done',
                    {'threadId': 'native-thread', 'role': 'assistant', 'text': '已接受合成回答'})
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                closing = asyncio.create_task(self.live.close())
                try:
                    await self.until(lambda: self.output.closed)
                    self.assertFalse(closing.done())
                finally:
                    release.set()
                    await closing
            self.assertEqual(store.list_question_activity('one')[0]['text'], '已接受合成回答')


class WireContractTests(unittest.TestCase):
    def test_utf8_content_budget_roundtrip(self):
        text="本場會議😀"*700
        chunks=list(utf8_chunks(text))
        self.assertEqual("".join(chunks),text)
        self.assertTrue(all(0<len(x.encode())<=500 for x in chunks))
