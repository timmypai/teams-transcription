"""Owned native relay identity and cancellation; local synthetic WebRTC only."""
import asyncio
import shutil
import unittest

from teams_transcription.b_browser_tx import BrowserTXSink
from teams_transcription.browser import Browser
from native_output_fixture import NativeSource, fixture_page, meeting_fixture


@unittest.skipUnless(shutil.which('chromium'), 'Chromium fixture unavailable')
class BrowserTXIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sinks = []
        self.runtime = await Browser().__aenter__()
        self.context, self.page = await fixture_page(self.runtime, capture=True)
        await meeting_fixture(self.page)
        self.source = await NativeSource.create(self.runtime)

    async def asyncTearDown(self):
        for sink in self.sinks:
            await sink.stop()
        await self.source.close()
        await self.context.close()
        await self.runtime.__aexit__(None, None, None)

    async def sink(self, policy=lambda binding: True):
        sink = BrowserTXSink(self.page, meeting_id='one', dual_channel_policy=policy)
        await sink.bind()
        self.sinks.append(sink)
        await sink.attach_live(self.source.page)
        return sink

    async def test_native_source_required_and_colocation_rejected(self):
        sink = BrowserTXSink(self.page, meeting_id='one')
        await sink.bind()
        with self.assertRaisesRegex(RuntimeError, 'attach_live'):
            await sink.start()
        with self.assertRaisesRegex(RuntimeError, 'isolated'):
            await sink.attach_live(self.page)
        colocated = await self.context.new_page()
        try:
            with self.assertRaisesRegex(RuntimeError, 'isolated'):
                await sink.attach_live(colocated)
        finally:
            await colocated.close()
        self.assertFalse(await self.page.evaluate('!!window.__bTX'))

    async def test_unavailable_source_never_replaces_sender(self):
        sink = BrowserTXSink(self.page, meeting_id='one')
        await sink.bind()
        self.sinks.append(sink)
        await self.source.page.evaluate('__live.pc.close()')
        with self.assertRaisesRegex(Exception, 'receiver unavailable'):
            await sink.attach_live(self.source.page)
        self.assertFalse(sink._pending)
        self.assertTrue(await self.page.evaluate('fixture.sender.track===fixture.track && !fixture.track.enabled'))

    async def test_unsupported_target_api_fails_and_cleans_before_negotiation(self):
        # Reject missing WebCodecs before acquiring encoded streams whose
        # transport has not negotiated yet; failure must not strand cleanup.
        await self.page.evaluate('window.AudioDecoder=undefined')
        sink=BrowserTXSink(self.page,meeting_id='one')
        await sink.bind()
        self.sinks.append(sink)
        async with asyncio.timeout(3):
            with self.assertRaises(Exception):
                await sink.attach_live(self.source.page)
        self.assertEqual(sink.cleanup_status,'cleaned')
        self.assertFalse(await self.page.evaluate('!!window.__bTX'))
        self.assertTrue(await self.page.evaluate(
            'fixture.sender.track===fixture.track && !fixture.track.enabled'))

    async def test_cancel_during_relay_attach_cleans_both_pages(self):
        sink = BrowserTXSink(self.page, meeting_id='one')
        await sink.bind()
        self.sinks.append(sink)
        await self.page.evaluate(r"""()=>{
          const create=__meeting.createNativeOutputPeer;
          __meeting.createNativeOutputPeer=()=>{
            const relay=create(),set=relay.setLocalDescription.bind(relay);
            relay.setLocalDescription=async description=>{
              await set(description);window.answerEntered=true;
              await new Promise(resolve=>window.releaseAnswer=resolve);
            };
            return relay;
          };
        }""")
        attaching=asyncio.create_task(sink.attach_live(self.source.page))
        await self.page.wait_for_function('window.answerEntered===true')
        attaching.cancel()
        await self.page.evaluate('releaseAnswer()')
        with self.assertRaises(asyncio.CancelledError):
            await attaching
        self.assertEqual(sink.cleanup_status, 'cleaned')
        self.assertFalse(await self.page.evaluate('!!window.__bTX'))
        self.assertFalse(await self.source.page.evaluate('!!window.__bNativeTXSource'))
        self.assertTrue(await self.page.evaluate('fixture.sender.track===fixture.track && !fixture.track.enabled'))

    async def test_default_rx_only_and_revoked_policy_keep_track_disabled(self):
        sink = await self.sink(None)
        await sink.start()
        with self.assertRaisesRegex(RuntimeError, 'dual-channel'):
            await sink.set_muted(False)
        self.assertTrue(await self.page.evaluate('!__bTX.track.enabled'))
        await sink.stop()
        permit = True
        def policy(binding):
            self.assertEqual(binding['meeting_id'], 'one')
            self.assertTrue(binding['track_id'])
            return permit
        sink = await self.sink(policy)
        await sink.start()
        await sink.set_muted(False)
        permit = False
        with self.assertRaisesRegex(RuntimeError, 'dual-channel'):
            await sink.set_muted(False)
        self.assertTrue(await self.page.evaluate('!__bTX.track.enabled'))

    async def test_explicit_meeting_marker_required(self):
        await self.page.evaluate("__bMeetingId='two'")
        sink = BrowserTXSink(self.page, meeting_id='one')
        with self.assertRaisesRegex(Exception, 'Meeting identity mismatch'):
            await sink.bind()

    async def test_meeting_change_revokes_synchronously_without_stale_restore(self):
        sink = await self.sink()
        await sink.start()
        await sink.set_muted(False)
        await self.page.evaluate("window.generated=__bTX.track; __bMeetingId='two'")
        self.assertTrue(await self.page.evaluate('__bTX.disposed && !generated.enabled'))
        with self.assertRaises(Exception):
            await sink.observe()
        await sink.stop()
        self.assertTrue(await self.page.evaluate("generated.readyState==='ended' && fixture.sender.track===generated"))
        self.assertFalse(await self.page.evaluate('fixture.track.enabled'))

    async def test_peer_replacement_never_reselected_and_foreign_track_untouched(self):
        sink = await self.sink()
        await sink.start()
        await sink.set_muted(False)
        await self.page.evaluate(r"""()=>{
          window.generated=__bTX.track;
          const pc=new RTCPeerConnection(),track=fixture.track.clone();track.enabled=false;
          const sender=pc.addTrack(track,new MediaStream([track]));
          __meeting.peers[0]=pc;window.foreign={pc,track,sender};
        }""")
        with self.assertRaises(Exception):
            await sink.set_muted(False)
        await sink.stop()
        self.assertTrue(await self.page.evaluate('foreign.sender.track===foreign.track && !foreign.track.enabled'))
        self.assertEqual(await self.page.evaluate('generated.readyState'), 'ended')

    async def test_foreign_sender_replacement_is_not_touched_by_stop(self):
        sink = await self.sink()
        await sink.start()
        await self.page.evaluate(r"""async ()=>{
          window.generated=__bTX.track;window.foreign=fixture.track.clone();
          await fixture.sender.replaceTrack(foreign);
        }""")
        await sink.stop()
        self.assertTrue(await self.page.evaluate('fixture.sender.track===foreign'))
        self.assertEqual(await self.page.evaluate('generated.readyState'), 'ended')

    async def test_foreign_owner_state_not_touched_by_stop(self):
        sink = await self.sink()
        await sink.start()
        await self.page.evaluate("window.owned=__bTX;window.__bTX={owner:'foreign'}")
        await sink.stop()
        self.assertEqual(await self.page.evaluate('__bTX.owner'), 'foreign')
        self.assertEqual(await self.page.evaluate('owned.track.readyState'), 'ended')
        self.assertTrue(await self.page.evaluate('fixture.sender.track===fixture.track'))

    async def test_target_document_and_same_document_navigation_invalidate(self):
        sink = await self.sink()
        await sink.start()
        await sink.set_muted(False)
        await self.page.evaluate("location.hash='another-meeting'")
        with self.assertRaises(Exception):
            await sink.observe()
        await sink.stop()
        await self.page.goto('https://native-output.invalid/new-document')
        await meeting_fixture(self.page)
        with self.assertRaises(Exception):
            await sink.start()
        self.assertFalse(await self.page.evaluate('!!window.__bTX'))

    async def test_source_track_end_and_live_object_replacement_revoke(self):
        sink = await self.sink()
        await sink.start()
        await sink.set_muted(False)
        await self.source.page.evaluate('__live.pc.getReceivers()[0].track.stop()')
        await self.page.wait_for_function('__bTX.disposed && !__bTX.track.enabled')
        with self.assertRaises(Exception):
            await sink.observe()
        self.assertFalse(sink.diagnostics()['connected'])

    async def test_source_identity_change_after_authorization_await_cannot_enable(self):
        async def policy(_):
            await self.source.page.evaluate('window.__live={pc:__live.pc}')
            return True
        sink = await self.sink(policy)
        await sink.start()
        with self.assertRaises(Exception):
            await sink.set_muted(False)
        self.assertFalse(await self.page.evaluate('__bTX.track.enabled'))

    async def install_replace_gate(self):
        await self.page.evaluate(r"""()=>{
          const replace=fixture.sender.replaceTrack.bind(fixture.sender);
          fixture.sender.replaceTrack=async track=>{
            await replace(track);
            if(track!==fixture.track){window.replaced=true;await new Promise(resolve=>window.releaseReplace=resolve);}
          };
        }""")

    async def test_cancel_after_replace_cleans_owned_state(self):
        sink = await self.sink()
        await self.install_replace_gate()
        task = asyncio.create_task(sink.start())
        await self.page.wait_for_function('window.replaced===true')
        self.assertFalse(sink.started)
        task.cancel()
        await self.page.evaluate('releaseReplace()')
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(sink.cleanup_status, 'cleaned')
        self.assertFalse(await self.page.evaluate('!!window.__bTX'))
        self.assertTrue(await self.page.evaluate('fixture.sender.track===fixture.track && !fixture.track.enabled'))

    async def test_cancel_cleanup_timeout_blocks_restart_until_explicit_success(self):
        sink = await self.sink()
        sink.cleanup_timeout = .03
        await self.install_replace_gate()
        task = asyncio.create_task(sink.start())
        await self.page.wait_for_function('window.replaced===true')
        task.cancel()
        with self.assertRaises(asyncio.TimeoutError):
            await task
        self.assertEqual(sink.cleanup_status, 'unknown')
        self.assertFalse(await self.page.evaluate('__bTX.track.enabled'))
        with self.assertRaisesRegex(RuntimeError, 'cleanup pending'):
            await sink.start()
        await self.page.evaluate('releaseReplace()')
        sink.cleanup_timeout = 3
        await sink.stop()
        self.assertEqual(sink.cleanup_status, 'cleaned')

    async def test_concurrent_stop_is_idempotent(self):
        sink = await self.sink()
        await sink.start()
        await sink.set_muted(False)
        await asyncio.gather(sink.stop(), sink.stop())
        self.assertEqual(sink.cleanup_status, 'cleaned')
        self.assertTrue(await self.page.evaluate('fixture.sender.track===fixture.track && !fixture.track.enabled'))
        self.assertFalse(await self.page.evaluate('!!window.__bTX'))

    async def test_missing_stats_fail_closed_without_fabricated_zeroes(self):
        sink = await self.sink()
        await sink.start()
        await sink.set_muted(False)
        await self.page.evaluate("""()=>{
          __bTX.decoder.stats=()=>({render:{frames:960,duration:.02},input:{packets:1}});
        }""")
        with self.assertRaisesRegex(Exception, 'statistics unavailable'):
            await sink.observe()
        self.assertFalse(await self.page.evaluate('__bTX.track.enabled'))
        self.assertFalse(sink.diagnostics()['available'])
        self.assertNotIn('energy', sink.diagnostics())

    async def test_before_first_rtp_is_connected_without_claiming_activity(self):
        sink = await self.sink()
        await sink.start()
        sample=await sink.observe()
        self.assertTrue(sample['connected'])
        self.assertEqual(sample['source_frames'],0)
        self.assertEqual(sample['source_packets'],0)
        self.assertEqual(sample['source_energy'],0)
        self.assertEqual(sample['energy'],0)
        self.assertFalse(await self.page.evaluate('__bTX.track.enabled'))

    async def test_decoder_graph_mutation_revokes_owned_sender(self):
        sink = await self.sink()
        await sink.start()
        await sink.set_muted(False)
        await self.page.evaluate('__bTX.decoder.ctx.suspend()')
        await self.page.wait_for_function('__bTX.disposed && !__bTX.track.enabled')
        self.assertEqual(await self.page.evaluate('__bTX.decoder.ctx.state'),'closed')
        with self.assertRaises(Exception):
            await sink.observe()
