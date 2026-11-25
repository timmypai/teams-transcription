"""Actual B startup methods with local boundary fixtures; no browser/model I/O."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from teams_transcription.b_live import LiveSpeechChannel
from teams_transcription.b_native import PINNED_CODEX_VERSION
from teams_transcription.browser import MeetingBrowser, RESPONSE_SENDER_JS
from teams_transcription.diagnostics import response_start_failure, response_start_receipt
from teams_transcription.response_runtime import ProductionResponseController
from teams_transcription.storage import Session
from lifecycle_probe import session_events


PRIVATE = 'private-token https://teams.microsoft.com/secret?access_token=secret v=0 SDP'


class LocalFrame:
    """Only JS boundary return values are simulated; no JS/media is executed."""
    def __init__(self, fail=None):
        self.fail = fail

    async def evaluate(self, script, args=None):
        if isinstance(args, dict) and 'policyId' in args:
            accepted=self.fail != 'policy_check'
            return {'accepted':accepted,'reason':'none' if accepted else 'foreign_sender'}
        if isinstance(args, dict) and 'senderIndex' in args:
            if self.fail == 'sink_bind':
                raise PermissionError(PRIVATE)
            return {'owner':args['owner'], 'meeting_id':args['meetingId']}
        if script == RESPONSE_SENDER_JS:
            return {'candidates':[{'peer':0, 'sender':0}] * (2 if self.fail == 'sender_select' else 1),
                'diagnostic':{}}
        return True


class ResponseStartDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def native_factory(self, source):
        from teams_transcription import response_runtime
        factory = response_runtime.build_native_meeting_responder
        def build(*args, **kwargs):
            native = factory(*args, **kwargs)
            native.rpc = AsyncMock(); native.rpc.executable = 'synthetic-codex'
            native.rpc.thread.return_value = 'synthetic-thread'
            native.rpc.notifications = asyncio.Queue()
            native.rpc.process = SimpleNamespace(returncode=None)
            native.peer = AsyncMock(); native.peer.page = source
            native.peer.offer.return_value = 'synthetic-offer'
            async def request(method, params):
                if method == 'account/read':
                    return {'account': {'type': 'chatgpt'}}
                if method in {'thread/realtime/start', 'thread/realtime/stop'}:
                    kind = 'sdp' if method.endswith('/start') else 'closed'
                    await native.rpc.notifications.put({'method': 'thread/realtime/' + kind,
                        'params': {'threadId': 'synthetic-thread', 'sdp': 'synthetic-answer'}})
                return {}
            native.rpc.request.side_effect = request
            return native
        return build

    def controller(self, root, fail=None):
        config = {'provider':'codex', 'source':'teams',
            'allowed_egress_hosts':['example.com'],
            'codex_bin':str(Path(root)/'nonexistent-codex'), 'runtime_dir':str(Path(root)/'runtime')}
        session = Session(root).create(config)
        frame = LocalFrame(fail)
        meeting = MeetingBrowser(None, None, None)
        meeting.page = SimpleNamespace(frames=[frame])
        if fail == 'guard_construct':
            del config['allowed_egress_hosts']
        if fail == 'policy_bind':
            meeting.dual_policy = object()  # Actual existing-owner rejection.
        if fail == 'native_construct':
            del config['codex_bin']
        ice = [{'urls':'invalid'}] if fail == 'guard_prepare' else []
        return ProductionResponseController(session, meeting, None, config, ice)

    async def assert_receipt(self, controller, phase, kind):
        expected = {'phase':phase, 'exception_type':kind}
        self.assertEqual(controller.status()['startup_error'], expected)
        event = session_events(controller.session)[-1]
        self.assertEqual(event['type'], 'response_unavailable')
        self.assertEqual(event['startup_error'], expected)
        # Both the durable receipt and defensive status snapshot survive stop.
        before = controller.status()['startup_error']
        await controller.stop()
        self.assertEqual(controller.status()['startup_error'], before)
        before['phase'] = 'modified_by_reader'
        self.assertEqual(controller.status()['startup_error'], expected)
        self.assertNotIn(PRIVATE, json.dumps(event))
        self.assertFalse(controller.status()['output_enabled'])

    async def test_actual_pre_rpc_failures_have_distinct_durable_phases(self):
        cases = [('guard_construct','ValueError'), ('guard_prepare','ValueError'),
            ('sender_select','RuntimeError'), ('sink_bind','PermissionError'),
            ('policy_check','RuntimeError'), ('policy_bind','RuntimeError'),
            ('native_construct','KeyError'), ('cli_version_spawn','FileNotFoundError')]
        for phase, kind in cases:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                controller = self.controller(root, phase)
                self.assertFalse(await controller.start())
                self.assertFalse((Path(root)/'runtime/response-native').exists())
                await self.assert_receipt(controller, phase, kind)

    async def test_cli_version_check_and_wait_fail_before_rpc(self):
        for phase, out, exc, kind in [
            ('cli_version_check', b'wrong-version', None, 'RuntimeError'),
            ('cli_version_wait', b'', TimeoutError(PRIVATE), 'TimeoutError')]:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root:
                controller = self.controller(root)
                process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(out,None)))
                if exc:
                    process.communicate.side_effect = exc
                with patch('teams_transcription.b_native.asyncio.create_subprocess_exec', AsyncMock(return_value=process)) as spawn:
                    self.assertFalse(await controller.start())
                self.assertEqual(spawn.await_count, 1)
                self.assertEqual(spawn.call_args.args[1:], ('--version',))
                self.assertFalse((Path(root)/'runtime/response-native').exists())
                await self.assert_receipt(controller, phase, kind)

    async def test_actual_rpc_start_creates_directory_then_fails_without_process(self):
        with tempfile.TemporaryDirectory() as root:
            controller = self.controller(root)
            version = SimpleNamespace(returncode=0,
                communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(),None)))
            # Actual CodexRPC.start runs mkdir; only process creation is rejected.
            with patch('teams_transcription.b_native.asyncio.create_subprocess_exec',
                    AsyncMock(side_effect=[version, PermissionError(PRIVATE)])) as spawn:
                self.assertFalse(await controller.start())
            self.assertEqual(spawn.await_count, 2)
            self.assertTrue((Path(root)/'runtime/response-native').is_dir())
            self.assertIsNone(controller.native.rpc.process)
            await self.assert_receipt(controller, 'rpc_initialize', 'PermissionError')

    async def test_first_native_error_survives_internal_and_controller_cleanup_errors(self):
        with tempfile.TemporaryDirectory() as root:
            controller = self.controller(root)
            version = SimpleNamespace(returncode=0,
                communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(),None)))
            with patch('teams_transcription.b_native.asyncio.create_subprocess_exec',
                    AsyncMock(side_effect=[version, PermissionError(PRIVATE)])), \
                    patch.object(LiveSpeechChannel, 'close', AsyncMock(side_effect=OSError(PRIVATE))):
                self.assertFalse(await controller.start())
                status = controller.status()
                self.assertEqual(status['error_code'], 'response_cleanup_unknown')
                self.assertEqual(status['startup_error'], {'phase':'rpc_initialize','exception_type':'PermissionError'})
                self.assertEqual(status['cleanup_errors'], [{'phase':'native_stop','exception_type':'OSError'}])
                # Repeat cleanup still fails; it cannot replace or grow receipts.
                with self.assertRaises(OSError):
                    await controller.stop()
                self.assertEqual(controller.status()['startup_error'], status['startup_error'])
                self.assertEqual(controller.status()['cleanup_errors'], status['cleanup_errors'])
            event = json.dumps(session_events(controller.session),ensure_ascii=False)
            self.assertNotIn(PRIVATE, event)
            self.assertIn('PermissionError', event)
            self.assertIn('OSError', event)
            self.assertFalse(controller.native.output.enabled)
            self.assertIsNone(controller.native.rpc.process)

    async def test_policy_timeout_after_native_ready_is_not_reported_as_native_start(self):
        from teams_transcription import response_runtime
        from teams_transcription.b_dual_policy import OwnedSenderDualPolicy
        from test_native_output_attach_diagnostics import Page
        with tempfile.TemporaryDirectory() as root:
            controller = self.controller(root)
            now = [0.]
            class Target(Page):
                async def evaluate(self, script, args=None):
                    if isinstance(args, dict) and 'policyId' in args and now[0]:
                        raise TimeoutError(PRIVATE)
                    return await super().evaluate(script, args)
            target, source = Target(None), Page(None)
            controller.meeting.page = SimpleNamespace(frames=[target])
            factory = response_runtime.build_native_meeting_responder
            def build(*args, **kwargs):
                native = factory(*args, **kwargs)
                native.rpc = AsyncMock(); native.rpc.executable = 'synthetic-codex'
                native.rpc.thread.return_value = 'synthetic-thread'
                native.rpc.notifications = asyncio.Queue()
                native.rpc.process = SimpleNamespace(returncode=None)
                native.peer = AsyncMock(); native.peer.page = source
                native.peer.offer.return_value = 'synthetic-offer'
                async def request(method, params):
                    if method == 'account/read':
                        return {'account': {'type': 'chatgpt'}}
                    if method in {'thread/realtime/start', 'thread/realtime/stop'}:
                        kind = 'sdp' if method.endswith('/start') else 'closed'
                        await native.rpc.notifications.put({'method': 'thread/realtime/' + kind,
                            'params': {'threadId': 'synthetic-thread', 'sdp': 'synthetic-answer'}})
                    return {}
                native.rpc.request.side_effect = request
                async def connected(**kwargs):
                    now[0] = 5.593
                    self.assertFalse(await controller.policy.check())
                native.peer.wait_connected.side_effect = connected
                return native
            version = SimpleNamespace(returncode=0,
                communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(), None)))
            def policy(*args, **kwargs):
                return OwnedSenderDualPolicy(*args, clock=lambda: now[0], **kwargs)
            with patch.object(response_runtime, 'build_native_meeting_responder', build), \
                    patch.object(response_runtime, 'OwnedSenderDualPolicy', policy), \
                    patch('teams_transcription.b_native.asyncio.create_subprocess_exec', return_value=version):
                self.assertFalse(await controller.start())
            expected = {'phase': 'input_policy_check', 'exception_type': 'RuntimeError'}
            status = controller.status()
            self.assertEqual(controller.native.startup_phase, 'ready')
            self.assertIsNone(controller.native.startup_error)
            self.assertEqual(status['startup_error'], expected)
            self.assertEqual(status['startup_phase'], 'input_policy_check')
            self.assertEqual(status['capture_policy_failure'], {
                'reason': 'evaluation_timeout', 'exception_type': 'TimeoutError', 'elapsed_ms': 5593})
            self.assertEqual(status['state'], 'unavailable')
            self.assertFalse(status['output_enabled'])
            self.assertFalse(controller.native.output.enabled)
            self.assertEqual(controller.native.tool_calls, 0)
            self.assertEqual(session_events(controller.session)[-1]['startup_error'], expected)
            self.assertNotIn(PRIVATE, json.dumps(session_events(controller.session)))
            await controller.stop()
            self.assertEqual(controller.status()['startup_error'], expected)

    async def test_B_admission_waits_for_fresh_policy_check_after_native_ready(self):
        from teams_transcription import response_runtime
        from test_native_output_attach_diagnostics import Page
        entered, release = asyncio.Event(), asyncio.Event()
        class Target(Page):
            checks = 0
            async def evaluate(self, script, args=None):
                if isinstance(args, dict) and 'policyId' in args and not args['authorize']:
                    self.checks += 1
                    if self.checks == 2:
                        entered.set()
                        await release.wait()
                return await super().evaluate(script, args)
        with tempfile.TemporaryDirectory() as root:
            target, source = Target(None), Page(None)
            controller = self.controller(root)
            controller.meeting.page = SimpleNamespace(frames=[target])
            version = SimpleNamespace(returncode=0,
                communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(), None)))
            with patch.object(response_runtime, 'build_native_meeting_responder', self.native_factory(source)), \
                    patch('teams_transcription.b_native.asyncio.create_subprocess_exec', return_value=version):
                task = asyncio.create_task(controller.start())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    self.assertEqual(controller.native.startup_phase, 'ready')
                    self.assertFalse(controller.capture_ready())
                    self.assertFalse(any(e['type'] == 'response_ready' for e in session_events(controller.session)))
                    release.set()
                    self.assertTrue(await task)
                    self.assertEqual(controller.policy.timeout, 5)
                    self.assertTrue(controller.capture_ready())
                    self.assertFalse(controller.native.output.enabled)
                finally:
                    release.set()
                    if not task.done(): task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await controller.stop()

    async def test_stop_during_fresh_policy_check_cannot_reopen_B_admission(self):
        from teams_transcription import response_runtime
        from test_native_output_attach_diagnostics import Page
        entered, release = asyncio.Event(), asyncio.Event()
        class Target(Page):
            checks = 0
            async def evaluate(self, script, args=None):
                if isinstance(args, dict) and 'policyId' in args and not args['authorize']:
                    self.checks += 1
                    if self.checks == 2:
                        entered.set()
                        await release.wait()
                return await super().evaluate(script, args)
        with tempfile.TemporaryDirectory() as root:
            target, source = Target(None), Page(None)
            controller = self.controller(root)
            controller.meeting.page = SimpleNamespace(frames=[target])
            version = SimpleNamespace(returncode=0,
                communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(), None)))
            with patch.object(response_runtime, 'build_native_meeting_responder', self.native_factory(source)), \
                    patch('teams_transcription.b_native.asyncio.create_subprocess_exec', return_value=version):
                task = asyncio.create_task(controller.start())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await asyncio.wait_for(controller.stop(), 1)
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    self.assertTrue(task.cancelled())
                    self.assertEqual(controller.status()['state'], 'stopped')
                    self.assertFalse(controller.capture_ready())
                    self.assertFalse(any(e['type'] == 'response_ready' for e in session_events(controller.session)))
                    self.assertTrue(controller.native.output.closed)
                finally:
                    release.set()
                    if not task.done(): task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await controller.stop()

    async def test_stop_during_native_initialization_cancels_startup_before_admission(self):
        from teams_transcription import response_runtime
        from test_native_output_attach_diagnostics import Page
        entered, release = asyncio.Event(), asyncio.Event()
        async def connected(**kwargs):
            entered.set()
            await release.wait()
        with tempfile.TemporaryDirectory() as root:
            target, source = Page(None), Page(None)
            controller = self.controller(root)
            controller.meeting.page = SimpleNamespace(frames=[target])
            factory = self.native_factory(source)
            def build(*args, **kwargs):
                native = factory(*args, **kwargs)
                native.peer.wait_connected.side_effect = connected
                return native
            version = SimpleNamespace(returncode=0,
                communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(), None)))
            with patch.object(response_runtime, 'build_native_meeting_responder', build), \
                    patch('teams_transcription.b_native.asyncio.create_subprocess_exec', return_value=version):
                task = asyncio.create_task(controller.start())
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    await asyncio.wait_for(controller.stop(), 1)
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)
                    self.assertTrue(task.cancelled())
                    self.assertEqual(controller.status()['state'], 'stopped')
                    self.assertFalse(controller.capture_ready())
                    self.assertFalse(await controller.start())
                    self.assertFalse(any(e['type'] == 'response_ready' for e in session_events(controller.session)))
                    self.assertTrue(controller.native.output.closed)
                finally:
                    release.set()
                    if not task.done(): task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await controller.stop()

    async def test_concurrent_stop_and_cancelled_waiter_do_not_recancel_startup_cleanup(self):
        from teams_transcription import response_runtime
        from test_native_output_attach_diagnostics import Page
        for cancel_waiter in (False, True):
            with self.subTest(cancel_waiter=cancel_waiter), tempfile.TemporaryDirectory() as root:
                checking, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
                class Target(Page):
                    checks = 0
                    cleanup_cancellations = 0
                    async def evaluate(self, script, args=None):
                        if isinstance(args, dict) and 'policyId' in args and not args['authorize']:
                            self.checks += 1
                            if self.checks == 2:
                                checking.set()
                                await asyncio.Future()
                        if isinstance(args, str) and 'b.pc.getSenders().includes' in script:
                            cleaning.set()
                            try:
                                await release.wait()
                            except asyncio.CancelledError:
                                self.cleanup_cancellations += 1
                                raise
                        return await super().evaluate(script, args)
                target, source = Target(None), Page(None)
                controller = self.controller(root)
                controller.meeting.page = SimpleNamespace(frames=[target])
                version = SimpleNamespace(returncode=0,
                    communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(), None)))
                with patch.object(response_runtime, 'build_native_meeting_responder', self.native_factory(source)), \
                        patch('teams_transcription.b_native.asyncio.create_subprocess_exec', return_value=version):
                    startup = asyncio.create_task(controller.start())
                    waiters = []
                    try:
                        await asyncio.wait_for(checking.wait(), 1)
                        waiters.append(asyncio.create_task(controller.stop()))
                        await asyncio.wait_for(cleaning.wait(), 1)
                        waiters.append(asyncio.create_task(controller.stop()))
                        await asyncio.sleep(0)
                        if cancel_waiter:
                            waiters[0].cancel()
                            await asyncio.gather(waiters[0], return_exceptions=True)
                        self.assertEqual(startup.cancelling(), 1)
                        self.assertEqual(target.cleanup_cancellations, 0)
                        self.assertFalse(controller.capture_ready())
                        release.set()
                        await asyncio.wait_for(asyncio.gather(*waiters, return_exceptions=True), 1)
                        self.assertTrue(startup.cancelled())
                        self.assertEqual(controller.status()['state'], 'stopped')
                        self.assertTrue(controller.native.output.closed)
                        self.assertTrue(controller._policy_released)
                        self.assertFalse(any(e['type'] == 'response_ready'
                            for e in session_events(controller.session)))
                    finally:
                        release.set()
                        if not startup.done(): startup.cancel()
                        await asyncio.gather(startup, *waiters, return_exceptions=True)
                        await controller.stop()

    async def test_failed_fresh_policy_check_keeps_native_ready_output_muted_and_admission_closed(self):
        from teams_transcription import response_runtime
        from test_native_output_attach_diagnostics import Page
        for failure in ('foreign_sender', 'evaluation_timeout'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as root:
                class Target(Page):
                    checks = 0
                    async def evaluate(self, script, args=None):
                        if isinstance(args, dict) and 'policyId' in args and not args['authorize']:
                            self.checks += 1
                            if self.checks == 2:
                                if failure == 'evaluation_timeout': raise TimeoutError(PRIVATE)
                                return {'accepted': False, 'reason': failure}
                        return await super().evaluate(script, args)
                target, source = Target(None), Page(None)
                controller = self.controller(root)
                controller.meeting.page = SimpleNamespace(frames=[target])
                version = SimpleNamespace(returncode=0,
                    communicate=AsyncMock(return_value=(PINNED_CODEX_VERSION.encode(), None)))
                with patch.object(response_runtime, 'build_native_meeting_responder', self.native_factory(source)), \
                        patch('teams_transcription.b_native.asyncio.create_subprocess_exec', return_value=version):
                    self.assertFalse(await controller.start())
                status = controller.status()
                self.assertEqual(status['startup_phase'], 'input_policy_check')
                self.assertEqual(status['capture_policy_failure']['reason'], failure)
                self.assertFalse(controller.capture_ready())
                self.assertFalse(status['output_enabled'])
                self.assertFalse(any(e['type'] == 'response_ready' for e in session_events(controller.session)))
                self.assertNotIn(PRIVATE, json.dumps(session_events(controller.session)))
                await controller.stop()

    def test_unknown_class_and_foreign_receipt_are_fixed_and_bounded(self):
        private_class = type('PrivateTokenInClassName', (Exception,), {})
        self.assertEqual(response_start_failure(private_class(PRIVATE), PRIVATE),
            {'phase':'unknown','exception_type':'Exception'})
        self.assertEqual(response_start_receipt({'phase':[], 'exception_type':{}, 'text':PRIVATE}),
            {'phase':'unknown','exception_type':'Exception'})


if __name__ == '__main__':
    unittest.main()
