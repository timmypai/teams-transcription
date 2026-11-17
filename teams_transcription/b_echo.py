"""Bounded response-only echo filter. RX ledger is never modified or paused."""
import math
import time
from collections import deque


class OwnedOutputIntervals:
    def __init__(self, *, clock=time.monotonic, tail=.15):
        self.clock, self.tail = clock, tail
        self.intervals = deque(maxlen=64)
        self.opened = None

    def begin(self):
        if self.opened is None:
            self.opened = self.clock()

    def end(self):
        if self.opened is not None:
            self.intervals.append((self.opened, self.clock() + self.tail))
            self.opened = None

    def overlaps(self, start, end):
        if (type(start) not in (float, int) or type(end) not in (float, int)
                or not math.isfinite(start) or not math.isfinite(end) or start > end):
            raise ValueError("RX requires finite source monotonic interval")
        return ((self.opened is not None and end >= self.opened)
                or any(start <= right and end >= left for left, right in self.intervals))
