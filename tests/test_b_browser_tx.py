"""Native receiver relay and downstream waveform tests; no model or devices."""
import asyncio
import shutil
import unittest

from teams_transcription.b_browser_tx import BrowserTXSink
from teams_transcription.b_dual_policy import OwnedSenderDualPolicy
from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription.browser import Browser
from native_output_fixture import NativeReceiver, NativeSource, fixture_page, meeting_fixture
from native_marker import marker_present


@unittest.skipUnless(shutil.which('chromium'), 'Chromium fixture unavailable')
class BrowserTXTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime = await Browser().__aenter__()
        self.context, self.page = await fixture_page(self.runtime, capture=True)
        await meeting_fixture(self.page)
        self.source = await NativeSource.create(self.runtime)
        self.receiver = await NativeReceiver.create(self.runtime, self.page)
        self.policy = OwnedSenderDualPolicy(self.page, 'one')
        self.sink = BrowserTXSink(self.page, meeting_id='one', dual_channel_policy=self.policy.authorize)
        await self.sink.bind()
        await self.sink.attach_live(self.source.page)
        await self.sink.start()

    async def asyncTearDown(self):
        await self.sink.stop()
        await self.receiver.close()
        await self.source.close()
        await self.context.close()
        await self.runtime.__aexit__(None, None, None)

    async def test_ordered_first_last_and_continuous_middle_repeat_same_native_track(self):
        self.assertFalse(hasattr(self.sink, 'write'), 'Production output has no PCM entry point')
        self.assertFalse(self.sink.diagnostics()['available'])
        output = NativeTrackOutput('one', self.sink)
        async def quiet():
            while not await output.observe():
                await asyncio.sleep(.05)
        await output.begin_speech('one')
        # Incoming native media exists before authorization but may not leave Teams.
        await self.source.tone()
        await asyncio.sleep(.4)
        await self.receiver.reset()
        await asyncio.sleep(.25)
        self.assertLess(max(row['rms'] for row in await self.receiver.bins()), .001)
        await self.source.silence()
        # Finish calibration using actual received quiet, with output disabled.
        await asyncio.wait_for(quiet(), 3)
        await output.end_speech(quiet=True)
        track_id = self.sink.binding['track_id']
        previous_frames = 0
        output.start_output()
        for _ in range(2):
            await output.begin_speech('one')
            self.assertTrue(await self.policy.check())
            await self.receiver.reset()
            duration = await self.source.pattern()
            await asyncio.sleep(duration + .35)
            bins = await self.receiver.bins()
            audible = [i for i,row in enumerate(bins) if row['rms'] > .06]
            self.assertTrue(audible)
            first, last = audible[0], audible[-1]
            signal = bins[first:last+1]
            self.assertAlmostEqual(len(signal) / 100, 2.7, delta=.15)
            # The endpoint is a different context and PC, after the exact Teams
            # sender. No tap on the source scheduling graph can satisfy this.
            first_tone = [row for row in signal[:25] if 250 <= row['frequency'] <= 400]
            middle = signal[50:-50]
            last_tone = [row for row in signal[-25:] if 750 <= row['frequency'] <= 1000]
            self.assertGreater(len(first_tone), 18)
            self.assertGreater(len(last_tone), 18)
            low_middle=[{'offset_ms':10*(i+50),'rms':row['rms'],'frequency':row['frequency']}
                        for i,row in enumerate(middle) if row['rms']<=.06]
            self.assertFalse(low_middle,{'middle_dropouts':low_middle,
                                        'audible_duration_ms':10*len(signal)})
            self.assertGreater(sum(450 <= row['frequency'] <= 650 for row in middle), len(middle)*.95)
            stats = await self.sink.observe()
            self.assertTrue(stats['connected'])
            self.assertGreater(stats['frames'], previous_frames)
            self.assertGreater(stats['energy'], 0)
            self.assertGreater(stats['packets'], 0)
            self.assertAlmostEqual(stats['frames']/48000, stats['duration'], delta=1/48000)
            previous_frames = stats['frames']
            self.assertEqual(self.sink.binding['track_id'], track_id)
            self.assertTrue(await self.page.evaluate('fixture.sender.track===__bTX.track && __meeting.peers.length===1'))
            await asyncio.wait_for(quiet(), 3)
            delivery = await output.end_speech(quiet=True)
            self.assertEqual(delivery, {'status':'quiet','truncated':False,'drained':False,'reason':None})
            self.assertEqual(await self.page.evaluate('__bTX.track.id'), track_id)
        snapshot = self.sink.diagnostics()
        snapshot['packets'] = -1
        self.assertGreater(self.sink.diagnostics()['packets'], 0)
        self.assertNotIn('underrun_count', snapshot)
        final = self.sink.diagnostics()
        await self.sink.stop()
        self.assertEqual(self.sink.diagnostics(), final)
        self.assertTrue(await self.page.evaluate('fixture.sender.track===fixture.track && !fixture.track.enabled'))

    async def test_missing_provider_packets_are_silent_without_finishing_the_answer(self):
        output=NativeTrackOutput('one',self.sink)
        output.start_output()
        await output.begin_speech('one')
        await self.source.tone(frequency=550)
        await self.receiver.page.wait_for_function(
            'downstream.bins.slice(-10).filter(b=>b.rms>.06).length===10')
        self.assertFalse(await output.observe())
        track_id=self.sink.binding['track_id']
        await self.source.page.evaluate('nativeFixture.dropPackets=true')
        # Let already received audio finish; do not mistake it for concealment.
        await asyncio.sleep(.35)
        before=await self.sink.observe()
        await self.receiver.reset()
        for _ in range(20):
            await asyncio.sleep(.05)
            self.assertFalse(await output.observe())
        after=await self.sink.observe()
        self.assertEqual(after['source_frames'],before['source_frames'])
        self.assertEqual(after['source_packets'],before['source_packets'])
        self.assertGreater(after['frames'],before['frames'])
        self.assertLess(max(b['rms'] for b in await self.receiver.bins()),.001)
        self.assertTrue(output.active)
        await self.source.tone(frequency=1100)
        await self.source.page.evaluate('nativeFixture.dropPackets=false')
        await self.receiver.page.wait_for_function(
            'downstream.bins.slice(-10).filter(b=>b.rms>.06 && b.frequency>=950 && b.frequency<=1250).length===10')
        self.assertEqual(self.sink.binding['track_id'],track_id)
        self.assertFalse(await output.observe())
        await self.source.silence()
        async with asyncio.timeout(4):
            while not await output.observe():
                await asyncio.sleep(.05)
        self.assertEqual((await output.end_speech(quiet=True))['status'],'quiet')

    async def _receiver_snapshot(self):
        return await self.receiver.page.evaluate("""async ()=>{
          const report=await downstream.pc.getReceivers()[0].getStats();
          const audio=[...report.values()].find(row=>row.type==='inbound-rtp' && row.kind==='audio');
          return {bins:downstream.bins,packets:audio.packetsReceived,
            samples:audio.totalSamplesReceived,concealed:audio.concealedSamples,
            silent_concealed:audio.silentConcealedSamples};
        }""")

    async def _sender_packets(self):
        return await self.page.evaluate("""async ()=>{
          const report=await fixture.sender.getStats();
          return [...report.values()].find(row=>row.type==='outbound-rtp' && row.kind==='audio').packetsSent;
        }""")

    async def _assert_revocation_tail(self, before):
        # The native sender boundary is immediate. Already encoded RTP and
        # receiver packet-loss concealment are not a remotely flushable queue.
        self.assertTrue(await self.page.evaluate(
            '__bTX.disposed && !__bTX.track.enabled && __bTX.track.readyState==="ended"'))
        at_boundary=await self._sender_packets()
        await asyncio.sleep(.1)
        settled_packets=await self._sender_packets()
        await asyncio.sleep(1.1)
        after=await self._receiver_snapshot()
        final_packets=await self._sender_packets()
        tail=after['bins'][len(before['bins']):]
        audible=[i for i,row in enumerate(tail) if row['rms']>=.001]
        self.revocation_evidence={
            'sender_packets_at_boundary':at_boundary,
            'sender_packets_100ms':settled_packets,
            'sender_packets_final':final_packets,
            'receiver_packets_after_boundary':after['packets']-before['packets'],
            'receiver_samples_after_boundary':after['samples']-before['samples'],
            'concealed_samples_after_boundary':after['concealed']-before['concealed'],
            'silent_concealed_after_boundary':after['silent_concealed']-before['silent_concealed'],
            'last_audible_tail_ms':10*(audible[-1]+1) if audible else 0,
            'final_200ms_peak_rms':max(row['rms'] for row in tail[-20:]),
        }
        self.assertEqual(final_packets,settled_packets,self.revocation_evidence)
        self.assertGreater(after['concealed'],before['concealed'],self.revocation_evidence)
        self.assertGreaterEqual(len(tail),110,self.revocation_evidence)
        # A bounded local Chromium tail, not a production timeout or a promise
        # of remote drain: retain the whole tail instead of resetting it away.
        self.assertLess(self.revocation_evidence['last_audible_tail_ms'],1000,self.revocation_evidence)
        self.assertLess(self.revocation_evidence['final_200ms_peak_rms'],.001,self.revocation_evidence)
        with self.assertRaises(Exception):
            await self.sink.observe()
        self.assertFalse(self.sink.diagnostics()['connected'])
        print('synthetic-revocation-scalars', self._testMethodName, self.revocation_evidence,
              {'suspect_bins':[{**row,'offset_ms':i*10} for i,row in enumerate(tail)
                  if row['rms']>.06 and 950<=row['frequency']<=1250]})
        return tail

    async def test_revoke_stops_downstream_audio_without_python_packet_gate(self):
        await self.sink.set_muted(False)
        await self.source.tone()
        await self.receiver.page.wait_for_function(
            'downstream.bins.slice(-10).filter(row=>row.rms>.05).length===10')
        before=await self._receiver_snapshot()
        self.assertTrue(await self.page.evaluate("""()=>{
          __bMeetingId='another-meeting';
          return __bTX.disposed && !__bTX.track.enabled && __bTX.track.readyState==='ended';
        }"""))
        # Fresh provider audio after revocation must never cross the stopped
        # sender, even while the downstream decoder conceals the previous tone.
        await self.source.tone(frequency=1100)
        tail=await self._assert_revocation_tail(before)
        # Match the new marker's measured coherent RMS, not zero crossings of
        # old Chromium PLC. Captured no-marker control demonstrates why the old
        # discriminator is invalid; exact sender/RTP/quiet checks above remain.
        self.assertFalse(any(marker_present(row) for row in tail),
            {'revocation':self.revocation_evidence,
             'marker_bins':[{**row,'offset_ms':i*10} for i,row in enumerate(tail) if marker_present(row)]})

    async def test_revocation_marker_positive_control_reaches_exact_downstream_peer(self):
        await self.sink.set_muted(False)
        await self.source.tone(frequency=1100)
        observed=await self.receiver.page.wait_for_function(
            'downstream.bins.length>=10 && downstream.bins.slice(-10).every(row=>row.rms>.06 && row.marker_rms>.06) ? downstream.bins.slice(-10) : null')
        rows=await observed.json_value()
        self.assertTrue(all(row['rms']>.06 and row['marker_rms']>.06 for row in rows))
        print('synthetic-marker-positive-control',rows)

    async def test_revocation_tail_negative_control_never_sends_marker_source(self):
        # The source NEVER receives a 1100Hz instruction in this control. Retain
        # real Chromium's full tail and exact sender counters for diagnosis;
        # a zero-crossing match here cannot identify new provider marker audio.
        controls=[]
        for index,delay in enumerate((0,.005,.01,.02,.04,.08)):
            if index:
                # A revoked meeting binding cannot be adopted or revived just
                # to repeat a test. Use fresh contexts/peers for each control.
                await self.asyncTearDown()
                await self.asyncSetUp()
            await self.receiver.reset()
            await self.sink.set_muted(False)
            await self.source.tone(frequency=550)
            await self.receiver.page.wait_for_function(
                'downstream.bins.length>=10 && downstream.bins.slice(-10).every(row=>row.rms>.06 && row.frequency>=450 && row.frequency<=650)')
            await asyncio.sleep(delay)
            before=await self._receiver_snapshot()
            await self.page.evaluate("__bMeetingId='another-meeting'")
            tail=await self._assert_revocation_tail(before)
            self.assertFalse(any(marker_present(row) for row in tail),self.revocation_evidence)
            controls.append(dict(delay_ms=round(delay*1000),
                legacy_marker_bins=[row for row in tail if row['rms']>.06 and 950<=row['frequency']<=1250],
                coherent_marker_bins=[row for row in tail if row['marker_rms']>.06]))
        print('synthetic-no-marker-source-controls',controls)

    async def test_source_navigation_revokes_target_and_keeps_a_rx_separate(self):
        self.assertEqual(await self.page.evaluate('__meeting.peers.length'),1)
        await self.sink.set_muted(False)
        await self.source.tone()
        await self.receiver.page.wait_for_function(
            'downstream.bins.slice(-10).filter(row=>row.rms>.05).length===10')
        before=await self._receiver_snapshot()
        await self.source.page.goto('about:blank?source-replaced')
        await self.page.wait_for_function('__bTX.disposed && !__bTX.track.enabled')
        await self._assert_revocation_tail(before)
        self.assertEqual(await self.page.evaluate('__meeting.peers.length'),1)

    async def test_stop_preserves_stopped_non_null_transceiver(self):
        await self.sink.set_muted(False)
        await self.page.evaluate("""()=>{
          window.savedOwned=__bTX.track;
          window.stoppedTransceiver=fixture.pc.getTransceivers().find(t=>t.sender===fixture.sender);
          stoppedTransceiver.stop();window.stoppedSenderTrack=fixture.sender.track;
        }""")
        await self.sink.stop()
        self.assertEqual(self.sink.cleanup_status,'cleaned')
        self.assertTrue(await self.page.evaluate(
            'stoppedTransceiver.stopped && fixture.sender.track===stoppedSenderTrack && '
            'savedOwned.readyState==="ended" && !savedOwned.enabled && fixture.track.readyState==="live"'))

    async def test_stop_does_not_restore_ended_original_track(self):
        await self.sink.set_muted(False)
        await self.page.evaluate('window.savedOwned=__bTX.track;fixture.track.stop()')
        await self.sink.stop()
        self.assertEqual(self.sink.cleanup_status,'cleaned')
        self.assertTrue(await self.page.evaluate(
            'fixture.sender.track===savedOwned && savedOwned.readyState==="ended" && !savedOwned.enabled'))
