"""Controlled local worker wiring; no model, Teams, mic or audible playback."""
import asyncio
import json
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from playwright.async_api import Error as PlaywrightError

from teams_transcription.b_response import MeetingMinutesTool
from teams_transcription.b_guard import LiveBinding
from teams_transcription.browser import MeetingBrowser
from teams_transcription.native_media import NativeMeetingMedia
from teams_transcription.diagnostics import EXCEPTION_TYPES, PHASES, failure_diagnostic
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.storage import Session
from teams_transcription.worker import run_session
from tests.test_worker_minutes import InjectedLive, local_open, local_summary
from native_output_fixture import NativeSource


async def wait_for_response_controller(session, worker_task, *, timeout=5):
    """Fail immediately with bounded metadata if the worker cannot reach ready."""
    async with asyncio.timeout(timeout):
        while True:
            if worker_task.done():
                diagnostic = session.status().get("diagnostic", {})
                if worker_task.cancelled():
                    result = "cancelled"
                elif worker_task.exception() is not None:
                    result = "raised"
                    diagnostic = failure_diagnostic(worker_task.exception(), "unknown")
                else:
                    code = worker_task.result()
                    result = code if type(code) is int and code in (0, 1) else "unexpected"
                details = {"worker_result": result,
                    "phase": diagnostic.get("phase") if diagnostic.get("phase") in PHASES else "unknown",
                    "exception_type": diagnostic.get("exception_type") if diagnostic.get("exception_type") in EXCEPTION_TYPES else "Exception"}
                raise AssertionError("worker ended before response controller ready: " + json.dumps(details))
            controller = getattr(session, "response_controller", None)
            if controller is not None and controller.state == "sleeping":
                return controller
            await asyncio.sleep(.01)


async def local_sender_open(self, url=None, storage_state=None):
    await local_open(self, url, storage_state)
    await self.page.evaluate("""() => {
        const peer=new RTCPeerConnection(), destination=fixture.c.createMediaStreamDestination();
        peer.addTrack(destination.stream.getAudioTracks()[0], destination.stream);
        window.fixture.sender=peer;
    }""")


class FakeNative:
    instances = []

    def __init__(self, browser, runtime, ident, reader, *, sink, guard, **kwargs):
        self.meeting_id, self.sink, self.guard, self.runtime = ident, sink, guard, runtime
        self.browser = browser
        self.source = None
        self.tool = MeetingMinutesTool(ident, reader)
        self.activity_recorder = kwargs.get('activity_recorder')
        self.error = None
        self.enabled = False
        self.received = []
        self.stopped = False
        self.instances.append(self)

    async def start(self, *, enable_output=False):
        assert enable_output is False
        self.snapshot = await self.tool.dispatch(self.tool.name,{'query':'上次待辦'})

    async def attach_output(self):
        self.source = await NativeSource.create(SimpleNamespace(browser=self.browser))
        await self.sink.attach_live(self.source.page)

    def status(self):
        return {"state":"stopped" if self.stopped else "sleeping", "output_enabled":self.enabled,
            "output_muted":not self.enabled, "stop_status":"stopped" if self.stopped else "not_started",
            "cleanup_status":"cleaned" if self.stopped else "idle"}

    def enable_output(self):
        self.enabled = True

    async def disable_output(self):
        self.enabled = False
        if self.sink.started:
            await self.sink.set_muted(True)

    def push_rx(self, ident, data, *, started_at, ended_at):
        assert ident == self.meeting_id
        self.received.append((data, started_at, ended_at, time.monotonic()))
        return True

    async def stop(self):
        self.enabled = False
        self.stopped = True
        await self.sink.stop()
        if self.source is not None:
            await self.source.close()
        return "stopped"


class ResponseRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        return {"provider":"codex", "source":"meeting", "meeting_url":"https://teams.microsoft.com/meet/fixture",
            "codex_bin":"unused-fixture", "duration":.35, "name":"Fixture", "join_timeout":1,
            "audio_timeout":1, "drain_seconds":.01, "save_audio":False,
            "prior_context":{'available':True,'session_id':'synthetic-prior','revision':1,
                'summary':{'待辦':[{'text':'上次待辦：確認發言。','owner':'','date':'','sources':['S0001']}]},
                'open_tasks':[]}, "response_enabled":True,
            "response_output_enabled":True,  # private config cannot authorize playback
            "allowed_egress_hosts":["live.example.test"]}

    async def test_actual_worker_fans_capture_clock_to_scoped_native_default_muted(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create({**self.config(), "duration":None})
            with patch("teams_transcription.worker.LiveProvider", InjectedLive), \
                    patch.object(MeetingBrowser, "open", local_sender_open), \
                    patch("teams_transcription.worker.summary.generate", local_summary), \
                    patch("teams_transcription.response_runtime.build_native_meeting_responder", FakeNative):
                task = asyncio.create_task(run_session(session, install_signals=False))
                try:
                    controller = await wait_for_response_controller(session, task)
                    native = controller.native
                    async with asyncio.timeout(5):
                        while not native.received:
                            self.assertFalse(task.done(), "worker stopped before receiving PCM")
                            await asyncio.sleep(.01)
                finally:
                    session.stop()
                    self.assertEqual(await asyncio.wait_for(task, 5), 0)
            native = FakeNative.instances[-1]
            self.assertIsNotNone(native.guard)
            self.assertIn(session.ident, str(native.runtime))
            self.assertEqual(native.snapshot["meeting_id"], session.ident)
            self.assertIs(native.activity_recorder.store, session.store)
            self.assertEqual(native.activity_recorder.meeting_id, session.ident)
            self.assertIn("上次待辦", str(native.snapshot["context"]))
            self.assertTrue(native.received)
            for data, start, end, receipt in native.received:
                self.assertAlmostEqual(end-start, len(data)/48000, places=6)
                self.assertLessEqual(end, receipt+.05)
                self.assertLess(receipt-end, .3)
            self.assertTrue(native.stopped)
            self.assertFalse(native.enabled)
            self.assertTrue(session.response_controller.status()["output_muted"])
            self.assertEqual(MeetingStore(root).read(session.ident)["status"], "stopped")
            self.assertFalse((session.path/"audio.wav").exists())

    async def test_explicit_deny_all_starts_B_muted_and_blocks_browser_requests(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config()
            config.update(allowed_egress_hosts=[], duration=2)
            session = Session(root).create(config)
            with patch("teams_transcription.worker.LiveProvider", InjectedLive), \
                    patch.object(MeetingBrowser, "open", local_sender_open), \
                    patch("teams_transcription.worker.summary.generate", local_summary), \
                    patch("teams_transcription.response_runtime.build_native_meeting_responder", FakeNative):
                task = asyncio.create_task(run_session(session, install_signals=False))
                context = None
                try:
                    controller = await wait_for_response_controller(session, task)
                    self.assertTrue(controller.status()["output_muted"])
                    context = await controller.browser.new_context(service_workers="block")
                    await controller.native.guard.install(context, LiveBinding(session.ident, "fixture-thread"))
                    page = await context.new_page()
                    with self.assertRaisesRegex(PlaywrightError, "ERR_BLOCKED_BY_CLIENT"):
                        await page.goto("https://not-approved.invalid/blocked")
                finally:
                    if context is not None:
                        await context.close()
                    session.stop()
                    await task

    async def test_missing_allowlist_never_starts_B_rpc_and_A_keeps_transcribing(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config()
            config.pop("allowed_egress_hosts")
            session = Session(root).create(config)
            with patch("teams_transcription.worker.LiveProvider", InjectedLive), \
                    patch.object(MeetingBrowser, "open", local_sender_open), \
                    patch("teams_transcription.worker.summary.generate", local_summary), \
                    patch("teams_transcription.response_runtime.build_native_meeting_responder") as builder:
                self.assertEqual(await run_session(session, install_signals=False), 0)
            builder.assert_not_called()
            self.assertEqual(session.response_controller.status()["error_code"], "response_start_failed")
            self.assertFalse(session.response_controller.status()["output_enabled"])

    async def test_explicit_owned_TX_and_restore_keep_actual_worker_RX_alive(self):
        with tempfile.TemporaryDirectory() as root:
            config = self.config()
            config["duration"] = None
            session = Session(root).create(config)
            with patch("teams_transcription.worker.LiveProvider", InjectedLive), \
                    patch.object(MeetingBrowser, "open", local_sender_open), \
                    patch("teams_transcription.worker.summary.generate", local_summary), \
                    patch("teams_transcription.response_runtime.build_native_meeting_responder", FakeNative):
                task = asyncio.create_task(run_session(session, install_signals=False))
                try:
                    controller = await wait_for_response_controller(session, task)
                    native = controller.native
                    self.assertFalse(controller.status()["output_enabled"])
                    await controller.enable_output()
                    await native.attach_output()
                    await native.sink.start()
                    await native.sink.set_muted(False)
                    before = len(native.received)
                    async with asyncio.timeout(5):
                        while len(native.received) <= before:
                            self.assertFalse(task.done(), "worker stopped while TX was enabled")
                            await asyncio.sleep(.01)
                    self.assertGreater(len(native.received), before)
                    self.assertTrue(await native.sink.frame.evaluate("() => __bTX.track.enabled"))
                    await controller.disable_output()
                    await native.sink.stop()
                    after = len(native.received)
                    async with asyncio.timeout(5):
                        while len(native.received) <= after:
                            self.assertFalse(task.done(), "worker stopped after restoring RX")
                            await asyncio.sleep(.01)
                    self.assertGreater(len(native.received), after)
                    self.assertFalse(controller.meeting.dual_policy.faulted)
                    session.stop()
                    self.assertEqual(await asyncio.wait_for(task, 5), 0)
                finally:
                    if not task.done():
                        session.stop()
                        await task
            self.assertEqual(MeetingStore(root).read(session.ident)["status"], "stopped")
            self.assertTrue(native.stopped)

    async def test_stop_after_admission_with_no_PCM_is_stopped_not_failed(self):
        async def stop_capture(meeting, **kwargs):
            session.stop()
            return {"ready":True}
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(self.config())
            with patch("teams_transcription.worker.LiveProvider", InjectedLive), \
                    patch.object(MeetingBrowser, "open", local_sender_open), \
                    patch.object(MeetingBrowser, "start_capture", stop_capture):
                self.assertEqual(await run_session(session, install_signals=False), 0)
            self.assertEqual(MeetingStore(root).read(session.ident)["status"], "stopped")
            self.assertEqual(MeetingStore(root).read(session.ident)["events"], [])

    async def test_partial_native_start_restores_owned_sender_and_A_continues(self):
        class PartialStart(FakeNative):
            async def start(self, **kwargs):
                await super().start(**kwargs)
                await self.attach_output()
                await self.sink.start()
                raise RuntimeError("private-token SDP fixture failure")
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create({**self.config(), "duration":None})
            with patch("teams_transcription.worker.LiveProvider", InjectedLive), \
                    patch.object(MeetingBrowser, "open", local_sender_open), \
                    patch("teams_transcription.worker.summary.generate", local_summary), \
                    patch("teams_transcription.response_runtime.build_native_meeting_responder", PartialStart):
                task = asyncio.create_task(run_session(session, install_signals=False))
                try:
                    # Wait for the intended startup failure, not a .35s capture
                    # timer that can cancel native RTC setup before it fails.
                    async with asyncio.timeout(5):
                        while True:
                            controller = getattr(session, "response_controller", None)
                            if controller and controller.status()["error_code"] == "response_start_failed":
                                break
                            self.assertFalse(task.done(), "A stopped before B startup failure")
                            await asyncio.sleep(.01)
                    live = InjectedLive.instances[-1]
                    self.assertFalse(live.closed)
                    await live.on_final({'event_id':'fixture-one', 'revision':2,
                        'text':'B啟動失敗後仍保存來源',
                        'timestamp':'2026-10-09T10:00:01Z', 'sequence':2})
                    source = MeetingStore(root).read(session.ident)
                    self.assertFalse(source["terminal"])
                    self.assertEqual(source["events"][0]["text"], 'B啟動失敗後仍保存來源')
                    self.assertEqual(source["events"][0]["revision"], 2)
                finally:
                    session.stop()
                    self.assertEqual(await asyncio.wait_for(task, 5), 0)
            controller = session.response_controller
            self.assertIsNone(controller.meeting.dual_policy)
            self.assertTrue(controller._policy_released)
            self.assertFalse(controller.sink.started)
            self.assertFalse(controller.sink._pending)
            self.assertEqual(controller.status()["error_code"], "response_start_failed")
            with session.store._locked() as db:
                events = [row[0] for row in db.execute('SELECT payload_json FROM session_events WHERE meeting_id=?',(session.ident,))]
            self.assertNotIn('private-token',json.dumps({'status':session.status(),'events':events}))

    async def test_unknown_native_cleanup_preserves_actual_unmuted_flags_and_rejects_enable(self):
        class UnknownCleanup(FakeNative):
            async def start(self, **kwargs):
                await super().start(**kwargs)
                raise RuntimeError("fixture start failure")

            async def stop(self):
                raise RuntimeError("fixture cleanup failure")

            def status(self):
                # Simulated backend evidence only; no actual playback occurs.
                return {**super().status(), "output_enabled":True,
                    "output_muted":False, "cleanup_status":"unknown"}
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(self.config())
            with patch("teams_transcription.worker.LiveProvider", InjectedLive), \
                    patch.object(MeetingBrowser, "open", local_sender_open), \
                    patch("teams_transcription.worker.summary.generate", local_summary), \
                    patch("teams_transcription.response_runtime.build_native_meeting_responder", UnknownCleanup):
                self.assertEqual(await run_session(session, install_signals=False), 1)
            self.assertEqual(session.status()['state'], 'failed')
            self.assertEqual(session.status()['cleanup_status'], 'unknown')
            self.assertEqual(session.response_controller.status()["state"], "stop_unknown")
            self.assertEqual(session.response_controller.status()["error_code"], "response_cleanup_unknown")
            self.assertTrue(session.response_controller.status()["output_enabled"])
            self.assertFalse(session.response_controller.status()["output_muted"])
            with self.assertRaises(RuntimeError):
                await session.response_controller.enable_output()

    async def test_source_timestamp_required_for_B_but_not_A(self):
        start_observation = NativeMeetingMedia._start_observation
        start_capture = MeetingBrowser.start_capture
        reports = []
        async def no_clock(media, *, deadline):
            evaluate = media.target.evaluate
            async def unavailable_sample(expression, *args, **kwargs):
                result = await evaluate(expression, *args, **kwargs)
                if 'now:performance.now()' in expression:
                    result['now'] = None
                return result
            with patch.object(media.target, 'evaluate', side_effect=unavailable_sample):
                return await start_observation(media, deadline=deadline)
        async def capture(meeting, **kwargs):
            report = await start_capture(meeting, **kwargs)
            reports.append(report)
            return report
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(self.config())
            with patch("teams_transcription.worker.LiveProvider", InjectedLive), \
                    patch.object(MeetingBrowser, "open", local_sender_open), \
                    patch.object(MeetingBrowser, "start_capture", capture), \
                    patch.object(NativeMeetingMedia, "_start_observation", no_clock), \
                    patch("teams_transcription.worker.summary.generate", local_summary), \
                    patch("teams_transcription.response_runtime.build_native_meeting_responder", FakeNative):
                result = await run_session(session, install_signals=False)
                self.assertEqual(result, 0, {key:session.status().get(key)
                    for key in ('state','error','diagnostic','cleanup_status')})
            self.assertEqual(FakeNative.instances[-1].received, [])
            self.assertEqual(InjectedLive.instances[-1].pushes, 0)
            self.assertTrue(InjectedLive.instances[-1].final_emitted)
            self.assertEqual(session.response_controller.status()['state'], 'stopped')
            self.assertTrue(reports[-1]['ready'])
            self.assertTrue(reports[-1]['native_source']['ready'])
            self.assertFalse(reports[-1]['native_source']['failed'])
            self.assertFalse(reports[-1]['observation_ready'])
            self.assertFalse(reports[-1]['native_source']['observation_ready'])


if __name__ == "__main__":
    unittest.main()
