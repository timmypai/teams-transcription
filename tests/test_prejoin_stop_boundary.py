"""No browser/Teams/model: prove the actual helper cannot dispatch Join."""
import importlib.util,pathlib,unittest
from unittest.mock import AsyncMock
from tests import test_response_computer_audio_ui as ui
path=pathlib.Path(__file__).resolve().parents[1]/".validation/prejoin_probe.py"
spec=importlib.util.spec_from_file_location("prejoin_probe",path)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
class PrejoinStopBoundaryTests(unittest.IsolatedAsyncioTestCase):
    def setup(self):
        page=ui.Page();page.evaluate.return_value="complete"
        for item in page.elements:item.is_enabled=AsyncMock(return_value=True)
        meeting=module.PrejoinOnlyBrowser(None,None,AsyncMock(),response_mode=True)
        meeting.page=page
        meeting.context=type("Context",(),{"close":AsyncMock()})()
        return page,meeting
    async def test_real_helper_selects_readback_but_never_clicks_join(self):
        page,meeting=self.setup()
        try:
            with self.assertRaises(module.JoinBoundaryReached):
                await ui.ResponseComputerAudioUITests().join(meeting)
            self.assertTrue(meeting._response_computer_audio_verified)
            self.assertEqual(page.trace.count("Computer audio"),1)
            self.assertNotIn("Join now",page.trace)
            self.assertFalse(meeting.join_diagnostic()["join_dispatch_started"])
            self.assertTrue(meeting.boundary["reached"])
        finally:await meeting.close()
    async def test_disabled_or_multiple_join_candidates_are_not_passed_or_clicked(self):
        for mode in ["disabled","multiple"]:
            page,meeting=self.setup()
            if mode=="disabled":
                next(x for x in page.elements if x.label=="Join now").is_enabled.return_value=False
            else:
                extra=ui.Element(page,"Join now","button");extra.is_enabled=AsyncMock(return_value=True);page.elements.append(extra)
            try:
                with self.assertRaises(module.JoinBoundaryNotReady):
                    await ui.ResponseComputerAudioUITests().join(meeting)
                self.assertNotIn("Join now",page.trace)
                self.assertFalse(meeting.boundary["reached"])
            finally:await meeting.close()
if __name__=="__main__":unittest.main(verbosity=2)
