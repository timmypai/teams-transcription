"""Local negotiated null sender with a real isolated native Live receiver."""
from types import SimpleNamespace
import unittest

from teams_transcription.browser import Browser, MeetingBrowser
from teams_transcription.b_browser_tx import BrowserTXSink
from teams_transcription.b_dual_policy import DUAL_CHECK_JS, OwnedSenderDualPolicy
from native_output_fixture import NativeSource,fixture_page


NEGOTIATE = r"""async ({directions,answer=true}) => {
  window.localPeers=[];window.remotePeers=[];
  for(const direction of directions) {
    const pc=new RTCPeerConnection({iceServers:[]});
    const remote=new RTCPeerConnection({iceServers:[]});
    pc.addTransceiver('audio',{direction});
    localPeers.push(pc);remotePeers.push(remote);
    await pc.setLocalDescription(await pc.createOffer());
    if(answer) {
      await remote.setRemoteDescription(pc.localDescription);
      // Configure the transceiver associated with this offer, not an unused one.
      const offeredMid=pc.getTransceivers()[0].mid;
      const offered=remote.getTransceivers().filter(t=>t.mid===offeredMid && t.receiver.track.kind==='audio');
      if(offered.length!==1) throw new Error('Local fixture requires one offered audio transceiver');
      offered[0].direction='sendrecv';
      await remote.setLocalDescription(await remote.createAnswer());
      await pc.setRemoteDescription(remote.localDescription);
    }
  }
  window.__meeting={peers:localPeers,createNativeOutputPeer:()=>new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true})};
  window.originalReceiver=localPeers[0].getTransceivers()[0].receiver.track;
  return localPeers.map(pc=>({current:pc.getTransceivers()[0].currentDirection,
    nullTrack:pc.getSenders()[0].track===null}));
}"""


class NullTrackLocalBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_actual_negotiated_sending_and_rejections_with_owned_cleanup(self):
        async with Browser() as runtime:
            for direction in ['sendonly','sendrecv','recvonly','inactive',None,'multiple']:
                with self.subTest(direction=direction):
                    context,page=await fixture_page(runtime)
                    await context.route('**/*',lambda route:route.abort())
                    sink=None
                    source=None
                    try:
                        directions=['sendonly','sendrecv'] if direction=='multiple' else [direction or 'sendonly']
                        state=await page.evaluate(NEGOTIATE,{'directions':directions,'answer':direction is not None})
                        self.assertTrue(all(row['nullTrack'] for row in state))
                        meeting=MeetingBrowser(None,None,None);meeting.page=SimpleNamespace(frames=[page])
                        if direction in ['sendonly','sendrecv']:
                            self.assertEqual(state[0]['current'],direction)
                            frame,peer,sender=await meeting.response_sender('local-one')
                            policy=OwnedSenderDualPolicy(frame,'local-one')
                            sink=BrowserTXSink(frame,meeting_id='local-one',peer_index=peer,sender_index=sender,
                                selection_id=meeting._response_selection_id,dual_channel_policy=policy.authorize)
                            await sink.bind()
                            self.assertFalse(await page.evaluate('()=>!!window.__bTX'))
                            source=await NativeSource.create(runtime)
                            await sink.attach_live(source.page)
                            await sink.start()
                            self.assertTrue(await page.evaluate('()=>__bTX.binding.original===null && !__bTX.track.enabled'))
                            self.assertTrue(await policy.check())
                            await sink.set_muted(False)
                            self.assertTrue(await policy.check())
                            await page.evaluate('()=>window.savedOwned=__bTX.track')
                            await sink.pause()
                            await sink.start()
                            await sink.set_muted(False)
                            self.assertTrue(await page.evaluate('()=>__bTX.track===savedOwned && __bTX.sender.track===savedOwned'))
                            await sink.stop()
                            self.assertTrue(await page.evaluate('()=>localPeers[0].getSenders()[0].track===null && savedOwned.readyState==="ended" && originalReceiver===localPeers[0].getTransceivers()[0].receiver.track && originalReceiver.readyState==="live"'))
                            self.assertTrue(await policy.check())
                        else:
                            with self.assertRaisesRegex(RuntimeError,'unique_muted_sender_required'):
                                await meeting.response_sender('local-one')
                    finally:
                        if sink:await sink.stop()
                        if source:await source.close()
                        await page.evaluate('()=>{for(const pc of [...localPeers,...remotePeers])pc.close()}')
                        await context.close()

    async def test_null_sender_direction_loss_and_foreign_replacement_fail_closed(self):
        async with Browser() as runtime:
            for mutation in ('stopped','foreign'):
                with self.subTest(mutation=mutation):
                    context,page=await fixture_page(runtime)
                    source=await NativeSource.create(runtime)
                    sink=None
                    try:
                        await page.evaluate(NEGOTIATE,{'directions':['sendrecv']})
                        meeting=MeetingBrowser(None,None,None)
                        meeting.page=SimpleNamespace(frames=[page])
                        frame,peer,sender=await meeting.response_sender('local-one')
                        policy=OwnedSenderDualPolicy(frame,'local-one')
                        sink=BrowserTXSink(frame,meeting_id='local-one',peer_index=peer,sender_index=sender,
                            selection_id=meeting._response_selection_id,dual_channel_policy=policy.authorize)
                        await sink.bind()
                        await sink.attach_live(source.page)
                        await sink.start()
                        await sink.set_muted(False)
                        await page.evaluate('window.savedOwned=__bTX.track')
                        # Run the real policy in the same browser turn as the
                        # mutation, before the periodic ownership guard can hide
                        # a synchronous policy failure.
                        accepted=await page.evaluate(r"""async ({mutation,policyId})=>{
                          const transceiver=localPeers[0].getTransceivers()[0];
                          if(mutation==='stopped') {
                            window.stoppedTransceiver=transceiver;transceiver.stop();
                            window.stoppedSenderTrack=transceiver.sender.track;
                          } else {
                            window.foreign=originalReceiver.clone();foreign.enabled=false;
                            await transceiver.sender.replaceTrack(foreign);
                          }
                          return (""" + DUAL_CHECK_JS + r""")({
                            policyId,meetingId:'local-one',authorize:false,initialized:true});
                        }""", {'mutation':mutation,'policyId':policy.policy_id})
                        self.assertFalse(accepted)
                        self.assertFalse(await policy.check())
                        self.assertFalse(await page.evaluate('savedOwned.enabled'))
                        await sink.stop()
                        self.assertEqual(await page.evaluate('savedOwned.readyState'),'ended')
                        if mutation=='foreign':
                            self.assertTrue(await page.evaluate(
                                'localPeers[0].getSenders()[0].track===foreign && foreign.readyState==="live"'))
                        else:
                            self.assertTrue(await page.evaluate(
                                'localPeers[0].getTransceivers()[0]===stoppedTransceiver && '
                                'stoppedTransceiver.stopped && stoppedTransceiver.sender.track===stoppedSenderTrack'))
                    finally:
                        if sink:await sink.stop()
                        await source.close()
                        await context.close()


if __name__=='__main__':unittest.main()
