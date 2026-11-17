"""Ready waits report a finished worker without needing a browser or model."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright._impl._errors import TargetClosedError

from tests.test_response_runtime import wait_for_response_controller


class ResponseFixtureWaitTests(unittest.IsolatedAsyncioTestCase):
    def session(self):
        return SimpleNamespace(status=lambda: {'diagnostic': {'phase': 'browser_open',
            'exception_type': 'TargetClosedError', 'private': 'private-token'}})

    async def test_failed_worker_reports_reason_without_polling_until_timeout(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(1)
        with patch('tests.test_response_runtime.asyncio.sleep') as sleep:
            with self.assertRaisesRegex(AssertionError, '"worker_result": 1') as caught:
                await wait_for_response_controller(self.session(), future, timeout=.1)
        sleep.assert_not_called()
        self.assertIn('browser_open', str(caught.exception))
        self.assertIn('TargetClosedError', str(caught.exception))
        self.assertNotIn('private-token', str(caught.exception))

    async def test_successful_worker_ending_before_ready_is_also_an_error(self):
        future = asyncio.get_running_loop().create_future()
        future.set_result(0)
        with self.assertRaisesRegex(AssertionError, '"worker_result": 0'):
            await wait_for_response_controller(self.session(), future, timeout=.1)

    async def test_raised_worker_error_reports_class_without_exception_text(self):
        future = asyncio.get_running_loop().create_future()
        future.set_exception(TargetClosedError('private-token SDP https://fixture.invalid'))
        with self.assertRaisesRegex(AssertionError, 'TargetClosedError') as caught:
            await wait_for_response_controller(self.session(), future, timeout=.1)
        self.assertIn('"worker_result": "raised"', str(caught.exception))
        self.assertNotIn('private-token', str(caught.exception))

    async def test_cancelled_worker_is_reported_immediately(self):
        future = asyncio.get_running_loop().create_future()
        future.cancel()
        with self.assertRaisesRegex(AssertionError, '"worker_result": "cancelled"'):
            await wait_for_response_controller(self.session(), future, timeout=.1)

    async def test_running_worker_returns_ready_controller(self):
        future = asyncio.get_running_loop().create_future()
        session = self.session()
        session.response_controller = SimpleNamespace(state='sleeping')
        self.assertIs(await wait_for_response_controller(session, future), session.response_controller)
        future.cancel()
