"""B-only encoded Opus receiver; native rendering supplies zero underflow, not PLC."""

NO_PLC_RECEIVER_JS = r"""
function noPlcSetupReceipt(value) {
  const stages=['receiver_api','browser_api','encoded_streams','audio_context',
    'context_resume','track_generator','source_graph','monitor_graph','worklet_url',
    'worklet_module','render_meter','decoder_create','graph_guard'];
  const types=['Error','TypeError','NotSupportedError','SecurityError','InvalidStateError',
    'OperationError','NetworkError','AbortError','TimeoutError','unknown'];
  const reasons=['receiver_unsupported','api_unsupported','audio_context_invalid',
    'graph_mutated','source_ended','unknown'];
  let stage,kind,reason;
  try{stage=value?.stage;kind=value?.exception_type;reason=value?.reason;}catch{}
  return Object.freeze({stage:stages.includes(stage)?stage:'unknown',
    exception_type:types.includes(kind)?kind:'unknown',
    reason:reasons.includes(reason)?reason:'unknown'});
}

function noPlcSetupFailure(error,stage) {
  const reasons={b_no_plc_receiver_unsupported:'receiver_unsupported',
    b_no_plc_api_unsupported:'api_unsupported',b_no_plc_audio_context:'audio_context_invalid',
    b_no_plc_graph_mutated:'graph_mutated',b_no_plc_source_ended:'source_ended'};
  let name,message;
  try{name=error?.name;}catch{}
  try{message=error?.message;}catch{}
  return noPlcSetupReceipt({stage,exception_type:name,
    reason:typeof message==='string'&&Object.hasOwn(reasons,message)?reasons[message]:'unknown'});
}

function noPlcSetupEnvironment(receiver,ctx) {
  const supported=name=>{try{return typeof globalThis[name]==='function';}catch{return null;}};
  let secure=null,isolated=null,meta=null,encoded=null,worklet=null,state='unknown',rate=null;
  try{if(typeof isSecureContext==='boolean')secure=isSecureContext;}catch{}
  try{if(typeof crossOriginIsolated==='boolean')isolated=crossOriginIsolated;}catch{}
  try{if(typeof document?.querySelector==='function')meta=!!document.querySelector('meta[http-equiv="Content-Security-Policy" i]');}catch{}
  try{encoded=typeof receiver?.createEncodedStreams==='function';}catch{}
  try{if(ctx)worklet=!!ctx.audioWorklet;}catch{}
  try{if(['running','suspended','interrupted','closed'].includes(ctx?.state))state=ctx.state;}catch{}
  try{if(Number.isInteger(ctx?.sampleRate)&&ctx.sampleRate>=8000&&ctx.sampleRate<=384000)rate=ctx.sampleRate;}catch{}
  return Object.freeze({secure_context:secure,cross_origin_isolated:isolated,meta_csp_present:meta,receiver_encoded_streams:encoded,
    audio_decoder:supported('AudioDecoder'),encoded_audio_chunk:supported('EncodedAudioChunk'),
    track_generator:supported('MediaStreamTrackGenerator'),audio_worklet_node:supported('AudioWorkletNode'),
    audio_context:supported('AudioContext'),context_audio_worklet:worklet,context_state:state,sample_rate:rate});
}

function createNoPlcRtpClock() {
  let previous=null,ticks=0;
  return timestamp=>{
    if(!Number.isInteger(timestamp)||timestamp<0||timestamp>0xffffffff)
      throw new Error('b_no_plc_rtp_timestamp');
    if(previous===null){previous=timestamp;return 0;}
    const delta=(timestamp-previous)>>>0;
    // RTP serial-number arithmetic: the older half of the ring is not new
    // media. Do not advance state for duplicates or reordered late packets.
    if(delta===0||delta>=0x80000000)return null;
    previous=timestamp;ticks+=delta;
    const micros=Math.round(ticks*1000000/48000);
    if(!Number.isSafeInteger(micros))throw new Error('b_no_plc_rtp_timestamp');
    return micros;
  };
}

async function createNoPlcReceiver(receiver,onFailure) {
  const inputTrack=receiver?.track;
  const input={frames:0,energy:0,duration:0,packets:0};
  let render={frames:0,energy:0,duration:0};
  let closed=false,failed=false,initialized=false,reader,nativeWriter,decoder,writer;
  let ctx,generator,source,destination,track,monitorTrack,monitorSource,meter,mute;
  let timer,url,pump,meterPending=false,packetFrames=0,expectedFrames=0,packetResolve,packetReject;
  let setupStep='receiver_api';
  const pending=[],disconnectors=[],clock=createNoPlcRtpClock();
  // RFC 6716 permits at most 120ms in one Opus packet. This bounds one codec
  // operation, not the length of an answer or an application media queue.
  const maxPacketFrames=5760,scratch=new Float32Array(maxPacketFrames);
  function opusFrames(data) {
    const bytes=new Uint8Array(data),config=bytes[0]>>3,code=bytes[0]&3;
    // RFC 6716 section 3: TOC configuration gives one frame's duration;
    // packet code 3 carries its frame count in the following byte.
    const frames=config>=16?120<<(config&3):
      config>=12?480<<(config&1):[480,960,1920,2880][config&3];
    if(code===3&&bytes.length<2)throw new Error('b_no_plc_opus_packet');
    const count=code===0?1:code===3?bytes[1]&63:2,total=frames*count;
    if(!count||total>maxPacketFrames)throw new Error('b_no_plc_opus_packet');
    return total;
  }
  const state={receiver,inputTrack,track:null,ctx:null,generator:null,source:null,
    destination:null,closing:null,valid,close,
    stats(){if(!closed)valid();requestMeter();return {input:{...input},render:{...render}};}};

  function fail(reason) {
    if(closed)return;
    failed=true;close();
    try{onFailure?.(new Error(reason));}catch{}
  }
  function close() {
    if(closed)return;
    closed=true;initialized=false;clearInterval(timer);
    inputTrack?.removeEventListener('ended',ended);
    if(inputTrack){inputTrack.enabled=false;inputTrack.stop();}
    if(ctx)ctx.onstatechange=null;
    if(track){track.enabled=false;track.stop();}
    if(generator){generator.enabled=false;generator.stop();}
    monitorTrack?.stop();
    for(const disconnect of disconnectors){try{disconnect();}catch{}}
    if(meter){meter.onprocessorerror=null;meter.port.onmessage=null;meter.port.close();}
    if(decoder&&decoder.state!=='closed')decoder.close();
    packetReject?.(new Error('b_no_plc_closed'));packetReject=null;packetResolve=null;
    for(const audio of pending)audio.close();pending.length=0;scratch.fill(0);
    if(url){URL.revokeObjectURL(url);url=null;}
    const cleanup=[];
    const attempt=fn=>{try{cleanup.push(Promise.resolve(fn()).catch(()=>{}));}catch{}};
    if(reader)attempt(()=>reader.cancel());
    if(nativeWriter)attempt(()=>nativeWriter.abort());
    if(writer)attempt(()=>writer.abort());
    if(ctx&&ctx.state!=='closed')attempt(()=>ctx.close());
    if(pump)cleanup.push(pump);
    state.closing=Promise.allSettled(cleanup).then(()=>{
      try{reader?.releaseLock();}catch{}
      try{nativeWriter?.releaseLock();}catch{}
      try{writer?.releaseLock();}catch{}
    });
  }
  function ended(){fail('b_no_plc_source_ended');}
  function requireOpen() {
    if(closed||inputTrack?.readyState!=='live')throw new Error('b_no_plc_source_ended');
  }
  const sameTrack=(stream,expected)=>{
    const tracks=stream.getAudioTracks();return tracks.length===1&&tracks[0]===expected;
  };
  const protectedNodes=[];
  function own(node){disconnectors.push(node.disconnect.bind(node));return node;}
  function protect(node) {
    // WebAudio has no graph-inspection API. Seal the owned node's mutation
    // entrypoints and verify their identity; internal cleanup uses saved native
    // disconnect methods. These nodes are deliberately not caller-rewirable.
    const forbidden=()=>{fail('b_no_plc_graph_mutated');throw new Error('b_no_plc_graph_mutated');};
    node.connect=forbidden;node.disconnect=forbidden;
    protectedNodes.push({node,forbidden,
      channelCount:node.channelCount,channelCountMode:node.channelCountMode,
      channelInterpretation:node.channelInterpretation});
  }
  function valid() {
    if(closed||!initialized)return false;
    const intact=state.receiver===receiver&&receiver.track===inputTrack&&state.inputTrack===inputTrack&&
      inputTrack.readyState==='live'&&inputTrack.enabled&&state.track===track&&
      track.readyState==='live'&&state.ctx===ctx&&ctx.state==='running'&&
      state.generator===generator&&generator.readyState==='live'&&generator.enabled&&
      state.source===source&&state.destination===destination&&
      sameTrack(source.mediaStream,generator)&&sameTrack(destination.stream,track)&&
      monitorTrack.readyState==='live'&&monitorTrack.enabled&&sameTrack(monitorSource.mediaStream,monitorTrack)&&
      protectedNodes.every(({node,forbidden,channelCount,channelCountMode,channelInterpretation})=>
        node.context===ctx&&node.connect===forbidden&&node.disconnect===forbidden&&
        node.channelCount===channelCount&&node.channelCountMode===channelCountMode&&
        node.channelInterpretation===channelInterpretation);
    if(!intact)fail('b_no_plc_graph_mutated');
    return intact;
  }
  function requestMeter() {
    if(closed||!meter||meterPending)return;
    meterPending=true;meter.port.postMessage('sample');
  }
  async function actualCodec(frame) {
    const metadata=frame.getMetadata();
    let payloadType=metadata.payloadType;
    if(!Number.isInteger(payloadType)) {
      // Older encoded-frame APIs omit payloadType. Receiver stats report the
      // codec actually used, unlike getCapabilities/the offered codec list.
      const report=await receiver.getStats();requireOpen();
      const rows=[...report.values()].filter(row=>row.type==='inbound-rtp'&&
        (row.kind==='audio'||row.mediaType==='audio'));
      if(rows.length!==1)throw new Error('b_no_plc_codec_unknown');
      payloadType=report.get(rows[0].codecId)?.payloadType;
    }
    const codecs=receiver.getParameters().codecs.filter(codec=>codec.payloadType===payloadType);
    if(codecs.length!==1)throw new Error('b_no_plc_codec_unknown');
    const codec=codecs[0];
    if(codec.mimeType?.toLowerCase()!=='audio/opus'||codec.clockRate!==48000||
      ![1,2].includes(codec.channels))throw new Error('b_no_plc_codec_unsupported');
    return codec;
  }
  try {
    if(!inputTrack||inputTrack.kind!=='audio'||inputTrack.readyState!=='live'||
      typeof receiver.createEncodedStreams!=='function')throw new Error('b_no_plc_receiver_unsupported');
    setupStep='browser_api';
    if(typeof AudioDecoder!=='function'||typeof EncodedAudioChunk!=='function'||
      typeof MediaStreamTrackGenerator!=='function'||typeof AudioWorkletNode!=='function'||
      typeof AudioContext!=='function')throw new Error('b_no_plc_api_unsupported');
    // Install before the first await: no packet can enter the native concealed
    // receiver path while WebCodecs/worklet setup is pending. Never forward to
    // streams.writable, and never connect inputTrack to any output graph.
    setupStep='encoded_streams';
    const streams=receiver.createEncodedStreams();
    reader=streams.readable.getReader();nativeWriter=streams.writable.getWriter();
    inputTrack.addEventListener('ended',ended);
    setupStep='audio_context';
    ctx=new AudioContext({sampleRate:48000});state.ctx=ctx;
    if(!ctx.audioWorklet||ctx.sampleRate!==48000)throw new Error('b_no_plc_audio_context');
    setupStep='context_resume';
    await ctx.resume();requireOpen();
    setupStep='track_generator';
    generator=new MediaStreamTrackGenerator({kind:'audio'});state.generator=generator;
    writer=generator.writable.getWriter();
    setupStep='source_graph';
    source=own(ctx.createMediaStreamSource(new MediaStream([generator])));state.source=source;
    destination=own(ctx.createMediaStreamDestination());state.destination=destination;
    source.connect(destination);
    track=destination.stream.getAudioTracks()[0];state.track=track;
    // A generator alone starves downstream RTP and causes downstream PLC.
    // Native WebAudio renders its underflow as zeros and keeps this destination
    // track's clock running. No worklet creates, queues, or schedules audio.
    setupStep='monitor_graph';
    monitorTrack=track.clone();
    monitorSource=own(ctx.createMediaStreamSource(new MediaStream([monitorTrack])));
    setupStep='worklet_url';
    url=URL.createObjectURL(new Blob([`
      class BNoPlcRenderMeter extends AudioWorkletProcessor {
        constructor(){super();this.frames=0;this.energy=0;
          this.port.onmessage=()=>this.port.postMessage({frames:this.frames,
            energy:this.energy,duration:this.frames/sampleRate});}
        process(inputs){
          const channels=inputs[0];
          if(channels?.length){
            const frames=channels[0].length;let energy=0;
            for(const samples of channels)for(let i=0;i<frames;i++)energy+=samples[i]*samples[i];
            this.frames+=frames;this.energy+=energy/channels.length/sampleRate;
          }
          return true;
        }
      }
      registerProcessor('b-no-plc-render-meter',BNoPlcRenderMeter);
    `],{type:'text/javascript'}));
    setupStep='worklet_module';
    await ctx.audioWorklet.addModule(url);requireOpen();
    URL.revokeObjectURL(url);url=null;
    setupStep='render_meter';
    meter=own(new AudioWorkletNode(ctx,'b-no-plc-render-meter'));
    mute=own(ctx.createGain());mute.gain.value=0;
    monitorSource.connect(meter);meter.connect(mute);mute.connect(ctx.destination);
    // Only scalar request/reply observations cross the worklet port, with one
    // outstanding request even when the host thread stops consuming messages.
    meter.port.onmessage=event=>{
      meterPending=false;if(!closed)render=event.data;
    };
    meter.onprocessorerror=()=>fail('b_no_plc_meter_failed');
    for(const node of [source,destination,monitorSource,meter,mute])protect(node);
    ctx.onstatechange=()=>{if(initialized&&ctx.state!=='running')fail('b_no_plc_context_stopped');};
    let configured=null;
    setupStep='decoder_create';
    decoder=new AudioDecoder({
      output:audio=>{
        if(closed){audio.close();return;}
        if(audio.sampleRate!==48000||![1,2].includes(audio.numberOfChannels)||
          audio.numberOfFrames<=0||packetFrames+audio.numberOfFrames>expectedFrames){
          audio.close();fail('b_no_plc_decoded_packet');return;
        }
        packetFrames+=audio.numberOfFrames;pending.push(audio);
        if(packetFrames===expectedFrames)packetResolve?.();
      },
      error:()=>fail('b_no_plc_decode_failed')
    });
    setupStep='graph_guard';
    initialized=true;
    if(!valid())throw new Error('b_no_plc_graph_mutated');
    requestMeter();timer=setInterval(()=>{if(valid())requestMeter();},50);
    pump=(async()=>{
      while(!closed) {
        // Backpressure is structural: do not read the next encoded packet until
        // this packet has decoded and every AudioData write completed.
        // No decode/write task chain or answer-sized application queue exists.
        const {done,value:frame}=await reader.read();
        if(closed)return;
        if(done){fail('b_no_plc_source_ended');return;}
        if(!valid())return;
        const codec=await actualCodec(frame);requireOpen();
        if(!configured) {
          const head=new Uint8Array([79,112,117,115,72,101,97,100,1,codec.channels,
            0,0,128,187,0,0,0,0,0]);
          const config={codec:'opus',sampleRate:48000,numberOfChannels:codec.channels,description:head};
          const support=await AudioDecoder.isConfigSupported(config);requireOpen();
          if(!support.supported)throw new Error('b_no_plc_codec_unsupported');
          decoder.configure(config);configured=codec;
        } else if(codec.payloadType!==configured.payloadType||codec.channels!==configured.channels) {
          throw new Error('b_no_plc_codec_changed');
        }
        const timestamp=clock(frame.timestamp);
        if(timestamp===null||frame.data.byteLength===0)continue;
        packetFrames=0;expectedFrames=opusFrames(frame.data);
        // Opus has packet-local output duration but continuous prediction state.
        // Per-packet flush changes Chromium's decoded audio; rendezvous with the
        // packet's actual output instead, without flushing or resetting.
        await new Promise((resolve,reject)=>{
          packetResolve=resolve;packetReject=reject;
          try{decoder.decode(new EncodedAudioChunk({type:'key',timestamp,data:frame.data}));}
          catch(error){reject(error);}
        });
        packetResolve=null;packetReject=null;requireOpen();
        for(const audio of pending) {
          try {
            let energy=0;
            for(let channel=0;channel<audio.numberOfChannels;channel++) {
              audio.copyTo(scratch,{planeIndex:channel,format:'f32-planar'});
              for(let i=0;i<audio.numberOfFrames;i++)energy+=scratch[i]*scratch[i];
              scratch.fill(0,0,audio.numberOfFrames);
            }
            const frames=audio.numberOfFrames,channels=audio.numberOfChannels;
            await writer.ready;requireOpen();
            await writer.write(audio);requireOpen();
            input.frames+=frames;input.duration+=frames/48000;
            input.energy+=energy/channels/48000;
          } finally {audio.close();}
        }
        pending.length=0;input.packets++;
      }
    })().catch(error=>{if(!closed)fail(error?.message?.startsWith('b_no_plc_')?
      error.message:'b_no_plc_receive_failed');});
    return state;
  } catch(error) {
    const detail=Object.freeze({...noPlcSetupFailure(error,setupStep),
      environment:noPlcSetupEnvironment(receiver,ctx)});
    const known={receiver_unsupported:'b_no_plc_receiver_unsupported',
      api_unsupported:'b_no_plc_api_unsupported',audio_context_invalid:'b_no_plc_audio_context',
      graph_mutated:'b_no_plc_graph_mutated',source_ended:'b_no_plc_source_ended'};
    if(!closed)fail(known[detail.reason]||'b_no_plc_setup_failed');
    await state.closing;
    const failure=new Error(failed?'b_no_plc_setup_failed':'b_no_plc_source_ended');
    failure.bNoPlcSetup=detail;throw failure;
  }
}
"""
