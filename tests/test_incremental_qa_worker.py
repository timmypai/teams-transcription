"""Production config/lifecycle regression; fake media, no model or browser."""
import asyncio
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch



from teams_transcription.meeting_store import MeetingStore


from teams_transcription.storage import Session
from teams_transcription.worker import run_session
from test_rx_contract import Fixture, config



async def inline_storage(function, *args, **kwargs):
    # Controlled offline persistence only; avoids sandbox-denied thread wakeups.
    return function(*args, **kwargs)


class IncrementalQAWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.thread_patch = patch('asyncio.to_thread', side_effect=inline_storage)
        self.thread_patch.start()
        self.addCleanup(self.thread_patch.stop)
    

    async def test_ordinary_qa_worker_finishes_source_without_summary_generate(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract=False,duration=.02,response_enabled=True))
            controller = SimpleNamespace(ready=False,
                status=lambda: {'state':'stopped','cleanup_status':'cleaned'},
                push_rx=lambda *a, **k: True)
            async def start():
                controller.ready=True
                return True
            async def stop():
                controller.ready=False
                return 'stopped'
            controller.start=AsyncMock(side_effect=start)
            controller.stop=AsyncMock(side_effect=stop)
            controller.capture_ready=lambda:controller.ready
            fixture = Fixture()
            with fixture.install() as summary, patch(
                    'teams_transcription.response_runtime.ProductionResponseController',
                    return_value=controller) as factory:
                self.assertEqual(await run_session(session, install_signals=False), 0)
                summary.assert_not_called()
            self.assertEqual(session.status()['summary_state'], 'not_requested')
            result = MeetingStore(root).read(session.ident)
            self.assertEqual(result['status'], 'completed')
            self.assertTrue(result['events'])
            self.assertFalse((session.path / 'summary.md').exists())
            self.assertFalse((session.path / 'summary.json').exists())
            self.assertFalse((session.path / 'audio.wav').exists())
            self.assertFalse((session.path / 'source.wav').exists())

    async def test_missing_source_still_fails_without_summary_artifact(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract=False, duration=.02))
            with Fixture(emit_final=False).install() as summary:
                self.assertEqual(await run_session(session, install_signals=False), 1)
                summary.assert_not_called()
            self.assertEqual(session.status()['summary_state'], 'not_requested')
            self.assertEqual(MeetingStore(root).read(session.ident)['status'], 'failed')
            self.assertFalse((session.path / 'summary.md').exists())

    async def test_worker_failure_does_not_create_summary_placeholder(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract=False, duration=.02))
            fixture = Fixture(start_failure=True)
            with fixture.install() as summary:
                self.assertEqual(await run_session(session, install_signals=False), 1)
                summary.assert_not_called()
            self.assertEqual(session.status()['summary_state'], 'not_requested')
            self.assertFalse((session.path / 'summary.md').exists())
            self.assertEqual(MeetingStore(root).read(session.ident)['status'], 'failed')
