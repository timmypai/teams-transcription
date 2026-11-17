"""Cancel at stop RPC/closed waits; no real auth/model/network/audio calls."""
import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from teams_transcription.b_audio import GatedPCMOutput
from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription.b_live import LiveSpeechChannel
from teams_transcription.b_native import NativeLiveResponder
from teams_transcription.b_response import MeetingMinutesTool


class CloseCancellationTests(unittest.IsolatedAsyncioTestCase):
    def fixture(self, native=False):
        output=(NativeTrackOutput if native else GatedPCMOutput)('one',AsyncMock())
        args=(None,'/tmp/unused-close','one',output)
        if native:
            channel=NativeLiveResponder(*args,MeetingMinutesTool('one',lambda m:{}),stop_timeout=.05)
        else:
            channel=LiveSpeechChannel(*args,stop_timeout=.05)
        channel.peer=AsyncMock()
        channel.rpc=AsyncMock()
        channel.rpc.process=SimpleNamespace(returncode=None)
        channel.rpc.notifications=asyncio.Queue()
        channel.thread_id='own-thread'
        channel._answer=asyncio.get_running_loop().create_future()
        channel.receiver=asyncio.create_task(channel._receive())
        channel.started=True
        return channel

    async def assert_clean(self,channel):
        self.assertFalse(channel.started)
        self.assertTrue(channel.output.closed)
        self.assertIsNone(channel.receiver)
        self.assertTrue(channel._cleanup_done)
        self.assertEqual(channel.cleanup_status,'cleaned')
        channel.peer.close.assert_awaited_once()
        channel.rpc.close.assert_awaited_once()

    async def test_cancel_pending_stop_rpc_waits_timeout_then_owned_cleanup(self):
        channel=self.fixture()
        entered=asyncio.Event()
        async def request(*args):
            entered.set()
            await asyncio.Event().wait()
        channel.rpc.request.side_effect=request
        closing=asyncio.create_task(channel.close())
        await entered.wait()
        receiver=channel.receiver
        closing.cancel()
        await asyncio.sleep(.005)
        self.assertFalse(closing.done())
        self.assertFalse(receiver.done())
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertEqual(channel.stop_status,'timeout')
        self.assertFalse(channel.stop_ack_received)
        self.assertFalse(channel.stop_closed_received)
        self.assertIn('timed out',channel.stop_error)
        await self.assert_clean(channel)
        self.assertEqual(await channel.close(),'timeout')
        channel.rpc.request.assert_awaited_once()

    async def test_repeated_cancel_while_closed_pending_preserves_ack_and_cleanup(self):
        channel=self.fixture()
        ack=asyncio.Event()
        async def request(*args):
            ack.set()
            return {}
        channel.rpc.request.side_effect=request
        closing=asyncio.create_task(channel.close())
        await ack.wait()
        closing.cancel()
        await asyncio.sleep(.005)
        closing.cancel()
        channel.rpc.notifications.put_nowait({'method':'thread/realtime/closed',
            'params':{'threadId':'own-thread','reason':'stopped'}})
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertEqual(channel.stop_status,'stopped')
        self.assertTrue(channel.stop_ack_received)
        self.assertTrue(channel.stop_closed_received)
        await self.assert_clean(channel)

    async def test_concurrent_close_shares_operation_cancel_does_not_cancel_other_caller(self):
        channel=self.fixture()
        ack=asyncio.Event()
        async def request(*args):
            ack.set()
            return {}
        channel.rpc.request.side_effect=request
        one=asyncio.create_task(channel.close())
        two=asyncio.create_task(channel.close())
        await ack.wait()
        one.cancel()
        channel.rpc.notifications.put_nowait({'method':'thread/realtime/closed',
            'params':{'threadId':'own-thread','reason':'stopped'}})
        with self.assertRaises(asyncio.CancelledError):
            await one
        self.assertEqual(await two,'stopped')
        await self.assert_clean(channel)
        channel.rpc.request.assert_awaited_once()

    async def test_peer_cleanup_error_still_closes_rpc_and_reports_unknown_cleanup(self):
        channel=self.fixture()
        channel.peer.close.side_effect=RuntimeError('fixture peer cleanup failure')
        with self.assertRaisesRegex(RuntimeError,'cleanup failed'):
            await channel.close()
        self.assertEqual(channel.stop_status,'timeout')
        self.assertEqual(channel.cleanup_status,'unknown')
        self.assertFalse(channel._cleanup_done)
        self.assertIsNone(channel.receiver)
        channel.rpc.close.assert_awaited_once()
        self.assertIn('peer:',channel.cleanup_errors[0])

    async def test_native_close_cancel_also_clears_children_and_rx_queue(self):
        channel=self.fixture(native=True)
        channel.state='sleeping'
        channel._rx.put_nowait((b'\0\0',0,1))
        channel._rx_task=asyncio.create_task(asyncio.Event().wait())
        entered=asyncio.Event()
        async def request(*args):
            entered.set()
            await asyncio.Event().wait()
        channel.rpc.request.side_effect=request
        closing=asyncio.create_task(channel.close())
        await entered.wait()
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        await self.assert_clean(channel)
        self.assertEqual(channel.state,'stop_unknown')
        self.assertTrue(channel._rx_task.done())
        self.assertTrue(channel._rx.empty())
