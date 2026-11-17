"""Real join loop against synthetic locators; no browser, model or audio device."""
import asyncio
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright._impl._errors import TargetClosedError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from teams_transcription.browser import MeetingBrowser
from teams_transcription.diagnostics import teams_join_diagnostic
from teams_transcription.storage import Session
from teams_transcription.worker import run_session
from test_rx_contract import Fixture, config
from test_response_computer_audio_ui import StatusRegion
from lifecycle_probe import session_events


PRIVATE = 'PRIVATE_JOIN_TOKEN https://teams.microsoft.com/meet/secret SDP v=0 person-name'


class Locator:
    def __init__(self, page, role, pattern):
        self.page, self.role, self.pattern = page, role, pattern
        self.first = self

    def matches(self, label):
        return self.pattern.search(label) is not None

    async def is_visible(self):
        if self.role == 'button':
            if self.matches('Join now'):
                self.page.scanning_join.set()
                if self.page.hang_visibility:
                    await asyncio.Event().wait()
            return ((self.matches('Join now') and self.page.join_visible) or
                    (self.matches('Leave') and self.page.leave_visible) or
                    (self.matches('Participants') and self.page.participants_visible))
        return self.role == 'textbox' and self.page.name_visible

    async def count(self):
        return 0

    async def get_attribute(self, key):
        return 'true'

    async def click(self, **kwargs):
        if self.matches('Join now'):
            self.page.clicked.set()
            if self.page.click_error:
                raise self.page.click_error
            if self.page.hang_click:
                await asyncio.Event().wait()
            self.page.join_visible = False
            if self.page.admit_after_click:
                self.page.leave_visible = True
                self.page.participants_visible = True

    async def fill(self, name, **kwargs):
        self.page.filling.set()
        if self.page.hang_fill:
            await asyncio.Event().wait()


class Page:
    def __init__(self, text=PRIVATE, *, join_visible=False, leave_visible=False,
                 name_visible=False, hang_body=False, hang_click=False, hang_fill=False,
                 admit_after_click=False, click_error=None, hang_visibility=False):
        self.text = text
        self.join_visible, self.leave_visible, self.name_visible = join_visible, leave_visible, name_visible
        self.participants_visible = False
        self.hang_body, self.hang_click, self.hang_fill = hang_body, hang_click, hang_fill
        self.admit_after_click, self.click_error = admit_after_click, click_error
        self.hang_visibility = hang_visibility
        self.reading, self.clicked, self.filling = asyncio.Event(), asyncio.Event(), asyncio.Event()
        self.scanning_join = asyncio.Event()
        self.frames = [self]
        self.reads = 0

    def locator(self, selector):
        if selector.startswith('[role="status"]'):
            return StatusRegion(self)
        assert selector == 'body'
        return self

    async def inner_text(self, **kwargs):
        self.reads += 1
        self.reading.set()
        if self.hang_body:
            await asyncio.Event().wait()
        return self.text

    def get_by_role(self, role, *, name):
        return Locator(self, role, name)

    def get_by_placeholder(self, pattern):
        return Locator(self, 'textbox', pattern)

    def is_detached(self):
        return False

    def is_closed(self):
        return False


class TeamsJoinDiagnosticTests(unittest.IsolatedAsyncioTestCase):
    def meeting(self, page):
        events = []
        async def event(kind, data):
            events.append((kind, data))
        meeting = MeetingBrowser(None, AsyncMock(), event)
        meeting.page = page
        meeting.stats = AsyncMock(side_effect=AssertionError('failure diagnostics must not query media'))
        return meeting, events

    def browser_for(self, page):
        context = SimpleNamespace(expose_binding=AsyncMock(), add_init_script=AsyncMock(),
                                  new_page=AsyncMock(return_value=page), route=AsyncMock())
        return SimpleNamespace(new_context=AsyncMock(return_value=context))

    async def test_goto_success_survives_into_join_snapshot_with_existing_timeout(self):
        page = Page(join_visible=True, admit_after_click=True)
        page.goto = AsyncMock()
        meeting, events = self.meeting(page)
        meeting.browser = self.browser_for(page)
        await meeting.open('https://teams.microsoft.com/meet/offline-fixture')
        self.assertEqual(page.goto.call_args.kwargs,
                         {'wait_until': 'domcontentloaded', 'timeout': 45000})
        diagnostic = meeting.join_diagnostic()
        self.assertEqual(diagnostic['phase'], 'page_ready')
        self.assertTrue(all(diagnostic[key] for key in
                            ('context_ready', 'page_ready', 'goto_started', 'goto_completed')))
        await meeting.join('teams', PRIVATE, 5, lambda: False)
        self.assertTrue(meeting.join_diagnostic()['goto_completed'])
        self.assert_private_absent(events)

    async def test_goto_failure_and_cancel_preserve_last_stage_without_page_or_url(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                page = Page()
                entered = asyncio.Event()
                async def goto(*args, **kwargs):
                    entered.set()
                    if cancel:
                        await asyncio.Event().wait()
                    raise TargetClosedError(PRIVATE)
                page.goto = goto
                meeting, events = self.meeting(page)
                meeting.browser = self.browser_for(page)
                task = asyncio.create_task(meeting.open('https://teams.microsoft.com/meet/offline-fixture'))
                await asyncio.wait_for(entered.wait(), 3)
                if cancel:
                    task.cancel()
                with self.assertRaises(asyncio.CancelledError if cancel else TargetClosedError):
                    await task
                diagnostic = meeting.join_diagnostic()
                self.assertEqual(diagnostic['phase'], 'goto')
                self.assertEqual(diagnostic['outcome'], 'cancelled' if cancel else 'error')
                self.assertEqual(diagnostic['exception_type'], 'CancelledError' if cancel else 'TargetClosedError')
                self.assertTrue(diagnostic['goto_started'])
                self.assertFalse(diagnostic['goto_completed'])
                self.assertFalse(diagnostic['body_observed'])
                self.assert_private_absent(events)

    async def test_context_and_page_creation_failures_keep_precise_stage(self):
        for phase in ('context_setup', 'page_create'):
            with self.subTest(phase=phase):
                meeting, events = self.meeting(Page())
                meeting.browser = self.browser_for(meeting.page)
                target = (meeting.browser.new_context if phase == 'context_setup' else
                          meeting.browser.new_context.return_value.new_page)
                target.side_effect = TargetClosedError(PRIVATE)
                with self.assertRaises(TargetClosedError):
                    await meeting.open('https://teams.microsoft.com/meet/offline-fixture')
                self.assertEqual(meeting.join_diagnostic()['phase'], phase)
                self.assertFalse(meeting.join_diagnostic()['goto_started'])
                self.assert_private_absent(events)

    async def test_goto_playwright_timeout_is_classified_without_changing_navigation_deadline(self):
        page = Page()
        page.goto = AsyncMock(side_effect=PlaywrightTimeoutError(PRIVATE))
        meeting, events = self.meeting(page)
        meeting.browser = self.browser_for(page)
        with self.assertRaises(PlaywrightTimeoutError):
            await meeting.open('https://teams.microsoft.com/meet/offline-fixture')
        diagnostic = meeting.join_diagnostic()
        self.assertEqual((diagnostic['phase'], diagnostic['outcome'], diagnostic['exception_type']),
                         ('goto', 'timeout', 'TimeoutError'))
        self.assertEqual(page.goto.call_args.kwargs['timeout'], 45000)
        self.assert_private_absent(events)

    async def test_page_setup_failure_is_distinct_from_page_creation(self):
        meeting, events = self.meeting(Page())
        meeting.browser = self.browser_for(meeting.page)
        meeting.browser.new_context.return_value.route.side_effect = TargetClosedError(PRIVATE)
        with patch.dict('os.environ', {'HTTPS_PROXY': 'http://offline-fixture.invalid'}):
            with self.assertRaises(TargetClosedError):
                await meeting.open('https://teams.microsoft.com/meet/offline-fixture')
        diagnostic = meeting.join_diagnostic()
        self.assertEqual(diagnostic['phase'], 'page_setup')
        self.assertTrue(diagnostic['page_ready'])
        self.assertFalse(diagnostic['goto_started'])
        self.assert_private_absent(events)

    def assert_private_absent(self, value):
        encoded = json.dumps(value)
        for marker in ('PRIVATE_JOIN_TOKEN', 'https://', 'SDP', 'person-name'):
            self.assertNotIn(marker, encoded)

    async def test_stalled_body_timeout_preserves_deadline_without_media_query(self):
        meeting, events = self.meeting(Page(hang_body=True))
        started = time.monotonic()
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(meeting.join('teams', PRIVATE, 1, lambda: False), 3)
        self.assertLess(time.monotonic() - started, 3)
        diagnostic = meeting.join_diagnostic()
        self.assertEqual((diagnostic['phase'], diagnostic['outcome'], diagnostic['exception_type']),
                         ('body_read', 'timeout', 'TimeoutError'))
        self.assertFalse(diagnostic['body_observed'])
        self.assertFalse(diagnostic['join_dispatch_started'])
        self.assertEqual(events[-1][0], 'meeting_join_diagnostics')
        self.assert_private_absent(events)
        meeting.stats.assert_not_awaited()

    async def test_dispatch_started_is_distinct_from_click_completed(self):
        page = Page(join_visible=True, hang_click=True)
        meeting, events = self.meeting(page)
        task = asyncio.create_task(meeting.join('teams', PRIVATE, 10, lambda: False))
        await asyncio.wait_for(page.clicked.wait(), 3)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        diagnostic = meeting.join_diagnostic()
        self.assertEqual((diagnostic['phase'], diagnostic['outcome']), ('join_dispatch', 'cancelled'))
        self.assertTrue(diagnostic['controls_checked']['join'])
        self.assertTrue(diagnostic['controls_seen']['join'])
        self.assertTrue(diagnostic['join_dispatch_started'])
        self.assertFalse(diagnostic['join_dispatch_completed'])
        self.assertEqual(diagnostic['actions']['join'], 'dispatching')
        self.assert_private_absent(events)

    async def test_waiting_for_control_is_distinct_from_a_completed_absence_check(self):
        for stalled in (False, True):
            with self.subTest(stalled=stalled):
                page = Page(hang_visibility=stalled)
                meeting, events = self.meeting(page)
                task = asyncio.create_task(meeting.join('teams', PRIVATE, 10, lambda: False))
                await asyncio.wait_for(page.scanning_join.wait(), 3)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                diagnostic = meeting.join_diagnostic()
                self.assertEqual(diagnostic['phase'], 'join_control_scan' if stalled else 'post_join_wait')
                self.assertEqual(diagnostic['controls_checked']['join'], not stalled)
                self.assertFalse(diagnostic['controls_seen']['join'])
                self.assertFalse(diagnostic['join_dispatch_started'])
                self.assertEqual(diagnostic['actions']['join'], 'inspecting' if stalled else 'not_visible')
                self.assert_private_absent(events)

    async def test_name_fill_stop_keeps_phase_and_never_dispatches_join(self):
        page = Page(name_visible=True, hang_fill=True)
        meeting, events = self.meeting(page)
        stopped = False
        task = asyncio.create_task(meeting.join('teams', PRIVATE, 10, lambda: stopped))
        await asyncio.wait_for(page.filling.wait(), 3)
        stopped = True
        with self.assertRaises(InterruptedError):
            await asyncio.wait_for(task, 3)
        diagnostic = meeting.join_diagnostic()
        self.assertEqual((diagnostic['phase'], diagnostic['outcome']), ('name_fill', 'stopped'))
        self.assertEqual(diagnostic['actions']['name'], 'dispatching')
        self.assertFalse(diagnostic['join_dispatch_started'])
        self.assert_private_absent(events)

    async def test_swallowed_click_error_retains_safe_class_and_timeout(self):
        meeting, events = self.meeting(Page(join_visible=True, click_error=TargetClosedError(PRIVATE)))
        with self.assertRaises(TimeoutError):
            await meeting.join('teams', PRIVATE, 1, lambda: False)
        diagnostic = meeting.join_diagnostic()
        self.assertEqual(diagnostic['exception_type'], 'TimeoutError')
        self.assertTrue(any(e['action'] == 'join' and e['exception_type'] == 'TargetClosedError'
                            for e in diagnostic['errors']))
        self.assertTrue(diagnostic['join_dispatch_started'])
        self.assertFalse(diagnostic['join_dispatch_completed'])
        self.assert_private_absent(events)

    async def test_completed_click_and_confirmed_admission_are_distinct(self):
        meeting, events = self.meeting(Page(join_visible=True, admit_after_click=True))
        await meeting.join('teams', PRIVATE, 5, lambda: False)
        self.assertEqual([kind for kind, _ in events], ['joining', 'connecting', 'joined'])
        connecting = events[1][1]['join']
        self.assertEqual(connecting['outcome'], 'in_progress')
        self.assertTrue(connecting['join_dispatch_completed'])
        diagnostic = meeting.join_diagnostic()
        self.assertEqual((diagnostic['phase'], diagnostic['page_state'], diagnostic['outcome']),
                         ('joined', 'joined', 'joined'))
        self.assertTrue(diagnostic['controls_seen']['leave'])
        # Later RX/cleanup observations must not mutate the terminal snapshot.
        meeting._join_phase('body_read')
        meeting._join_error('leave', TargetClosedError(PRIVATE))
        self.assertEqual(meeting.join_diagnostic(), diagnostic)
        self.assert_private_absent(events)

    async def test_lobby_with_leave_is_never_reported_as_joined(self):
        for text in ('Waiting in the lobby',
                     'Hi, ' + PRIVATE + '. Someone will let you in when the meeting starts.'):
            with self.subTest(text=text):
                page = Page(text + ' ' + PRIVATE, leave_visible=True)
                meeting, events = self.meeting(page)
                with self.assertRaises(TimeoutError):
                    await meeting.join('teams', PRIVATE, 1.2, lambda: False)
                self.assertGreaterEqual(page.reads, 2)
                self.assertEqual(meeting.join_diagnostic()['page_state'], 'lobby')
                self.assertEqual(meeting.join_diagnostic()['phase'], 'lobby_wait')
                self.assertFalse(any(kind == 'joined' for kind, _ in events))
                self.assertEqual(sum(kind == 'waiting_for_admission' for kind, _ in events), 1)
                self.assert_private_absent(events)

    async def test_known_page_failures_use_enums_and_diagnostic_failure_cannot_replace_error(self):
        for text, state in [('Sign in to join', 'login_required'),
                            ("Sorry, we couldn't connect you", 'connect_failed'),
                            ('Meeting has ended', 'ended')]:
            with self.subTest(state=state):
                meeting, events = self.meeting(Page(text + PRIVATE))
                original = meeting.event
                async def failing_event(kind, data):
                    await original(kind, data)
                    if kind == 'meeting_join_diagnostics':
                        raise ValueError(PRIVATE)
                meeting.event = failing_event
                with self.assertRaises(RuntimeError):
                    await meeting.join('teams', PRIVATE, 5, lambda: False)
                self.assertEqual(meeting.join_diagnostic()['page_state'], state)
                self.assertEqual(meeting.join_diagnostic()['outcome'], 'error')
                self.assertFalse(meeting.join_diagnostic()['join_dispatch_started'])
                self.assert_private_absent(events)

    async def test_worker_persists_join_snapshot_before_live_or_capture(self):
        fixture = Fixture()
        page = Page('Sign in to join ' + PRIVATE)
        instances = []
        class ControlledMeeting(MeetingBrowser):
            async def open(self, *args):
                self.page = page
                instances.append(self)
            async def close(self):
                pass
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(duration=None, join_timeout=5,
                rx_test_contract=False, rx_session_contract=True))
            with fixture.install(), patch('teams_transcription.worker.MeetingBrowser', ControlledMeeting):
                result = await run_session(session, install_signals=False)
            self.assertEqual(result, 1)
            status = session.status()
            self.assertEqual((status['state'], status['error'], status['cleanup_status']),
                             ('failed', 'meeting_join_failed', 'cleaned'))
            self.assertEqual(status['diagnostic']['join'], instances[0].join_diagnostic())
            self.assertEqual(status['diagnostic']['join']['page_state'], 'login_required')
            self.assertEqual((fixture.live_starts, fixture.captures, fixture.pushes), (0, 0, 0))
            events = session_events(session)
            self.assertTrue(any(e['type'] == 'meeting_join_diagnostics' for e in events))
            self.assert_private_absent(status)
            self.assert_private_absent(events)
            self.assert_private_absent(session.store.read(session.ident))

    async def test_worker_stop_and_cancel_persist_last_join_fields(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel), tempfile.TemporaryDirectory() as root:
                fixture = Fixture()
                page = Page(hang_body=True)
                class ControlledMeeting(MeetingBrowser):
                    async def open(self, *args):
                        self.page = page
                    async def close(self):
                        pass
                session = Session(root).create(config(duration=None, join_timeout=10,
                    rx_test_contract=False, rx_session_contract=True))
                with fixture.install(), patch('teams_transcription.worker.MeetingBrowser', ControlledMeeting):
                    task = asyncio.create_task(run_session(session, install_signals=False))
                    await asyncio.wait_for(page.reading.wait(), 3)
                    if cancel:
                        task.cancel()
                    else:
                        session.stop()
                    # The supervisor keeps cancellation's existing terminal semantics.
                    if cancel:
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 5)
                    else:
                        self.assertEqual(await asyncio.wait_for(task, 5), 0)
                status = session.status()
                self.assertEqual(status['diagnostic']['join']['phase'], 'body_read')
                self.assertEqual(status['diagnostic']['join']['outcome'], 'cancelled')
                self.assertEqual((status['state'], status['error']),
                                 ('stopped', 'rx_cancelled' if cancel else 'rx_stopped'))
                self.assertEqual(status['cleanup_status'], 'cleaned')
                self.assertEqual((fixture.live_starts, fixture.captures, fixture.pushes), (0, 0, 0))
                self.assert_private_absent(status)
                self.assert_private_absent(json.dumps(session_events(session),ensure_ascii=False))

    async def test_worker_browser_open_failure_persists_goto_snapshot(self):
        fixture = Fixture()
        page = Page()
        page.goto = AsyncMock(side_effect=TargetClosedError(PRIVATE))
        browser = self.browser_for(page)
        class ControlledMeeting(MeetingBrowser):
            async def open(self, *args):
                self.browser = browser
                await super().open(*args)
            async def close(self):
                pass
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(duration=None,
                rx_test_contract=False, rx_session_contract=True))
            with fixture.install(), patch('teams_transcription.worker.MeetingBrowser', ControlledMeeting):
                self.assertEqual(await run_session(session, install_signals=False), 1)
            status = session.status()
            self.assertEqual((status['state'], status['error'], status['cleanup_status']),
                             ('failed', 'browser_open_failed', 'cleaned'))
            diagnostic = status['diagnostic']['join']
            self.assertEqual((diagnostic['phase'], diagnostic['exception_type']), ('goto', 'TargetClosedError'))
            self.assertTrue(diagnostic['goto_started'])
            self.assertFalse(diagnostic['goto_completed'])
            self.assertEqual((fixture.live_starts, fixture.captures, fixture.pushes), (0, 0, 0))
            self.assert_private_absent(status)
            self.assert_private_absent(json.dumps(session_events(session),ensure_ascii=False))


class JoinDiagnosticSchemaTests(unittest.TestCase):
    def test_only_fixed_enums_booleans_and_last_eight_errors_survive(self):
        value = dict(phase=PRIVATE, page_state=PRIVATE, outcome=PRIVATE, exception_type=PRIVATE,
                     body_observed=PRIVATE, join_dispatch_started=1, join_dispatch_completed=True,
                     controls_seen={'join': True, PRIVATE: True}, controls_checked=PRIVATE,
                     actions={'join': PRIVATE, PRIVATE: 'completed'}, url=PRIVATE, body=PRIVATE,
                     errors=[dict(phase=PRIVATE, action=PRIVATE, exception_type=PRIVATE, text=PRIVATE)] * 100)
        result = teams_join_diagnostic(value)
        self.assertEqual(result['schema_version'], 1)
        self.assertEqual(result['phase'], 'unknown')
        self.assertFalse(result['body_observed'])
        self.assertFalse(result['join_dispatch_started'])
        self.assertTrue(result['join_dispatch_completed'])
        self.assertEqual(result['actions']['join'], 'not_attempted')
        self.assertEqual(len(result['errors']), 8)
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(teams_join_diagnostic(None)['outcome'], 'unknown')
