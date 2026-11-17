"""Injected A guard contract checks; no new network allowlist or real traffic."""
import unittest
import asyncio
from unittest.mock import AsyncMock, MagicMock

from teams_transcription.b_guard import ExistingLiveGuard, LiveBinding
from teams_transcription.b_live import ResponseLivePeer, LiveSpeechChannel
from teams_transcription.b_audio import GatedPCMOutput
from teams_transcription.runtime_guard import RuntimeLiveGuard
from tests.test_runtime_guard import sdp, resolver


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_channel_owns_official_start_ACK_and_dynamic_media_proof(self):
        browser, context, page = AsyncMock(), AsyncMock(), AsyncMock()
        browser.new_context.return_value = context
        context.new_page.return_value = page
        page.main_frame = object()
        page.on = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        async def evaluate(script, *args):
            if "async (iceServers" in script:
                return sdp("10.0.0.2")
        page.evaluate.side_effect = evaluate
        host = RuntimeLiveGuard("meeting", ["live.example.test"], resolver=resolver)
        channel = LiveSpeechChannel(browser, "/tmp/not-used", "meeting",
            GatedPCMOutput("meeting", AsyncMock()), guard=host.adapter(), stop_timeout=.1)
        rpc = channel.rpc = AsyncMock()
        rpc.notifications = asyncio.Queue()
        rpc.thread.return_value = "own-thread"
        channel.peer.wait_connected = AsyncMock()
        async def request(method, params):
            if method == "account/read":
                return {"account":{"type":"chatgpt"}}
            if method == "thread/realtime/start":
                rpc.notifications.put_nowait({"method":"thread/realtime/sdp", "params":{
                    "threadId":"foreign", "sdp":sdp("127.0.0.1")}})
                rpc.notifications.put_nowait({"method":"thread/realtime/sdp", "params":{
                    "threadId":"own-thread", "sdp":sdp("9.9.9.9")}})
            if method == "thread/realtime/stop":
                rpc.notifications.put_nowait({"method":"thread/realtime/closed", "params":{"threadId":"own-thread"}})
            return {}
        rpc.request.side_effect = request
        try:
            await channel.start()
            self.assertTrue(host._answer_authorized)
            self.assertIsNone(host._answer_proof)
            remote = [c for c in page.evaluate.await_args_list if "setRemoteDescription" in c.args[0]]
            self.assertEqual(len(remote), 1)
            self.assertEqual(remote[0].args[1], sdp("9.9.9.9"))
            self.assertFalse(channel.output.enabled)
        finally:
            await channel.close()

    async def test_install_before_page_and_both_sdps_validate_bound_context(self):
        order=[]
        context=AsyncMock()
        page=context.new_page.return_value
        page.main_frame=object()
        page.on=MagicMock()
        page.is_closed=MagicMock(return_value=False)
        page.evaluate.side_effect=["fixture-offer",None,None,None]
        async def install(c,b):
            self.assertIs(c,context)
            self.assertEqual(b,LiveBinding("meeting","thread"))
            order.append("install")
            return "opaque-existing-guard"
        async def new_page():
            self.assertEqual(order,["install"])
            order.append("page")
            return page
        context.new_page.side_effect=new_page
        validate=AsyncMock(return_value=True)
        guard=ExistingLiveGuard(install,validate)
        browser=AsyncMock()
        browser.new_context.return_value=context
        peer=ResponseLivePeer(browser,AsyncMock(),AsyncMock(),AsyncMock(),guard=guard,meeting_id="meeting")
        await peer.bind_session("thread")
        self.assertEqual(await peer.offer(),"fixture-offer")
        await peer.answer("fixture-answer")
        self.assertEqual([c.args[-1] for c in validate.await_args_list],["offer","answer"])
        self.assertTrue(all(c.args[1] is context for c in validate.await_args_list))
        await peer.close()
        context.close.assert_awaited_once()

    async def test_guard_cannot_cross_context_provider_or_session_and_literal_true_required(self):
        context=object()
        guard=ExistingLiveGuard(lambda c,b:object(),lambda *a:True)
        binding=LiveBinding("one","thread")
        await guard.install(context,binding)
        for c,b in [(object(),binding),(context,LiveBinding("other","thread")),
                    (context,LiveBinding("one","foreign")),(context,LiveBinding("one","thread","foreign"))]:
            with self.assertRaisesRegex(RuntimeError,"mismatch"):
                await guard.validate(c,b,"sdp","answer")
        with self.assertRaisesRegex(RuntimeError,"reused"):
            await guard.install(context,binding)
        guard.validate_callback=lambda *a:1
        with self.assertRaisesRegex(RuntimeError,"rejected"):
            await guard.validate(context,binding,"sdp","answer")

    async def test_missing_guard_refused_before_oauth_or_context(self):
        browser=AsyncMock()
        channel=LiveSpeechChannel(browser,"/tmp/guard-unused","one",GatedPCMOutput("one",AsyncMock()))
        channel.rpc=AsyncMock()
        with self.assertRaisesRegex(RuntimeError,"guards"):
            await channel.start()
        channel.rpc.start.assert_not_awaited()
        browser.new_context.assert_not_awaited()

    async def test_rejected_answer_never_sets_remote_description(self):
        context=object()
        guard=ExistingLiveGuard(lambda *a:object(),lambda *a:False)
        peer=ResponseLivePeer(None,AsyncMock(),AsyncMock(),AsyncMock(),guard=guard,meeting_id="one")
        await peer.bind_session("thread")
        peer.context=context
        peer.page=AsyncMock()
        peer.page.is_closed=MagicMock(return_value=False)
        peer._document_valid=True
        await guard.install(context,peer.binding)
        with self.assertRaisesRegex(RuntimeError,"rejected"):
            await peer.answer("invalid")
        peer.page.evaluate.assert_not_awaited()

    async def test_live_document_navigation_revokes_media_and_context_return(self):
        peer=ResponseLivePeer(None,AsyncMock(),AsyncMock(),AsyncMock(),guard=None,meeting_id="one")
        peer.page=MagicMock()
        peer.page.main_frame=object()
        peer.page.is_closed.return_value=False
        peer._document_valid=True
        peer._navigated(peer.page.main_frame)
        await __import__('asyncio').sleep(0)
        peer.on_error.assert_awaited_once()
        with self.assertRaisesRegex(RuntimeError,"invalidated"):
            await peer.send_event({"type":"delegation.context.append"})
        with self.assertRaisesRegex(RuntimeError,"invalidated"):
            await peer.push(b"\0\0")

    async def test_context_capture_dispatch_failure_closes_the_new_epoch(self):
        peer=ResponseLivePeer(None,AsyncMock(),AsyncMock(),AsyncMock(),meeting_id="one")
        peer.page=AsyncMock()
        peer.page.is_closed=MagicMock(return_value=False)
        peer._document_valid=True
        peer.page.evaluate.side_effect=[RuntimeError("controlled send failure"),None]
        event={"type":"session.context.append","channel":"commentary",
            "content":[{"type":"input_text","text":"RESULT_COMPLETE"}]}
        with self.assertRaisesRegex(RuntimeError,"controlled send failure"):
            await peer.send_event(event,capture_output=True)
        self.assertFalse(peer._capture_enabled)
        self.assertEqual(peer._capture_generation,2)
        self.assertEqual(peer.page.evaluate.await_args_list[0].args[1],
            {"event":event,"captureGeneration":1})
        self.assertEqual(peer.page.evaluate.await_args_list[1].args[1],
            {"enabled":False,"generation":2})

    async def test_context_capture_dispatch_cancellation_closes_the_new_epoch(self):
        peer=ResponseLivePeer(None,AsyncMock(),AsyncMock(),AsyncMock(),meeting_id="one")
        peer.page=AsyncMock()
        peer.page.is_closed=MagicMock(return_value=False)
        peer._document_valid=True
        entered, release=asyncio.Event(),asyncio.Event()
        async def evaluate(script,args):
            if "event" in args:
                entered.set()
                await release.wait()
        peer.page.evaluate.side_effect=evaluate
        task=asyncio.create_task(peer.send_event({"type":"session.context.append"},
            capture_output=True))
        try:
            await asyncio.wait_for(entered.wait(),1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertFalse(peer._capture_enabled)
            self.assertEqual(peer.page.evaluate.await_args.args[1],
                {"enabled":False,"generation":2})
        finally:
            release.set()
            await asyncio.gather(task,return_exceptions=True)

    async def test_context_capture_dispatch_rechecks_document_after_evaluate(self):
        peer=ResponseLivePeer(None,AsyncMock(),AsyncMock(),AsyncMock(),meeting_id="one")
        peer.page=AsyncMock()
        peer.page.is_closed=MagicMock(return_value=False)
        peer._document_valid=True
        async def evaluate(script,args):
            peer._document_valid=False
        peer.page.evaluate.side_effect=evaluate
        with self.assertRaisesRegex(RuntimeError,"invalidated"):
            await peer.send_event({"type":"session.context.append"},capture_output=True)
        self.assertFalse(peer._capture_enabled)
        self.assertEqual(peer._capture_generation,2)

    async def test_sink_ready_keeps_the_context_dispatch_capture_epoch(self):
        peer=ResponseLivePeer(None,AsyncMock(),AsyncMock(),AsyncMock(),meeting_id="one")
        peer.page=AsyncMock()
        peer.page.is_closed=MagicMock(return_value=False)
        peer._document_valid=True
        await peer.send_event({"type":"session.context.append"})
        self.assertFalse(peer._capture_enabled)
        self.assertIsNone(peer.page.evaluate.await_args.args[1]["captureGeneration"])
        await peer.send_event({"type":"session.context.append"},capture_output=True)
        self.assertTrue(peer._capture_enabled)
        self.assertEqual(peer.page.evaluate.await_args.args[1]["captureGeneration"],1)
        await peer.set_output_capture(True)
        self.assertEqual(peer._capture_generation,1)
        self.assertEqual(peer.page.evaluate.await_count,2)
