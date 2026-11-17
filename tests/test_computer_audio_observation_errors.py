"""Auxiliary SDK errors are isolated; actual primary UI/guards stay strict."""
import asyncio
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.async_api import Error, TimeoutError as PlaywrightTimeoutError
from playwright._impl._errors import TargetClosedError
from teams_transcription.browser import ResponseUIContextError
from teams_transcription.diagnostics import computer_audio_wait_diagnostic
from tests import test_response_computer_audio_ui as ui
from tests import test_computer_audio_wait as waiting


class FaultCollection:
    def __init__(self,operation,delegate=None):
        self.operation=operation
        if delegate is not None:self.first=delegate.first
    async def count(self):return await self.operation()


class FaultPage(ui.Page):
    def __init__(self,target='all_radio_count',*,effect=None,exc=None):
        super().__init__();self.target=target;self.ready=False;self.failed=False
        self.effect=effect;self.error=exc or Error('PRIVATE_OBSERVER_TEXT')
        self.closed=False;self.detached=False;self.entered=asyncio.Event()
        self.evaluate=self.evaluate_page
    def is_closed(self):return self.closed
    def is_detached(self):return self.detached
    async def fail(self):
        self.failed=True;self.ready=True;self.entered.set()
        if self.effect:await self.effect(self)
        raise self.error
    async def evaluate_page(self,script):
        if script=='() => document.readyState' and self.target=='document_state' and not self.failed:
            return await self.fail()
        return 'complete'
    def get_by_role(self,role,*,name=None):
        key=('all_radio_count' if role=='radio' and name is None else
             'dialog_count' if role=='dialog' else
             'known_join_button_count' if role=='button' and name and name.search('Join now') else
             'known_browser_button_count' if role=='button' and name and name.search('Continue on this browser') else None)
        if role=='radio' and name is not None:
            return super().get_by_role(role,name=name) if self.ready else ui.Collection(self,[])
        if key==self.target and not self.failed:
            return FaultCollection(self.fail,super().get_by_role(role,name=name))
        return super().get_by_role(role,name=name)


class ObservationErrorTests(unittest.IsolatedAsyncioTestCase):
    def meeting(self,page):return ui.ResponseComputerAudioUITests().meeting(page)
    async def join(self,meeting,**kwargs):await waiting.ComputerAudioWaitTests().join(meeting,**kwargs)

    async def test_each_optional_count_and_document_error_can_precede_radio_readiness(self):
        for target in ['all_radio_count','dialog_count','known_browser_button_count','known_join_button_count','document_state']:
            with self.subTest(target=target):
                page=FaultPage(target);meeting,events=self.meeting(page)
                await self.join(meeting)
                receipt=meeting.join_diagnostic()['computer_audio_wait']
                self.assertEqual(receipt['auxiliary_error_observation'],target)
                self.assertEqual(receipt['auxiliary_exception_type'],'Error')
                self.assertTrue(receipt['observation_error'])
                self.assertFalse(receipt['context_counts_checked'])
                self.assertEqual(receipt['wait_outcome'],'became_visible')
                self.assertEqual(page.trace.count('Join now'),1)
                self.assertNotIn('PRIVATE',json.dumps(events))
                await meeting.close();meeting.context.close.assert_awaited_once()
        page=FaultPage(exc=PlaywrightTimeoutError('PRIVATE_TIMEOUT'));meeting,events=self.meeting(page)
        await self.join(meeting)
        self.assertEqual(meeting.join_diagnostic()['computer_audio_wait']['auxiliary_error_kind'],'timeout')
        self.assertEqual(page.trace.count('Join now'),1)
        await meeting.close()

    async def test_optional_failure_does_not_commit_partial_counts(self):
        page=FaultPage('dialog_count');meeting,events=self.meeting(page)
        receipt=dict(all_radio_count=41,dialog_count=42,known_browser_button_count=43,known_join_button_count=44)
        await meeting._response_context_counts(receipt,deadline=__import__('time').monotonic()+1,stopped=lambda:False)
        self.assertEqual([receipt[k] for k in ['all_radio_count','dialog_count','known_browser_button_count','known_join_button_count']],[41,42,43,44])
        self.assertFalse(receipt['context_counts_checked'])
        self.assertEqual(receipt['auxiliary_error_observation'],'dialog_count')
        await meeting.close()

    async def test_auxiliary_cancel_is_not_swallowed(self):
        async def hang(page):await asyncio.Event().wait()
        page=FaultPage(effect=hang);meeting,events=self.meeting(page)
        task=asyncio.create_task(self.join(meeting))
        await asyncio.wait_for(page.entered.wait(),.2);task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(meeting.join_diagnostic()['computer_audio_wait']['wait_outcome'],'cancelled')
        self.assertNotIn('Join now',page.trace)
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_stop_and_deadline_raised_during_auxiliary_error_still_terminate(self):
        for expired in [False,True]:
            with self.subTest(expired=expired):
                flag=[False];clock=[10.]
                async def trip(page):
                    if expired:clock[0]=20.
                    else:flag[0]=True
                page=FaultPage(effect=trip);meeting,events=self.meeting(page)
                with patch('teams_transcription.browser.time',SimpleNamespace(monotonic=lambda:clock[0])):
                    with self.assertRaises(TimeoutError if expired else InterruptedError):
                        await self.join(meeting,stopped=lambda:flag[0])
                self.assertNotIn('Join now',page.trace)
                await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_closed_page_or_live_target_closed_context_do_not_continue(self):
        for mode in ['closed','target_closed']:
            with self.subTest(mode=mode):
                async def invalidate(page):
                    if mode=='closed':page.closed=True
                    if mode=='detached':page.detached=True
                    if mode=='removed':page.frames=[]
                page=FaultPage(effect=invalidate,exc=TargetClosedError('PRIVATE') if mode=='target_closed' else None)
                meeting,events=self.meeting(page)
                with self.assertRaises(TargetClosedError if mode=='target_closed' else ResponseUIContextError):
                    await self.join(meeting)
                self.assertNotIn('Join now',page.trace)
                self.assertNotIn('Computer audio',page.trace)
                await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_main_exact_selector_error_is_not_isolated(self):
        page=ui.Page();meeting,events=self.meeting(page);original=page.get_by_role
        error=Error('PRIVATE_PRIMARY_SELECTOR')
        async def fail():raise error
        def roles(role,*,name=None):
            if role=='radio' and name is not None:return FaultCollection(fail)
            return original(role,name=name)
        page.get_by_role=roles
        with self.assertRaises(Error) as caught:await self.join(meeting)
        self.assertIs(caught.exception,error)
        self.assertEqual(meeting.join_diagnostic()['computer_audio_wait']['auxiliary_error_count'],0)
        self.assertNotIn('PRIVATE',json.dumps(events))
        await meeting.close()

    async def test_frame_invalidated_at_final_radio_or_join_await_never_clicks(self):
        for boundary in ['radio','join']:
            with self.subTest(boundary=boundary):
                page=ui.Page(checked='false' if boundary=='radio' else 'true')
                dead=[False];page.is_detached=lambda:dead[0]
                meeting,events=self.meeting(page)
                if boundary=='radio':
                    radio=next(e for e in page.elements if e.role=='radio')
                    original=radio.get_attribute
                    async def read(key):
                        value=await original(key);dead[0]=True;return value
                    radio.get_attribute=read
                else:
                    button=next(e for e in page.elements if e.label=='Join now')
                    async def visible():dead[0]=True;return True
                    button.is_visible=visible
                with self.assertRaises(ResponseUIContextError):await self.join(meeting)
                self.assertNotIn('Join now',page.trace)
                self.assertNotIn('Computer audio',page.trace)
                await meeting.close();meeting.context.close.assert_awaited_once()

    def test_observation_error_fields_are_fixed_and_bounded(self):
        receipt=computer_audio_wait_diagnostic({'auxiliary_error_kind':'PRIVATE',
            'auxiliary_exception_type':'PRIVATE','auxiliary_error_observation':'PRIVATE',
            'auxiliary_error_count':10**20,'observation_error':True,'text':'PRIVATE'})
        self.assertEqual(receipt['auxiliary_error_count'],65535)
        self.assertNotIn('PRIVATE',json.dumps(receipt))


if __name__=='__main__':unittest.main()
