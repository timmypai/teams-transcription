"""Actual join/helper with replaceable frame rosters; no browser/model I/O."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock
from playwright.async_api import Error

from teams_transcription.browser import ResponseUIContextError, ResponseUIFrameSnapshotError
from teams_transcription.diagnostics import computer_audio_wait_diagnostic
from tests import test_response_computer_audio_ui as ui
from tests import test_computer_audio_wait as waiting


class Count:
    def __init__(self,fn):self.fn=fn
    async def count(self):return await self.fn()


class Frame:
    def __init__(self,page,radios=0):
        self.page=page;self.detached=False;self.hook=None;self.fired=False
        self.radios=[ui.Element(page,'Computer audio','radio') for _ in range(radios)]
        self.evaluate=AsyncMock(return_value=None)
    def is_detached(self):return self.detached
    def locator(self,selector):return ui.Collection(self.page,[])
    def get_by_placeholder(self,name):return self.page.get_by_placeholder(name)
    def get_by_role(self,role,*,name=None):
        key='primary' if role=='radio' and name is not None else 'auxiliary' if role=='radio' else None
        if self.hook and key==self.hook[0] and not self.fired:
            async def count():
                self.fired=True;await self.hook[1](self)
                raise Error('PRIVATE_FRAME_SNAPSHOT')
            return Count(count)
        if role=='radio':return ui.Collection(self.page,[e for e in self.radios if name is None or name.search(e.label)])
        return self.page.get_by_role(role,name=name)


class FrameLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setup(self,radios=0):
        page=ui.Page(radios=0);main=Frame(page,radios);page.frames=[main]
        meeting,events=ui.ResponseComputerAudioUITests().meeting(page)
        return page,main,meeting,events
    async def join(self,meeting,**kwargs):await waiting.ComputerAudioWaitTests().join(meeting,**kwargs)

    async def test_auxiliary_child_removal_or_replacement_restarts_full_same_page_scan(self):
        for replacement in [False,True]:
            with self.subTest(replacement=replacement):
                page,main,meeting,events=self.setup();child=Frame(page);page.frames.append(child)
                async def remove(old):
                    old.detached=True
                    if replacement:page.frames=[main,Frame(page,1)]
                    else:
                        main.radios=[ui.Element(page,'Computer audio','radio')];page.frames=[main]
                child.hook=('auxiliary',remove)
                await self.join(meeting)
                receipt=meeting.join_diagnostic()['computer_audio_wait']
                self.assertEqual(receipt['rescan_count'],1)
                self.assertEqual(receipt['invalidation_stage'],'auxiliary_scan')
                self.assertTrue(receipt['frame_detached']);self.assertFalse(receipt['frame_member'])
                self.assertFalse(receipt['page_closed'])
                self.assertEqual(page.trace.count('Join now'),1)
                self.assertNotIn('PRIVATE',json.dumps(events))
                await meeting.close()

    async def test_preview_read_sdk_error_rescans_only_with_proven_frame_loss(self):
        for replaced in (True, False):
            with self.subTest(replaced=replaced):
                page, old, meeting, events = self.setup()
                page.url = 'https://teams.microsoft.com/v2/'
                meeting._response_context_counts = AsyncMock()
                async def preview_read(frame):
                    if replaced:
                        frame.detached = True
                        page.frames = [Frame(page, 1)]
                old.hook = ('auxiliary', preview_read)
                if replaced:
                    await self.join(meeting)
                    self.assertEqual(page.frames[0].radios[0].checked, 'true')
                    self.assertEqual(page.trace.count('Join now'), 1)
                else:
                    with self.assertRaises(Error):
                        await self.join(meeting)
                    self.assertNotIn('Computer audio', page.trace)
                    self.assertNotIn('Join now', page.trace)
                self.assertNotIn('PRIVATE', json.dumps(events))
                await meeting.close()

    async def test_actual_auxiliary_method_signals_whole_scan_discard_not_page_failure(self):
        page,main,meeting,events=self.setup();child=Frame(page);page.frames.append(child)
        async def remove(old):old.detached=True;page.frames=[main]
        child.hook=('auxiliary',remove)
        receipt={'all_radio_count':41,'context_counts_checked':True}
        with self.assertRaises(ResponseUIFrameSnapshotError):
            await meeting._response_context_counts(receipt,time.monotonic()+1,lambda:False)
        self.assertFalse(page.is_closed())
        self.assertEqual(receipt['all_radio_count'],41)
        self.assertFalse(receipt['context_counts_checked'])
        self.assertNotIn('Join now',page.trace)
        await meeting.close()

    async def test_primary_selector_proven_main_frame_replacement_can_rescan(self):
        page,old,meeting,events=self.setup()
        async def navigate(frame):frame.detached=True;page.frames=[Frame(page,1)]
        old.hook=('primary',navigate)
        await self.join(meeting)
        self.assertIs(meeting.page,page)
        receipt=meeting.join_diagnostic()['computer_audio_wait']
        self.assertEqual(receipt['rescan_count'],1)
        self.assertEqual(receipt['invalidation_stage'],'primary_scan')
        self.assertEqual(page.trace.count('Join now'),1)
        await meeting.close()

    async def test_old_candidate_is_discarded_and_new_multiple_radios_rejected(self):
        for reason in ['frame_detached','frame_removed','roster_changed']:
            with self.subTest(reason=reason):
                page,main,meeting,events=self.setup(1);child=Frame(page);page.frames.append(child)
                stale=main.radios[0]
                async def replace(old):
                    if reason=='frame_detached':old.detached=True
                    page.frames=[main,child,Frame(page,1)] if reason=='roster_changed' else [main,Frame(page,1)]
                child.hook=('primary',replace)
                with self.assertRaisesRegex(RuntimeError,'ambiguous'):await self.join(meeting)
                receipt=meeting.join_diagnostic()['computer_audio_wait']
                self.assertEqual(receipt['rescan_count'],1)
                self.assertEqual(receipt['invalidation_reason'],reason)
                self.assertEqual(receipt['visible_radio_count'],2)
                self.assertEqual(stale.checked,'false')
                self.assertNotIn('Computer audio',page.trace);self.assertNotIn('Join now',page.trace)
                await meeting.close()

    async def test_selected_frame_loss_at_readback_or_join_visibility_remains_fatal(self):
        for stage in ['readback','join']:
            with self.subTest(stage=stage):
                page,main,meeting,events=self.setup(1)
                if stage=='readback':
                    radio=main.radios[0];original=radio.get_attribute
                    async def read(key):
                        value=await original(key);main.detached=True;page.frames=[Frame(page,1)];return value
                    radio.get_attribute=read
                else:
                    main.radios[0].checked='true'
                    button=next(e for e in page.elements if e.label=='Join now')
                    async def shown():main.detached=True;page.frames=[Frame(page,1)];return True
                    button.is_visible=shown
                with self.assertRaises(ResponseUIContextError):await self.join(meeting)
                self.assertEqual(meeting.join_diagnostic()['computer_audio_wait']['rescan_count'],0)
                self.assertNotIn('Computer audio',page.trace);self.assertNotIn('Join now',page.trace)
                await meeting.close()

    async def test_page_close_stop_and_deadline_are_never_snapshot_recovery(self):
        for reason in ['close','stop','deadline']:
            with self.subTest(reason=reason):
                page,main,meeting,events=self.setup();flag=[False]
                async def invalidate(old):
                    old.detached=True;page.frames=[Frame(page,1)]
                    if reason=='close':page.is_closed=lambda:True
                    elif reason=='stop':flag[0]=True
                    else:await asyncio.sleep(.05)
                main.hook=('primary',invalidate)
                expected=ResponseUIContextError if reason=='close' else InterruptedError if reason=='stop' else TimeoutError
                with self.assertRaises(expected):await self.join(meeting,timeout=.04,stopped=lambda:flag[0])
                self.assertNotIn('Join now',page.trace);self.assertNotIn('Computer audio',page.trace)
                await meeting.close()

    async def test_continuous_frame_churn_exits_at_original_deadline_without_join(self):
        page,main,meeting,events=self.setup()
        async def churn(old):
            old.detached=True;fresh=Frame(page);fresh.hook=('primary',churn);page.frames=[fresh]
        main.hook=('primary',churn);start=time.monotonic()
        with self.assertRaises(TimeoutError):await self.join(meeting,timeout=.09)
        self.assertLess(time.monotonic()-start,.2)
        self.assertGreaterEqual(meeting.join_diagnostic()['computer_audio_wait']['rescan_count'],1)
        self.assertNotIn('Join now',page.trace)
        await meeting.close()

    async def test_same_page_replaced_by_different_page_is_fatal(self):
        page,main,meeting,events=self.setup()
        async def replace(old):meeting.page=ui.Page(radios=1)
        main.hook=('primary',replace)
        with self.assertRaises(ResponseUIContextError):await self.join(meeting)
        self.assertEqual(meeting.join_diagnostic()['computer_audio_wait']['invalidation_reason'],'page_replaced')
        self.assertNotIn('Join now',page.trace)
        await meeting.close()

    async def test_radio_already_dispatched_cannot_restart_after_frame_loss(self):
        page,main,meeting,events=self.setup(1)
        async def replace(old):old.detached=True;page.frames=[Frame(page,1)]
        name=next(e for e in page.elements if e.role=='textbox')
        async def filled(text,**kwargs):main.hook=('primary',replace)
        name.fill=filled
        with self.assertRaises(ResponseUIContextError):await self.join(meeting)
        self.assertEqual(page.trace.count('Computer audio'),1)
        self.assertEqual(page.trace.count('Join now'),0)
        self.assertEqual(meeting.join_diagnostic()['computer_audio_wait']['rescan_count'],0)
        await meeting.close()

    async def test_join_dispatch_is_not_reissued_after_roster_change(self):
        page,main,meeting,events=self.setup(1)
        button=next(e for e in page.elements if e.label=='Join now')
        async def clicked(**kwargs):
            page.trace.append('Join now');main.detached=True;page.frames=[Frame(page,1)]
        button.click=clicked
        with self.assertRaises(TimeoutError):await self.join(meeting,timeout=.04)
        self.assertEqual(page.trace.count('Join now'),1)
        self.assertEqual(page.trace.count('Computer audio'),1)
        await meeting.close()

    def test_invalidation_receipt_is_bounded_and_unknown_is_explicit(self):
        receipt=computer_audio_wait_diagnostic({'invalidation_reason':'PRIVATE',
            'invalidation_stage':'PRIVATE','frame_detached':'PRIVATE','frame_member':True,
            'page_closed':False,'rescan_count':10**30,'url':'PRIVATE','id':'PRIVATE'})
        self.assertEqual(receipt['invalidation_reason'],'unknown')
        self.assertEqual(receipt['rescan_count'],65535)
        self.assertTrue(receipt['frame_member']);self.assertFalse(receipt['frame_detached'])
        self.assertNotIn('PRIVATE',json.dumps(receipt))


if __name__=='__main__':unittest.main()
