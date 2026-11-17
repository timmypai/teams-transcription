"""Byte-bounded PCM buffering with explicit sample gaps, never synthetic silence."""
import asyncio
from dataclasses import dataclass


@dataclass(frozen=True)
class PCMFrame:
    sample_start: int
    data: bytes

    @property
    def sample_count(self):
        return len(self.data) // 2


class AudioBuffer:
    def __init__(self, seconds=10, rate=24000):
        self.limit = seconds * rate * 2
        self.rate = rate
        self.bytes = 0
        self.expected = None
        self.queue = asyncio.Queue()
        self.gaps = []

    def put(self, frame):
        if not isinstance(frame.data, bytes) or len(frame.data) % 2 or not frame.data or frame.sample_start < 0:
            raise ValueError("Invalid PCM16 frame")
        if self.expected is not None and frame.sample_start != self.expected:
            self.gaps.append({"sample_start": self.expected,
                "sample_count": max(0, frame.sample_start - self.expected), "reason": "delivery_ambiguous"})
            raise RuntimeError("Non-contiguous capture; stopped with explicit gap")
        if self.bytes + len(frame.data) > self.limit:
            self.gaps.append({"sample_start": frame.sample_start, "sample_count": frame.sample_count,
                              "reason": "buffer_overflow"})
            raise RuntimeError("Audio buffer limit reached; stopped with explicit gap")
        self.expected = frame.sample_start + frame.sample_count
        self.bytes += len(frame.data)
        self.queue.put_nowait(frame)

    async def get(self):
        frame = await self.queue.get()
        self.bytes -= len(frame.data)
        self.queue.task_done()
        return frame

    @property
    def buffer_seconds(self):
        return self.bytes / (self.rate * 2)
