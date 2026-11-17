import unittest
from unittest.mock import AsyncMock

from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription.b_echo import OwnedOutputIntervals
from teams_transcription.b_native import NativeLiveResponder
from teams_transcription.b_response import MeetingMinutesTool


class SourceIntervalTests(unittest.TestCase):
    def test_delayed_rx_echo_rejected_but_true_next_question_audio_admitted(self):
        responder = NativeLiveResponder(None,'/tmp/unused-native-echo','one',
            NativeTrackOutput('one',AsyncMock()),MeetingMinutesTool('one',lambda _:{}),clock=lambda:10.)
        responder.started = True
        responder.intervals.intervals.append((8.,9.8))
        pcm = b'\x01\x00'*1200
        self.assertFalse(responder.push_rx('one',pcm,started_at=9.75,ended_at=9.8))
        self.assertTrue(responder.push_rx('one',pcm,started_at=9.9,ended_at=9.95))
        self.assertEqual(responder._rx.get_nowait(),(pcm,9.9,9.95))
        self.assertEqual(responder.rx_dropped,1)
        self.assertFalse(responder.push_rx('another-meeting',pcm,started_at=9.9,ended_at=9.95))
        self.assertTrue(responder._rx.empty())

    def test_invalid_and_foreign_timebases_refused(self):
        intervals = OwnedOutputIntervals()
        for start,end in [(float('nan'),1),(2,1),(None,1),(True,1)]:
            with self.assertRaises(ValueError):
                intervals.overlaps(start,end)
