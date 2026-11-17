"""Bounded PCM queue behavior; no service, browser, model or audio device."""
import unittest

from teams_transcription.audio_buffer import AudioBuffer,PCMFrame


class BufferTests(unittest.IsolatedAsyncioTestCase):
    async def test_byte_bound_and_exact_gap_during_silence(self):
        buffer=AudioBuffer(seconds=1)
        buffer.put(PCMFrame(0,b'\0'*48000))
        self.assertEqual(buffer.buffer_seconds,1)
        with self.assertRaises(RuntimeError):
            buffer.put(PCMFrame(24000,b'\0'*4800))
        self.assertEqual(buffer.gaps,[{'sample_start':24000,'sample_count':2400,'reason':'buffer_overflow'}])
        self.assertEqual((await buffer.get()).sample_count,24000)
        self.assertEqual(buffer.bytes,0)
