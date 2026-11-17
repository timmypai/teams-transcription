"""Local native media behavior; no Teams, model, microphone or external ICE."""
import asyncio
import base64
import json
import math
import struct
import time
import unittest
from unittest.mock import patch
from playwright.async_api import Error as PlaywrightError

from teams_transcription.browser import Browser, LivePeer, MeetingBrowser


RECEIVER_JS = r"""async sdp => {
  const ctx = new AudioContext(); await ctx.resume();
  const pc = new RTCPeerConnection({iceServers:[]});
  const analyser = ctx.createAnalyser(); analyser.fftSize = 8192;
  analyser.smoothingTimeConstant = 0;
  const mute = ctx.createGain(); mute.gain.value = 0;
  analyser.connect(mute); mute.connect(ctx.destination);
  const players = [], sources = [];
  pc.ontrack = async ({track}) => {
    const stream = new MediaStream([track]);
    const player = document.createElement('audio'); player.muted = true;
    player.srcObject = stream; players.push(player); await player.play();
    const source = ctx.createMediaStreamSource(stream);
    source.connect(analyser); sources.push(source);
  };
  pc.ondatachannel = ({channel}) => { window.fixtureReceiver.channel = channel; };
  window.fixtureReceiver = {ctx,pc,analyser,players,sources,
    sample() {
      const data = new Float32Array(analyser.fftSize);
      analyser.getFloatTimeDomainData(data);
      const bins = new Float32Array(analyser.frequencyBinCount);
      analyser.getFloatFrequencyData(bins);
      const level = hz => {
        const bin = Math.round(hz * analyser.fftSize / ctx.sampleRate);
        return Math.max(...bins.slice(bin-2,bin+3));
      };
      return {rms:Math.sqrt(data.reduce((sum,x)=>sum+x*x,0)/data.length),
        tones:[440,880,1320,1900].map(level)};
    }};
  window.fixtureReceiver.beginToneObservation = (index, seconds) => {
    const processor = ctx.createScriptProcessor(2048,1,1);
    const requiredFrames = Math.ceil(ctx.sampleRate*seconds);
    let frames=0, blocks=0, maximum=-Infinity;
    window.fixtureReceiver.toneObservation = new Promise(resolve => {
      processor.onaudioprocess = event => {
        maximum=Math.max(maximum,window.fixtureReceiver.sample().tones[index]);
        frames+=event.inputBuffer.length; blocks++;
        if (frames>=requiredFrames) {
          processor.onaudioprocess=null;
          analyser.disconnect(processor); processor.disconnect();
          resolve({maximum,frames,blocks,required_frames:requiredFrames});
        }
      };
    });
    analyser.connect(processor); processor.connect(mute);
    return true;
  };
  await pc.setRemoteDescription({type:'offer',sdp});
  await pc.setLocalDescription(await pc.createAnswer());
  await new Promise((resolve,reject) => {
    if (pc.iceGatheringState === 'complete') return resolve();
    const timer=setTimeout(()=>reject(new Error('fixture_ice_timeout')),5000);
    pc.addEventListener('icegatheringstatechange',()=> {
      if (pc.iceGatheringState === 'complete') { clearTimeout(timer); resolve(); }
    });
  });
  return pc.localDescription.sdp;
}"""

async def create_synthetic_meeting(browser, on_audio, event):
    """Admitted local capture page for native-media controls, never a Teams join."""
    meeting = MeetingBrowser(browser, on_audio, event, defer_capture=True)
    await meeting.open()
    await meeting.page.set_content('<button>Leave</button><button>Participants</button>')
    return meeting


async def add_synthetic_tone(frame, frequency):
    await frame.evaluate("""async frequency => {
      const ctx = new AudioContext(); await ctx.resume();
      const tone=ctx.createOscillator(), gain=ctx.createGain();
      gain.gain.value=.2; tone.frequency.value=frequency;
      const destination=ctx.createMediaStreamDestination();
      tone.connect(gain); gain.connect(destination); tone.start();
      const analyser=ctx.createAnalyser(); analyser.fftSize=8192;
      analyser.smoothingTimeConstant=0;
      const mute=ctx.createGain(); mute.gain.value=0;
      gain.connect(analyser); analyser.connect(mute); mute.connect(ctx.destination);
      const fixture={ctx,tone,gain,destination,analyser,level(hz) {
        const bins=new Float32Array(analyser.frequencyBinCount);
        analyser.getFloatFrequencyData(bins);
        const bin=Math.round(hz*analyser.fftSize/ctx.sampleRate);
        return Math.max(...bins.slice(bin-2,bin+3));
      }};
      (window.fixtureTones ||= []).push(fixture);
      await __meeting.attach(destination.stream.getAudioTracks()[0]);
    }""", frequency)



class NativeMeetingMediaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = Browser()
        await self.runtime.__aenter__()
        self.observer_release = asyncio.Event()
        self.observer_release.set()
        self.observed = 0
        self.observed_documents = {}
        self.observed_frames = 0
        self.observed_gaps = 0
        self.observed_next = None

        async def audio(packet):
            self.observed += 1
            document = packet.get('source_document')
            if document is not None and packet.get('source_clock') == 'monotonic':
                self.observed_documents[document] = packet
                if self.observed_next is not None and packet['start_frame'] != self.observed_next:
                    self.observed_gaps += 1
                self.observed_next = packet['start_frame'] + packet['frames']
                self.observed_frames += packet['frames']
            await self.observer_release.wait()

        async def event(kind, value=None):
            pass

        self.meeting = await create_synthetic_meeting(self.runtime.browser, audio, event)
        self.peer = LivePeer(self.runtime.browser, event, media_source=self.meeting)
        self.receiver_context = await self.runtime.browser.new_context(permissions=[])
        self.receiver = await self.receiver_context.new_page()

    async def asyncTearDown(self):
        self.observer_release.set()
        try:
            await self.peer.close()
        finally:
            try:
                await self.meeting.close()
            finally:
                await self.receiver_context.close()
                await self.runtime.__aexit__()

    async def add_tone(self, frame, frequency):
        await add_synthetic_tone(frame, frequency)

    async def connect_receiver(self):
        offer = await self.peer.offer()
        answer = await self.receiver.evaluate(RECEIVER_JS, offer)
        await self.peer.answer(answer)
        await self.peer.wait_connected(timeout=10)

    async def assert_tone(self, index, present=True):
        predicate = (f"fixtureReceiver.sample().tones[{index}] > -35" if present else
                     f"fixtureReceiver.sample().tones[{index}] < -50")
        await self.receiver.wait_for_function(predicate, timeout=5000)
        return await self.receiver.evaluate("fixtureReceiver.sample()")

    async def assert_mixed_observation(self, *frequencies):
        async def observed():
            while True:
                clock = self.meeting._native_media._observation_clock
                packet = self.observed_documents.get(clock['document'])
                if packet:
                    self.assertEqual(packet.get('source_clock'), 'monotonic')
                    self.assertEqual(packet['capture_clock_id'], clock['id'])
                    self.assertAlmostEqual(packet['source_ended_at']-packet['source_started_at'],
                        packet['frames']/packet['sample_rate'], places=6)
                    samples = [sample/32768 for (sample,) in struct.iter_unpack(
                        '<h', base64.b64decode(packet['data']))]
                    def amplitude(hz):
                        real = sum(value*math.cos(2*math.pi*hz*n/24000) for n,value in enumerate(samples))
                        imag = sum(value*math.sin(2*math.pi*hz*n/24000) for n,value in enumerate(samples))
                        return 2*math.hypot(real,imag)/len(samples)
                    if all(amplitude(hz) > .03 for hz in frequencies):
                        return packet
                await asyncio.sleep(.01)
        return await asyncio.wait_for(observed(), 3)

    async def test_waveform_is_capture_gated_and_never_uses_pcm_push(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await asyncio.sleep(.25)
        silent = await self.receiver.evaluate("fixtureReceiver.sample()")
        self.assertLess(silent['rms'], .003)
        self.assertEqual(self.observed, 0)
        with self.assertRaisesRegex(RuntimeError, 'native_media'):
            await self.peer.push(b'\x00\x00' * 2048)
        await self.meeting.start_capture(timeout=3)
        signal = await self.assert_tone(0)
        self.assertGreater(signal['rms'], .03)
        self.assertLess(signal['tones'][3], signal['tones'][0] - 20)
        stats = await self.peer.stats()
        self.assertEqual(stats['input_mode'], 'native_media')
        self.assertIsNone(stats['sent_frames'])
        self.assertIsNone(stats['scheduled_queue_ms'])
        self.assertTrue(stats['native_source']['ready'])
        await self.peer.page.evaluate("""() => {
          window.fixtureRelayPeers=[...__live.nativeRelays.values()].map(r=>r.pc);
          window.fixtureRelayTracks=[...__live.nativeRelays.values()].flatMap(r=>[...r.tracks]);
        }""")
        await self.meeting.stop_capture()
        source = await self.meeting.page.evaluate("__meeting.nativeReadiness(null)")
        self.assertFalse(source['source_live'])
        self.assertFalse(source['capture_enabled'])
        self.assertFalse(source['context_running'])
        self.assertTrue(await self.peer.page.evaluate("""() =>
          fixtureRelayPeers.every(pc=>pc.connectionState==='closed') &&
          fixtureRelayTracks.every(track=>track.readyState==='ended') &&
          __live.nativeRelays.size===0"""))
        # RTP/Opus may render a finite pre-stop tail or comfort noise. Prove
        # cutoff using a new, positively observed input waveform instead of
        # demanding instantaneous all-band decoder silence.
        await self.meeting.page.evaluate("() => { fixtureTones[0].tone.frequency.value=1900; }")
        await self.meeting.page.wait_for_function("fixtureTones[0].level(1900)>-35", timeout=3000)
        self.assertTrue(await self.meeting.page.evaluate("""() =>
          fixtureTones[0].ctx.state==='running' &&
          fixtureTones[0].destination.stream.getAudioTracks()[0].readyState==='live'"""))
        await self.receiver.evaluate("() => fixtureReceiver.beginToneObservation(3,2)")
        observed = await asyncio.wait_for(self.receiver.evaluate(
            "() => fixtureReceiver.toneObservation"), 5)
        self.assertGreater(observed['blocks'], 1)
        self.assertGreaterEqual(observed['frames'], observed['required_frames'])
        self.assertLess(observed['maximum'], -50)
        await self.assert_tone(0, present=False)
        stopped = (await self.peer.stats())['native_source']
        self.assertTrue(stopped['stopped'])
        self.assertFalse(stopped['failed'])
        self.assertFalse(stopped['ready'])
        self.assertNotIn('sdp', json.dumps(stats).lower())

    async def test_native_signal_survives_observer_backpressure_beyond_old_queue_horizon(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        self.observer_release.clear()
        await self.meeting.start_capture(timeout=3)
        before = (await self.peer.stats())['native_source']
        # The old capacity-256 PCM path failed around 21.8 seconds. Keep the
        # real observer promises unresolved beyond that interval, not merely
        # one packet, and inspect the independently decoded waveform each time.
        for _ in range(5):
            await asyncio.sleep(4.5)
            signal = await self.assert_tone(0)
            self.assertGreater(signal['rms'], .03)
        observer = await self.meeting._native_media.observation_stats()
        self.assertEqual(observer['pending'], 16)
        self.assertGreater(observer['dropped'], 0)
        self.assertLessEqual(self.observed, 16)
        after = (await self.peer.stats())['native_source']
        self.assertTrue(after['ready'])
        self.assertFalse(after['failed'])
        self.assertGreater(after['packets_received'], before['packets_received'])

    async def test_slow_initial_calibration_retries_before_observation_ready(self):
        frame = self.meeting.page.main_frame
        await self.add_tone(frame, 440)
        await self.connect_receiver()
        evaluate = self.peer.page.evaluate
        samples = 0
        async def delayed_sample(expression, *args, **kwargs):
            nonlocal samples
            if 'now:performance.now()' in expression:
                samples += 1
                if samples == 1:
                    await asyncio.sleep(.12)
            return await evaluate(expression, *args, **kwargs)
        with patch.object(self.peer.page, 'evaluate', side_effect=delayed_sample):
            report = await self.meeting.start_capture(timeout=3)
        self.assertGreaterEqual(samples, 2)
        self.assertTrue(report['observation_ready'])
        await self.assert_mixed_observation(440)
        await self.assert_tone(0)

    async def test_unavailable_clock_is_explicit_without_stopping_native_A(self):
        frame = self.meeting.page.main_frame
        await self.add_tone(frame, 440)
        await self.connect_receiver()
        evaluate = self.peer.page.evaluate
        async def unavailable_sample(expression, *args, **kwargs):
            result = await evaluate(expression, *args, **kwargs)
            if 'now:performance.now()' in expression:
                result['now'] = None
            return result
        with patch.object(self.peer.page, 'evaluate', side_effect=unavailable_sample):
            report = await self.meeting.start_capture(timeout=.4)
        self.assertTrue(report['ready'])
        self.assertFalse(report['observation_ready'])
        self.assertFalse(self.meeting.native_audio_failed)
        self.assertIsNone(self.meeting._native_media._observation_clock['offset'])
        self.assertEqual(self.observed, 0)
        await self.assert_tone(0)

    async def test_late_sources_reuse_the_single_mixed_observation_clock(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_mixed_observation(440)
        clock = self.meeting._native_media._observation_clock
        await self.meeting.page.evaluate("() => document.body.appendChild(document.createElement('iframe'))")
        child = self.meeting.page.frames[1]
        evaluate = child.evaluate
        async def no_source_calibration(expression, *args, **kwargs):
            if 'now:performance.now()' in expression:
                raise AssertionError('Native B must not calibrate a second document clock')
            return await evaluate(expression, *args, **kwargs)
        with patch.object(child, 'evaluate', side_effect=no_source_calibration):
            await self.add_tone(child, 880)
            await self.assert_mixed_observation(440, 880)
        self.assertIs(self.meeting._native_media._observation_clock, clock)
        self.assertEqual(len(self.observed_documents), 1)
        self.assertFalse(self.meeting.capture_clocks)
        report = await self.meeting.native_audio_readiness()
        self.assertTrue(report['ready'])
        self.assertTrue(report['observation_ready'])
        self.assertFalse(report['failed'])

    async def test_mixed_observation_binds_target_owner_and_stops_before_cleanup(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_mixed_observation(440)
        media = self.meeting._native_media
        now = await self.peer.page.evaluate('() => performance.now()')
        packet = dict(data='AAA=', sample_rate=24000, frames=1, start_frame=0,
            capture_clock_id=media._target_owner, capture_start_ms=now-1/24,
            capture_end_ms=now, source_document='page-forged', source_clock='monotonic',
            source_started_at=0, source_ended_at=1, fixture_probe=True)
        probes = []
        on_audio = self.meeting.on_audio
        async def observe(value):
            if value.get('fixture_probe'):
                probes.append(value)
            else:
                await on_audio(value)
        foreign = await self.peer.context.new_page()
        try:
            with patch.object(self.meeting, 'on_audio', new=observe):
                await foreign.evaluate('p => nativeMeetingObservation(p)', packet)
                self.assertEqual(probes, [])
                with self.assertRaisesRegex(PlaywrightError, 'native_observation_packet_unavailable'):
                    await self.peer.page.evaluate('p => nativeMeetingObservation(p)',
                        {**packet, 'capture_clock_id':'foreign-generation'})
                self.assertEqual(probes, [])
                await self.peer.page.evaluate("() => { __live.nativeOwner='foreign-owner'; }")
                try:
                    await self.peer.page.evaluate('p => nativeMeetingObservation(p)', packet)
                    self.assertEqual(probes, [])
                finally:
                    await self.peer.page.evaluate('owner => { __live.nativeOwner=owner; }', media._target_owner)
                await self.peer.page.evaluate('p => nativeMeetingObservation(p)', packet)
                self.assertEqual(len(probes), 1)
                self.assertIs(probes[0]['source_document'], media._observation_clock['document'])
                self.assertNotEqual(probes[0]['source_started_at'], 0)
                await self.meeting.stop_capture()
                await self.peer.page.evaluate('p => nativeMeetingObservation(p)', packet)
                self.assertEqual(len(probes), 1)
            self.assertTrue(await self.peer.page.evaluate('() => __live.nativeObservation.closed'))
            self.assertEqual(await self.peer.page.evaluate(
                '() => __live.destination.stream.getAudioTracks()[0].readyState'), 'live')
        finally:
            await foreign.close()

    async def test_all_frames_and_dynamic_tracks_mix_into_one_model_sender(self):
        await self.meeting.page.set_content('<button>Leave</button><button>Chat</button><iframe></iframe>')
        main, child = self.meeting.page.frames
        await self.add_tone(main, 440)
        await self.add_tone(child, 880)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        signal = await self.assert_tone(1)
        await self.assert_mixed_observation(440, 880)
        self.assertEqual(len(self.observed_documents), 1)
        before_frames, before_time = self.observed_frames, time.monotonic()
        await asyncio.sleep(.6)
        self.assertLessEqual(self.observed_frames-before_frames,
            int((time.monotonic()-before_time)*24000)+4096)
        self.assertEqual(self.observed_gaps, 0)
        for frame in (main, child):
            self.assertEqual(await frame.evaluate('() => __meeting.packets'), 0)
        self.assertGreater(signal['tones'][0], -35)
        native = (await self.peer.stats())['native_source']
        self.assertEqual(native['frame_count'], 2)
        self.assertEqual(native['connected_frames'], 2)
        self.assertEqual(native['live_tracks'], 2)
        await self.add_tone(main, 1320)
        signal = await self.assert_tone(2)
        self.assertTrue(all(level > -35 for level in signal['tones'][:3]))
        self.assertEqual((await self.meeting.native_audio_readiness())['live_tracks'], 3)
        self.assertEqual(await self.peer.page.evaluate("__live.pc.getSenders().filter(s=>s.track?.kind==='audio').length"), 1)
        # Neither local source relay is eligible for B's Teams sender scan.
        for frame in (main, child):
            self.assertEqual(await frame.evaluate("__meeting.peers.length"), 0)
        await main.evaluate("() => { fixtureTones[0].destination.stream.getTracks().forEach(t=>t.stop()); }")
        await self.assert_tone(0, present=False)
        signal = await self.assert_tone(1)
        self.assertGreater(signal['tones'][2], -35)
        self.assertTrue((await self.meeting.native_audio_readiness())['ready'])

    async def test_empty_ui_frames_are_ignored_and_new_audio_frames_join_the_native_mix(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.meeting.page.evaluate("() => document.body.appendChild(document.createElement('iframe'))")
        child = self.meeting.page.frames[1]
        await child.goto('data:text/html,<p>Unrelated meeting UI</p>')
        report = await self.meeting.native_audio_readiness()
        self.assertFalse(self.meeting.native_audio_failed)
        self.assertTrue(report['ready'])
        self.assertEqual(report['frame_count'], 1)
        await self.assert_tone(0)
        await self.add_tone(child, 880)
        signal = await self.assert_tone(1)
        self.assertGreater(signal['tones'][0], -35)
        report = await self.meeting.native_audio_readiness()
        self.assertFalse(report['failed'])
        self.assertEqual(report['frame_count'], 2)
        self.assertEqual(report['connected_frames'], 2)
        await self.assert_mixed_observation(440, 880)


    async def test_detached_owned_capture_frame_closes_every_remaining_relay(self):
        await self.meeting.page.set_content('<button>Leave</button><button>Chat</button><iframe></iframe>')
        main, child = self.meeting.page.frames
        await self.add_tone(main, 440)
        await self.add_tone(child, 880)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.assert_tone(1)
        await self.peer.page.evaluate("() => { window.fixtureRelayPeers=[...__live.nativeRelays.values()].map(r=>r.pc); }")
        await self.meeting.page.evaluate("() => document.querySelector('iframe').remove()")
        self.assertTrue((await self.meeting.native_audio_readiness())['failed'])
        await self.assert_tone(0, present=False)
        await self.assert_tone(1, present=False)
        await self.peer.page.wait_for_function("fixtureRelayPeers.every(pc=>pc.connectionState==='closed')", timeout=3000)

    async def test_cancelling_stop_caller_does_not_cancel_owned_cleanup(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.peer.page.evaluate("""() => {
          window.fixtureRelayPeers=[...__live.nativeRelays.values()].map(r=>r.pc);
          window.fixtureRelayTracks=[...__live.nativeRelays.values()].flatMap(r=>[...r.tracks]);
        }""")
        stopping = asyncio.create_task(self.meeting.stop_capture())
        await asyncio.sleep(0)
        stopping.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        await self.meeting.stop_capture()
        await self.assert_tone(0, present=False)
        self.assertTrue(await self.peer.page.evaluate("""() =>
          fixtureRelayPeers.every(pc=>pc.connectionState==='closed') &&
          fixtureRelayTracks.every(track=>track.readyState==='ended') &&
          __live.nativeRelays.size===0"""))
        source = await self.meeting.page.evaluate("__meeting.nativeReadiness(null)")
        self.assertFalse(source['source_live'])
        self.assertFalse(source['capture_enabled'])
        self.assertFalse(source['context_running'])
        await self.meeting.close()
        report = (await self.peer.stats())['native_source']
        self.assertTrue(report['stopped'])
        self.assertFalse(report['failed'])
        self.assertEqual(report['cleanup_status'], 'cleaned')

    async def test_partial_connect_releases_source_peer_and_keeps_unknown_cleanup_visible(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        self.peer = LivePeer(self.runtime.browser, lambda event: asyncio.sleep(0))
        await self.peer.offer()
        # A real peer with the wrong input mode rejects the receiver only
        # after source offer allocation. No fake PC or forwarded echo is used.
        with self.assertRaises(RuntimeError):
            await self.meeting.connect_native_audio(self.peer.page)
        report = await self.meeting.native_audio_readiness()
        self.assertTrue(report['failed'])
        self.assertEqual(report['cleanup_status'], 'unknown')
        await self.peer.close()
        await self.meeting.disconnect_native_audio(self.peer.page)
        report = await self.meeting.native_audio_readiness()
        self.assertTrue(report['failed'])
        self.assertTrue(report['stopped'])
        self.assertEqual(report['cleanup_status'], 'cleaned')


    async def test_target_scope_mismatch_cannot_disconnect_owned_source(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        with self.assertRaisesRegex(RuntimeError, 'scope_mismatch'):
            await self.meeting.disconnect_native_audio(self.receiver)
        self.assertTrue((await self.meeting.native_audio_readiness())['ready'])
        await self.assert_tone(0)

    async def test_target_document_loss_gates_source_without_waiting_for_observer(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.peer.page.close()
        self.assertTrue(self.meeting.native_audio_failed)
        report = await self.meeting.native_audio_readiness()
        self.assertTrue(report['failed'])
        self.assertFalse(report['ready'])
        await self.assert_tone(0, present=False)
        # Cleanup still belongs to the source owner after the target vanished.
        await self.meeting.stop_capture()
        source = await self.meeting.page.evaluate("__meeting.nativeReadiness(null)")
        self.assertFalse(source['source_live'])
        self.assertEqual((await self.meeting.native_audio_readiness())['cleanup_status'], 'cleaned')

    async def test_pcm_adapter_still_delivers_its_actual_scheduled_waveform(self):
        self.peer = LivePeer(self.runtime.browser, lambda event: asyncio.sleep(0))
        await self.connect_receiver()
        frames = 36000
        samples = (round(6500 * math.sin(2*math.pi*440*i/24000)) for i in range(frames))
        await self.peer.push(struct.pack('<'+'h'*frames, *samples))
        signal = await self.assert_tone(0)
        self.assertGreater(signal['rms'], .03)
        self.assertLess(signal['tones'][3], signal['tones'][0]-20)
        stats = await self.peer.stats()
        self.assertEqual(stats['input_mode'], 'pcm')
        self.assertEqual(stats['sent_frames'], frames)
        self.assertNotIn('native_source', stats)

    async def test_navigation_of_owned_frame_invalidates_the_whole_native_mix(self):
        await self.meeting.page.set_content('<button>Leave</button><button>Chat</button><iframe></iframe>')
        main, child = self.meeting.page.frames
        await self.add_tone(main, 440)
        await self.add_tone(child, 880)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.assert_tone(1)
        await self.peer.page.evaluate("() => { window.fixtureRelayPeers=[...__live.nativeRelays.values()].map(r=>r.pc); }")
        await child.goto('data:text/html,<p>Replacement capture document</p>')
        report = await self.meeting.native_audio_readiness()
        self.assertTrue(report['failed'])
        self.assertFalse(report['ready'])
        await self.assert_tone(0, present=False)
        await self.assert_tone(1, present=False)
        await self.peer.page.wait_for_function("fixtureRelayPeers.every(pc=>pc.connectionState==='closed')", timeout=3000)

    async def test_ended_input_track_is_not_terminal_and_a_new_track_flows_naturally(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.meeting.page.evaluate("() => fixtureTones[0].destination.stream.getTracks().forEach(t=>t.stop())")
        await self.assert_tone(0, present=False)
        report = await self.meeting.native_audio_readiness()
        self.assertFalse(report['failed'])
        self.assertFalse(report['ready'])
        self.assertFalse(self.meeting.native_audio_failed)
        await self.add_tone(self.meeting.page.main_frame, 880)
        await self.assert_tone(1)
        self.assertFalse(self.meeting.native_audio_failed)

    async def test_suspended_graph_is_transient_but_closed_native_graph_latches_failure(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.peer.page.evaluate("() => __live.ctx.suspend()")
        report = await self.meeting.native_audio_readiness()
        self.assertFalse(report['ready'])
        self.assertFalse(report['failed'])
        self.assertFalse(self.meeting.native_audio_failed)
        await self.peer.page.evaluate("() => __live.ctx.resume()")
        await self.assert_tone(0)
        self.observer_release.clear()
        await self.peer.page.evaluate("() => __live.ctx.close()")
        await self.assert_tone(0, present=False)
        self.assertTrue(self.meeting.native_audio_failed)

    async def test_unavailable_snapshot_does_not_turn_a_healthy_native_stream_into_failure(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        busy_started = asyncio.Event()
        await self.peer.context.expose_binding('fixtureBusy', lambda source: busy_started.set())
        blocking = asyncio.create_task(self.peer.page.evaluate("""() => {
          window.fixtureBusy();
          const until=performance.now()+6000;
          while (performance.now()<until) {}
        }"""))
        try:
            await asyncio.wait_for(busy_started.wait(), 2)
            report = await self.meeting.native_audio_readiness()
            self.assertFalse(report['available'])
            self.assertFalse(report['failed'])
            self.assertFalse(self.meeting.native_audio_failed)
        finally:
            await blocking
        await self.assert_tone(0)
        self.assertTrue((await self.meeting.native_audio_readiness())['ready'])

    async def test_unknown_source_gate_is_recovered_only_by_owned_context_disposal(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.meeting.page.evaluate("() => { window.__meeting=undefined; }")
        with self.assertRaisesRegex(RuntimeError, 'cleanup_unconfirmed'):
            await self.meeting.stop_capture()
        report = await self.meeting.native_audio_readiness()
        self.assertEqual(report['cleanup_status'], 'unknown')
        await self.meeting.close()
        report = await self.meeting.native_audio_readiness()
        self.assertTrue(report['failed'])
        self.assertEqual(report['cleanup_status'], 'cleaned')

    async def test_new_audio_frame_can_connect_while_target_graph_is_suspended(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.peer.page.evaluate("() => __live.ctx.suspend()")
        await self.meeting.page.evaluate("() => document.body.appendChild(document.createElement('iframe'))")
        await self.add_tone(self.meeting.page.frames[1], 880)
        await self.peer.page.wait_for_function("""() =>
          __live.nativeRelays.size===2 && [...__live.nativeRelays.values()].every(
            relay=>relay.pc.connectionState==='connected' && relay.tracks.size===1)""", timeout=10000)
        report = await self.meeting.native_audio_readiness()
        self.assertFalse(report['ready'])
        self.assertFalse(report['failed'])
        self.assertFalse(self.meeting.native_audio_failed)
        await self.peer.page.evaluate("() => __live.ctx.resume()")
        signal = await self.assert_tone(1)
        self.assertGreater(signal['tones'][0], -35)
        self.assertTrue((await self.meeting.native_audio_readiness())['ready'])

    async def test_queued_frame_reannouncement_connects_its_latest_unowned_document(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_tone(0)
        await self.meeting.page.evaluate("() => document.body.appendChild(document.createElement('iframe'))")
        child = self.meeting.page.frames[1]
        # A deterministic setup-occupancy barrier; bindings, capture and RTC
        # remain real, and the verdict is the independently received waveform.
        async with self.meeting._native_media._lock:
            await self.add_tone(child, 880)
            await child.evaluate("() => meetingNativeSource({capture_id:__meeting.capture_clock_id})")
            await child.goto('data:text/html,<p>Latest source document</p>')
            await self.add_tone(child, 1320)
            await child.evaluate("() => meetingNativeSource({capture_id:__meeting.capture_clock_id})")
        signal = await self.assert_tone(2)
        self.assertGreater(signal['tones'][0], -35)
        await self.assert_tone(1, present=False)
        report = await self.meeting.native_audio_readiness()
        self.assertFalse(report['failed'])
        self.assertEqual(report['frame_count'], 2)
        self.assertEqual(report['connected_frames'], 2)
        packet = await self.assert_mixed_observation(440, 1320)
        self.assertEqual(packet['capture_clock_id'], self.meeting._native_media._target_owner)
        self.assertEqual(len(self.observed_documents), 1)

    async def test_recording_revoked_during_target_scope_check_drops_pending_pcm(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        await self.meeting.start_capture(timeout=3)
        await self.assert_mixed_observation(440)
        media = self.meeting._native_media
        original_policy = self.meeting.dual_policy
        class Policy:
            allowed = True
            async def check(self):
                return self.allowed
        policy = Policy()
        revoked = asyncio.Event()
        evaluate = self.peer.page.evaluate
        async def revoke_after_scope_check(*args, **kwargs):
            result = await evaluate(*args, **kwargs)
            policy.allowed = False
            revoked.set()
            return result
        try:
            async with media._observation_lock:
                self.meeting.dual_policy = policy
                before = self.observed
            with patch.object(self.peer.page, 'evaluate', side_effect=revoke_after_scope_check):
                await asyncio.wait_for(revoked.wait(), 3)
                async with media._observation_lock:
                    self.assertEqual(self.observed, before)
        finally:
            self.meeting.dual_policy = original_policy

    async def test_native_ready_does_not_reuse_unready_observation_report(self):
        await self.add_tone(self.meeting.page.main_frame, 440)
        await self.connect_receiver()
        readiness = self.meeting.capture_readiness
        async def unready_observation_report():
            report = await readiness()
            report['ready'] = False
            return report
        with patch.object(self.meeting, 'capture_readiness', side_effect=unready_observation_report), \
                patch.object(self.meeting._native_media, '_start_observation', return_value=False):
            report = await self.meeting.start_capture(timeout=.3)
        signal = await self.assert_tone(0)
        self.assertGreater(signal['rms'], .03)
        self.assertTrue(report['native_source']['ready'])
        self.assertFalse(report['observation_ready'])
        self.assertTrue(report['ready'])
