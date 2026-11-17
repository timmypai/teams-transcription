"""主程式問題邊界：收到的 PCM 活動、連續 quiet 與穩定逐字 delta。

這是 acoustic activity heuristic，不是語意 VAD、speaker proof 或 provider
final。封包本身也不能證明實際遠端來源：host 必須先執行 admission／echo gate。
不保留音訊，不把沒有封包的時間當成靜音，也不接受 provider final 替代 delta。
"""
from dataclasses import dataclass
import math
import re
import struct
import time
import unicodedata


INPUT_ACTIVITY_RMS_THRESHOLD = 256  # PCM16 units, approximately -42 dBFS.
CAPTURE_FRESHNESS_SECONDS = .25
CAPTURE_GAP_TOLERANCE_SECONDS = .02  # Browser callback timestamps have jitter.
_SAMPLE_RATE = 24000
_EDGE_PUNCTUATION = ' \t\r\n，,。.!！？?：:、'


@dataclass(frozen=True)
class FramedQuestion:
    kind: str
    text: str
    sequence: int


class ApplicationQuestionFramer:
    """One pending utterance; only poll() emits application requests.

    A follow-up must begin with admitted activity before its explicit deadline;
    its text/quiet may settle afterwards. Every emitted request consumes that
    permission. reset() cancels pending input and permission, not the sequence.
    """

    def __init__(self, wake_words=('小白小白',), *, clock=time.monotonic,
                 silence_seconds=1.5, text_settle_seconds=.6, followup_seconds=20):
        if not wake_words or any(not isinstance(word, str) or not word.strip()
                                 for word in wake_words):
            raise ValueError('Wake phrases must be nonempty')
        for duration in (silence_seconds, text_settle_seconds, followup_seconds):
            if (isinstance(duration, bool) or not isinstance(duration, (int, float))
                    or not math.isfinite(duration) or duration <= 0):
                raise ValueError('Framing durations must be positive and finite')
        self.clock = clock
        self.silence_seconds = silence_seconds
        self.text_settle_seconds = text_settle_seconds
        self.followup_seconds = followup_seconds
        self._quiet_target_frames = math.ceil(silence_seconds * _SAMPLE_RATE)
        self._wake_patterns = []
        for word in wake_words:
            word = unicodedata.normalize('NFKC', word).casefold()
            if word.isascii():
                pattern = r'(?<!\w)' + re.escape(word).replace(r'\ ', r'\s+') + r'(?!\w)'
            else:
                pattern = r'[\s，,、]*'.join(re.escape(char) for char in word)
            self._wake_patterns.append(re.compile(pattern))
        self._sequence = 0
        self.reset()

    def reset(self):
        """Discard pending input, capture continuity and follow-up permission."""
        self._clear_pending()
        self._capture_end = None
        self._packet_active = False
        self._quiet_frames = 0
        self._followup_until = None

    def _clear_pending(self):
        self._text = ''
        self._text_changed_at = None
        self._heard_activity = False
        self._pending_followup = False

    def _capture_recent(self, now):
        return (self._capture_end is not None
                and -.1 <= now - self._capture_end <= CAPTURE_FRESHNESS_SECONDS)

    def _quiet_complete(self):
        return self._quiet_frames >= self._quiet_target_frames

    def push_pcm(self, pcm, *, started_at, ended_at):
        """Consume an admitted 24k mono PCM16 packet; return new activity burst.

        Only actual quiet sample counts accumulate. A gap/overlap beyond callback
        jitter restarts that count; no packet means no further quiet evidence.
        Replayed, stale or out-of-order packets cannot advance the boundary.
        """
        if (not isinstance(pcm, bytes) or not 0 < len(pcm) <= 4096 or len(pcm) % 2
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in (started_at, ended_at))
                or ended_at <= started_at
                or abs(ended_at - started_at - len(pcm) / 48000) > .01):
            raise ValueError('Expected an admitted 24k mono PCM16 capture packet')
        now = self.clock()
        if (now - ended_at > CAPTURE_FRESHNESS_SECONDS or ended_at > now + .1
                or (self._capture_end is not None and ended_at <= self._capture_end)):
            return False
        continuous = (self._capture_end is not None
                      and abs(started_at - self._capture_end)
                      <= CAPTURE_GAP_TOLERANCE_SECONDS + 1e-9)
        if not continuous:
            self._quiet_frames = 0
        frames = len(pcm) // 2
        squares = sum(sample * sample for (sample,) in struct.iter_unpack('<h', pcm))
        active = squares >= frames * INPUT_ACTIVITY_RMS_THRESHOLD ** 2
        new_burst = active and (not self._packet_active or not continuous)
        if active:
            if not self._heard_activity:
                self._heard_activity = True
                self._pending_followup = (self._followup_until is not None
                                          and started_at < self._followup_until)
            self._quiet_frames = 0
        else:
            self._quiet_frames = min(self._quiet_target_frames,
                                     self._quiet_frames + frames)
        self._capture_end = ended_at
        self._packet_active = active
        return new_burst

    def append_transcript(self, delta):
        """Accept only user deltas associated with locally observed activity."""
        if not isinstance(delta, str) or not delta or not self._heard_activity:
            return False
        self._text += delta
        self._text_changed_at = self.clock()
        return True

    def _parsed_text(self):
        normalized = unicodedata.normalize('NFKC', self._text).casefold()
        for pattern in self._wake_patterns:
            match = pattern.search(normalized)
            if match:
                return True, normalized[match.end():].strip(_EDGE_PUNCTUATION), normalized[match.start():match.end()]
        return False, normalized.strip(_EDGE_PUNCTUATION), ''

    def poll(self):
        """Emit at most once after fresh observed quiet and unchanged deltas."""
        now = self.clock()
        if not self._capture_recent(now) or not self._quiet_complete():
            return None
        if (not self._heard_activity or not self._text or self._text_changed_at is None
                or now - self._text_changed_at + 1e-9 < self.text_settle_seconds):
            return None
        has_wake, question, wake = self._parsed_text()
        if has_wake:
            kind, text = ('question', question) if question else ('greeting', wake)
        elif self._pending_followup and question:
            kind, text = 'question', question
        else:
            self._clear_pending()
            return None
        self._sequence += 1
        request = FramedQuestion(kind, text, self._sequence)
        self._clear_pending()
        self._followup_until = None
        return request

    def arm_followup(self):
        """Caller invokes only after completing a bare-wake greeting."""
        self._followup_until = self.clock() + self.followup_seconds

    def pending_status(self):
        has_wake, question, _ = self._parsed_text()
        return {'characters': len(self._text), 'has_wake': has_wake,
                'has_question': bool(question) and (has_wake or self._pending_followup)}

    def status(self):
        """Bounded non-content metadata; never claims provider finality."""
        now = self.clock()
        return {'provenance': 'application', 'provider_final': False,
                'boundary': 'pcm_quiet_and_stable_delta',
                'activity_detector': 'pcm_rms_heuristic',
                'source_audio_verified': False,
                'input_activity_rms_threshold': INPUT_ACTIVITY_RMS_THRESHOLD,
                'silence_seconds': self.silence_seconds,
                'text_settle_seconds': self.text_settle_seconds,
                'capture_freshness_seconds': CAPTURE_FRESHNESS_SECONDS,
                'capture_gap_tolerance_seconds': CAPTURE_GAP_TOLERANCE_SECONDS,
                'capture_recent': self._capture_recent(now),
                'observed_quiet_seconds': self._quiet_frames / _SAMPLE_RATE,
                'heard_activity': self._heard_activity,
                'followup_active': self._followup_until is not None and now < self._followup_until,
                'emitted_requests': self._sequence,
                'pending': self.pending_status()}
