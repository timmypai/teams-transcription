"""Process-local instrumentation of the pinned c4090ed no-PLC receiver for synthetic QA."""
import json

WORKLET_JS = r"""
class QAPassiveRender extends AudioWorkletProcessor {
 constructor(){super();this.chunks=[];this.bytes=0;this.total=0;this.used=0;
  this.samples=null;this.channels=0;this.origin=null;this.credit=true;this.finish=false;
  this.sentFinished=false;this.cap=false;this.frames=0;this.zero=0;this.missing=0;this.cost=0;
  this.port.onmessage=e=>{if(e.data==='finish'){this.finish=true;this.flush();}
   else this.credit=true;this.send();};}
 flush(){if(this.used){const samples=this.samples.slice(0,this.used*this.channels);
  this.chunks.push({samples,frames:this.used,channels:this.channels,media_ms:this.at*1000/sampleRate});
  this.samples=null;this.used=0;}}
 send(){if(!this.credit||this.sentFinished)return;if(this.finish)this.flush();
  const pcm=[];let size=0;while(this.chunks.length&&size+this.chunks[0].samples.byteLength<=65536){
   const r=this.chunks.shift();pcm.push(r);size+=r.samples.byteLength;this.bytes-=r.samples.byteLength;}
  if(!pcm.length&&!this.finish)return;
  this.credit=false;this.sentFinished=this.finish&&!this.chunks.length;this.port.postMessage({pcm,frames:this.frames,zero:this.zero,missing:this.missing,
   cost:this.cost,cap:this.cap,finished:this.finish&&!this.chunks.length,
   clock_resolution_ms:typeof performance!=='undefined'?0.001:1},pcm.map(r=>r.samples.buffer));}
 process(inputs){const begin=typeof performance!=='undefined'?performance.now():Date.now();
  const ch=inputs[0],n=ch?.[0]?.length||128;
  if(this.origin===null)this.origin=currentFrame;
  if(!this.finish && currentFrame-this.origin<sampleRate*120){
   this.frames+=n;
   if(!ch?.length){this.missing+=n;}
   else if(ch.length<=2){
    if(this.channels!==ch.length){this.flush();this.channels=ch.length;}
    if(this.total+n*ch.length*4>33554432){this.cap=true;}
    else {for(let i=0;i<n;i++){
     if(!this.samples){this.samples=new Float32Array(2048*ch.length);this.at=currentFrame+i;}
     let zero=true;for(let c=0;c<ch.length;c++){const v=ch[c][i];this.samples[this.used*ch.length+c]=v;zero&&=v===0;}
     if(zero)this.zero++;this.used++;this.total+=ch.length*4;this.bytes+=ch.length*4;
     if(this.used===2048)this.flush();}
    }
   }else this.cap=true;
  } else if(!this.finish){this.cap=true;this.finish=true;}
  const end=typeof performance!=='undefined'?performance.now():Date.now();
  this.cost+=Math.max(0,end-begin);this.send();return true;
 }
}
registerProcessor('qa-passive-render',QAPassiveRender);
"""

COLLECTOR_JS = r"""
function createQAAudioObservation(){
 const origin=performance.now(),chunks=[],events=[],bytes={source:0,render:0},capped={source:false,render:false};
 const status={audio_cap:false,event_cap:false,window_cap:false,observer_error:false,render_tail_unflushed:false};
 let stopped=false,sourceStopped=false,attachMs=0,ctx,source,meter,mute,clone,url,plane=null,planeRow=null,packet=0,copyMs=0,renderDone=false;
 const endian=new Uint8Array(new Uint32Array([0x01020304]).buffer)[0]===4;
 const valid=()=>{if(performance.now()-origin>120000){status.window_cap=true;return false;}return !stopped&&endian;};
 function event(row){if(events.length>=2048){status.event_cap=true;return;}events.push(row);}
 function pcm(kind,samples,row){
  if(!valid())return;if(bytes[kind]+samples.byteLength>33554432||chunks.length>=4096){status.audio_cap=true;capped[kind]=true;return;}
  bytes[kind]+=samples.byteLength;chunks.push({kind,samples,...row,observed_ms:performance.now()});}
 const handlers={
  packet(frame,read){const m=frame.getMetadata?.()||{};return {kind:'packet',packet_number:++packet,
   read_started_ms:read,dequeue_ms:performance.now(),receive_ms:Number.isFinite(m.receiveTime)?m.receiveTime:null,
   rtp_timestamp:frame.timestamp,encoded_bytes:frame.data.byteLength,observer_ms:0};},
  decodeStart(row,expected){if(row){row.expected_frames=expected;row.decode_started_ms=performance.now();}},
  decodeDone(row){if(row)row.decode_done_ms=performance.now();},
  readyStart(row){if(row)row.write_ready_started_ms=performance.now();},
  readyDone(row){if(row)row.write_ready_done_ms=performance.now();},
  writeDone(row){if(row)row.write_done_ms=performance.now();},
  packetDone(row){if(row)event(row);},
  source(samples,frames,channel,channels,row,audioMs){
   if(!valid()||sourceStopped||capped.source)return;
   if(channel===0){plane=new Float32Array(frames*channels);planeRow={channels,frames,media_ms:audioMs,packet_number:row?.packet_number};}
   if(!plane||plane.length!==frames*channels)return;
   for(let i=0;i<frames;i++)plane[i*channels+channel]=samples[i];
   if(channel===channels-1){pcm('source',plane,planeRow);plane=null;planeRow=null;}
  }
 };
 const observation={
  call(name,...args){const begin=performance.now();try{return handlers[name]?.(...args);}
   catch(_){status.observer_error=true;return null;}finally{const cost=performance.now()-begin;copyMs+=cost;
    const row=args.find(x=>x&&x.kind==='packet');if(row)row.observer_ms+=cost;}},
  async attach(state){const begin=performance.now();try{
   ctx=state.ctx;clone=state.track.clone();clone.enabled=true;
   url=URL.createObjectURL(new Blob([__WORKLET__],{type:'text/javascript'}));
   await ctx.audioWorklet.addModule(url);URL.revokeObjectURL(url);url=null;
   source=ctx.createMediaStreamSource(new MediaStream([clone]));
   meter=new AudioWorkletNode(ctx,'qa-passive-render');mute=ctx.createGain();mute.gain.value=0;
   source.connect(meter);meter.connect(mute);mute.connect(ctx.destination);
   let previous={frames:0,zero:0,missing:0,cost:0};
   meter.port.onmessage=e=>{const d=e.data,received=performance.now();
    try{for(const row of d.pcm||[])pcm('render',row.samples,{frames:row.frames,channels:row.channels,media_ms:row.media_ms});
     event({kind:'render_bin',audio_ms:ctx.currentTime*1000,frames:d.frames-previous.frames,zero_frames:d.zero-previous.zero,
      missing_frames:d.missing-previous.missing,observer_ms:d.cost-previous.cost,received_ms:received});
     event({kind:'observer',browser_ms:received,copy_ms:copyMs,render_total_frames:d.frames,
      overhead_clock_resolution_ms:d.clock_resolution_ms});
     previous={frames:d.frames,zero:d.zero,missing:d.missing,cost:d.cost};
     if(d.cap)status.audio_cap=true;
     if(d.finished){renderDone=true;status.render_tail_unflushed=false;}
    }catch(_){status.observer_error=true;}finally{meter?.port.postMessage('ack');}};
  }catch(_){status.observer_error=true;}finally{attachMs=performance.now()-begin;}},
  async finish(){sourceStopped=true;meter?.port.postMessage('finish');
   const until=performance.now()+100;while(!renderDone&&performance.now()<until)await new Promise(r=>setTimeout(r,5));
   status.render_tail_unflushed=!renderDone;},
  take(){const begin=performance.now(),pcmRows=[];let size=0;
   while(chunks.length&&size+chunks[0].samples.byteLength<=524288&&pcmRows.length<256){
    const r=chunks.shift(),raw=new Uint8Array(r.samples.buffer,r.samples.byteOffset,r.samples.byteLength);
    let binary='';for(const b of raw)binary+=String.fromCharCode(b);
    const {samples,...row}=r;pcmRows.push({...row,data:btoa(binary)});size+=raw.byteLength;}
   const rows=events.splice(0,400);rows.push({kind:'observer',browser_ms:performance.now(),
    copy_ms:copyMs,export_ms:performance.now()-begin,attach_ms:attachMs});
   return {elapsed_ms:performance.now()-origin,pcm:pcmRows,events:rows,status:{...status},
    pending_chunks:chunks.length,pending_events:events.length,browser_ms:performance.now(),copy_ms:copyMs};},
  close(){stopped=true;status.render_tail_unflushed=!renderDone;try{clone?.stop();source?.disconnect();meter?.disconnect();mute?.disconnect();meter?.port.close();if(url)URL.revokeObjectURL(url);}catch(_){status.observer_error=true;}},
 };
 if(!endian)status.observer_error=true;
 window.__qaAudioObservation=observation;return observation;
}
""".replace('__WORKLET__',json.dumps(WORKLET_JS))


def instrument_receiver(source):
    """Fail on pinned-source drift. Original reads/decoder/writer/control remain intact."""
    changes=(
        ('async function createNoPlcReceiver(receiver,onFailure) {',
         'async function createNoPlcReceiver(receiver,onFailure) {\n  const qa=createQAAudioObservation();let qaPacket=null,qaRead=0;'),
        ('    closed=true;initialized=false;clearInterval(timer);',
         '    qa.close();\n    closed=true;initialized=false;clearInterval(timer);'),
        ('        const {done,value:frame}=await reader.read();',
         "        qaRead=performance.now();\n        const {done,value:frame}=await reader.read();\n        if(!closed&&!done&&frame)qaPacket=qa.call('packet',frame,qaRead);"),
        ('        if(timestamp===null||frame.data.byteLength===0)continue;',
         "        if(timestamp===null||frame.data.byteLength===0){qa.call('packetDone',qaPacket);continue;}"),
        ('        packetFrames=0;expectedFrames=opusFrames(frame.data);',
         "        packetFrames=0;expectedFrames=opusFrames(frame.data);\n        qa.call('decodeStart',qaPacket,expectedFrames);"),
        ('        packetResolve=null;packetReject=null;requireOpen();',
         "        qa.call('decodeDone',qaPacket);\n        packetResolve=null;packetReject=null;requireOpen();"),
        ("              audio.copyTo(scratch,{planeIndex:channel,format:'f32-planar'});",
         "              audio.copyTo(scratch,{planeIndex:channel,format:'f32-planar'});\n              qa.call('source',scratch,audio.numberOfFrames,channel,audio.numberOfChannels,qaPacket,ctx.currentTime*1000);"),
        ('            await writer.ready;requireOpen();',
         "            qa.call('readyStart',qaPacket);\n            await writer.ready;requireOpen();\n            qa.call('readyDone',qaPacket);"),
        ('            await writer.write(audio);requireOpen();',
         "            await writer.write(audio);requireOpen();\n            qa.call('writeDone',qaPacket);"),
        ('        pending.length=0;input.packets++;',
         "        pending.length=0;input.packets++;\n        qa.call('packetDone',qaPacket);"),
        ('    return state;', '    await qa.attach(state);\n    return state;'),
    )
    for before,after in changes:
        if source.count(before)!=1:raise ValueError('pinned_receiver_contract_changed')
        source=source.replace(before,after)
    return COLLECTOR_JS+'\n'+source
