"""Standalone capture fences / owned cleanup; local sleep children, no model."""
import asyncio
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch

from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.assistant_store import AssistantStoreError
from teams_transcription.daemon import AssistantDaemon
from teams_transcription.rx_contract import ScopedSession,RxContractError
from teams_transcription.worker import run_session
from test_incremental_qa_worker import inline_storage
from test_rx_contract import Fixture,URL

LIMITS={'capture_seconds':None,'total_seconds':2.,'cleanup_seconds':.3,'ready_ttl_seconds':1.5}


class Controller:
    def __init__(self, *, mode='clean', child=True):
        self.mode, self.child = mode, child
        self.native = SimpleNamespace(rpc=SimpleNamespace(process=None, tasks=[], _close_task=None), _close_task=None)
        self.started = asyncio.Event()
        self.state, self.enabled, self.stops = 'starting', False, 0
        self._start_complete,self._stop_requested=False,False
    async def start(self):
        if self.child:
            self.native.rpc.process = await asyncio.create_subprocess_exec('sleep', '30')
        self.state = 'sleeping'; self.started.set()
        if self.mode == 'hang_start':
            await asyncio.Event().wait()
        self._start_complete=True
        return True
    def capture_ready(self):
        # The production controller offers this synchronous readiness hook.
        # Partial startup and a requested/hung stop must never claim readiness.
        return self._start_complete and not self._stop_requested and self.state=='sleeping'
    async def _finish_stop(self):
        if self.mode == 'spawn_rpc_on_cancel':
            try:
                await asyncio.Event().wait()
            finally:
                self.native.rpc._close_task = asyncio.create_task(asyncio.Event().wait())
        if self.mode in {'hang_close','hang_start'}:
            await asyncio.Event().wait()
        if self.mode == 'unknown':
            self.state = 'stop_unknown'; return 'unknown'
        process = self.native.rpc.process
        if process and process.returncode is None:
            process.terminate(); await process.wait()
        self.state = 'stopped'
        return 'stopped' if process else 'not_started'
    async def stop(self):
        self._stop_requested=True
        self.stops += 1
        if self.native._close_task is None:
            self.native._close_task = asyncio.create_task(self._finish_stop())
        return await asyncio.shield(self.native._close_task)
    def status(self):
        return {'state':self.state, 'output_enabled':self.enabled, 'output_muted':not self.enabled,
                'cleanup_status':'unknown' if self.state == 'stop_unknown' else 'cleaned' if self.state == 'stopped' else 'idle'}
    async def disable_output(self): self.enabled = False
    def enable_output(self): raise AssertionError('test never authorizes TX')
    def push_rx(self, *args, **kwargs): return True


async def reap_controller(controller):
    process = controller.native.rpc.process
    if process and process.returncode is None:
        process.kill(); await process.wait()
    rpc_task = controller.native.rpc._close_task
    if rpc_task and not rpc_task.done():
        rpc_task.cancel(); await asyncio.gather(rpc_task, return_exceptions=True)
    task = controller.native._close_task
    if task and not task.done():
        task.cancel(); await asyncio.gather(task, return_exceptions=True)


class FormalQACleanupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for replacement in (
            patch('asyncio.to_thread', side_effect=inline_storage),
            patch('teams_transcription.rx_contract.options', return_value=LIMITS.copy()),
            patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005)):
            replacement.start(); self.addCleanup(replacement.stop)

    def test_scoped_controller_shares_base_and_rejects_late_publish(self):
        base = SimpleNamespace(response_controller=None)
        runtime = SimpleNamespace(writes_open=True)
        scoped = ScopedSession(base, runtime)
        controller = object(); scoped.response_controller = controller
        self.assertIs(base.response_controller, controller)
        self.assertIs(scoped.response_controller, controller)
        runtime.writes_open = False
        with self.assertRaisesRegex(RxContractError, 'rx_attempt_closed'):
            scoped.response_controller = object()
        self.assertIs(base.response_controller, controller)

    async def run_formal(self,controller):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)
            settings=AssistantSettings(data_root=root,admin_password_file=str(path/'config/password.json'),
                admin_session_secret_file=str(path/'config/secret.key'),google_credentials_file=str(path/'config/google.json'))
            daemon=AssistantDaemon(settings,runner=lambda session:run_session(session,install_signals=False),
                summarizer=AsyncMock())
            now=datetime.now(timezone.utc)
            occurrence=daemon.store.create_manual_occurrence(dict(title='Controlled cleanup',join_url=URL,
                start_at=(now-timedelta(seconds=1)).isoformat(),end_at=(now+timedelta(minutes=5)).isoformat(),
                recording_authorized=True,speaking_authorized=False))
            fixture=Fixture()
            task=None
            try:
                with fixture.install() as summary,patch(
                        'teams_transcription.response_runtime.ProductionResponseController',return_value=controller):
                    await daemon.start()
                    started=await daemon.join(occurrence['id'])
                    session=daemon._session
                    task=daemon._capture
                    assert session is not None and task is not None
                    await asyncio.wait_for(controller.started.wait(),1.5)
                    self.assertIs(session.response_controller,controller)
                    value=await daemon.set_output(started['session_id'],False)
                    self.assertTrue(value['output_muted'])
                    with self.assertRaisesRegex(AssistantStoreError,'meeting_output_unavailable'):
                        await daemon.set_output('foreign-meeting',False)
                    await daemon.stop(started['session_id'])
                    await asyncio.wait_for(task,1)
                    summary.assert_not_called()
                self.assertFalse(controller.enabled)
                self.assertEqual(controller.stops,1)
                process=controller.native.rpc.process
                if process:
                    self.assertIsNotNone(process.returncode)
                if controller.native._close_task:
                    self.assertTrue(controller.native._close_task.done())
                if controller.native.rpc._close_task:
                    self.assertTrue(controller.native.rpc._close_task.done())
                return session.status(),bool(daemon.store.capture_interlocks())
            finally:
                await daemon.close()
                if task and not task.done():
                    task.cancel()
                    await asyncio.gather(task,return_exceptions=True)
                await reap_controller(controller)

    async def test_clean_stop_reaps_b_child_and_preserves_muted_fenced_control(self):
        controller = Controller()
        status, closed = await self.run_formal(controller)
        self.assertEqual(status['cleanup_status'], 'cleaned')
        self.assertEqual(status['response_cleanup_status'], 'cleaned')
        self.assertIsNotNone(controller.native.rpc.process.returncode)
        self.assertFalse(closed)

    async def test_not_started_stop_can_be_clean_without_child(self):
        status, closed = await self.run_formal(Controller(child=False))
        self.assertEqual(status['cleanup_status'], 'cleaned')
        self.assertFalse(closed)

    async def test_unknown_b_stop_is_not_cleaned_and_owned_child_is_reaped(self):
        controller = Controller(mode='unknown')
        status, closed = await self.run_formal(controller)
        self.assertEqual(status['cleanup_status'], 'unknown')
        self.assertEqual(status['response_cleanup_status'], 'unknown')
        self.assertEqual(status['error'], 'rx_cleanup_unknown')
        self.assertIsNotNone(controller.native.rpc.process.returncode)
        self.assertTrue(closed)

    async def test_hung_b_close_and_its_shielded_task_are_bounded_reaped_unknown(self):
        controller = Controller(mode='hang_close')
        status, closed = await self.run_formal(controller)
        self.assertEqual(status['cleanup_status'], 'unknown')
        self.assertIsNotNone(controller.native.rpc.process.returncode)
        self.assertTrue(controller.native._close_task.done())
        self.assertTrue(closed)

    async def test_partial_b_start_process_is_owned_and_reaped_within_cleanup(self):
        controller = Controller(mode='hang_start')
        status, closed = await self.run_formal(controller)
        self.assertEqual(status['cleanup_status'], 'unknown')
        self.assertIsNotNone(controller.native.rpc.process.returncode)
        self.assertTrue(closed)

    async def test_unknown_b_cleanup_does_not_kill_unrelated_child(self):
        unrelated = await asyncio.create_subprocess_exec('sleep', '30')
        try:
            status, _ = await self.run_formal(Controller(mode='unknown'))
            self.assertEqual(status['cleanup_status'], 'unknown')
            self.assertIsNone(unrelated.returncode)
        finally:
            if unrelated.returncode is None: unrelated.kill()
            await unrelated.wait()

    async def test_native_cancel_finally_spawned_rpc_close_is_collected_and_cancelled(self):
        controller = Controller(mode='spawn_rpc_on_cancel')
        status, closed = await self.run_formal(controller)
        self.assertIsNotNone(controller.native.rpc._close_task)
        self.assertTrue(controller.native.rpc._close_task.done())
        self.assertEqual(status['cleanup_status'], 'unknown')
        self.assertTrue(closed)
