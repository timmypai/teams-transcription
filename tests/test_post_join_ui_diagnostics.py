"""Synthetic post-Join receipts only: no browser, network, audio or models."""
import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch
from playwright.async_api import Error
from teams_transcription.browser import MeetingBrowser
from teams_transcription.diagnostics import post_join_ui_diagnostic, teams_join_diagnostic
from tests import test_prejoin_ui_privacy as privacy
from tests import test_response_computer_audio_ui as ui

PRIVATE = 'PRIVATE_POSTJOIN_NAME_TOKEN_TEXT'


class NoAdmissionElement(ui.Element):
    async def click(self, **kwargs):
        self.page.trace.append(self.label)
        self.visible = False
        self.page.elements = [ui.Element(self.page, 'Waiting for someone to let you in', 'generic'),
            ui.Element(self.page, PRIVATE, 'heading')]
        self.page.text = 'Waiting for someone to let you in ' + PRIVATE


class PostJoinPage(privacy.PrivacyPage):
    def __init__(self):
        super().__init__()
        self.text = 'synthetic prejoin ' + PRIVATE
        self.elements = [ui.Element(self, 'Computer audio', 'radio'),
            NoAdmissionElement(self, 'Join now', 'button')]
    async def inner_text(self):
        return self.text


class PostJoinUITests(unittest.IsolatedAsyncioTestCase):
    def meeting(self, page=None):
        page = page or privacy.PrivacyPage()
        meeting, events = ui.ResponseComputerAudioUITests().meeting(page)
        meeting._response_join_started_at = time.monotonic()
        meeting._join_state = teams_join_diagnostic({'outcome': 'in_progress', 'phase': 'post_join_wait'})
        return page, meeting, events

    def assert_private_absent(self, value):
        encoded = json.dumps(value)
        for forbidden in ('PRIVATE', 'https://', 'SDP', 'person-name'):
            self.assertNotIn(forbidden, encoded)

    async def test_postjoin_uses_new_classification_and_never_prejoin_snapshot(self):
        for message in ('Waiting for someone to let you in',
                        'Hi, ' + PRIVATE + '. Someone will let you in when the meeting starts.'):
            with self.subTest(message=message):
                page, meeting, _ = self.meeting()
                meeting._join_state['computer_audio_wait'] = {'ui_snapshot': {'category': 'audio_fallback', '_sampled_at': time.monotonic()-170}}
                page.elements = [ui.Element(page, message, 'generic'), ui.Element(page, PRIVATE, 'heading')]
                await meeting._post_join_ui_snapshot(time.monotonic()+1, lambda: False)
                result = meeting.join_diagnostic()['post_join_ui']
                self.assertTrue(result['available'])
                self.assertEqual(result['snapshot']['category'], 'admission_lobby')
                self.assertEqual(result['snapshot']['safe_name_counts']['lobby_message'], 1)
                self.assertLess(result['snapshot']['sample_age_ms'], 100)
                self.assertEqual(result['snapshot']['sample_scope'], 'post_join_observation')
                self.assertEqual(page.trace, [])
                self.assert_private_absent(result)

    async def test_terminal_snapshot_is_fresh_and_frozen_without_new_queries(self):
        page, meeting, events = self.meeting()
        await meeting._post_join_ui_snapshot(time.monotonic()+1, lambda: False)
        query_count = page.queries
        await meeting._join_failed(TimeoutError(PRIVATE))
        frozen = meeting.join_diagnostic()['post_join_ui']
        self.assertTrue(frozen['available'])
        with patch('teams_transcription.browser.time.monotonic', return_value=time.monotonic()+30):
            self.assertEqual(meeting.join_diagnostic()['post_join_ui'], frozen)
        self.assertEqual(page.queries, query_count)
        self.assert_private_absent(events)

    async def test_stale_terminal_is_explicit_unavailable_not_old_category(self):
        page, meeting, _ = self.meeting()
        await meeting._post_join_ui_snapshot(time.monotonic()+1, lambda: False)
        meeting._post_join_observation['snapshot']['_sampled_at'] -= 2
        await meeting._join_failed(TimeoutError(PRIVATE))
        result = meeting.join_diagnostic()['post_join_ui']
        self.assertFalse(result['available'])
        self.assertEqual(result['unavailable_reason'], 'stale')
        self.assertIsNone(result['snapshot'])

    async def test_incomplete_error_discards_previous_complete_snapshot(self):
        page, meeting, _ = self.meeting()
        await meeting._post_join_ui_snapshot(time.monotonic()+1, lambda: False)
        class Fault:
            async def count(self):
                raise Error(PRIVATE)
        page.get_by_role = lambda *a, **k: Fault()
        await meeting._post_join_ui_snapshot(time.monotonic()+1, lambda: False)
        result = meeting.join_diagnostic()['post_join_ui']
        self.assertFalse(result['available'])
        self.assertEqual(result['unavailable_reason'], 'playwright_error')
        self.assertIsNone(result['snapshot'])
        self.assert_private_absent(result)

    async def test_stop_and_deadline_do_not_query_or_dispatch(self):
        for stopped in (True, False):
            page, meeting, _ = self.meeting()
            await meeting._post_join_ui_snapshot(time.monotonic()+1 if stopped else time.monotonic()-1, lambda: stopped)
            result = meeting.join_diagnostic()['post_join_ui']
            self.assertFalse(result['available'])
            self.assertEqual(result['unavailable_reason'], 'stopped' if stopped else 'deadline')
            self.assertEqual(page.queries, 0)
            self.assertEqual(page.trace, [])

    async def test_stalled_observation_is_bounded_and_does_not_extend_join_deadline(self):
        page, meeting, _ = self.meeting()
        entered = asyncio.Event()
        async def hang(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        meeting._response_ui_snapshot = hang
        started = time.monotonic()
        await meeting._post_join_ui_snapshot(started+.03, lambda: False)
        self.assertTrue(entered.is_set())
        self.assertLess(time.monotonic()-started, .15)
        self.assertFalse(meeting.join_diagnostic()['post_join_ui']['available'])
        self.assertEqual(page.trace, [])

    async def test_frame_change_is_unavailable_and_does_not_invalidate_audio_choice(self):
        page, meeting, _ = self.meeting()
        meeting._response_computer_audio_verified = True
        async def changed(receipt, deadline, stopped, roster, **kwargs):
            page.frames = []
        meeting._response_ui_snapshot = changed
        await meeting._post_join_ui_snapshot(time.monotonic()+1, lambda: False)
        result = meeting.join_diagnostic()['post_join_ui']
        self.assertEqual(result['unavailable_reason'], 'context_changed')
        self.assertTrue(meeting._response_computer_audio_verified)

    async def test_real_join_loop_clicks_once_then_records_lobby_with_original_timeout(self):
        page = PostJoinPage()
        _, meeting, events = self.meeting(page)
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await meeting.join('teams', PRIVATE, .12, lambda: False)
        self.assertLess(time.monotonic()-started, .4)
        self.assertEqual(page.trace.count('Join now'), 1)
        result = meeting.join_diagnostic()
        self.assertEqual(result['outcome'], 'timeout')
        self.assertTrue(result['post_join_ui']['available'])
        self.assertEqual(result['post_join_ui']['snapshot']['category'], 'admission_lobby')
        self.assertFalse(any(kind == 'joined' for kind, _ in events))
        self.assert_private_absent(events)

    async def test_meeting_controls_are_candidates_not_admission_decisions(self):
        page, meeting, _ = self.meeting()
        page.elements = [ui.Element(page, 'Leave', 'button'), ui.Element(page, 'Mute', 'button')]
        await meeting._post_join_ui_snapshot(time.monotonic()+1, lambda: False)
        result = meeting.join_diagnostic()['post_join_ui']['snapshot']
        self.assertEqual(result['category'], 'meeting_controls_candidate')
        self.assertEqual(result['safe_name_counts']['leave_button'], 1)
        self.assertEqual(result['safe_name_counts']['mute_button'], 1)
        self.assertEqual(meeting._join_state['phase'], 'post_join_wait')
        self.assertFalse(meeting._join_state['controls_checked']['leave'])

    def test_sanitizer_rejects_prejoin_scope_hostile_values_and_old_samples(self):
        for scope, age in [('last_complete_observation', 1), ('post_join_observation', 1501)]:
            result = post_join_ui_diagnostic({'available': True, 'unavailable_reason': 'none',
                'snapshot': {'category': 'audio_fallback', 'sample_scope': scope, 'sample_age_ms': age, 'token': PRIVATE},
                'secret': PRIVATE})
            self.assertFalse(result['available'])
            self.assertIsNone(result['snapshot'])
            self.assert_private_absent(result)
        result = post_join_ui_diagnostic({'unavailable_reason': [PRIVATE], 'snapshot': PRIVATE, 'observation_attempts': True})
        self.assertFalse(result['available'])
        self.assert_private_absent(result)


if __name__ == '__main__':
    unittest.main(verbosity=2)
