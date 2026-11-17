"""Actual join/helper deadline and safe snapshots on synthetic UI only."""
import asyncio
import json
import time
import unittest
from unittest.mock import patch

from teams_transcription.diagnostics import computer_audio_wait_diagnostic
from tests import test_response_computer_audio_ui as ui_fixture
Page,Collection,Element=ui_fixture.Page,ui_fixture.Collection,ui_fixture.Element


class DelayedPage(Page):
    def __init__(self,delay):
        super().__init__();self.visible_at=time.monotonic()+delay
    def get_by_role(self,role,*,name=None):
        if role=='radio' and time.monotonic()<self.visible_at:return Collection(self,[])
        return super().get_by_role(role,name=name)


class ComputerAudioWaitTests(unittest.IsolatedAsyncioTestCase):
    def meeting(self,page):return ui_fixture.ResponseComputerAudioUITests().meeting(page)
    async def join(self,meeting,*,timeout=.3,stopped=lambda:False):
        original=asyncio.sleep
        async def sleep(delay):await original(0 if delay>=.1 else delay)
        with patch('teams_transcription.browser.asyncio.sleep',new=sleep):
            await meeting.join('teams','fixture',timeout,stopped)

    async def test_delayed_radio_waits_read_only_and_dispatches_join_once(self):
        page=DelayedPage(.03);meeting,events=self.meeting(page)
        await self.join(meeting)
        receipt=meeting.join_diagnostic()['computer_audio_wait']
        self.assertEqual(receipt['wait_outcome'],'became_visible')
        self.assertEqual(receipt['classification'],'checked')
        self.assertGreaterEqual(receipt['poll_count'],3)
        self.assertTrue(receipt['context_counts_checked'])
        self.assertEqual(receipt['context_counts_scope'],'last_missing_scan')
        self.assertEqual(page.trace.count('Join now'),1)
        self.assertEqual(page.trace.count('Computer audio'),1)
        self.assertNotIn('Continue without audio or video',page.trace)
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_permanent_missing_stops_at_same_join_deadline_and_preserves_counts(self):
        page=Page(radios=0)
        page.elements.append(Element(page,'PRIVATE_DIALOG_TEXT','dialog'))
        page.evaluate.return_value='loading'
        meeting,events=self.meeting(page);start=time.monotonic()
        with self.assertRaises(TimeoutError):await self.join(meeting,timeout=.04)
        self.assertLess(time.monotonic()-start,.2)
        receipt=meeting.join_diagnostic()['computer_audio_wait']
        self.assertEqual(receipt['wait_outcome'],'deadline')
        self.assertEqual(receipt['classification'],'radio_absent')
        self.assertEqual((receipt['exact_radio_count'],receipt['dialog_count']),(0,1))
        self.assertEqual(receipt['document_state'],'loading')
        self.assertNotIn('PRIVATE',json.dumps(events))
        self.assertNotIn('Join now',page.trace)
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_cancellation_while_missing_keeps_terminal_wait_receipt_and_cleans(self):
        page=Page(radios=0);meeting,events=self.meeting(page)
        task=asyncio.create_task(self.join(meeting))
        async with asyncio.timeout(.2):
            while not meeting.join_diagnostic() or not meeting.join_diagnostic()['computer_audio_wait']:
                await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        self.assertEqual(meeting.join_diagnostic()['computer_audio_wait']['wait_outcome'],'cancelled')
        self.assertNotIn('Join now',page.trace)
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_stop_during_missing_wait_never_dispatches_join(self):
        page=Page(radios=0);meeting,events=self.meeting(page)
        def stopped():return bool(meeting._join_state and meeting._join_state.get('computer_audio_wait'))
        with self.assertRaises(InterruptedError):await self.join(meeting,stopped=stopped)
        self.assertEqual(meeting.join_diagnostic()['computer_audio_wait']['wait_outcome'],'stopped')
        self.assertNotIn('Join now',page.trace)
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_cancel_during_radio_click_preserves_cancellation_receipt_and_cleanup(self):
        page=Page(hang_radio=True);meeting,events=self.meeting(page)
        task=asyncio.create_task(self.join(meeting))
        await asyncio.wait_for(page.radio_entered.wait(),.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):await task
        snapshot=next(data['join'] for kind,data in events if kind=='meeting_join_diagnostics')
        self.assertEqual(snapshot['computer_audio_wait']['wait_outcome'],'cancelled')
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_uncertain_join_dispatch_is_not_repeated(self):
        page=Page();meeting,events=self.meeting(page)
        button=next(e for e in page.elements if e.label=='Join now')
        async def uncertain(**kwargs):
            page.trace.append('Join now')
            raise RuntimeError('PRIVATE_UI_FAILURE')
        button.click=uncertain
        with self.assertRaises(RuntimeError):await self.join(meeting)
        self.assertEqual(page.trace.count('Join now'),1)
        self.assertTrue(meeting.join_diagnostic()['join_dispatch_started'])
        self.assertFalse(meeting.join_diagnostic()['join_dispatch_completed'])
        self.assertNotIn('PRIVATE',json.dumps(events))
        await meeting.close();meeting.context.close.assert_awaited_once()

    async def test_ambiguity_and_unchecked_choice_do_not_wait_or_relax(self):
        for page,reason in [(Page(radios=2),'ambiguous'),(Page(radio_effect=False),'not_selected')]:
            with self.subTest(reason=reason):
                meeting,events=self.meeting(page)
                with self.assertRaisesRegex(RuntimeError,'response_computer_audio_'+reason):await self.join(meeting)
                receipt=meeting.join_diagnostic()['computer_audio_wait']
                self.assertEqual(receipt['classification'],reason)
                self.assertEqual(receipt['wait_outcome'],'error')
                self.assertEqual(receipt['poll_count'],1)
                self.assertNotIn('Join now',page.trace)
                await meeting.close()

    def test_fixed_snapshot_rejects_private_or_unbounded_values(self):
        value=computer_audio_wait_diagnostic({'stage':'PRIVATE','classification':'PRIVATE',
            'wait_outcome':'PRIVATE','document_state':'PRIVATE','dialog_count':10**30,
            'poll_count':True,'exact_radio_count':-1,'text':'PRIVATE','url':'PRIVATE'})
        self.assertEqual(value['dialog_count'],65535)
        self.assertEqual(value['poll_count'],0)
        self.assertEqual(value['exact_radio_count'],0)
        self.assertNotIn('PRIVATE',json.dumps(value))


if __name__=='__main__':unittest.main()
