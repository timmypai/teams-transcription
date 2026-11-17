"""Existing helper media graph under controlled Node + mocked Playwright only."""
import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

from teams_transcription.b_live import ResponseLivePeer
from teams_transcription.b_guard import ExistingLiveGuard
from teams_transcription import b_media_profile as graph
from teams_transcription.qa_probe import probe_rtp_snapshot


class MediaGraphTests(unittest.TestCase):
    def test_actual_reused_javascript_graph_contract_and_sink_fail_closed(self):
        script = Path(__file__).parent/'fixtures/verify_response_media_profile.js'
        result = subprocess.run(['node', str(script)], input=json.dumps({
            'render':graph.RENDER_OUTPUT_JS, 'silent':graph.SILENT_PEER_JS,
            'close':graph.FULL_CLOSE_JS, 'trusted':graph.TRUSTED_PAGE_CHECK_JS,
            'bridge':graph.PCM_OUTPUT_BRIDGE_JS}),
            text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt['host'], 'controlled_node_graph_not_browser')
        self.assertEqual(receipt['models_called'], 0)
        self.assertGreater(receipt['assertions'], 40)
        self.assertGreater(receipt['sustained_frames'], 60 * 48000)
        print(json.dumps(receipt))

    def test_published_sender_contract_drift_is_rejected(self):
        with patch.object(graph, 'PEER_JS', 'unrecognized sender'):
            with self.assertRaisesRegex(ValueError, 'contract_changed'):
                graph.silent_peer_js()


class MediaProfileTests(unittest.IsolatedAsyncioTestCase):
    def make_peer(self, profile='trusted-none', flags=None, render_error=False):
        browser = AsyncMock(); context = browser.new_context.return_value
        page = context.new_page.return_value
        page.is_closed = MagicMock(return_value=False)
        page.main_frame = object(); page.on = MagicMock()
        state = {'trusted_origin':True,'secure_context':True,'audio_context_present':True,'set_sink_id_present':True}
        if flags: state.update(flags)
        async def evaluate(code, *args):
            if code == graph.TRUSTED_PAGE_CHECK_JS:return state
            if code == graph.SILENT_PEER_JS:return 'controlled-offer'
            if code == graph.RENDER_OUTPUT_JS and render_error:raise RuntimeError('silent_sink_not_verified')
            return None
        page.evaluate.side_effect = evaluate
        guard = ExistingLiveGuard(AsyncMock(return_value=object()), AsyncMock(return_value=True))
        peer = ResponseLivePeer(browser,AsyncMock(),AsyncMock(),AsyncMock(),
            guard=guard,meeting_id='meeting',media_profile=profile)
        return peer, page, context, guard

    async def test_candidate_prepares_trusted_document_then_owns_and_validates_graph(self):
        peer,page,context,guard = self.make_peer()
        async def goto(*args, **kwargs):
            page.on.assert_not_called()  # Intentional initial nav cannot invalidate new graph.
            self.assertIs(guard.context, context)
        page.goto.side_effect = goto
        await peer.bind_session('owned-thread')
        self.assertEqual(await peer.offer(), 'controlled-offer')
        codes = [call.args[0] for call in page.evaluate.await_args_list]
        self.assertEqual(codes,[graph.TRUSTED_PAGE_CHECK_JS,graph.SILENT_PEER_JS,graph.RENDER_OUTPUT_JS])
        self.assertEqual(peer.graph_prepare_status,'returned')
        self.assertTrue(peer._document_valid)
        guard.validate_callback.assert_awaited_once()
        await peer.close();context.close.assert_awaited_once()
        self.assertIn(graph.FULL_CLOSE_JS,[c.args[0] for c in page.evaluate.await_args_list])

    async def test_candidate_missing_sink_stops_before_offer_without_legacy_fallback(self):
        peer,page,context,_ = self.make_peer(flags={'set_sink_id_present':False})
        await peer.bind_session('owned-thread')
        with self.assertRaisesRegex(ValueError,'silent_sink_api_missing'):await peer.offer()
        self.assertEqual(peer.graph_prepare_status,'trusted_origin_failed')
        self.assertFalse(peer._document_valid)
        self.assertEqual([c.args[0] for c in page.evaluate.await_args_list],[graph.TRUSTED_PAGE_CHECK_JS])
        await peer.close();context.close.assert_awaited_once()

    async def test_candidate_none_sink_failure_propagates_and_owns_partial_uplink_close(self):
        peer,page,context,_ = self.make_peer(render_error=True)
        await peer.bind_session('owned-thread')
        with self.assertRaisesRegex(RuntimeError,'silent_sink_not_verified'):await peer.offer()
        self.assertEqual(peer.graph_prepare_status,'failed')
        self.assertEqual(peer.graph_prepare_failure,'silent_sink_not_verified')
        await peer.close();context.close.assert_awaited_once()

    async def test_trusted_page_exact_get_main_frame_only_and_route_removed(self):
        peer,page,_,_ = self.make_peer()
        await graph.prepare_trusted_audio_page(page)
        url,handler = page.route.call_args.args
        self.assertEqual(url,graph.LOCAL_AUDIO_PAGE)
        for method,frame,nav,request_url in [('POST',page.main_frame,True,url),
                ('GET',object(),True,url),('GET',page.main_frame,False,url),
                ('GET',page.main_frame,True,'http://127.0.0.1/other')]:
            route=AsyncMock();route.request=SimpleNamespace(url=request_url,method=method,
                frame=frame,is_navigation_request=lambda:nav)
            await handler(route);route.abort.assert_awaited_once();route.fulfill.assert_not_awaited()
        route=AsyncMock();route.request=SimpleNamespace(url=url,method='GET',frame=page.main_frame,
            is_navigation_request=lambda:True)
        await handler(route);route.fulfill.assert_awaited_once();route.continue_.assert_not_awaited()
        page.unroute.assert_awaited_once_with(url,handler)

    async def test_candidate_later_navigation_revokes_bound_document_and_pcm(self):
        peer,page,_,_ = self.make_peer()
        await peer.bind_session('owned-thread');await peer.offer()
        peer._navigated(page.main_frame)
        await __import__('asyncio').sleep(0)
        with self.assertRaisesRegex(RuntimeError,'invalidated'):await peer.push(b'\1\0')
        peer.on_error.assert_awaited_once()
        await peer.close()

    async def test_unknown_profile_rejected_before_browser_context(self):
        browser=AsyncMock()
        with self.assertRaisesRegex(ValueError,'Unknown'):
            ResponseLivePeer(browser,AsyncMock(),AsyncMock(),AsyncMock(),media_profile='unsupported')
        browser.new_context.assert_not_awaited()

    async def test_rtp_snapshot_reuses_stats_with_counts_only(self):
        native=SimpleNamespace(peer=SimpleNamespace(stats=AsyncMock(return_value={
            'connection':'connected','data_channel':'open','sent_frames':4000,
            'audio_context_state':'running','reports':[{'bytesSent':42,'packetsSent':3,
                'id':'secret-id','address':'secret-address'}], 'sdp':'secret-sdp'})))
        row=await probe_rtp_snapshot(native,'wake_after_send_1',0)
        self.assertEqual(row['outbound_audio'],[{'bytesSent':42,'packetsSent':3}])
        self.assertEqual(row['read_status'],'returned')
        self.assertNotIn('secret',json.dumps(row))
        native.peer.stats.assert_awaited_once()

    async def test_rtp_snapshot_timeout_is_bounded_and_missing_remains_unknown(self):
        import asyncio
        async def blocked():await asyncio.Event().wait()
        row=await probe_rtp_snapshot(SimpleNamespace(peer=SimpleNamespace(stats=blocked)),
            'failure_before_stop',0,limit_seconds=.01)
        self.assertEqual(row['read_status'],'timeout')
        self.assertNotIn('outbound_audio',row)
        row=await probe_rtp_snapshot(None,'failure_before_stop',0)
        self.assertEqual(row['read_status'],'unavailable')
