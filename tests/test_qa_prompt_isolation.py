"""Explicit local diagnostic admission remains restricted to a muted synthetic scope."""
import asyncio
from pathlib import Path
import tempfile
import unittest

from teams_transcription import qa_probe
from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription.b_native import NativeLiveResponder
from teams_transcription.b_response import MeetingMinutesTool


class QuestionAdmissionIsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostic_admission_rejects_real_scope_or_enabled_output(self):
        with tempfile.TemporaryDirectory() as root:
            def channel(ident, gate):
                native=NativeLiveResponder(None,Path(root),ident,
                    NativeTrackOutput(ident,qa_probe.NoPhysicalSink()),
                    MeetingMinutesTool(ident,lambda _:{}),muted_probe_text_gate=gate)
                native.started=True
                native.thread_id='owned'
                native.context.bind_session('owned')
                native.state='sleeping'
                return native
            with self.assertRaises(ValueError):
                channel('ordinary-meeting',True)
            for ident in ('ordinary-meeting','synthetic-qa'):
                with self.subTest(meeting=ident):
                    with self.assertRaises(RuntimeError):
                        channel(ident,False).admit_synthetic_question_stage()
            diagnostic=channel('synthetic-qa',True)
            diagnostic.output.start_output()
            with self.assertRaises(RuntimeError):
                diagnostic.admit_synthetic_question_stage()
            self.assertEqual(diagnostic.state,'sleeping')
            self.assertEqual(diagnostic.tool_calls,0)
            await diagnostic.output.stop_output()
            receipt=diagnostic.admit_synthetic_question_stage()
            try:
                self.assertEqual(diagnostic.state,'listening')
                self.assertFalse(receipt['input_transcript_injected'])
                self.assertEqual(receipt['wake_validation'],'skipped_unverified')
                self.assertEqual(diagnostic.status()['input_pending']['characters'],0)
                self.assertFalse(diagnostic.output.active)
                self.assertEqual(diagnostic.tool_calls,0)
            finally:
                diagnostic._phase_timeout.cancel()
                await asyncio.gather(diagnostic._phase_timeout,return_exceptions=True)


if __name__=='__main__':unittest.main()
