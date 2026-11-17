"""Native HTML radio selection readback; only synthetic UI, no browser/model."""
import time,unittest
from teams_transcription.browser import ResponseUIContextError
from teams_transcription.diagnostics import teams_join_diagnostic
from tests import test_response_computer_audio_ui as ui

class NativeRadio(ui.Element):
    def __init__(self,page,checked='false',*,native=True,enabled=True,aria=None):
        super().__init__(page,'Computer audio','radio',checked)
        self.native,self.enabled,self.aria=native,enabled,aria
        self.hook=None
    async def get_attribute(self,key):return self.aria if key=='aria-checked' else None
    async def evaluate(self,script):
        if self.hook:await self.hook()
        return self.native
    async def is_checked(self):return self.checked=='true'
    async def is_enabled(self):return self.enabled

class NativeRadioTests(unittest.IsolatedAsyncioTestCase):
    def setup(self,**kwargs):
        page=ui.Page();radio=NativeRadio(page,**kwargs)
        page.elements=[radio]+[e for e in page.elements if e.role!='radio']
        meeting,events=ui.ResponseComputerAudioUITests().meeting(page)
        meeting._join_state=teams_join_diagnostic({'outcome':'in_progress'})
        return page,radio,meeting,events
    async def test_native_radio_without_aria_is_checked_and_reverified(self):
        for checked in ['false','true']:
            page,radio,meeting,events=self.setup(checked=checked)
            try:
                await ui.ResponseComputerAudioUITests().join(meeting,.2)
                self.assertTrue(meeting._response_computer_audio_verified)
                self.assertEqual(page.trace.count('Computer audio'),int(checked=='false'))
                self.assertEqual(page.trace.count('Join now'),1)
            finally:await meeting.close()
    async def test_absent_aria_does_not_accept_non_native_disabled_or_unselected_controls(self):
        for kind in ['non_native','disabled','unselected']:
            page,radio,meeting,events=self.setup(native=kind!='non_native',enabled=kind!='disabled')
            if kind=='unselected':page.radio_effect=False
            try:
                with self.assertRaisesRegex(RuntimeError,'not_selected'):
                    await ui.ResponseComputerAudioUITests().join(meeting,.2)
                self.assertFalse(meeting._response_computer_audio_verified)
                self.assertNotIn('Join now',page.trace)
            finally:await meeting.close()
    async def test_present_aria_keeps_its_exact_original_rule(self):
        for aria,expected in [('true',True),('false',False),('mixed',False),('invalid',False)]:
            page,radio,meeting,events=self.setup(checked='true',aria=aria)
            try:
                self.assertEqual(await meeting._response_radio_selected(radio,time.monotonic()+1,lambda:False,page,tuple(page.frames)),expected)
            finally:await meeting.close()
    async def test_stop_and_context_loss_after_native_type_query_block_readback(self):
        for kind in ['stop','roster','deadline']:
            page,radio,meeting,events=self.setup(checked='true');flag=[False]
            deadline=time.monotonic()+1
            async def hook():
                if kind=='stop':flag[0]=True
                elif kind=='roster':page.frames=[]
                else:
                    import asyncio
                    await asyncio.sleep(.025)
            radio.hook=hook
            if kind=='deadline':deadline=time.monotonic()+.01
            try:
                with self.assertRaises((InterruptedError,TimeoutError,ResponseUIContextError)):
                    await meeting._response_radio_selected(radio,deadline,lambda:flag[0],page,tuple(page.frames))
                self.assertNotIn('Join now',page.trace)
            finally:await meeting.close()
if __name__=='__main__':unittest.main(verbosity=2)
