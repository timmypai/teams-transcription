"""Actual UI join/cleanup methods with synthetic locators; no browser/model."""
import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from teams_transcription.browser import MeetingBrowser


class Element:
    def __init__(self,page,label,role,checked='false'):
        self.page,self.label,self.role,self.checked=page,label,role,checked
        self.visible=True
    async def is_visible(self):return self.visible
    async def get_attribute(self,key):return self.checked if key=='aria-checked' else None
    async def click(self,**kwargs):
        self.page.trace.append(self.label)
        if self.role=='radio':
            self.page.radio_entered.set()
            if self.page.hang_radio:
                try:await asyncio.Event().wait()
                finally:self.page.radio_cancelled=True
            if self.page.radio_effect:self.checked='true'
        elif self.role=='switch':self.checked='false'
        elif self.label=='Join now':
            self.visible=False;self.page.leave.visible=True
            self.page.elements.append(Element(self.page,'Participants','button'))
        elif self.label=='Leave':self.visible=False
    async def fill(self,text,**kwargs):
        if self.page.lose_after_name:
            for element in self.page.elements:
                if element.role=='radio':element.checked='false'


class Collection:
    def __init__(self,page,elements):
        self.page,self.elements=page,elements
        self.first=self.nth(0)
    async def count(self):return len(self.elements)
    def nth(self,index):
        if index<len(self.elements):return self.elements[index]
        ghost=Element(self.page,'absent','none');ghost.visible=False;return ghost


class StatusRegion:
    """One synthetic semantic status, independent of action control locators."""
    def __init__(self,page):self.page=page
    async def count(self):return 1
    def nth(self,index):return self
    async def is_visible(self):return True
    def locator(self,selector):return Collection(self.page,[])
    async def inner_text(self,**kwargs):return await self.page.inner_text()


class Page:
    def __init__(self,radios=1,checked='false',*,radio_effect=True,hang_radio=False,lose_after_name=False,already_joined=False):
        self.trace=[];self.radio_entered=asyncio.Event();self.radio_cancelled=False
        self.radio_effect,self.hang_radio,self.lose_after_name=radio_effect,hang_radio,lose_after_name
        self.elements=[Element(self,'Computer audio','radio',checked) for _ in range(radios)]
        self.mic=Element(self,'microphone','switch','true')
        self.camera=Element(self,'camera','switch','true')
        self.leave=Element(self,'Leave','button');self.leave.visible=already_joined
        join=Element(self,'Join now','button');join.visible=not already_joined
        self.elements += [self.mic,self.camera,self.leave,join,
            Element(self,'Continue without audio or video','button'),Element(self,'name','textbox')]
        if already_joined:self.elements.append(Element(self,'Participants','button'))
        self.frames=[self];self.evaluate=AsyncMock(return_value=None)
    def locator(self,selector):
        return StatusRegion(self) if selector.startswith('[role="status"]') else self
    async def inner_text(self):return 'local UI fixture'
    def get_by_role(self,role,*,name=None):
        return Collection(self,[e for e in self.elements if e.role==role and (name is None or name.search(e.label))])
    def get_by_placeholder(self,name):return self.get_by_role('textbox',name=name)
    def is_detached(self):return False
    def is_closed(self):return False


class ResponseComputerAudioUITests(unittest.IsolatedAsyncioTestCase):
    def meeting(self,page,*,response=True):
        events=[]
        async def event(kind,data):events.append((kind,data))
        meeting=MeetingBrowser(None,None,event,response_mode=response)
        meeting.page=page;meeting.context=SimpleNamespace(close=AsyncMock())
        meeting.stats=AsyncMock(side_effect=AssertionError('UI failure must not probe media'))
        return meeting,events

    async def join(self,meeting,timeout=1):
        original_sleep=asyncio.sleep
        async def yield_only(seconds):await original_sleep(0)
        with patch('teams_transcription.browser.asyncio.sleep',new=yield_only):
            await meeting.join('teams','fixture',timeout,lambda:False)

    async def test_response_mode_selects_and_verifies_before_join_with_mic_camera_off(self):
        for checked in ['false','true']:
            with self.subTest(checked=checked):
                page=Page(checked=checked);meeting,events=self.meeting(page)
                await self.join(meeting)
                self.assertNotIn('Continue without audio or video',page.trace)
                self.assertEqual(page.trace.count('Computer audio'),int(checked=='false'))
                self.assertIn('Join now',page.trace)
                self.assertTrue(meeting._response_computer_audio_verified)
                self.assertEqual((page.mic.checked,page.camera.checked),('false','false'))
                self.assertEqual(meeting.join_diagnostic()['actions']['computer_audio'],'completed')
                await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_missing_ambiguous_unselected_or_changed_choice_fail_before_join(self):
        for page,code in [(Page(radios=0),'missing'),(Page(radios=2),'ambiguous'),
            (Page(radio_effect=False),'not_selected'),(Page(checked='true',lose_after_name=True),'not_selected')]:
            with self.subTest(code=code):
                meeting,events=self.meeting(page)
                if code=='missing':
                    with self.assertRaises(TimeoutError):await self.join(meeting,timeout=.04)
                else:
                    with self.assertRaisesRegex(RuntimeError,'response_computer_audio_'+code):await self.join(meeting)
                self.assertNotIn('Join now',page.trace)
                self.assertNotIn('Continue without audio or video',page.trace)
                self.assertFalse(meeting._response_computer_audio_verified)
                self.assertEqual(meeting.join_diagnostic()['outcome'],'timeout' if code=='missing' else 'error')
                await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_already_joined_without_prejoin_verification_is_not_reported_success(self):
        meeting,events=self.meeting(Page(already_joined=True))
        with self.assertRaisesRegex(RuntimeError,'response_computer_audio_not_verified'):await self.join(meeting)
        self.assertFalse(any(kind=='joined' for kind,data in events))
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_rx_only_preserves_without_audio_fallback(self):
        page=Page(radios=0);meeting,events=self.meeting(page,response=False)
        await self.join(meeting)
        self.assertIn('Continue without audio or video',page.trace)
        self.assertIn('Join now',page.trace)
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_hanging_choice_is_cancelled_by_existing_join_bound_and_cleaned(self):
        page=Page(hang_radio=True);meeting,events=self.meeting(page)
        with self.assertRaises(TimeoutError):await self.join(meeting,timeout=.04)
        self.assertTrue(page.radio_entered.is_set());self.assertTrue(page.radio_cancelled)
        self.assertNotIn('Join now',page.trace)
        self.assertEqual(meeting.join_diagnostic()['outcome'],'timeout')
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_formal_worker_missing_choice_fails_before_live_and_closes_context(self):
        from test_rx_contract import Fixture, config
        from teams_transcription.storage import Session
        from teams_transcription.worker import run_session
        page=Page(radios=0);context=SimpleNamespace(close=AsyncMock())
        instances=[]
        class UIMeeting(MeetingBrowser):
            async def open(self,*args):
                instances.append(self);self.page=page;self.context=context
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(config(rx_session_contract=True,
                rx_test_contract=False,response_enabled=True))
            fixture=Fixture()
            with fixture.install(),patch('teams_transcription.worker.MeetingBrowser',UIMeeting):
                result=await run_session(session,install_signals=False)
            self.assertEqual(result,1)
            self.assertTrue(instances[0].response_mode)
            self.assertEqual(fixture.live_starts,0)
            self.assertEqual(session.status()['state'],'failed')
            self.assertEqual(session.status()['cleanup_status'],'cleaned')
            self.assertNotIn('Join now',page.trace)
            context.close.assert_awaited_once()


if __name__=='__main__':unittest.main()
