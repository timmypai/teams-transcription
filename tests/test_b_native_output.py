"""Synthetic source/render counters exercise native speech completion and leases."""
import asyncio
import unittest
from unittest.mock import AsyncMock

from teams_transcription.b_native_output import NativeTrackOutput


class NativeTrackOutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.media = dict(connected=True, frames=0, packets=0, duration=0., energy=0.,
            source_frames=0, source_packets=0, source_duration=0., source_energy=0.)
        self.sink = AsyncMock()
        self.sink.observe.side_effect = lambda: dict(self.media)
        self.output = NativeTrackOutput('owned-meeting', self.sink, clock=lambda: 100.)
        await self.output.begin_speech('owned-meeting')

    async def asyncTearDown(self):
        await self.output.close()

    def advance(self, *, source=0., render=0., source_rms=0., render_rms=0.):
        for prefix, seconds, rms in (('source_', source, source_rms), ('', render, render_rms)):
            self.media[prefix + 'frames'] += round(seconds * 48000)
            self.media[prefix + 'duration'] += seconds
            self.media[prefix + 'energy'] += (rms / 32768) ** 2 * seconds
        if source:
            self.media['source_packets'] += 1
            self.media['packets'] += 1

    async def voice(self):
        self.advance(source=.1, render=.1, source_rms=1000, render_rms=1000)
        self.assertFalse(await self.output.observe())

    async def test_missing_rtp_and_continuous_rendered_zeros_cannot_finish(self):
        await self.voice()
        self.advance(render=30.)
        self.assertFalse(await self.output.observe())
        self.assertEqual(self.output.quiet_received_seconds, 0.)

    async def test_source_quiet_before_rendered_tail_does_not_strand_completion(self):
        await self.voice()
        self.advance(source=.81, render=.1, render_rms=1000)
        self.assertFalse(await self.output.observe())
        # Input has finished, but an already decoded tail is still rendering.
        self.advance(render=.1, render_rms=1000)
        self.assertFalse(await self.output.observe())
        self.advance(render=.81)
        self.assertTrue(await self.output.observe())
        result = await self.output.end_speech(quiet=True)
        self.assertEqual(result['status'], 'quiet')
        self.assertFalse(result['drained'])
        self.sink.pause.assert_awaited_once()

    async def test_source_voice_invalidates_only_source_quiet(self):
        await self.voice()
        self.advance(source=.81, render=.81)
        self.assertTrue(await self.output.observe())
        self.advance(source=.1, source_rms=1000)
        self.assertFalse(await self.output.observe())
        self.assertEqual(self.output.quiet_received_seconds, 0.)
        self.advance(source=.81)
        self.assertTrue(await self.output.observe())

    async def test_rendered_voice_invalidates_only_rendered_quiet(self):
        await self.voice()
        self.advance(source=.81, render=.81)
        self.assertTrue(await self.output.observe())
        self.advance(render=.1, render_rms=1000)
        self.assertFalse(await self.output.observe())
        self.assertEqual(self.output.quiet_received_seconds, 0.)
        self.advance(render=.81)
        self.assertTrue(await self.output.observe())

    async def test_no_real_source_activity_or_rendered_activity_cannot_finish(self):
        for source_rms, render_rms in ((1000, 0), (0, 1000), (0, 0)):
            with self.subTest(source_rms=source_rms, render_rms=render_rms):
                await self.output.end_speech()
                await self.output.begin_speech('owned-meeting')
                self.advance(source=.1, render=.1, source_rms=source_rms, render_rms=render_rms)
                self.assertFalse(await self.output.observe())
                self.advance(source=1., render=1.)
                self.assertFalse(await self.output.observe())

    async def test_source_quiet_requires_packets_frames_and_duration_to_advance(self):
        for missing in ('source_packets', 'source_frames', 'source_duration'):
            with self.subTest(missing=missing):
                await self.output.end_speech()
                await self.output.begin_speech('owned-meeting')
                await self.voice()
                previous = self.media[missing]
                self.advance(source=1., render=1.)
                self.media[missing] = previous
                self.assertFalse(await self.output.observe())
                self.assertEqual(self.output.quiet_received_seconds, 0.)

    async def test_new_turn_reset_discards_both_old_quiet_tails(self):
        await self.voice()
        self.advance(source=.81, render=.81)
        self.assertTrue(await self.output.observe())
        self.output.quiet_received_seconds = 0.
        self.advance(render=1.)
        self.assertFalse(await self.output.observe())
        self.advance(source=.81)
        self.assertTrue(await self.output.observe())

    async def test_regressing_source_observations_fail_closed(self):
        for key in ('source_frames', 'source_packets', 'source_duration', 'source_energy'):
            with self.subTest(key=key):
                await self.output.end_speech()
                self.output.start_output()
                await self.output.begin_speech('owned-meeting')
                await self.voice()
                self.media[key] /= 2
                with self.assertRaisesRegex(RuntimeError, 'observation reset'):
                    await self.output.observe()
                self.assertFalse(self.output.active)
                self.assertFalse(self.output.enabled)
                self.assertFalse(self.output.connected)
                self.sink.pause.assert_awaited()
                # Restore the monotone baseline before acquiring the next lease.
                self.media[key] *= 2

    async def test_invalid_source_observations_fail_closed(self):
        for key in ('source_frames', 'source_packets', 'source_duration', 'source_energy'):
            for invalid in (None, True, -1, float('nan'), float('inf'), '0'):
                with self.subTest(key=key, invalid=invalid):
                    previous = self.media[key]
                    self.media[key] = invalid
                    with self.assertRaisesRegex(RuntimeError, 'Invalid native relay observation'):
                        await self.output.observe()
                    self.assertFalse(self.output.active)
                    self.assertFalse(self.output.connected)
                    self.media[key] = previous
                    await self.output.begin_speech('owned-meeting')

    async def test_disabled_lease_observes_without_unmuting_and_scope_stays_bound(self):
        with self.assertRaisesRegex(ValueError, 'Invalid output scope'):
            await self.output.begin_speech('foreign-meeting')
        self.assertTrue(self.output.active)
        await self.voice()
        self.advance(source=.81, render=.81)
        self.assertTrue(await self.output.observe())
        self.sink.set_muted.assert_not_awaited()
        self.assertFalse(self.output.enabled)
        await self.output.close()
        self.assertFalse(self.output.active)
        self.sink.stop.assert_awaited_once()
        with self.assertRaisesRegex(RuntimeError, 'closed'):
            self.output.start_output()

    async def test_stop_waits_for_inflight_observation_then_revokes_lease(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def observe():
            entered.set()
            await release.wait()
            return dict(self.media)
        self.sink.observe.side_effect = observe
        observation = asyncio.create_task(self.output.observe())
        await entered.wait()
        stop = asyncio.create_task(self.output.stop_output())
        await asyncio.sleep(0)
        self.assertFalse(stop.done())
        release.set()
        self.assertFalse(await observation)
        self.assertEqual((await stop)['status'], 'stopped')
        self.assertFalse(self.output.active)
        self.sink.pause.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
