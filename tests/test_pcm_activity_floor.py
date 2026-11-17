"""Independent generic PCM probes and native scalar-energy activity floor."""
import asyncio
import math
import struct
import unittest
from unittest.mock import AsyncMock

from teams_transcription.b_audio import GatedPCMOutput, PCM_ACTIVITY_RMS_THRESHOLD
from teams_transcription.b_native_output import NativeTrackOutput


def pcm(samples):
    return struct.pack('<' + 'h' * len(samples), *samples)


def waveform(peak):
    # Two complete 200 Hz periods at 48 kHz; not recorded human speech.
    return pcm([round(peak * math.sin(2 * math.pi * i / 240)) for i in range(480)])


class PCMActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.now = 100.0
        self.clock = lambda: self.now
        self.output = GatedPCMOutput('one', AsyncMock(), clock=self.clock)

    async def asyncTearDown(self):
        await self.output.close()

    async def test_exact_signed_le_sample_counts_zero_single_lsb_tails_and_extrema(self):
        await self.output.begin_speech('one')
        await self.output.write('one', pcm([0] * 480))
        self.assertEqual((self.output.frames, self.output.nonzero_frames, self.output.activity_frames), (480, 0, 0))
        self.assertEqual((self.output.last_packet_peak, self.output.last_packet_rms), (0, 0))
        self.now = 101
        await self.output.write('one', pcm([1] + [0] * 479))
        self.assertEqual(self.output.nonzero_frames, 1)  # Never inflate to 480.
        self.assertEqual(self.output.last_packet_nonzero_frames, 1)
        self.assertEqual(self.output.last_packet_peak, 1)
        self.assertAlmostEqual(self.output.last_packet_rms, math.sqrt(1/480), places=12)
        self.assertEqual(self.output.last_nonzero, 101)
        self.assertEqual(self.output.last_active, 0)
        self.now = 102
        await self.output.write('one', pcm([1, -1] * 240))
        self.assertEqual(self.output.nonzero_frames, 481)
        self.assertEqual((self.output.last_packet_peak, self.output.last_packet_rms), (1, 1))
        self.assertEqual(self.output.activity_frames, 0)
        self.assertEqual(self.output.last_active, 0)
        self.assertEqual(self.output.last_nonzero, 102)
        samples = [-32768, -256, -1, 0, 1, 256, 32767]
        await self.output.write('one', pcm(samples))
        self.assertEqual(self.output.nonzero_frames, 487)
        self.assertEqual(self.output.last_packet_nonzero_frames, 6)
        self.assertEqual(self.output.last_packet_peak, 32768)
        self.assertAlmostEqual(self.output.last_packet_rms, math.sqrt(sum(x*x for x in samples)/7))

    async def test_low_amplitude_waveform_and_explicit_threshold_boundary(self):
        self.assertEqual(PCM_ACTIVITY_RMS_THRESHOLD, 8)
        await self.output.begin_speech('one')
        await self.output.write('one', pcm([7, -7] * 240))
        self.assertEqual(self.output.activity_frames, 0)
        self.now = 101
        await self.output.write('one', pcm([8, -8] * 240))
        self.assertEqual(self.output.activity_frames, 480)
        self.assertEqual(self.output.last_active, 101)
        self.now = 102
        await self.output.write('one', waveform(16))
        self.assertEqual(self.output.last_packet_peak, 16)
        self.assertGreater(self.output.last_packet_rms, 11)
        self.assertLess(self.output.last_packet_rms, 12)
        self.assertEqual(self.output.activity_frames, 960)
        self.assertEqual(self.output.last_active, 102)
        # A sparse large impulse qualifies by packet energy; the floor is not VAD.
        await self.output.write('one', pcm([1000] + [0] * 479))
        self.assertEqual(self.output.last_packet_nonzero_frames, 1)
        self.assertEqual(self.output.last_packet_peak, 1000)
        self.assertGreater(self.output.last_packet_rms, 45)
        self.assertEqual(self.output.activity_frames, 1440)


    async def test_original_lease_expiry_closes_output_and_rejects_late_packets(self):
        await self.output.begin_speech('one', max_seconds=.02)
        await self.output.write('one', waveform(16))
        await asyncio.sleep(.04)
        frames = self.output.frames
        self.assertFalse(self.output.active)
        self.assertIsNone(self.output._expiry)
        self.assertFalse(await self.output.write('one', waveform(1200)))
        self.assertEqual(self.output.frames, frames)
        self.assertEqual(self.output.last_delivery['status'], 'muted')

class NativeActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.media = dict(frames=0, packets=0, energy=0., duration=0., connected=True,
            source_frames=0, source_packets=0, source_energy=0., source_duration=0.)
        self.sink = AsyncMock()
        self.sink.observe.side_effect = lambda:dict(self.media)
        self.output = NativeTrackOutput('one', self.sink)
        await self.output.begin_speech('one')

    async def asyncTearDown(self):
        await self.output.close()

    async def receive(self, rms, seconds):
        for prefix in ('', 'source_'):
            self.media[prefix + 'frames'] += round(seconds * 48000)
            self.media[prefix + 'packets'] += 1
            self.media[prefix + 'duration'] += seconds
            self.media[prefix + 'energy'] += (rms / 32768) ** 2 * seconds
        return await self.output.observe()

    async def test_scalar_energy_floor_requires_activity_then_received_quiet(self):
        self.assertFalse(await self.receive(1, 1))
        self.assertEqual(self.output.activity_frames, 0)
        self.assertFalse(await self.receive(7, 1))
        self.assertEqual(self.output.activity_frames, 0)
        self.assertFalse(await self.receive(16, .1))
        self.assertEqual(self.output.activity_frames, 4800)
        self.assertFalse(await self.receive(1, .4))
        self.assertFalse(await self.receive(16, .1))
        self.assertFalse(await self.receive(1, .4))
        self.assertTrue(await self.receive(1, .41))

    async def test_observe_cancellation_mutes_but_keeps_global_permission(self):
        await self.output.end_speech()
        self.output.start_output()
        await self.output.begin_speech('one')
        entered = asyncio.Event()
        async def blocked():
            entered.set()
            await asyncio.Event().wait()
        self.sink.observe.side_effect = blocked
        task = asyncio.create_task(self.output.observe())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.output.active)
        self.assertTrue(self.output.enabled)
        self.sink.pause.assert_awaited()
        self.sink.observe.side_effect = lambda:dict(self.media)
        await self.output.begin_speech('one')
        self.assertTrue(self.output.active)

    async def test_pause_cancellation_finishes_mute_without_revoking_permission(self):
        await self.output.end_speech()
        self.output.start_output()
        await self.output.begin_speech('one')
        entered, release = asyncio.Event(), asyncio.Event()
        async def pause():
            entered.set()
            await release.wait()
        self.sink.pause.side_effect = pause
        task = asyncio.create_task(self.output.end_speech())
        await entered.wait()
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.output.active)
        self.assertTrue(self.output.enabled)
        await self.output.begin_speech('one')
        self.assertTrue(self.output.active)


if __name__ == '__main__':
    unittest.main()
