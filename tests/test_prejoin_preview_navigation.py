"""Synthetic delayed device-preview modal; no meetings, browser or models."""
import asyncio,time,unittest
from unittest.mock import patch
import shutil
from teams_transcription.browser import Browser, MeetingBrowser
from playwright.async_api import Error
from teams_transcription.browser import ResponseUIContextError
from teams_transcription.diagnostics import teams_join_diagnostic
from tests import test_response_computer_audio_ui as ui

class Dialog(ui.Element):
    def __init__(self,page):
        super().__init__(page,"Set up your mic and camera","dialog")
        self.children=[]
    def get_by_role(self,role,*,name=None):
        return ui.Collection(self.page,[e for e in self.children if e.role==role and (name is None or name.search(e.label))])
    def get_by_text(self,pattern):
        return ui.Collection(self.page,[self] if pattern.search(self.label) else [])

class PreviewButton(ui.Element):
    async def is_enabled(self):
        if self.page.hook:await self.page.hook()
        return self.page.enabled
    async def click(self,**kwargs):
        self.page.trace.append(self.label)
        if self.page.click_error:raise Error("PRIVATE_UNCERTAIN_CLICK")
        self.page.elements=list(self.page.after)
        self.page.modal.visible=False

class PreviewPage(ui.Page):
    def __init__(self,*,delayed=False,after_radios=1,radio_effect=True):
        super().__init__(radios=after_radios,radio_effect=radio_effect)
        self.after=[e for e in self.elements if e.label!='Continue without audio or video']
        self.modal=Dialog(self)
        self.button=PreviewButton(self,"Continue without audio or video","button")
        self.modal.children=[self.button]
        self.modal_elements=[self.modal,self.button]
        self.elements=[] if delayed else self.modal_elements
        self.delayed=delayed;self.scans=0;self.enabled=True;self.hook=None;self.click_error=False
        self.url="https://teams.microsoft.com/SYNTHETIC"
        self.evaluate.return_value="complete"
    def get_by_role(self,role,*,name=None):
        if self.delayed and role=='radio' and name is not None:
            self.scans+=1
            if self.scans>=3:self.elements=self.modal_elements;self.delayed=False
        return super().get_by_role(role,name=name)
    def get_by_text(self,pattern):
        return ui.Collection(self,[e for e in self.elements if pattern.search(e.label)])

class PreviewNavigationTests(unittest.IsolatedAsyncioTestCase):
    def meeting(self,page):
        meeting=ui.ResponseComputerAudioUITests().meeting(page)[0]
        meeting._join_state=teams_join_diagnostic({'outcome':'in_progress'})
        return meeting
    async def test_late_preview_is_navigation_then_actual_radio_readback_before_join(self):
        page=PreviewPage(delayed=True);meeting=self.meeting(page)
        try:
            await ui.ResponseComputerAudioUITests().join(meeting,.3)
            self.assertGreaterEqual(page.scans,3)
            self.assertEqual(page.trace.count('Continue without audio or video'),1)
            self.assertEqual(page.trace.count('Computer audio'),1)
            self.assertLess(page.trace.index('Continue without audio or video'),page.trace.index('Computer audio'))
            self.assertLess(page.trace.index('Computer audio'),page.trace.index('Join now'))
            self.assertTrue(meeting._response_computer_audio_verified)
            receipt=meeting.join_diagnostic()
            self.assertTrue(receipt['preview_navigation_dispatched'])
            self.assertTrue(receipt['preview_navigation_completed'])
        finally:await meeting.close()
    async def test_preview_never_satisfies_radio_requirement(self):
        for count,effect in [(0,True),(2,True),(1,False)]:
            page=PreviewPage(after_radios=count,radio_effect=effect);meeting=self.meeting(page)
            try:
                with self.assertRaises((TimeoutError,RuntimeError)):
                    await ui.ResponseComputerAudioUITests().join(meeting,.1)
                self.assertEqual(page.trace.count('Continue without audio or video'),1)
                self.assertFalse(meeting._response_computer_audio_verified)
                self.assertNotIn('Join now',page.trace)
            finally:await meeting.close()
    async def test_only_exact_unique_dialog_scoped_enabled_preview_without_radio_or_join(self):
        for kind in ['dont_use_audio','join_without_audio','outside','two_dialogs','two_buttons','wrong_message','disabled','radio','join']:
            page=PreviewPage();meeting=self.meeting(page)
            if kind=='dont_use_audio':page.button.label="Don't use audio"
            if kind=='join_without_audio':page.button.label="Join without audio"
            if kind=='outside':page.modal.children=[]
            if kind=='two_dialogs':page.elements.append(Dialog(page))
            if kind=='two_buttons':
                duplicate=PreviewButton(page,page.button.label,'button')
                page.elements.append(duplicate);page.modal.children.append(duplicate)
            if kind=='wrong_message':page.modal.label="PRIVATE_CONTENT"
            if kind=='disabled':page.enabled=False
            if kind=='radio':page.elements.append(ui.Element(page,'Other choice','radio'))
            if kind=='join':page.elements.append(ui.Element(page,'Join now','button'))
            try:
                result=await meeting._response_preview_navigation(time.monotonic()+1,lambda:False,tuple(page.frames))
                self.assertFalse(result,kind);self.assertEqual(page.trace,[],kind)
                self.assertFalse(meeting._response_computer_audio_verified)
            finally:await meeting.close()
    async def test_cross_dialog_and_cross_frame_counts_cannot_authorize_click(self):
        for kind in ['hidden_other_dialog','other_frame','excess_text_matches','excess_dialog_matches']:
            page=PreviewPage();meeting=self.meeting(page)
            if kind=='hidden_other_dialog':
                other=Dialog(page);other.visible=False;other.children=[page.button]
                page.modal.children=[];page.elements.append(other)
            elif kind=='other_frame':
                other=ui.Page(radios=0);other.elements=[page.button]
                page.modal.children=[];page.elements=[page.modal];page.frames.append(other)
            elif kind=='excess_text_matches':
                page.modal.get_by_text=lambda pattern:ui.Collection(page,[page.modal]*17)
            else:
                page.elements.extend(Dialog(page) for _ in range(16))
            try:
                result=await meeting._response_preview_navigation(time.monotonic()+1,lambda:False,tuple(page.frames))
                self.assertFalse(result,kind);self.assertEqual(page.trace,[],kind)
            finally:await meeting.close()
    async def test_stop_deadline_and_roster_change_before_click_block_action(self):
        for kind in ['stop','deadline','roster']:
            page=PreviewPage();meeting=self.meeting(page);flag=[False]
            deadline=time.monotonic()+1
            async def hook():
                if kind=='stop':flag[0]=True
                elif kind=='deadline':await asyncio.sleep(.025)
                else:page.frames=[]
            page.hook=hook
            if kind=='deadline':deadline=time.monotonic()+.01
            try:
                with self.assertRaises((InterruptedError,TimeoutError,ResponseUIContextError)):
                    await meeting._response_preview_navigation(deadline,lambda:flag[0],tuple(page.frames))
                self.assertEqual(page.trace,[])
            finally:await meeting.close()
    async def test_uncertain_click_is_fatal_and_not_reissued(self):
        page=PreviewPage();page.click_error=True;meeting=self.meeting(page)
        try:
            with self.assertRaises(Error):
                await meeting._response_preview_navigation(time.monotonic()+1,lambda:False,tuple(page.frames))
            self.assertFalse(await meeting._response_preview_navigation(time.monotonic()+1,lambda:False,tuple(page.frames)))
            self.assertEqual(page.trace,['Continue without audio or video'])
            self.assertFalse(meeting._response_computer_audio_verified)
            self.assertFalse(meeting.join_diagnostic()['join_dispatch_started'])
        finally:await meeting.close()
    async def test_verify_stage_never_dispatches_preview_navigation(self):
        page=PreviewPage();meeting=self.meeting(page)
        try:
            with self.assertRaises(TimeoutError):
                await meeting._response_computer_audio(select=False,deadline=time.monotonic()+.04,stopped=lambda:False)
            self.assertEqual(page.trace,[])
        finally:await meeting.close()
@unittest.skipUnless(shutil.which('chromium'), 'Chromium fixture unavailable')
class LatePreviewBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_modal_after_audio_selection_is_dismissed_before_single_join(self):
        await self.check_late_modal('audio')

    async def test_modal_after_name_fill_rechecks_audio_before_single_join(self):
        await self.check_late_modal('name')

    async def check_late_modal(self, trigger):
        html = """<!doctype html><html><body>
        <button role="radio" aria-label="Computer audio" aria-checked="false" id="audio"
          onclick="this.setAttribute('aria-checked','true'); events.push('audio'); show('audio')">
          Computer audio</button>
        <input aria-label="name" placeholder="name" oninput="show('name')">
        <button id="join" onclick="events.push('join');this.hidden=true;
          document.querySelector('#leave').hidden=false;document.querySelector('#people').hidden=false">Join now</button>
        <button id="leave" hidden>Leave</button><button id="people" hidden>Participants</button>
        <div id="overlay" hidden style="position:fixed;inset:0;background:#ddd;z-index:10">
          <div role="dialog" aria-modal="true">
            <p>Are you sure you don't want audio or video? Make sure no other app is using your camera and mic.</p>
            <button onclick="events.push('preview');document.querySelector('#overlay').hidden=true;
              document.querySelector('#audio').setAttribute('aria-checked','false')">Continue without audio or video</button>
          </div>
        </div>
        <script>window.events=[];let shown=false;
          function show(stage){if(stage==='TRIGGER'&&!shown){shown=true;document.querySelector('#overlay').hidden=false}}
        </script></body></html>""".replace('TRIGGER',trigger)
        async with Browser() as runtime:
            context=await runtime.browser.new_context(permissions=[])
            await context.route('**/*',lambda route:route.fulfill(content_type='text/html',body=html))
            page=await context.new_page()
            await page.goto('https://teams.microsoft.com/synthetic-prejoin')
            async def event(*args):pass
            meeting=MeetingBrowser(runtime.browser,None,event,response_mode=True)
            meeting.page=page;meeting.context=context
            try:
                await meeting.join('teams','fixture',8,lambda:False)
                self.assertEqual(await page.evaluate('events'),['audio','preview','audio','join'])
                self.assertEqual(await page.locator('#audio').get_attribute('aria-checked'),'true')
                self.assertTrue(await meeting.admitted())
            finally:
                await meeting.close()

if __name__=='__main__':unittest.main(verbosity=2)
