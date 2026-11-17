"""Only synthetic pages; no live DOM/browser/model or private meeting data."""
import json,time,unittest
from playwright.async_api import Error
from teams_transcription.diagnostics import prejoin_ui_diagnostic,computer_audio_wait_diagnostic,teams_join_diagnostic
from tests import test_response_computer_audio_ui as ui

class PrivacyPage(ui.Page):
    def __init__(self):
        super().__init__(radios=0)
        self.url="https://teams.microsoft.com/PRIVATE_MEETING?token=PRIVATE_TOKEN"
        self.elements=[ui.Element(self,"PRIVATE_PERSON_NAME","button"),
                       ui.Element(self,"PRIVATE_MEETING_TITLE","heading"),
                       ui.Element(self,"PRIVATE_CHAT_TEXT","dialog"),
                       ui.Element(self,"This browser is not supported","generic")]
        self.evaluate.return_value="complete"
        self.queries=0
    def get_by_role(self,role,*,name=None):
        self.queries+=1
        return super().get_by_role(role,name=name)
    def get_by_text(self,pattern):
        self.queries+=1
        return ui.Collection(self,[e for e in self.elements if pattern.search(e.label)])

class PrejoinUIPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def setup(self,page=None):
        page=page or PrivacyPage()
        meeting,events=ui.ResponseComputerAudioUITests().meeting(page)
        meeting._response_join_started_at=time.monotonic()
        return page,meeting,events
    def test_hostile_values_are_fixed_bounded_and_private_text_is_removed(self):
        for unsafe in [["PRIVATE"],{"PRIVATE":"TOKEN"}]:
            raw={"category":"PRIVATE","category_candidates":["PRIVATE",unsafe],
                 "origin_kind":unsafe,"sample_scope":unsafe,"latest_attempt_error":unsafe,
                 "role_counts":{"button":10**30,"PRIVATE_PERSON":4},
                 "safe_name_counts":{"computer_audio_radio":True,"PRIVATE_NAME":1},
                 "unknown_name_counts":{"button":-1},"url":"PRIVATE_URL",
                 "html":"PRIVATE_HTML","title":"PRIVATE_TITLE","token":"PRIVATE_TOKEN",
                 "observed_elapsed_ms":10**30,"sample_age_ms":-1}
            result=prejoin_ui_diagnostic(raw)
            self.assertEqual(result["role_counts"]["button"],65535)
            self.assertEqual(result["origin_kind"],"unknown")
            self.assertEqual(result["sample_scope"],"not_available")
            self.assertEqual(result["observed_elapsed_ms"],900000)
            self.assertIsNone(result["sample_age_ms"])
            self.assertNotIn("PRIVATE",json.dumps(result))
    async def test_safe_ui_category_and_unknown_name_counts_without_content(self):
        page,meeting,events=self.setup()
        receipt={}
        try:
            await meeting._response_context_counts(receipt,time.monotonic()+1,lambda:False)
            meeting._join_state=teams_join_diagnostic({"outcome":"in_progress"})
            meeting._join_state["computer_audio_wait"]=receipt
            result=meeting.join_diagnostic()["computer_audio_wait"]["ui_snapshot"]
            self.assertEqual(result["category"],"unsupported_browser")
            self.assertEqual(result["role_counts"]["dialog"],1)
            self.assertEqual(result["safe_name_counts"]["unsupported_message"],1)
            self.assertEqual(result["unknown_name_counts"]["button"],1)
            self.assertEqual(result["origin_kind"],"teams")
            self.assertEqual(result["sample_scope"],"last_complete_observation")
            self.assertIsInstance(result["sample_age_ms"],int)
            self.assertNotIn("PRIVATE",json.dumps(result))
            self.assertNotIn("https://",json.dumps(result))
            self.assertEqual(page.trace,[])
        finally:await meeting.close()
    async def test_optional_sdk_error_retains_prior_snapshot_without_error_text(self):
        page,meeting,events=self.setup();receipt={}
        try:
            await meeting._response_context_counts(receipt,time.monotonic()+1,lambda:False)
            previous=receipt["ui_snapshot"].copy();original=page.get_by_role
            async def fail():raise Error("PRIVATE_ERROR_WITH_TOKEN")
            class Fault:
                count=staticmethod(fail)
            def roles(role,*,name=None):
                return Fault() if role=="radio" else original(role,name=name)
            page.get_by_role=roles
            await meeting._response_ui_snapshot(receipt,time.monotonic()+1,lambda:False,tuple(page.frames))
            self.assertEqual(receipt["ui_snapshot"],previous)
            meeting._join_state=teams_join_diagnostic({"outcome":"in_progress"})
            meeting._join_state["computer_audio_wait"]=receipt
            result=meeting.join_diagnostic()["computer_audio_wait"]["ui_snapshot"]
            self.assertEqual(result["sample_scope"],"previous_complete_observation")
            self.assertEqual(result["latest_attempt_error"],"playwright_error")
            self.assertNotIn("PRIVATE",json.dumps(result))
        finally:await meeting.close()
    async def test_stop_or_expired_deadline_prevents_observer_queries(self):
        for stopped in [False,True]:
            page,meeting,events=self.setup();initial=page.queries
            try:
                with self.assertRaises(InterruptedError if stopped else TimeoutError):
                    await meeting._response_ui_snapshot({},time.monotonic()-1 if not stopped else time.monotonic()+1,lambda:stopped,tuple(page.frames))
                self.assertEqual(page.queries,initial)
                self.assertEqual(page.trace,[])
            finally:await meeting.close()
    async def test_invalidated_snapshot_is_explicitly_historical(self):
        page,meeting,events=self.setup();receipt={}
        try:
            await meeting._response_context_counts(receipt,time.monotonic()+1,lambda:False)
            receipt["_ui_invalidated"]=True
            meeting._join_state=teams_join_diagnostic({"outcome":"in_progress"})
            meeting._join_state["computer_audio_wait"]=receipt
            result=meeting.join_diagnostic()["computer_audio_wait"]["ui_snapshot"]
            self.assertEqual(result["sample_scope"],"before_context_invalidation")
            self.assertNotIn("PRIVATE",json.dumps(result))
        finally:await meeting.close()
    async def test_diagnostic_category_never_bypasses_unique_radio_gate(self):
        page=PrivacyPage();page.elements=[ui.Element(page,"Computer audio","radio") for _ in range(2)]
        page,meeting,events=self.setup(page)
        try:
            with self.assertRaisesRegex(RuntimeError,"ambiguous"):
                await meeting.join("teams","synthetic",.2,lambda:False)
            self.assertFalse(meeting._response_computer_audio_verified)
            self.assertNotIn("Computer audio",page.trace)
            self.assertNotIn("Join now",page.trace)
        finally:await meeting.close()
if __name__=="__main__":unittest.main(verbosity=2)
