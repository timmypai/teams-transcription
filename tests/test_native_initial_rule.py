"""Offline startup and explicit muted diagnostic admission safety."""
import asyncio
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription.b_native import NativeLiveResponder, PINNED_CODEX_VERSION
from teams_transcription.b_response import MeetingMinutesTool



class NativeStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.sink=AsyncMock();self.output=NativeTrackOutput('one',self.sink)
        self.tool=MeetingMinutesTool('one',lambda m:{'meeting_id':m,'meeting_minutes':'預算十萬元。','version':1})
        self.native=NativeLiveResponder(None,Path(self.temp.name),'one',self.output,self.tool)



    async def test_actual_start_is_idle_muted_without_input_and_does_not_restart(self):
        self.native.rpc=AsyncMock();self.native.rpc.executable='codex'
        self.native.rpc.thread.return_value='own-thread'
        self.native.rpc.notifications=asyncio.Queue();self.native.rpc.process=SimpleNamespace(returncode=None)
        self.native.peer=AsyncMock();self.native.peer.offer.return_value='offer'
        calls=[]
        async def request(method,params):
            calls.append((method,params))
            if method=='account/read':return {'account':{'type':'chatgpt'}}
            if method in {'thread/realtime/start','thread/realtime/stop'}:
                event='sdp' if method.endswith('/start') else 'closed'
                await self.native.rpc.notifications.put({'method':'thread/realtime/'+event,
                    'params':{'threadId':'own-thread','sdp':'answer'} if event=='sdp' else {'threadId':'own-thread'}})
            return {}
        self.native.rpc.request.side_effect=request
        version=SimpleNamespace(returncode=0,communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(),b'')))
        with patch('asyncio.create_subprocess_exec',return_value=version):
            await self.native.start(enable_output=False)
            self.assertEqual(self.native.state,'sleeping')
            self.assertEqual(self.native.status()['input_pending']['characters'],0)
            self.assertIsNone(self.native.greeting_completion)
            self.assertFalse(self.output.enabled);self.assertFalse(self.output.active)
            self.sink.start.assert_awaited_once()
            self.sink.attach_live.assert_awaited_once()
            self.sink.set_muted.assert_awaited_once_with(True)
            with self.assertRaises(RuntimeError):await self.native.start(enable_output=False)
        self.native.peer.send_event.assert_not_awaited()
        self.assertNotIn('thread/realtime/appendSpeech',[m for m,_ in calls])
        await self.native.stop()
        self.assertTrue(self.native.stop_ack_received);self.assertTrue(self.native.stop_closed_received)

    async def test_question_stage_admission_explicit_scope_muted_and_bounded_no_fake_asr(self):
        with self.assertRaises(RuntimeError):self.native.admit_synthetic_question_stage()
        output=NativeTrackOutput('synthetic-qa',AsyncMock())
        native=NativeLiveResponder(None,Path(self.temp.name),'synthetic-qa',output,
            MeetingMinutesTool('synthetic-qa',lambda _:{}),muted_probe_text_gate=True,question_timeout=.01)
        native.started=True;native.thread_id='owned';native.context.bind_session('owned')
        native.state='sleeping'
        output.enabled=True
        with self.assertRaises(RuntimeError):native.admit_synthetic_question_stage()
        output.enabled=False
        receipt=native.admit_synthetic_question_stage()
        self.assertEqual(native.state,'listening');self.assertFalse(receipt['input_transcript_injected'])
        self.assertEqual(receipt['wake_validation'],'skipped_unverified')
        self.assertIsNone(native.greeting_completion)
        self.assertEqual(native.status()['input_pending']['characters'],0)
        self.assertFalse(native._speech_done.is_set());self.assertFalse(output.active)
        await native.handle_notification('thread/realtime/transcript/delta',
            {'threadId':'owned','role':'assistant','delta':'unverified diagnostic response'})
        self.assertIsNone(native.error);self.assertIsNone(native.greeting_completion)
        await native._phase_timeout
        self.assertEqual(native.state,'sleeping')



if __name__=='__main__':unittest.main()
