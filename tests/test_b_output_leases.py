"""Repeated native question leases on one sender and local relay."""
import asyncio
import shutil
import unittest

from teams_transcription.b_browser_tx import BrowserTXSink
from teams_transcription.b_dual_policy import OwnedSenderDualPolicy
from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription.browser import Browser
from native_output_fixture import NativeReceiver, NativeSource, fixture_page, meeting_fixture


@unittest.skipUnless(shutil.which('chromium'), 'Chromium fixture unavailable')
class BrowserOutputLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_quiet_receipt_reuses_track_and_close_restores_sender(self):
        async with Browser() as runtime:
            context,page = await fixture_page(runtime, capture=True)
            await meeting_fixture(page)
            source = await NativeSource.create(runtime)
            receiver = await NativeReceiver.create(runtime, page)
            policy = OwnedSenderDualPolicy(page, 'one')
            sink = BrowserTXSink(page, meeting_id='one', dual_channel_policy=policy.authorize)
            await sink.bind()
            output = NativeTrackOutput('one', sink)
            try:
                await output.attach_live(source.page)
                await asyncio.sleep(.2)
                # A disabled lease can observe the native receipt but never emits.
                await output.begin_speech('one')
                await source.tone()
                await asyncio.sleep(.2)
                await output.observe()
                self.assertGreater(output.frames, 0)
                self.assertFalse(await page.evaluate('__bTX.track.enabled'))
                await output.end_speech()
                await source.silence()
                output.start_output()
                track_id = sink.binding['track_id']
                await page.evaluate('window.retainedRelay=__bTX.relay;window.retainedTrack=__bTX.track')
                for frequency in (440,880):
                    await output.begin_speech('one')
                    await source.tone(frequency)
                    for _ in range(6):
                        await asyncio.sleep(.05)
                        self.assertFalse(await output.observe())
                    self.assertGreater(output.activity_frames, 0)
                    self.assertTrue(await policy.check())
                    await source.silence()
                    async def quiet():
                        while not await output.observe():
                            await asyncio.sleep(.05)
                    await asyncio.wait_for(quiet(), 3)
                    delivery = await output.end_speech(quiet=True)
                    self.assertEqual(delivery, {'status':'quiet','truncated':False,'drained':False,'reason':None})
                    self.assertEqual(sink.binding['track_id'], track_id)
                    self.assertTrue(sink.started)
                    self.assertTrue(await page.evaluate(
                        'fixture.sender.track===retainedTrack && !retainedTrack.enabled && __bTX.relay===retainedRelay'))
                    await asyncio.sleep(.25)
                    await receiver.reset()
                    await asyncio.sleep(.2)
                    self.assertLess(max(row['rms'] for row in await receiver.bins()), .001)
                await output.close()
                self.assertTrue(await page.evaluate(
                    'fixture.sender.track===fixture.track && !fixture.track.enabled && !window.__bTX && '
                    'retainedRelay.connectionState==="closed" && retainedTrack.readyState==="ended"'))
            finally:
                await output.close()
                await receiver.close()
                await source.close()
                await context.close()
