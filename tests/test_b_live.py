"""B protocol and synthetic incoming-track checks; no OAuth/model/Teams call."""
import asyncio
import base64
import json
import math
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from teams_transcription.b_audio import GatedPCMOutput
from teams_transcription.b_live import CAPTURE_OUTPUT_JS, LiveSpeechChannel, ResponseLivePeer
from teams_transcription.b_guard import ExistingLiveGuard
from teams_transcription.browser import Browser


class LiveChannelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.output = GatedPCMOutput("one", AsyncMock())
        self.output.sink.drain.return_value = {"status":"drained", "truncated":False, "reason":None}
        self.channel = LiveSpeechChannel(None, "/tmp/not-used", "one", self.output)
        self.rpc = self.channel.rpc = AsyncMock()
        self.rpc.notifications = asyncio.Queue()
        self.rpc.thread.return_value = "speech-thread"
        self.rpc.process = SimpleNamespace(returncode=0)
        self.channel.peer = AsyncMock()
        self.channel.peer.offer.return_value = "fixture-offer"
        self.calls = []
        async def request(method, p):
            self.calls.append((method, p))
            if method == "account/read":
                return {"account":{"type":"chatgpt"}}
            if method == "thread/realtime/start":
                self.rpc.notifications.put_nowait({"method":"thread/realtime/sdp",
                    "params":{"threadId":"speech-thread","sdp":"fixture-answer"}})
            if method == "thread/realtime/appendSpeech":
                # Representative bipolar audio; +/-1 LSB is explicitly noise now.
                await self.channel._pcm({"data":base64.b64encode(b"\x20\x00\xe0\xff"*1024).decode(), "sample_rate":48000,"channels":1})
                self.rpc.notifications.put_nowait({"method":"thread/realtime/item/completed", "params":{
                    "threadId":"speech-thread", "item":{"id":"speech", "type":"transcriptSegment",
                        "role":"assistant", "text":p["text"]}}})
            return {}
        self.rpc.request.side_effect = request

    async def asyncTearDown(self):
        await self.channel.close()

    async def test_native_attachment_follows_official_answer_before_ready(self):
        self.channel.output_mode = 'native'
        self.channel.output = AsyncMock()
        order = []
        async def answer(sdp):
            self.assertEqual(sdp, 'fixture-answer')
            order.append('answer')
        async def attach(page):
            self.assertIs(page, self.channel.peer.page)
            self.assertFalse(self.channel.started)
            order.append('attach')
        self.channel.peer.answer.side_effect = answer
        self.channel.output.attach_live.side_effect = attach
        await self.channel.start()
        self.assertEqual(order, ['answer', 'attach'])
        self.assertTrue(self.channel.started)
        self.channel.peer.set_output_capture.assert_not_awaited()

    async def test_native_peer_rejects_pcm_callback(self):
        with self.assertRaisesRegex(ValueError, 'forbids a PCM callback'):
            ResponseLivePeer(None, AsyncMock(), AsyncMock(), AsyncMock(), output_mode='native')

    async def test_start_append_speech_with_default_mute_and_close(self):
        await self.channel.start()
        await self.channel.speak("我在，有什麼問題？")
        self.assertFalse(self.output.enabled)
        self.output.sink.write.assert_not_awaited()
        self.assertGreater(self.output.nonzero_frames, 0)
        self.assertFalse(self.output.active)
        self.assertEqual(self.calls[-1][0], "thread/realtime/appendSpeech")
        start = self.calls[1][1]
        self.assertEqual(start["transport"]["type"], "webrtc")
        self.assertEqual(start["model"], "gpt-live-1-codex")
        self.assertTrue(start["clientManagedHandoffs"])
        self.assertFalse(start["includeStartupContext"])

    async def test_long_speech_has_no_implicit_total_deadline_or_text_length_rejection(self):
        await self.channel.start()
        now = [100.]
        self.output.clock = lambda:now[0]
        request = self.rpc.request.side_effect
        async def delayed_speech(method, params):
            if method == "thread/realtime/appendSpeech":
                now[0] += 61
            result = await request(method, params)
            if method == "thread/realtime/appendSpeech":
                now[0] += 1
            return result
        self.rpc.request.side_effect = delayed_speech
        text = "完整預算資料。" * 500
        original_timeout = asyncio.timeout
        def accelerate_old_answer_cutoff(seconds):
            return original_timeout(0 if seconds == 45 else seconds)
        with patch("teams_transcription.b_live.asyncio.timeout",
                side_effect=accelerate_old_answer_cutoff) as timeout:
            await self.channel.speak(text)
            timeout.assert_not_called()
        self.assertEqual(self.calls[-1], ("thread/realtime/appendSpeech",
            {"threadId":"speech-thread", "text":text}))
        self.assertGreater(self.output.activity_frames,0)
        self.assertIsNone(self.output.deadline)
        self.assertIsNone(self.output._expiry)
        self.assertFalse(self.output.active)
        self.assertIsNone(self.channel.error)

    async def test_speak_rejects_nontext_and_empty_text_without_opening_output(self):
        await self.channel.start()
        for text in (None, 1, "", " \n "):
            with self.assertRaises(RuntimeError):
                await self.channel.speak(text)
        self.assertFalse(self.output.active)
        self.assertFalse(any(method == "thread/realtime/appendSpeech" for method, _ in self.calls))

    async def test_media_connect_failure_cleans_resources(self):
        self.channel.peer.wait_connected.side_effect = TimeoutError("fixture ICE failed")
        with self.assertRaises(TimeoutError):
            await self.channel.start()
        self.assertFalse(self.channel.started)
        self.assertTrue(self.output.closed)
        self.assertIsNone(self.channel.receiver)
        self.channel.peer.close.assert_awaited_once()
        self.rpc.close.assert_awaited_once()

    async def test_SDP_before_own_start_attempt_and_foreign_thread_are_not_accepted(self):
        async def premature_offer():
            self.rpc.notifications.put_nowait({"method":"thread/realtime/sdp", "params":{
                "threadId":"speech-thread", "sdp":"premature-untrusted-answer"}})
            self.rpc.notifications.put_nowait({"method":"thread/realtime/sdp", "params":{
                "threadId":"foreign", "sdp":"foreign-untrusted-answer"}})
            await asyncio.sleep(.02)
            self.assertFalse(self.channel._answer.done())
            return "fixture-offer"
        self.channel.peer.offer.side_effect = premature_offer
        await self.channel.start()
        self.channel.peer.answer.assert_awaited_once_with("fixture-answer")

    async def test_api_key_account_refused_before_peer_or_live_request(self):
        self.rpc.request.side_effect = None
        self.rpc.request.return_value = {"account":{"type":"apiKey"}}
        with self.assertRaisesRegex(RuntimeError, "OAuth"):
            await self.channel.start()
        self.channel.peer.offer.assert_not_awaited()
        self.rpc.thread.assert_not_awaited()

    async def test_foreign_and_missing_thread_cannot_complete_speech(self):
        await self.channel.start()
        self.channel._speech = asyncio.get_running_loop().create_future()
        self.channel._speech_text = "expected"
        for scope in ["foreign", None]:
            self.rpc.notifications.put_nowait({"method":"thread/realtime/item/completed", "params":{
                "threadId":scope, "item":{"type":"transcriptSegment","role":"assistant","text":"expected"}}})
        await asyncio.sleep(.01)
        self.assertFalse(self.channel._speech.done())
        self.rpc.notifications.put_nowait({"method":"thread/realtime/outputAudio/delta", "params":{
            "threadId":"speech-thread","audio":{"data":"do-not-play","sampleRate":48000,"numChannels":1}}})
        await asyncio.sleep(.01)
        self.assertEqual(self.output.frames, 0)

    async def test_stop_cancels_inflight_speech_and_does_not_replay(self):
        await self.channel.start()
        self.rpc.request.side_effect = AsyncMock(return_value={})
        speech = asyncio.create_task(self.channel.speak("pending"))
        async with asyncio.timeout(1):
            while not self.output.active:
                await asyncio.sleep(.005)
        await self.channel.close()
        with self.assertRaises(asyncio.CancelledError):
            await speech
        self.assertTrue(self.output.closed)

    async def test_invalid_audio_disables_active_output(self):
        await self.channel.start()
        self.output.start_output()
        await self.output.begin_speech("one")
        await self.channel._pcm({"data":"invalid!", "sample_rate":48000,"channels":1})
        self.assertFalse(self.output.enabled)
        self.assertFalse(self.output.active)
        self.assertIsNotNone(self.channel.error)


    async def test_stop_ack_and_closed_are_required_before_cleanup(self):
        await self.channel.start()
        self.rpc.process.returncode = None
        self.channel.stop_timeout = .1
        receiver_was_alive=[]
        async def stop(method, params):
            receiver_was_alive.append(not self.channel.receiver.done())
            self.rpc.notifications.put_nowait({"method":"thread/realtime/closed",
                "params":{"threadId":"speech-thread","reason":"stopped"}})
            return {}
        self.rpc.request.side_effect=stop
        self.assertEqual(await self.channel.close(),"stopped")
        self.assertEqual(receiver_was_alive,[True])
        self.assertIsNone(self.channel.receiver)

    async def test_stop_rejection_reports_unknown(self):
        await self.channel.start()
        self.rpc.process.returncode=None
        self.rpc.request.side_effect=RuntimeError("stop rejected")
        self.assertEqual(await self.channel.close(),"unknown")
        self.channel.peer.close.assert_awaited_once()
        self.rpc.close.assert_awaited_once()


@unittest.skipUnless(shutil.which("chromium"), "Chromium fixture unavailable")
class IncomingTrackTests(unittest.IsolatedAsyncioTestCase):
    async def _install_measured_track(self, peer):
        await peer.page.evaluate("""async () => {
          const ctx = new AudioContext({sampleRate:48000}); await ctx.resume();
          const source = ctx.createOscillator(); source.type = 'sawtooth';
          source.frequency.value = 137;
          const gain = ctx.createGain(); gain.gain.value = .2;
          const destination = ctx.createMediaStreamDestination();
          source.connect(gain); gain.connect(destination); source.start();
          await __live.pc.ontrack({track:destination.stream.getAudioTracks()[0]});
          const node = [...__live.output.nodes][0], deliver = node.processor.onaudioprocess;
          window.fixture = {ctx, source, node, deliver, expected:[], idleCallbacks:0};
          node.processor.onaudioprocess = event => {
            if (__live.output.captureEnabled) {
              const samples = event.inputBuffer.getChannelData(0);
              const bytes = new Uint8Array(samples.length*2), view = new DataView(bytes.buffer);
              for (let i=0;i<samples.length;i++)
                view.setInt16(i*2,Math.max(-32768,Math.min(32767,Math.round(samples[i]*32768))),true);
              fixture.expected.push(btoa(String.fromCharCode(...bytes)));
            } else fixture.idleCallbacks++;
            deliver(event);
          };
        }""")

    async def test_generic_pcm_probe_preserves_waveform_during_renderer_stall(self):
        async with Browser() as runtime:
            for profile in ("legacy", "trusted-none"):
                for slow in (False, True):
                    with self.subTest(profile=profile, slow=slow):
                        received, errors = [], []
                        entered, release = asyncio.Event(), asyncio.Event()
                        if not slow:
                            release.set()
                        sink = AsyncMock()
                        sink.drain.return_value = {"status":"drained", "truncated":False, "reason":None}
                        output = GatedPCMOutput("local-active", sink)
                        output.start_output()
                        await output.begin_speech("local-active")
                        in_flight, max_in_flight = 0, 0
                        async def pcm(packet):
                            nonlocal in_flight, max_in_flight
                            in_flight += 1
                            max_in_flight = max(max_in_flight, in_flight)
                            try:
                                entered.set()
                                await release.wait()
                                received.append(packet["data"])
                                await output.write("local-active", base64.b64decode(packet["data"]),
                                    sample_rate=packet["sample_rate"], channels=packet["channels"])
                            finally:
                                in_flight -= 1
                        guard = ExistingLiveGuard(AsyncMock(return_value=object()), AsyncMock(return_value=True))
                        peer = ResponseLivePeer(runtime.browser, AsyncMock(), pcm,
                            lambda error: self._collect_audio_error(errors, error),
                            guard=guard, meeting_id="local-active", ice_servers=[], media_profile=profile)
                        await peer.bind_session("local-active-thread")
                        try:
                            await peer.offer()
                            await self._install_measured_track(peer)
                            await asyncio.sleep(.15)
                            self.assertGreater(await peer.page.evaluate("fixture.idleCallbacks"), 0)
                            self.assertEqual(received, [])
                            await peer.set_output_capture(True)
                            await asyncio.wait_for(entered.wait(), 2)
                            await asyncio.sleep(.15)
                            await peer.page.evaluate("""() => {
                              const until = performance.now()+650;
                              while (performance.now()<until) {}
                            }""")
                            await asyncio.sleep(.15)
                            expected = await peer.page.evaluate("""() => {
                              fixture.node.processor.onaudioprocess = null;
                              return fixture.expected;
                            }""")
                            self.assertEqual(errors, [], "An admitted renderer burst is not slow-consumer overflow")
                            self.assertGreater(len(expected), 8, "Exercise the old eight-unresolved-binding threshold")
                            release.set()
                            await peer.page.wait_for_function(
                                "__live.output.pcmBridge.queuedPackets === 0", timeout=4000)
                            self.assertEqual(received, expected, "Every admitted PCM block, including its head, stays FIFO")
                            self.assertEqual(max_in_flight, 1)
                            self.assertGreater(max(abs(v[0]) for v in struct.iter_unpack(
                                "<h", b"".join(base64.b64decode(p) for p in received[:5]))), 1000)
                            self.assertEqual([call.args[0] for call in sink.write.await_args_list],
                                [base64.b64decode(packet) for packet in expected])
                            self.assertFalse((await output.drain(timeout=3))["truncated"])
                            self.assertEqual(errors, [])
                        finally:
                            release.set()
                            await peer.close()
                            await output.close()

    @staticmethod
    async def _collect_audio_error(errors, error):
        errors.append(error)

    async def test_slow_binding_overflow_is_audio_bounded_and_explicit(self):
        async with Browser() as runtime:
            for profile in ("legacy", "trusted-none"):
                with self.subTest(profile=profile):
                    entered, release = asyncio.Event(), asyncio.Event()
                    errors, received = [], []
                    async def pcm(packet):
                        received.append(packet)
                        entered.set()
                        await release.wait()
                    guard = ExistingLiveGuard(AsyncMock(return_value=object()), AsyncMock(return_value=True))
                    peer = ResponseLivePeer(runtime.browser, AsyncMock(), pcm,
                        lambda error: self._collect_audio_error(errors, error),
                        guard=guard, meeting_id="local-overflow", ice_servers=[], media_profile=profile)
                    await peer.bind_session("local-overflow-thread")
                    try:
                        await peer.offer()
                        await self._install_measured_track(peer)
                        await peer.page.evaluate("fixture.node.processor.onaudioprocess=null")
                        await peer.set_output_capture(True)
                        state = await peer.page.evaluate("""() => {
                          const out = __live.output, bridge = out.pcmBridge;
                          const samples = new Float32Array(2048).fill(.125);
                          const event = {inputBuffer:{getChannelData:()=>samples}};
                          fixture.deliver(event);
                          for (let i=0;i<bridge.maxPackets;i++) fixture.deliver(event);
                          return {maxBytes:bridge.maxBytes, maxPackets:bridge.maxPackets,
                            queuedBytes:bridge.queuedBytes, queuedPackets:bridge.queuedPackets,
                            failed:bridge.failed, enabled:out.captureEnabled, dropped:out.dropped};
                        }""")
                        await asyncio.wait_for(entered.wait(), 2)
                        async with asyncio.timeout(2):
                            while not errors:
                                await asyncio.sleep(.01)
                        self.assertEqual(state["maxBytes"], 4_320_000)
                        self.assertEqual(state["maxPackets"], math.ceil(4_320_000/(2048*2)))
                        self.assertLessEqual(state["queuedBytes"], state["maxBytes"])
                        self.assertEqual(state["queuedPackets"], 1, "Fail closed discards waiting PCM, not the in-flight binding")
                        self.assertTrue(state["failed"])
                        self.assertFalse(state["enabled"])
                        self.assertEqual(state["dropped"], 1)
                        self.assertEqual(errors, ["output_backpressure"])
                        self.assertEqual(len(received), 1)
                        release.set()
                        await peer.page.wait_for_function("__live.output.pcmBridge.queuedPackets===0")
                        self.assertEqual(len(received), 1)
                    finally:
                        release.set()
                        await peer.close()

    async def test_capture_generation_discards_waiting_waveform_without_decoder_reset(self):
        async with Browser() as runtime:
            for profile in ("legacy", "trusted-none"):
                with self.subTest(profile=profile):
                    entered, release = asyncio.Event(), asyncio.Event()
                    received, errors = [], []
                    async def pcm(packet):
                        received.append((packet["capture_generation"], base64.b64decode(packet["data"])))
                        entered.set()
                        await release.wait()
                    guard = ExistingLiveGuard(AsyncMock(return_value=object()), AsyncMock(return_value=True))
                    peer = ResponseLivePeer(runtime.browser, AsyncMock(), pcm,
                        lambda error: self._collect_audio_error(errors, error),
                        guard=guard, meeting_id="local-generation", ice_servers=[], media_profile=profile)
                    await peer.bind_session("local-generation-thread")
                    try:
                        await peer.offer()
                        await self._install_measured_track(peer)
                        await peer.page.evaluate("fixture.node.processor.onaudioprocess=null")
                        await peer.set_output_capture(True)
                        old_generation = await peer.page.evaluate("""() => {
                          fixture.output = __live.output;
                          for (let i=1;i<=12;i++) fixture.deliver({
                            inputBuffer:{getChannelData:()=>new Float32Array(2048).fill(i/32)}});
                          return __live.output.captureGeneration;
                        }""")
                        await asyncio.wait_for(entered.wait(), 2)
                        await peer.set_output_capture(False)
                        await peer.set_output_capture(True)
                        new_generation = await peer.page.evaluate("""() => {
                          fixture.deliver({inputBuffer:{getChannelData:()=>new Float32Array(2048).fill(.75)}});
                          return __live.output.captureGeneration;
                        }""")
                        await peer.page.evaluate("packet => window.responseAudio(packet)", {
                            "capture_generation":old_generation, "data":base64.b64encode(b"\xff\x7f"*2048).decode(),
                            "sample_rate":48000, "channels":1})
                        release.set()
                        await peer.page.wait_for_function("__live.output.pcmBridge.queuedPackets===0")
                        self.assertEqual(received, [(old_generation, struct.pack("<h", 1024)*2048),
                            (new_generation, struct.pack("<h", 24576)*2048)])
                        self.assertTrue(await peer.page.evaluate(
                            "__live.output===fixture.output && fixture.node.player.srcObject.getAudioTracks()[0].readyState==='live'"))
                        self.assertEqual(errors, [])
                    finally:
                        release.set()
                        await peer.close()

    async def test_native_peer_installs_no_pcm_tap_or_callback(self):
        async with Browser() as runtime:
            for profile in ('legacy', 'trusted-none'):
                guard = ExistingLiveGuard(AsyncMock(return_value=object()), AsyncMock(return_value=True))
                peer = ResponseLivePeer(runtime.browser, AsyncMock(), None, AsyncMock(),
                    guard=guard, meeting_id='native-local', ice_servers=[],
                    media_profile=profile, output_mode='native')
                await peer.bind_session('native-local-thread')
                try:
                    await peer.offer()
                    state = await peer.page.evaluate("""() => ({
                      pcmBinding: typeof window.responseAudio,
                      output: typeof __live.output,
                      receiverCount: __live.pc.getReceivers().length
                    })""")
                    self.assertEqual(state['pcmBinding'], 'undefined')
                    self.assertEqual(state['output'], 'undefined')
                    with self.assertRaisesRegex(RuntimeError, 'no PCM'):
                        await peer.set_output_capture(True)
                finally:
                    await peer.close()

    async def test_idle_decoder_stall_does_not_fail_next_scoped_speech_capture(self):
        sink = AsyncMock()
        output = GatedPCMOutput("local-idle", sink)
        received, errors = [], []
        async def pcm(packet):
            received.append(base64.b64decode(packet["data"]))
            await output.write("local-idle", received[-1],
                sample_rate=packet["sample_rate"], channels=packet["channels"])
        async def on_error(reason):
            errors.append(reason)
        async with Browser() as runtime:
            guard = ExistingLiveGuard(AsyncMock(return_value=object()), AsyncMock(return_value=True))
            peer = ResponseLivePeer(runtime.browser, AsyncMock(), pcm, on_error,
                guard=guard, meeting_id="local-idle", ice_servers=[])
            await peer.bind_session("idle-thread")
            try:
                await peer.offer()
                await peer.page.evaluate("""async () => {
                  const ctx = new AudioContext({sampleRate:48000}); await ctx.resume();
                  const oscillator = ctx.createOscillator(), gain = ctx.createGain();
                  gain.gain.value = .2;
                  const destination = ctx.createMediaStreamDestination();
                  oscillator.connect(gain); gain.connect(destination); oscillator.start();
                  window.fixture = {ctx, oscillator};
                  await __live.pc.ontrack({track:destination.stream.getAudioTracks()[0]});
                }""")
                await asyncio.sleep(.2)
                await peer.page.evaluate("""() => {
                  const until = performance.now()+650;
                  while (performance.now()<until) {}
                }""")
                await asyncio.sleep(.2)
                self.assertEqual(errors, [], "Idle callback bursts must not permanently fail B")
                self.assertEqual(received, [], "Unadmitted audio must stay in the native browser graph")
                await output.begin_speech("local-idle")
                await peer.set_output_capture(True)
                previous_generation = await peer.page.evaluate("__live.output.captureGeneration")
                await asyncio.sleep(.3)
                self.assertGreater(output.activity_frames, 0)
                samples = struct.unpack("<"+"h"*(len(received[-1])//2), received[-1])
                self.assertGreater(max(samples), 1000)
                sink.write.assert_not_awaited()  # The ordinary muted-output gate remains intact.
                await peer.set_output_capture(False)
                await output.end_speech()
                count = len(received)
                await asyncio.sleep(.2)
                self.assertEqual(len(received), count)
                self.assertEqual(errors, [])
                await output.begin_speech("local-idle")
                await peer.set_output_capture(True)
                stale_audio = struct.pack("<h", 30000) * 2048
                await peer.page.evaluate("packet => window.responseAudio(packet)", {
                    "data":base64.b64encode(stale_audio).decode(), "sample_rate":48000,
                    "channels":1, "capture_generation":previous_generation})
                await asyncio.sleep(.2)
                self.assertNotIn(stale_audio, received)
                self.assertGreater(output.activity_frames, 0)
            finally:
                await output.close()
                if peer.page:
                    await peer.page.evaluate("async()=>{fixture.oscillator.stop();await fixture.ctx.close();}")
                await peer.close()

    async def test_synthetic_track_to_48k_pcm_default_mute_then_opt_in_fake_sink(self):
        sink = AsyncMock()
        sink.drain.return_value = {"status":"drained", "truncated":False, "reason":None}
        output = GatedPCMOutput("one", sink)
        errors = []
        async def pcm(source, packet):
            await output.write("one", base64.b64decode(packet["data"]),
                sample_rate=packet["sample_rate"], channels=packet["channels"])
        async with Browser() as runtime:
            context = await runtime.browser.new_context()
            await context.expose_binding("responseAudio", pcm)
            await context.expose_binding("responseAudioError", lambda source, error: errors.append(error))
            page = await context.new_page()
            await page.evaluate("window.__live = {pc:{}}")
            await page.evaluate(CAPTURE_OUTPUT_JS)
            await output.begin_speech("one")
            await page.evaluate("__live.output.captureEnabled=true;__live.output.captureGeneration=1")
            await page.evaluate("""async () => {
              const ctx = new AudioContext({sampleRate:48000}); await ctx.resume();
              const oscillator=ctx.createOscillator(), gain=ctx.createGain(); gain.gain.value=.2;
              const destination=ctx.createMediaStreamDestination();
              oscillator.connect(gain); gain.connect(destination); oscillator.start();
              window.fixture={ctx,oscillator,destination};
              await __live.pc.ontrack({track:destination.stream.getAudioTracks()[0]});
            }""")
            try:
                await asyncio.sleep(.3)
                self.assertGreater(output.nonzero_frames, 0)
                sink.write.assert_not_awaited()
                muted = await page.evaluate("[...__live.output.nodes].every(n => n.player.muted && n.mute.gain.value===0)")
                self.assertTrue(muted)
                await output.end_speech()
                output.start_output()
                await output.begin_speech("one")
                await asyncio.sleep(.3)
                self.assertGreater(sink.write.await_count, 0)
                data = b"".join(c.args[0] for c in sink.write.await_args_list)
                samples = struct.unpack("<" + "h"*(len(data)//2), data)
                self.assertGreater(max(samples), 1000)
                self.assertLess(max(samples), 10000)
                self.assertEqual(errors, [])
                self.assertEqual(await page.evaluate("__live.output.dropped"), 0)
            finally:
                await output.close()
                await page.evaluate("async () => { fixture.oscillator.stop(); await fixture.ctx.close(); await __live.output.ctx.close(); }")
                await context.close()

    async def test_input_keeps_real_silent_samples_between_utterances_on_same_track(self):
        from test_native_meeting_media import RECEIVER_JS

        async with Browser() as runtime:
            # Only a local RTC receiver is admitted; no provider or external ICE.
            guard = ExistingLiveGuard(AsyncMock(return_value=object()),
                AsyncMock(return_value=True))
            peer = ResponseLivePeer(runtime.browser, AsyncMock(), None,
                AsyncMock(), guard=guard, meeting_id="local-input", ice_servers=[],
                output_mode="native")
            receiver_context = await runtime.browser.new_context(permissions=[])
            receiver = await receiver_context.new_page()
            try:
                await peer.bind_session("local-input-thread")
                offer = await peer.offer()
                answer = await receiver.evaluate(RECEIVER_JS, offer)
                await peer.answer(answer)
                await peer.wait_connected(timeout=5)
                pcm = struct.pack("<" + "h" * 7200,
                    *(int(5000 * math.sin(2 * math.pi * 440 * i / 24000))
                      for i in range(7200)))
                await peer.push(pcm)
                await asyncio.sleep(.7)

                async def sample():
                    source = await peer.page.evaluate("""async () => {
                      const stats = await __live.pc.getStats();
                      for (const row of stats.values())
                        if (row.type === 'media-source' && row.kind === 'audio')
                          return row.totalSamplesDuration;
                    }""")
                    received = await receiver.evaluate("""async () => {
                      for (const row of (await fixtureReceiver.pc.getStats()).values())
                        if (row.type === 'inbound-rtp' && row.kind === 'audio')
                          return {concealed:row.concealedSamples, packets:row.packetsReceived};
                    }""")
                    return source, received

                source_before, received_before = await sample()
                await asyncio.sleep(2)
                source_after, received_after = await sample()
                # Decoder concealment also advances its clock when input stalls.
                # Require actual source samples and packets, not synthetic quiet.
                self.assertGreaterEqual(source_after - source_before, 1.6)
                self.assertGreaterEqual(received_after["packets"] - received_before["packets"], 60)
                self.assertLess(received_after["concealed"] - received_before["concealed"], 9600)
                self.assertEqual(await receiver.evaluate("fixtureReceiver.sample().rms"), 0)
                await peer.page.evaluate(
                    "__live.continuityTrack = __live.pc.getSenders()[0].track")
                await receiver.evaluate("fixtureReceiver.beginToneObservation(0, .8)")
                await peer.push(pcm)
                observed = await receiver.evaluate("fixtureReceiver.toneObservation")
                self.assertGreater(observed["maximum"], -35)
                self.assertTrue(await peer.page.evaluate(
                    "__live.continuityTrack === __live.pc.getSenders()[0].track"))
            finally:
                await peer.close()
                await receiver_context.close()


@unittest.skipUnless(shutil.which("codex"), "Installed Codex schema unavailable")
class InstalledProtocolTests(unittest.TestCase):
    def test_all_used_rpc_shapes_exist_in_installed_schema(self):
        import jsonschema
        with tempfile.TemporaryDirectory() as directory:
            subprocess.run(["codex", "app-server", "generate-json-schema", "--experimental", "--out", directory],
                check=True, capture_output=True, timeout=30)
            bundle = json.loads((Path(directory)/"codex_app_server_protocol.schemas.json").read_text())
        examples = {
            "ThreadRealtimeStartParams": {"threadId":"fixture", "model":"gpt-live-1-codex", "version":"v3",
                "outputModality":"audio", "clientManagedHandoffs":True, "includeStartupContext":False,
                "delegationAckFiller":False, "prompt":"fixture", "transport":{"type":"webrtc","sdp":"fixture"}},
            "ThreadRealtimeAppendSpeechParams":{"threadId":"fixture", "text":"fixture"},
            "ThreadRealtimeOutputAudioDeltaNotification":{"threadId":"fixture", "audio":{
                "data":"AA==", "sampleRate":48000, "numChannels":1}},
            "ThreadRealtimeStopParams":{"threadId":"fixture"},
            "ThreadRealtimeTranscriptDeltaNotification":{"threadId":"fixture","role":"user","delta":"小白小白"},
            "ThreadRealtimeTranscriptDoneNotification":{"threadId":"fixture","role":"assistant","text":"我在，有什麼問題？"},
            "ThreadRealtimeStartedNotification":{"threadId":"fixture","version":"v3","realtimeSessionId":"live-fixture"},
            "ThreadRealtimeItemStartedNotification":{"threadId":"fixture","item":{
                "id":"input-fixture","realtimeSessionId":"live-fixture","type":"transcriptSegment","role":"user","text":""}},
            "ThreadRealtimeItemTranscriptDeltaNotification":{"threadId":"fixture","itemId":"input-fixture","delta":"小白小白"},
            "ThreadRealtimeItemCompletedNotification":{"threadId":"fixture","item":{
                "id":"input-fixture","realtimeSessionId":"live-fixture","type":"transcriptSegment","role":"user","text":"小白小白"}},
            "ThreadRealtimeItemAddedNotification":{"threadId":"fixture","item":{"type":"handoff_request",
                "handoff_id":"id","item_id":"id","input_transcript":"q","active_transcript":[]}}}
        for name, example in examples.items():
            jsonschema.validate(example, {"$ref":"#/definitions/v2/"+name, "definitions":bundle["definitions"]})
