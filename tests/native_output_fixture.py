"""Synthetic browser-native sources and scalar downstream waveform observations."""

from teams_transcription.browser import CAPTURE_JS
from teams_transcription.b_no_plc_startup import NO_PLC_RECEIVER_JS


_GATHER = r"""
  const gathered=pc=>new Promise((resolve,reject)=>{
    if(pc.iceGatheringState==='complete') return resolve();
    const done=()=>{if(pc.iceGatheringState==='complete') {clearTimeout(timer);
      pc.removeEventListener('icegatheringstatechange',done);resolve();}};
    const timer=setTimeout(()=>reject(new Error('fixture ICE timeout')),5000);
    pc.addEventListener('icegatheringstatechange',done);
  });
"""


async def fixture_page(runtime, *, capture=False):
    context = await runtime.browser.new_context(permissions=[])
    await context.route('https://native-output.invalid/**', lambda route:
        route.fulfill(content_type='text/html', body='<!doctype html><title>Local native media</title>'))
    if capture:
        await context.add_init_script(CAPTURE_JS)
    page = await context.new_page()
    await page.goto('https://native-output.invalid/')
    return context, page


async def meeting_fixture(page, meeting_id='one', *, video=False):
    await page.evaluate(r"""async ({meetingId,video})=>{
      window.__bMeetingId=meetingId;
      const ctx=new AudioContext({sampleRate:48000}); await ctx.resume();
      const destination=ctx.createMediaStreamDestination();
      const osc=ctx.createOscillator(); osc.connect(destination); osc.start();
      const track=destination.stream.getAudioTracks()[0],pc=new RTCPeerConnection({iceServers:[]});
      track.enabled=false;
      const sender=pc.addTrack(track,destination.stream);
      window.fixture={ctx,osc,track,pc,sender};
      if(video) {
        const canvas=document.createElement('canvas');
        const videoTrack=canvas.captureStream().getVideoTracks()[0];
        const videoSender=pc.addTrack(videoTrack,new MediaStream([videoTrack]));
        fixture.video=videoTrack;fixture.videoSender=videoSender;
      }
    }""", {'meetingId':meeting_id,'video':video})


class NativeSource:
    """A real incoming __live.pc audio track; samples stay inside its context."""

    @classmethod
    async def create(cls, runtime):
        self = cls()
        self.context, self.page = await fixture_page(runtime)
        await self.page.evaluate(r"""async ()=>{""" + _GATHER + r"""
          const ctx=new AudioContext({sampleRate:48000});await ctx.resume();
          const osc=ctx.createOscillator(),gain=ctx.createGain(),destination=ctx.createMediaStreamDestination();
          gain.gain.value=0;osc.connect(gain);gain.connect(destination);osc.start();
          const producer=new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true}),
            pc=new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true});
          // Do not queue synthetic RTP before the production decoder is attached.
          // A provider may negotiate its receive track before its first answer.
          producer.addTransceiver('audio',{direction:'sendonly'});
          const outgoing=producer.getSenders()[0].createEncodedStreams();
          const abort=window.nativeSenderAbort=new AbortController();
          window.nativeSenderForwarding=outgoing.readable.pipeThrough(new TransformStream({transform(frame,controller){
            if(!window.nativeFixture?.dropPackets) controller.enqueue(frame);
          }})).pipeTo(outgoing.writable,{signal:abort.signal}).catch(()=>{});
          await producer.setLocalDescription(await producer.createOffer());
          await gathered(producer);
          await pc.setRemoteDescription(producer.localDescription);
          await pc.setLocalDescription(await pc.createAnswer());
          await gathered(pc);
          await producer.setRemoteDescription(pc.localDescription);
          window.__live={pc};window.nativeFixture={ctx,osc,gain,producer,pc,destination};
        }""")
        await self.page.wait_for_function("__live.pc.connectionState==='connected'")
        await self.page.evaluate('async()=>{' + NO_PLC_RECEIVER_JS + '''
          __live.noPlc=await createNoPlcReceiver(__live.pc.getReceivers()[0],
            ()=>{__live.decodeFailed=true;});
        }''')
        return self

    async def tone(self, frequency=550, amplitude=.3):
        await self.page.evaluate(r"""async ({frequency,amplitude})=>{
          const {ctx,osc,gain,producer,destination}=nativeFixture;
          const sender=producer.getSenders()[0];
          if(!sender.track) await sender.replaceTrack(destination.stream.getAudioTracks()[0]);
          osc.frequency.cancelScheduledValues(ctx.currentTime);osc.frequency.value=frequency;
          gain.gain.cancelScheduledValues(ctx.currentTime);gain.gain.value=amplitude;
        }""", {'frequency':frequency,'amplitude':amplitude})

    async def silence(self):
        await self.tone(amplitude=0)

    async def pattern(self):
        """Unique first/middle/last tones, scheduled without Python packet pacing."""
        await self.page.evaluate(r"""async ()=>{
          const {ctx,osc,gain,producer,destination}=nativeFixture;
          const sender=producer.getSenders()[0];
          if(!sender.track) await sender.replaceTrack(destination.stream.getAudioTracks()[0]);
          const t=ctx.currentTime+.12;
          osc.frequency.cancelScheduledValues(ctx.currentTime);
          gain.gain.cancelScheduledValues(ctx.currentTime);gain.gain.value=0;
          osc.frequency.setValueAtTime(330,t);gain.gain.setValueAtTime(.3,t);
          osc.frequency.setValueAtTime(550,t+.35);
          osc.frequency.setValueAtTime(880,t+2.35);
          gain.gain.setValueAtTime(0,t+2.7);
        }""")
        return 2.82

    async def close(self):
        if not self.page.is_closed():
            await self.page.evaluate('''async()=>{
              window.nativeSenderAbort?.abort();
              const d=window.__live?.noPlc;d?.close();
              await Promise.all([window.nativeSenderForwarding,d?.closing]);
            }''')
        await self.context.close()


class NativeReceiver:
    """Separate downstream peer, observing RMS/frequency bins, never PCM in Python."""

    @classmethod
    async def create(cls, runtime, meeting_page, peer='fixture.pc'):
        self = cls()
        self.context, self.page = await fixture_page(runtime)
        offer = await meeting_page.evaluate('async ()=>{' + _GATHER + f'''
          const pc={peer};await pc.setLocalDescription(await pc.createOffer());
          await gathered(pc);return pc.localDescription.sdp;
        }}''')
        answer = await self.page.evaluate(r"""async sdp=>{""" + _GATHER + r"""
          const ctx=new AudioContext({sampleRate:48000});await ctx.resume();
          const url=URL.createObjectURL(new Blob([`
            class Meter extends AudioWorkletProcessor {
              constructor(){super();this.n=0;this.energy=0;this.crossings=0;this.last=0;}
              process(inputs){
                const samples=inputs[0]?.[0];
                if(samples) for(const value of samples){
                  this.energy+=value*value;
                  if((value>=0)!==(this.last>=0)) this.crossings++;
                  this.last=value;
                  if(++this.n===480){
                    this.port.postMessage({rms:Math.sqrt(this.energy/480),frequency:this.crossings*50});
                    this.n=0;this.energy=0;this.crossings=0;
                  }
                }
                return true;
              }
            }
            registerProcessor('native-downstream-meter',Meter);
          `],{type:'text/javascript'}));
          try{await ctx.audioWorklet.addModule(url);}finally{URL.revokeObjectURL(url);}
          const pc=new RTCPeerConnection({iceServers:[]});
          const state=window.downstream={pc,ctx,bins:[]};
          pc.ontrack=e=>{
            const player=document.createElement('audio');player.muted=true;player.hidden=true;
            player.srcObject=new MediaStream([e.track]);
            const elementSource=ctx.createMediaElementSource(player);
            const source=ctx.createMediaStreamSource(player.srcObject);
            const meter=new AudioWorkletNode(ctx,'native-downstream-meter');
            const mute=ctx.createGain();mute.gain.value=0;
            source.connect(meter);meter.connect(mute);elementSource.connect(mute);mute.connect(ctx.destination);
            meter.port.onmessage=e=>state.bins.push(e.data);
            Object.assign(state,{source,elementSource,meter,mute,player,track:e.track});
            document.body.appendChild(player);player.muted=false;
            player.play().catch(error=>{state.error=String(error);});
          };
          await pc.setRemoteDescription({type:'offer',sdp});
          await pc.setLocalDescription(await pc.createAnswer());await gathered(pc);
          return pc.localDescription.sdp;
        }""", offer)
        await meeting_page.evaluate(f"sdp=>{peer}.setRemoteDescription({{type:'answer',sdp}})", answer)
        await self.page.wait_for_function("downstream.pc.connectionState==='connected' && downstream.bins.length>3")
        return self

    async def reset(self):
        await self.page.evaluate('downstream.bins=[]')

    async def bins(self):
        return await self.page.evaluate('downstream.bins')

    async def close(self):
        if not self.page.is_closed():
            await self.page.evaluate("""async ()=>{
              const state=window.downstream;
              if(!state) return;
              if(state.player){state.player.muted=true;state.player.pause();state.player.srcObject=null;}
              state.source?.disconnect();state.elementSource?.disconnect();state.meter?.disconnect();state.mute?.disconnect();
              state.pc.close();await state.ctx.close();
            }""")
        await self.context.close()
