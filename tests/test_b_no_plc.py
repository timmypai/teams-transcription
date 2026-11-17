"""No-provider Chromium checks of the production encoded-receiver entrypoint."""
import asyncio
import unittest

from teams_transcription.b_no_plc_startup import NO_PLC_RECEIVER_JS
from teams_transcription.browser import Browser
from native_output_fixture import fixture_page


_SETUP = r"""async (codecName='audio/opus') => {
  codecName??='audio/opus';
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  const gathered=pc=>new Promise((resolve,reject)=>{
    if(pc.iceGatheringState==='complete') return resolve();
    const changed=()=>{if(pc.iceGatheringState==='complete'){
      clearTimeout(timer);pc.removeEventListener('icegatheringstatechange',changed);resolve();}};
    const timer=setTimeout(()=>reject(Error('fixture_ice_timeout')),5000);
    pc.addEventListener('icegatheringstatechange',changed);
  });
  const connect=async(a,b)=>{
    await a.setLocalDescription(await a.createOffer());await gathered(a);
    await b.setRemoteDescription(a.localDescription);
    await b.setLocalDescription(await b.createAnswer());await gathered(b);
    await a.setRemoteDescription(b.localDescription);
  };
  const ctx=new AudioContext({sampleRate:48000});await ctx.resume();
  const oscillator=ctx.createOscillator(),gain=ctx.createGain(),destination=ctx.createMediaStreamDestination();
  oscillator.frequency.value=440;gain.gain.value=.3;
  oscillator.connect(gain);gain.connect(destination);oscillator.start();
  const producer=new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true});
  const receiver=new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true});
  const transceiver=producer.addTransceiver('audio',{direction:'sendonly'});
  transceiver.setCodecPreferences(RTCRtpSender.getCapabilities('audio').codecs.filter(
    codec=>codec.mimeType.toLowerCase()===codecName.toLowerCase()));
  const state=window.noPlcFixture={ctx,oscillator,gain,destination,producer,receiver,
    sleep,errors:[],drop:false,dropped:0,bins:[],bridge:null};
  const encoded=transceiver.sender.createEncodedStreams();
  state.forwardAbort=new AbortController();
  state.forward=encoded.readable.pipeThrough(new TransformStream({transform(frame,controller){
    if(state.drop){state.dropped++;return;}controller.enqueue(frame);
  }})).pipeTo(encoded.writable,{signal:state.forwardAbort.signal}).catch(()=>{});
  let ready;
  receiver.ontrack=event=>{ready=createNoPlcReceiver(event.receiver,
    error=>state.errors.push(error.message));};
  await connect(producer,receiver);state.bridge=await ready;
  const url=URL.createObjectURL(new Blob([`
    class NoPlcTestMeter extends AudioWorkletProcessor {
      constructor(){super();this.frames=0;this.energy=0;this.crossings=0;this.last=0;}
      process(inputs){
        const samples=inputs[0]?.[0];
        if(samples)for(const value of samples){
          this.energy+=value*value;
          if((value>=0)!==(this.last>=0))this.crossings++;
          this.last=value;
          if(++this.frames===480){this.port.postMessage({rms:Math.sqrt(this.energy/480),
            frequency:this.crossings*50});this.frames=0;this.energy=0;this.crossings=0;}
        }
        return true;
      }
    }
    registerProcessor('no-plc-test-meter',NoPlcTestMeter);
  `],{type:'text/javascript'}));
  try{await ctx.audioWorklet.addModule(url);}finally{URL.revokeObjectURL(url);}
  state.observer=ctx.createMediaStreamSource(new MediaStream([state.bridge.track]));
  state.meter=new AudioWorkletNode(ctx,'no-plc-test-meter');
  state.mute=ctx.createGain();state.mute.gain.value=0;
  state.observer.connect(state.meter);state.meter.connect(state.mute);state.mute.connect(ctx.destination);
  state.meter.port.onmessage=event=>{state.bins.push(event.data);};
  await transceiver.sender.replaceTrack(destination.stream.getAudioTracks()[0]);
  state.track=state.bridge.track;
  return {valid:state.bridge.valid(),track:state.track.id};
}"""

_SUMMARIZE = r"""() => {
  const state=noPlcFixture,bins=state.bins;state.bins=[];
  // Tone identity uses active bins, not native zero-underflow intervals.
  const tones=bins.filter(bin=>bin.rms>.1).map(bin=>bin.frequency).sort((a,b)=>a-b);
  return {count:bins.length,rms:Math.sqrt(bins.reduce((sum,bin)=>sum+bin.rms**2,0)/bins.length),
    maxRms:Math.max(...bins.map(bin=>bin.rms)),
    frequency:tones.length?tones[Math.floor(tones.length/2)]:0,toneBins:tones.length,
    stats:state.bridge.stats(),errors:state.errors,valid:state.bridge.valid(),
    sameTrack:state.track===state.bridge.track,dropped:state.dropped};
}"""

_CLOSE = r"""async () => {
  const state=window.noPlcFixture;if(!state)return;
  state.bridge?.close();await state.bridge?.closing;
  state.producer.close();state.receiver.close();state.oscillator.stop();
  state.forwardAbort.abort();
  for(const track of state.destination.stream.getTracks())track.stop();
  state.observer?.disconnect();state.meter?.disconnect();state.meter?.port.close();state.mute?.disconnect();
  await state.ctx.close();await state.forward;
}"""


def _entrypoint(expression):
    return '(arg)=>{' + NO_PLC_RECEIVER_JS + '\nreturn (' + expression + ')(arg);}'


class NoPlcReceiverBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_starvation_is_zero_and_distinct_tone_resumes_on_same_track(self):
        async with Browser() as runtime:
            context, page = await fixture_page(runtime)
            try:
                await page.evaluate(_entrypoint(_SETUP))
                await page.wait_for_function('noPlcFixture.bridge.stats().input.packets>50')
                await page.evaluate('noPlcFixture.bins=[]')
                await asyncio.sleep(.25)
                before = await page.evaluate(_SUMMARIZE)
                self.assertEqual(before['errors'], [])
                self.assertGreater(before['rms'], .1)
                self.assertGreater(before['toneBins'], 10)
                self.assertAlmostEqual(before['frequency'], 440, delta=25)
                await page.evaluate('noPlcFixture.drop=true')
                # Let only already admitted native media finish, then compare a
                # window entirely inside packet starvation (no synthetic quiet).
                await asyncio.sleep(.3)
                gap_start = await page.evaluate(_SUMMARIZE)
                await asyncio.sleep(.3)
                gap_end = await page.evaluate(_SUMMARIZE)
                self.assertGreater(gap_end['count'], 10)
                self.assertEqual(gap_end['maxRms'], 0)
                self.assertGreater(gap_end['dropped'], 10)
                self.assertEqual(gap_start['stats']['input'], gap_end['stats']['input'])
                self.assertGreater(gap_end['stats']['render']['frames'],
                                   gap_start['stats']['render']['frames'])
                self.assertEqual(gap_end['stats']['render']['energy'],
                                 gap_start['stats']['render']['energy'])
                await page.evaluate('''()=>{const s=noPlcFixture;
                    s.oscillator.frequency.value=880;s.drop=false;}''')
                await asyncio.sleep(.3)
                await page.evaluate('noPlcFixture.bins=[]')
                await asyncio.sleep(.25)
                after = await page.evaluate(_SUMMARIZE)
                self.assertEqual(after['errors'], [])
                self.assertTrue(after['valid'])
                self.assertTrue(after['sameTrack'])
                self.assertGreater(after['rms'], .1)
                self.assertGreater(after['toneBins'], 10)
                self.assertAlmostEqual(after['frequency'], 880, delta=25)
                self.assertGreater(after['stats']['input']['packets'],
                                   gap_end['stats']['input']['packets'])
                # Muting is a caller-owned question gate, not graph invalidity.
                self.assertTrue(await page.evaluate('''()=>{
                    noPlcFixture.bridge.track.enabled=false;return noPlcFixture.bridge.valid();}'''))
                print({'browser': runtime.browser.version, 'before_rms': before['rms'],
                       'gap_max_rms': gap_end['maxRms'], 'after_hz': after['frequency'],
                       'input_gap_frames': gap_end['stats']['input']['frames'] - gap_start['stats']['input']['frames'],
                       'render_gap_frames': gap_end['stats']['render']['frames'] - gap_start['stats']['render']['frames']})
            finally:
                await page.evaluate(_CLOSE)
                await context.close()

    async def test_source_end_close_and_graph_mutation_stop_owned_output(self):
        async with Browser() as runtime:
            for action in ('source_end', 'close', 'disconnect', 'identity'):
                with self.subTest(action=action):
                    context, page = await fixture_page(runtime)
                    try:
                        await page.evaluate(_entrypoint(_SETUP))
                        await page.wait_for_function('noPlcFixture.bridge.stats().input.packets>5')
                        result = await page.evaluate(r"""async action=>{
                          const s=noPlcFixture,b=s.bridge;
                          if(action==='source_end')s.receiver.close();
                          if(action==='close')b.close();
                          if(action==='disconnect'){try{b.source.disconnect();}catch{}}
                          if(action==='identity')b.generator=b.inputTrack;
                          b.valid();await s.sleep(100);await b.closing;
                          const first=b.stats();await s.sleep(100);
                          return {valid:b.valid(),ended:s.track.readyState,enabled:s.track.enabled,
                            ctx:b.ctx.state,first,last:b.stats(),errors:s.errors};
                        }""", action)
                        self.assertFalse(result['valid'])
                        self.assertEqual(result['ended'], 'ended')
                        self.assertFalse(result['enabled'])
                        self.assertEqual(result['ctx'], 'closed')
                        self.assertEqual(result['first'], result['last'])
                        self.assertEqual(len(result['errors']), 0 if action == 'close' else 1)
                    finally:
                        await page.evaluate(_CLOSE)
                        await context.close()

    async def test_actual_negotiated_non_opus_codec_fails_without_output(self):
        async with Browser() as runtime:
            context, page = await fixture_page(runtime)
            try:
                await page.evaluate(_entrypoint(_SETUP), 'audio/PCMU')
                await page.wait_for_function('noPlcFixture.errors.length>0')
                result = await page.evaluate("""async ()=>{
                  const s=noPlcFixture;await s.bridge.closing;
                  return {errors:s.errors,stats:s.bridge.stats(),track:s.track.readyState,
                    valid:s.bridge.valid(),ctx:s.bridge.ctx.state};}""")
                self.assertEqual(result['errors'], ['b_no_plc_codec_unsupported'])
                self.assertEqual(result['stats']['input']['frames'], 0)
                self.assertFalse(result['valid'])
                self.assertEqual(result['track'], 'ended')
                self.assertEqual(result['ctx'], 'closed')
            finally:
                await page.evaluate(_CLOSE)
                await context.close()

    async def test_unsupported_api_and_cancelled_partial_setup_fail_closed(self):
        async with Browser() as runtime:
            for action in ('unsupported', 'cancelled'):
                with self.subTest(action=action):
                    context, page = await fixture_page(runtime)
                    try:
                        result = await page.evaluate(_entrypoint(r"""
                          async action=>{
                            const pc=new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true});
                            const receiver=pc.addTransceiver('audio').receiver,errors=[],contexts=[];
                            const OriginalContext=AudioContext,OriginalDecoder=AudioDecoder;
                            let release,entered;
                            const gate=new Promise(resolve=>release=resolve);
                            const setupEntered=new Promise(resolve=>entered=resolve);
                            if(action==='unsupported')window.AudioDecoder=undefined;
                            else window.AudioContext=class extends OriginalContext {
                              constructor(options){super(options);contexts.push(this);
                                const add=this.audioWorklet.addModule.bind(this.audioWorklet);
                                this.audioWorklet.addModule=async url=>{
                                  entered();await gate;return add(url);
                                };
                              }
                            };
                            let rejected=false;
                            const setup=createNoPlcReceiver(receiver,error=>errors.push(error.message))
                              .catch(()=>{rejected=true;});
                            if(action==='cancelled'){
                              await setupEntered;pc.close();release();
                            }
                            await setup;pc.close();
                            window.AudioContext=OriginalContext;window.AudioDecoder=OriginalDecoder;
                            return {rejected,errors,contexts:contexts.map(ctx=>ctx.state),
                              receiverEnded:receiver.track.readyState==='ended'};
                          }
                        """), action)
                        self.assertTrue(result['rejected'])
                        self.assertTrue(result['receiverEnded'])
                        self.assertEqual(result['errors'], [
                            'b_no_plc_api_unsupported' if action == 'unsupported'
                            else 'b_no_plc_source_ended'])
                        self.assertEqual(result['contexts'], [] if action == 'unsupported' else ['closed'])
                    finally:
                        await context.close()

    async def test_rtp_clock_wrap_drops_duplicates_and_late_packets(self):
        async with Browser() as runtime:
            context, page = await fixture_page(runtime)
            try:
                result = await page.evaluate(_entrypoint(r"""
                  ()=>{const clock=createNoPlcRtpClock();
                    return [0xfffffc40,0xfffffc40,0xfffffc00,0,960,0,1920].map(value=>clock(value));}
                """))
                self.assertEqual(result, [0, None, None, 20000, 40000, None, 60000])
            finally:
                await context.close()


if __name__ == '__main__':
    unittest.main()
