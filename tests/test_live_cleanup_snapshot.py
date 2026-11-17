"""Owned A cleanup evidence only; no real browser, account or model."""
import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from teams_transcription.live import LiveProvider


class LiveCleanupSnapshotTests(unittest.IsolatedAsyncioTestCase):
    def provider(self):
        provider = LiveProvider(None, '/tmp/synthetic-unused-cleanup', AsyncMock(), AsyncMock())
        process = SimpleNamespace(returncode=None)
        async def reap():
            process.returncode = 0
        provider.rpc = SimpleNamespace(process=process, tasks=[],
            request=AsyncMock(return_value={}), close=AsyncMock(side_effect=reap))
        provider.peer = SimpleNamespace(request_session_close=AsyncMock(return_value=True),
            close=AsyncMock())
        provider.thread_id = 'synthetic-thread'
        provider.receiver = asyncio.get_running_loop().create_future()
        provider.receiver.set_result(None)
        return provider

    async def test_dispatch_return_does_not_prove_owned_session_closed(self):
        provider = self.provider()
        await provider.close()
        receipt = provider.cleanup_snapshot()
        self.assertTrue(receipt['session_close_sent'])
        self.assertTrue(receipt['stop_rpc_acknowledged'])
        self.assertTrue(receipt['rpc_reaped'])
        self.assertFalse(receipt['owned_session_closed'])
        self.assertFalse(receipt['cleanup_verified'])

    async def test_matching_requested_close_and_reap_verify_cleanup_without_ids(self):
        provider = self.provider()
        async def request(method, params):
            self.assertEqual(method, 'thread/realtime/stop')
            await provider._closed('rpc', method.replace('/stop', '/closed'), 'stopped',
                source_thread=provider.thread_id)
            return {}
        provider.rpc.request.side_effect = request
        await provider.close()
        receipt = provider.cleanup_snapshot()
        self.assertTrue(receipt['cleanup_verified'])
        self.assertNotIn('synthetic-thread', str(receipt))
        self.assertNotIn('process', receipt)

    async def test_reaped_process_does_not_hide_resource_cleanup_failure(self):
        provider = self.provider()
        async def request(method, params):
            await provider._closed('rpc', 'thread/realtime/closed', 'stopped',
                source_thread=provider.thread_id)
        async def close_failure():
            provider.rpc.process.returncode = 0
            raise OSError('synthetic_tmpfs_cleanup_failed')
        provider.rpc.request.side_effect = request
        provider.rpc.close.side_effect = close_failure
        with self.assertRaisesRegex(OSError, 'synthetic_tmpfs_cleanup_failed'):
            await provider.close()
        receipt = provider.cleanup_snapshot()
        self.assertTrue(receipt['rpc_reaped'])
        self.assertTrue(receipt['cleanup_failed'])
        self.assertFalse(receipt['resources_closed'])
        self.assertFalse(receipt['cleanup_verified'])

    async def test_unscoped_or_foreign_rpc_closed_cannot_ack_owned_cleanup(self):
        for source_thread in (None, 'foreign-thread'):
            with self.subTest(source_thread=source_thread):
                provider = self.provider()
                provider.rpc.notifications = asyncio.Queue()
                provider.receiver = asyncio.create_task(provider._receive())
                async def request(method, params):
                    payload = {'reason': 'stopped'}
                    if source_thread is not None:
                        payload['threadId'] = source_thread
                    await provider.rpc.notifications.put({
                        'method': 'thread/realtime/closed', 'params': payload})
                    await provider.rpc.notifications.join()
                    return {}
                provider.rpc.request.side_effect = request
                await provider.close()
                self.assertFalse(provider.cleanup_snapshot()['owned_session_closed'])
                self.assertFalse(provider.cleanup_snapshot()['cleanup_verified'])

    async def test_bound_datachannel_ack_can_verify_owned_close(self):
        provider = self.provider()
        async def request(method, params):
            await provider._closed('datachannel', 'session.closed', 'client_request')
        provider.rpc.request.side_effect = request
        await provider.close()
        self.assertTrue(provider.cleanup_snapshot()['cleanup_verified'])

    async def test_suppressed_stop_failure_remains_unverified(self):
        provider = self.provider()
        provider.rpc.request.side_effect = RuntimeError('synthetic_stop_failure')
        await provider.close()
        receipt = provider.cleanup_snapshot()
        self.assertTrue(receipt['rpc_reaped'])
        self.assertFalse(receipt['stop_rpc_acknowledged'])
        self.assertFalse(receipt['cleanup_verified'])

    async def test_platform_expiry_never_becomes_owned_stop_ack(self):
        provider = self.provider()
        await provider._closed('datachannel', 'session.closed', 'expired')
        self.assertEqual(provider.error, 'live_session_expired')
        self.assertFalse(provider.cleanup_snapshot()['owned_session_closed'])
        with self.assertRaisesRegex(RuntimeError, 'live_session_expired'):
            await provider.close()
        self.assertFalse(provider.cleanup_snapshot()['cleanup_verified'])
