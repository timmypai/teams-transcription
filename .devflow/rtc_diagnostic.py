"""Bounded local-only WebRTC transport diagnostic; scalar enums/counts, no SDP/IP."""
import asyncio
import json
import os
from pathlib import Path
from teams_transcription.browser import Browser

JS = r'''async (withTrack) => {
 const a=new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true});
 const b=new RTCPeerConnection({iceServers:[],encodedInsertableStreams:true});
 const ctx=new AudioContext({sampleRate:48000}); await ctx.resume();
 const dst=ctx.createMediaStreamDestination();
 if(withTrack) a.addTransceiver(dst.stream.getAudioTracks()[0],{direction:'sendonly'});
 else a.addTransceiver('audio',{direction:'sendonly'});
 const stream=a.getSenders()[0].createEncodedStreams();
 const abort=new AbortController();
 stream.readable.pipeTo(stream.writable,{signal:abort.signal}).catch(()=>{});
 const gather=pc=>new Promise((resolve,reject)=>{
   if(pc.iceGatheringState==='complete')return resolve();
   const t=setTimeout(()=>reject(new Error('local_gather_timeout')),5000);
   pc.addEventListener('icegatheringstatechange',()=>{if(pc.iceGatheringState==='complete'){clearTimeout(t);resolve();}});
 });
 try {
  await a.setLocalDescription(await a.createOffer()); await gather(a);
  await b.setRemoteDescription(a.localDescription);
  await b.setLocalDescription(await b.createAnswer()); await gather(b);
  await a.setRemoteDescription(b.localDescription);
  const deadline=performance.now()+4000;
  while(b.connectionState!=='connected' && performance.now()<deadline) await new Promise(r=>setTimeout(r,50));
  const states=[];
  for(const pc of [a,b]) {
   const stats=[...await pc.getStats()].map(x=>x[1]);
   states.push({connection:pc.connectionState,ice:pc.iceConnectionState,gathering:pc.iceGatheringState,
     localCandidates:stats.filter(s=>s.type==='local-candidate').length,
     remoteCandidates:stats.filter(s=>s.type==='remote-candidate').length,
     pairs:stats.filter(s=>s.type==='candidate-pair').map(s=>({state:s.state,nominated:!!s.nominated,
       requestsSent:s.requestsSent||0,responsesReceived:s.responsesReceived||0})),
     transports:stats.filter(s=>s.type==='transport').map(s=>({dtlsState:s.dtlsState,iceState:s.iceState}))});
  }
  return {withTrack,states};
 } finally {abort.abort();a.close();b.close();await ctx.close();}
}'''

async def diagnose(output):
    async with Browser() as runtime:
        context=await runtime.browser.new_context(permissions=[])
        await context.route('https://native-output.invalid/**',lambda route:route.fulfill(
            content_type='text/html',body='<!doctype html><title>Local transport diagnostic</title>'))
        page=await context.new_page()
        await page.goto('https://native-output.invalid/')
        result={'scope':'synthetic-local-peer-transport','network_profile':os.environ.get('TEST_NETWORK_PROFILE','unknown'),'browser':runtime.browser.version,'cases':[]}
        for track in [False,True]:
            try:
                result['cases'].append(await page.evaluate(JS,track))
            except Exception as exc:
                result['cases'].append({'withTrack':track,'failure_type':type(exc).__name__})
        await context.close()
    output.write_text(json.dumps(result,indent=2)+'\n')
    output.chmod(0o644)
