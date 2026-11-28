"""Explicit dual-mode RX fixture, synthetic local tracks only."""
import asyncio
import shutil
import unittest

from teams_transcription.b_browser_tx import BrowserTXSink
from teams_transcription.b_dual_policy import DUAL_CHECK_JS, OwnedSenderDualPolicy
from teams_transcription.browser import Browser
from native_output_fixture import NativeSource,fixture_page


@unittest.skipUnless(shutil.which('chromium'),'Chromium fixture unavailable')
class OwnedSenderDualPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime=await Browser().__aenter__()
        self.context,self.page=await fixture_page(self.runtime,capture=True)
        self.packets=[]; self.rejected=[]; self.policy=None
        async def receive(source,packet):
            # Concrete every-packet production hook. RX-only is a separate path.
            if self.policy and await self.policy.check(): self.packets.append(packet)
            else: self.rejected.append(packet['start_frame'])
        await self.context.expose_binding('meetingAudio',receive)
        await self.fixture()
        self.source=await NativeSource.create(self.runtime)
        self.policy=OwnedSenderDualPolicy(self.page,'one')
        self.sink=BrowserTXSink(self.page,meeting_id='one',dual_channel_policy=self.policy.authorize)
        await self.sink.bind()
        await self.sink.attach_live(self.source.page)

    async def fixture(self):
        await self.page.evaluate("""async()=>{
          window.__bMeetingId='one';
          const ctx=new AudioContext({sampleRate:48000}); await ctx.resume();
          const osc=ctx.createOscillator(),dest=ctx.createMediaStreamDestination();
          osc.connect(dest); osc.start();
          const pc=new RTCPeerConnection(),track=dest.stream.getAudioTracks()[0];
          const sender=pc.addTrack(track,dest.stream);
          const video=document.createElement('canvas').captureStream().getVideoTracks()[0];
          const videoSender=pc.addTrack(video,new MediaStream([video]));
          window.fixture={ctx,osc,pc,track,sender,video,videoSender};
        }""")

    async def asyncTearDown(self):
        await self.sink.stop()
        await self.source.close()
        await self.context.close()
        await self.runtime.__aexit__(None,None,None)

    async def enable(self):
        if not self.sink._pending:
            await self.sink.attach_live(self.source.page)
        await self.sink.start(); await self.sink.set_muted(False)

    async def test_owned_sender_enabled_rx_continues_all_others_disabled(self):
        self.assertTrue(await self.policy.check())
        await self.page.evaluate('__meeting.attach(fixture.track.clone())')
        await self.enable()
        self.assertTrue(await self.policy.check())
        before=len(self.packets)
        await self.source.tone()
        await asyncio.sleep(.2)
        self.assertGreater(len(self.packets),before)
        self.assertEqual(self.rejected,[])
        self.assertTrue(await self.page.evaluate('__bTX.track.enabled && !fixture.track.enabled && !fixture.video.enabled'))
        self.assertFalse(await self.page.evaluate('__meeting.dropped'))
        await self.source.silence()
        await self.page.evaluate('window.retainedTrack=__bTX.track')
        await self.sink.pause()
        self.assertTrue(await self.policy.check())
        # A new lease reauthorizes the same owned track without disturbing RX.
        await self.enable()
        self.assertTrue(await self.policy.check())
        self.assertTrue(await self.page.evaluate('__bTX.track===retainedTrack && retainedTrack.enabled'))

    async def test_foreign_video_enabled_rejects_and_disables_every_sender(self):
        await self.enable(); await self.source.tone()
        await self.page.evaluate('fixture.video.enabled=true')
        self.assertFalse(await self.policy.check())
        self.assertTrue(self.policy.faulted)
        self.assertTrue(await self.page.evaluate('!fixture.video.enabled && !__bTX.track.enabled && __bTX.disposed'))
        self.assertFalse(await self.policy.authorize(self.sink.binding))

    async def test_foreign_audio_on_another_peer_rejects_and_disables(self):
        await self.enable()
        await self.page.evaluate("""()=>{
          const pc=new RTCPeerConnection(),track=fixture.track.clone();
          pc.addTrack(track,new MediaStream([track])); track.enabled=true;
          window.foreign={pc,track};
        }""")
        self.assertFalse(await self.policy.check())
        self.assertTrue(await self.page.evaluate('!foreign.track.enabled && !__bTX.track.enabled'))

    async def test_muted_foreign_replacement_faults_before_periodic_guard(self):
        await self.enable()
        await self.sink.set_muted(True)
        accepted=await self.page.evaluate(r"""async policyId=>{
          window.savedOwned=__bTX.track;
          window.foreignTrack=fixture.track.clone();foreignTrack.enabled=false;
          await fixture.sender.replaceTrack(foreignTrack);
          return (""" + DUAL_CHECK_JS + r""")({
            policyId,meetingId:'one',authorize:false,initialized:true});
        }""",self.policy.policy_id)
        self.assertFalse(accepted)
        self.assertFalse(await self.policy.check())
        self.assertTrue(self.policy.faulted)
        await self.sink.stop()
        self.assertTrue(await self.page.evaluate(
            'fixture.sender.track===foreignTrack && foreignTrack.readyState==="live" && '
            '!foreignTrack.enabled && savedOwned.readyState==="ended" && !savedOwned.enabled'))

    async def test_before_authorization_enabled_sender_is_rejected(self):
        await self.page.evaluate('fixture.video.enabled=true')
        self.assertFalse(await self.policy.check())
        self.assertFalse(await self.page.evaluate('fixture.video.enabled'))
        with self.assertRaisesRegex(Exception,'authorization'):
            await self.enable()

    async def test_forged_track_id_rejected_and_owned_relay_revoked(self):
        await self.enable(); await self.source.tone()
        forged={**self.sink.binding,'track_id':'foreign'}
        self.assertFalse(await self.policy.authorize(forged))
        self.assertTrue(await self.page.evaluate('!__bTX.track.enabled && __bTX.disposed'))

    async def test_marker_change_invalidates_without_muting_unknown_new_meeting(self):
        await self.enable()
        await self.page.evaluate("__bMeetingId='two'; fixture.video.enabled=true")
        self.assertFalse(await self.policy.check())
        # Old owned TX is revoked; the new meeting's unrelated state is untouched.
        self.assertTrue(await self.page.evaluate('!__bTX.track.enabled && fixture.video.enabled'))

    async def test_peer_replacement_invalidates_even_when_every_sender_disabled(self):
        await self.enable()
        await self.page.evaluate('window.old=__bTX; __meeting.peers[0]=new RTCPeerConnection()')
        self.assertFalse(await self.policy.check())
        self.assertFalse(await self.page.evaluate('old.track.enabled'))

    async def test_navigation_invalidates(self):
        await self.enable()
        await self.page.goto('about:blank?new')
        await self.fixture()
        self.assertFalse(await self.policy.check())
        self.assertTrue(await self.page.evaluate('!fixture.track.enabled && !fixture.video.enabled'))

    async def test_same_document_url_change_invalidates(self):
        await self.enable()
        await self.page.evaluate("location.hash='another-meeting'")
        self.assertFalse(await self.policy.check())
        self.assertFalse(await self.page.evaluate('__bTX.track.enabled'))

    async def test_external_enable_of_owned_but_muted_track_is_not_authorized(self):
        await self.enable(); await self.sink.set_muted(True)
        await self.page.evaluate('__bTX.track.enabled=true')
        self.assertFalse(await self.policy.check())
        self.assertFalse(await self.page.evaluate('__bTX.track.enabled'))

    async def install_restore_gate(self):
        await self.page.evaluate("""()=>{
          const replace=fixture.sender.replaceTrack.bind(fixture.sender);
          fixture.sender.replaceTrack=async track=>{
            if(track===fixture.track) {
              window.restoreEntered=true;
              await new Promise(resolve=>{window.allowRestoreApply=resolve});
              await replace(track);
              window.restoreApplied=true;
              await new Promise(resolve=>{window.allowRestoreReply=resolve});
            } else {await replace(track);}
          };
        }""")

    async def test_normal_restore_await_keeps_rx_and_future_speech_alive(self):
        await self.page.evaluate('__meeting.attach(fixture.track.clone())')
        await self.enable()
        await self.sink.pause()
        self.assertTrue(await self.policy.check())
        await self.install_restore_gate()
        stopping=asyncio.create_task(self.sink.stop())
        try:
            await self.page.wait_for_function('window.restoreEntered===true')
            self.assertTrue(await self.page.evaluate('__bTX.stopping && __bTX.disposed && !__bTX.track.enabled'))
            self.assertTrue(await self.policy.check())
            before=len(self.packets); await asyncio.sleep(.2)
            self.assertGreater(len(self.packets),before)
            await self.page.evaluate('allowRestoreApply()')
            await self.page.wait_for_function('window.restoreApplied===true')
            self.assertTrue(await self.page.evaluate('fixture.sender.track===fixture.track && !fixture.track.enabled'))
            self.assertTrue(await self.policy.check())
            before=len(self.packets); await asyncio.sleep(.2)
            self.assertGreater(len(self.packets),before)
            self.assertEqual(self.rejected,[])
            self.assertFalse(self.policy.faulted)
        finally:
            await self.page.evaluate('()=>{window.allowRestoreApply?.();window.allowRestoreReply?.();}')
            await stopping
        self.assertTrue(await self.policy.check())
        # Remove gates before the next speech and verify capability reuse.
        await self.page.evaluate('delete fixture.sender.replaceTrack')
        await self.enable(); await self.source.tone()
        self.assertTrue(await self.policy.check())
        self.assertFalse(self.policy.faulted)

    async def test_restore_transition_cannot_hide_foreign_enabled_sender(self):
        await self.enable(); await self.install_restore_gate()
        stopping=asyncio.create_task(self.sink.stop())
        try:
            await self.page.wait_for_function('window.restoreEntered===true')
            await self.page.evaluate('fixture.video.enabled=true')
            self.assertFalse(await self.policy.check())
            self.assertTrue(self.policy.faulted)
            self.assertTrue(await self.page.evaluate('!fixture.video.enabled && !__bTX.track.enabled'))
        finally:
            await self.page.evaluate('allowRestoreApply()')
            await self.page.wait_for_function('window.restoreApplied===true')
            await self.page.evaluate('allowRestoreReply()')
            await stopping

    async def test_restore_transition_original_sender_external_enable_fails_closed(self):
        await self.enable(); await self.install_restore_gate()
        stopping=asyncio.create_task(self.sink.stop())
        try:
            await self.page.wait_for_function('window.restoreEntered===true')
            await self.page.evaluate('allowRestoreApply()')
            await self.page.wait_for_function('window.restoreApplied===true')
            await self.page.evaluate('fixture.track.enabled=true')
            self.assertFalse(await self.policy.check())
            self.assertTrue(self.policy.faulted)
            self.assertFalse(await self.page.evaluate('fixture.sender.track.enabled'))
        finally:
            await self.page.evaluate('allowRestoreReply()')
            await stopping

    async def test_guard_replacement_receipt_uses_event_time_not_later_sender_state(self):
        await self.enable()
        await self.source.tone()
        await self.page.evaluate("""async()=>{
          window.replacement=fixture.track.clone();replacement.enabled=true;
          const replacing=fixture.sender.replaceTrack(replacement);
          // The capture hook synchronously mutes every replacement. Re-enable
          // after that hook, before yielding to the guard, to exercise a later
          // policy mute without weakening the event-time true assertion.
          replacement.enabled=true;
          if(!replacement.enabled) throw new Error('fixture replacement not enabled');
          await replacing;
        }""")
        await self.page.wait_for_function('__bTX.revokeReason==="guard_sender_track_changed"')
        # The policy will mute foreign tracks. The diagnostic must retain the
        # value observed by the guard before that muting, not inspect it later.
        self.assertFalse(await self.policy.check())
        receipt=self.policy.failure_snapshot()
        self.assertEqual(receipt['reason'],'owned_identity')
        self.assertEqual(receipt['detail'],'revoked_guard_sender_track_changed')
        self.assertEqual(receipt['replacement_track'],'other_audio')
        self.assertIs(receipt['replacement_enabled'],True)
        self.assertEqual(receipt['replacement_ready_state'],'live')
        self.assertFalse(await self.page.evaluate('replacement.enabled'))
        await self.sink.stop()
        self.assertTrue(await self.page.evaluate('fixture.sender.track===replacement'))
        self.assertEqual(self.policy.failure_snapshot(),receipt)
