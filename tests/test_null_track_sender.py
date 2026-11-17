"""Actual selector/binding methods on Node objects; media is tested in Chromium."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import unittest

from teams_transcription.b_browser_tx import BrowserTXSink
from teams_transcription.browser import MeetingBrowser, RESPONSE_SENDER_JS, SELECT_RESPONSE_SENDER_JS
from teams_transcription.diagnostics import sender_selection_diagnostic


def null(direction='sendonly', **changes):
    return {'senders':[None], 'transceivers':[{'currentDirection':direction, **changes}]}


class NodeFrame:
    async def start(self, specs):
        self.process=await asyncio.create_subprocess_exec('node',
            str(Path(__file__).parent/'fixtures/null_sender_runtime.js'),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
        await self.request({'initialize':specs})
        return self

    async def request(self, value):
        self.process.stdin.write((json.dumps(value)+'\n').encode())
        await self.process.stdin.drain()
        result=json.loads(await asyncio.wait_for(self.process.stdout.readline(),3))
        if not result['ok']:
            raise RuntimeError('offline_js_operation_rejected')
        return result['value']

    async def evaluate(self, script, args=None):
        return await self.request({'script':script,'args':args})

    async def close(self):
        self.process.terminate()
        await asyncio.wait_for(self.process.wait(),3)


class NullTrackSenderTests(unittest.IsolatedAsyncioTestCase):
    async def frame(self, specs):
        frame=await NodeFrame().start(specs)
        self.addAsyncCleanup(frame.close)
        meeting=MeetingBrowser(None,None,None);meeting.page=SimpleNamespace(frames=[frame])
        return frame,meeting

    async def selected(self, specs):
        frame,meeting=await self.frame(specs)
        selected,peer,sender=await meeting.response_sender('one')
        self.assertIs(selected,frame)
        sink=BrowserTXSink(frame,meeting_id='one',peer_index=peer,sender_index=sender,
            selection_id=meeting._response_selection_id)
        await sink.bind()
        return frame,meeting,sink

    async def test_negotiated_null_sendonly_sendrecv_bind_default_off(self):
        for direction in ['sendonly','sendrecv']:
            with self.subTest(direction=direction):
                frame,meeting,sink=await self.selected([null(direction)])
                self.assertFalse(await frame.evaluate('()=>!!window.__bTX'))
                self.assertTrue(await frame.evaluate('()=>__meeting.peers[0].getSenders()[0].track===null'))
                self.assertFalse(sink._pending)
                with self.assertRaisesRegex(RuntimeError,'attach_live'):
                    await sink.start()
                diag=sender_selection_diagnostic(meeting._response_sender_diagnostic)
                self.assertEqual(diag['frames'][0]['peers'][0]['null_send_eligible_count'],1)
                self.assertNotIn('fixture-track',json.dumps(diag))

    async def test_null_recvonly_inactive_unnegotiated_stopped_video_and_multiple_rejected(self):
        cases=[[null('recvonly')],[null('inactive')],[null(None)],
            [null('sendonly',stopped=True)],[null('sendrecv',kind='video')],
            [null('sendonly'),null('sendrecv')],
            [{'senders':[None],'transceivers':[{'currentDirection':'sendonly'},
                {'currentDirection':'sendrecv'}]}]]
        for specs in cases:
            with self.subTest(specs=specs):
                frame,meeting=await self.frame(specs)
                with self.assertRaisesRegex(RuntimeError,'unique_muted_sender_required'):
                    await meeting.response_sender('one')
                self.assertFalse(await frame.evaluate('()=>!!window.__bTX'))

    async def test_transceiver_direction_or_sender_change_before_binding_rejects(self):
        for mutation in ['direction','sender']:
            with self.subTest(mutation=mutation):
                frame,meeting=await self.frame([null()])
                selected,peer,sender=await meeting.response_sender('one')
                if mutation=='direction':
                    await frame.evaluate('()=>{__meeting.peers[0].getTransceivers()[0].currentDirection="recvonly"}')
                else:
                    await frame.evaluate('()=>{__meeting.peers[0].getTransceivers()[0].sender={track:null}}')
                sink=BrowserTXSink(selected,meeting_id='one',peer_index=peer,sender_index=sender,
                    selection_id=meeting._response_selection_id)
                with self.assertRaises(RuntimeError):
                    await sink.bind()
                self.assertTrue(await frame.evaluate('()=>__meeting.peers[0].getSenders()[0].track===null'))

    async def test_non_null_muted_selection_identity_preserved(self):
        specs=[{'senders':[{'kind':'video','enabled':False,'readyState':'live'},
            {'kind':'audio','enabled':False,'readyState':'live'}]}]
        frame,meeting,sink=await self.selected(specs)
        self.assertTrue(await frame.evaluate('()=>__bSelectedSender.sender===__meeting.peers[0].getSenders()[1]'))
        self.assertEqual(sink.selection_id,meeting._response_selection_id)

    async def test_foreign_selection_capability_rejected(self):
        frame,meeting=await self.frame([null()])
        selected,peer,sender=await meeting.response_sender('one')
        sink=BrowserTXSink(selected,meeting_id='one',peer_index=peer,sender_index=sender,selection_id='0'*32)
        with self.assertRaises(RuntimeError):
            await sink.bind()
        self.assertFalse(await frame.evaluate('()=>!!window.__bTX'))

    async def test_old_scan_generation_cannot_bind_new_selection(self):
        frame,meeting=await self.frame([null()])
        first=(await frame.evaluate(RESPONSE_SENDER_JS))['candidates'][0]
        await meeting.response_sender('one')
        newer=(await frame.evaluate(RESPONSE_SENDER_JS))['candidates'][0]
        self.assertGreater(newer['generation'],first['generation'])
        with self.assertRaises(RuntimeError):
            await frame.evaluate(SELECT_RESPONSE_SENDER_JS,{'meetingId':'one',
                'generation':first['generation'],'slot':first['slot'],'selectionId':'0'*32})


if __name__=='__main__':unittest.main()
