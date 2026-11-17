"""Stop/deadline at actual join/helper await boundaries; no external I/O."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tests import test_response_computer_audio_ui as ui


class ComputerAudioDispatchGuardTests(unittest.IsolatedAsyncioTestCase):
    async def case(self,boundary,*,expired=False):
        page=ui.Page(checked='false' if boundary=='radio_click_readback' else 'true')
        meeting,events=ui.ResponseComputerAudioUITests().meeting(page)
        stopped=[False];clock=[10.];hits=[0]
        def trip():
            hits[0]+=1
            if expired:clock[0]=20.
            else:stopped[0]=True
        radio=next(e for e in page.elements if e.role=='radio')
        if boundary in {'radio_click_readback','final_verify_readback'}:
            original=radio.get_attribute;reads=[0]
            async def read(key):
                value=await original(key);reads[0]+=1
                if reads[0]==(1 if boundary=='radio_click_readback' else 5):trip()
                return value
            radio.get_attribute=read
        elif boundary=='final_verify_visible':
            original=radio.is_visible;reads=[0]
            async def visible():
                value=await original();reads[0]+=1
                if reads[0]==6:trip()
                return value
            radio.is_visible=visible
        else:
            button=next(e for e in page.elements if e.label=='Join now')
            original=button.is_visible
            async def visible():
                value=await original();trip();return value
            button.is_visible=visible
        native_sleep=asyncio.sleep
        async def yield_only(seconds):await native_sleep(0)
        with patch('teams_transcription.browser.time',SimpleNamespace(monotonic=lambda:clock[0])), \
                patch('teams_transcription.browser.asyncio.sleep',new=yield_only):
            with self.assertRaises(TimeoutError if expired else InterruptedError):
                await meeting.join('teams','fixture',.3,lambda:stopped[0])
        self.assertEqual(hits[0],1)
        self.assertEqual(page.trace.count('Join now'),0)
        self.assertEqual(page.trace.count('Computer audio'),0)
        self.assertFalse(meeting.join_diagnostic()['join_dispatch_started'])
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_stop_at_last_readback_or_visibility_prevents_all_dispatch(self):
        for boundary in ['radio_click_readback','final_verify_readback','final_verify_visible','join_visible']:
            with self.subTest(boundary=boundary):await self.case(boundary)

    async def test_deadline_at_last_readback_or_visibility_prevents_all_dispatch(self):
        for boundary in ['radio_click_readback','final_verify_readback','final_verify_visible','join_visible']:
            with self.subTest(boundary=boundary):await self.case(boundary,expired=True)


if __name__=='__main__':unittest.main()
