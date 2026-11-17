"""Saved numeric receipt and synthetic locator replay; no real DOM/browser/model."""
import asyncio,json,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from teams_transcription.diagnostics import computer_audio_wait_diagnostic
from tests import test_response_computer_audio_ui as ui
from tests import test_computer_audio_wait as waiting
from tests import test_computer_audio_frame_lifecycle as frames

FIXTURE=Path(__file__).parent/"fixtures/trial57_computer_audio_receipt.json"
class Trial57ReceiptScopeTests(unittest.IsolatedAsyncioTestCase):
    def test_saved_receipt_marks_prior_complete_counts(self):
        value=json.loads(FIXTURE.read_text())
        self.assertFalse(value["context_counts_checked"])
        result=computer_audio_wait_diagnostic(value)
        self.assertEqual(result["context_counts_scope"],"previous_complete_missing_scan")
        self.assertEqual((result["all_radio_count"],result["dialog_count"]),(0,1))
        self.assertEqual(result["invalidation_scope"],"last_recorded_invalidation")
    def test_initial_or_discarded_scan_is_not_previous_complete_counts(self):
        for value in [{},dict(context_counts_checked=False,document_state="unknown"),
                      dict(all_radio_count=0,dialog_count=1,context_counts_checked=False)]:
            self.assertEqual(computer_audio_wait_diagnostic(value)["context_counts_scope"],"not_checked")
    def test_current_complete_scan_preserves_existing_scope(self):
        result=computer_audio_wait_diagnostic(dict(context_counts_checked=True))
        self.assertEqual(result["context_counts_scope"],"last_missing_scan")
        self.assertEqual(result["invalidation_scope"],"none")
    def test_invalid_or_private_values_cannot_claim_prior_complete_scope(self):
        value=dict(context_counts_checked=False,document_state="complete",
                   all_radio_count="PRIVATE",dialog_count=1,
                   known_browser_button_count=0,known_join_button_count=0,
                   invalidation_scope="PRIVATE",url="PRIVATE")
        result=computer_audio_wait_diagnostic(value)
        self.assertEqual(result["context_counts_scope"],"not_checked")
        self.assertNotIn("PRIVATE",json.dumps(result))
    async def test_receipt_shape_replay_preserves_fail_closed_after_five_recoveries(self):
        # Replays only the numeric shape; actual dialog contents were not saved.
        page=ui.Page(radios=0)
        page.elements=[ui.Element(page,"PRIVATE_SYNTHETIC_DIALOG","dialog")]
        page.evaluate.return_value="complete"
        remaining=[5]
        class SyntheticFrame(frames.Frame):
            def get_by_role(self,role,*,name=None):
                return super().get_by_role(role,name=name)
        async def replace(old):
            old.detached=True;remaining[0]-=1
            fresh=SyntheticFrame(page)
            if remaining[0]>0:fresh.hook=("primary",replace)
            page.frames=[fresh]
        first=SyntheticFrame(page);first.hook=("primary",replace);page.frames=[first]
        meeting,events=ui.ResponseComputerAudioUITests().meeting(page)
        started=time.monotonic();clock=[10.];observations=[0]
        original=meeting._response_context_counts
        async def observe(*args,**kwargs):
            observations[0]+=1
            if observations[0]==2:clock[0]=20.
            return await original(*args,**kwargs)
        meeting._response_context_counts=observe
        try:
            with patch("teams_transcription.browser.time",SimpleNamespace(monotonic=lambda:clock[0])):
                with self.assertRaises(TimeoutError):
                    await waiting.ComputerAudioWaitTests().join(meeting,timeout=.4)
            self.assertLess(time.monotonic()-started,.8)
            receipt=meeting.join_diagnostic()["computer_audio_wait"]
            self.assertEqual(receipt["classification"],"radio_absent")
            self.assertEqual(receipt["rescan_count"],5)
            self.assertEqual((receipt["exact_radio_count"],receipt["visible_radio_count"],receipt["all_radio_count"],receipt["dialog_count"]),(0,0,0,1))
            self.assertTrue(receipt["primary_scan_complete"])
            self.assertFalse(receipt["context_counts_checked"])
            self.assertTrue(receipt["frame_detached"])
            self.assertEqual(receipt["context_counts_scope"],"previous_complete_missing_scan")
            self.assertEqual(receipt["invalidation_scope"],"last_recorded_invalidation")
            self.assertNotIn("Computer audio",page.trace)
            self.assertNotIn("Join now",page.trace)
            self.assertFalse(meeting._response_computer_audio_verified)
            self.assertNotIn("PRIVATE",json.dumps(events))
        finally:
            await meeting.close()
            meeting.context.close.assert_awaited_once()
if __name__=="__main__":unittest.main(verbosity=2)
