"""Native bounded render history. No module fetch, PCM transfer or PLC fallback."""

HISTORY_METER_JS = r"""
function createRenderHistory(ctx,own,protect,requireOpen) {
  const size=32768,rate=48000,margin=512,threshold=8/32768;
  const inlet=ctx.createGain(),split=own(ctx.createChannelSplitter(2));
  inlet.connect(split);
  const analysers=[],buffers=[];
  for(let i=0;i<2;i++){
    const analyser=own(ctx.createAnalyser());analyser.fftSize=size;
    analyser.channelCount=1;analyser.channelCountMode='explicit';
    analyser.smoothingTimeConstant=0;split.connect(analyser,i,0);
    analysers.push(analyser);buffers.push(new Float32Array(size));
  }
  for(const node of [split,...analysers])protect(node);
  let closed=false,last=null,cached=null,quietStart=null,epoch=0,calibrated=false;
  const birth=Math.round(ctx.currentTime*rate),freshAfter=birth+size+margin;
  function snapshot(){
    requireOpen();
    if(closed||ctx.state!=='running'||ctx.sampleRate!==rate||
      analysers.some(a=>a.fftSize!==size||a.channelCount!==1||a.channelCountMode!=='explicit')){
      quietStart=null;last=null;cached=null;epoch++;
      throw Error('b_no_plc_graph_mutated');
    }
    const before=Math.round(ctx.currentTime*rate);
    for(let i=0;i<2;i++)analysers[i].getFloatTimeDomainData(buffers[i]);
    const after=Math.round(ctx.currentTime*rate);
    if(last===after&&cached)return {...cached,fresh:false};
    const peaks=[0,0];let latestActive=null;
    for(let lane=0;lane<2;lane++)for(let at=0;at<size;at+=128){
      let energy=0;
      for(let i=at;i<at+128;i++){
        const value=buffers[lane][i];if(!Number.isFinite(value))throw Error('b_no_plc_graph_mutated');
        energy+=value*value;
      }
      const rms=Math.sqrt(energy/128);peaks[lane]=Math.max(peaks[lane],rms);
      if(lane<2&&rms>=threshold){const atFrame=before-size+at-margin;
        latestActive=latestActive===null?atFrame:Math.max(latestActive,atFrame);}
    }
    const advanced=last!==null&&before>last;
    const overlap=advanced&&before-last<=size/2;
    const bracket=after>=before&&after-before<=256;
    const loud=Math.max(peaks[0],peaks[1])>=threshold;
    const covered=before>=freshAfter&&overlap&&bracket;
    if(!covered||loud){quietStart=null;epoch++;}
    else if(quietStart===null)quietStart=after+margin;
    const through=Math.max(0,before-margin);
    last=after;
    cached={kind:'native-history-v1',clock_frame:after,coverage_epoch:epoch,fresh:true,
      coverage_valid:covered,window_frames:size,peak_rms:Math.max(peaks[0],peaks[1]),
      active:loud,latest_active_block_min_frame:latestActive??-1,quiet_from_frame:quietStart,
      quiet_through_frame:through,
      quiet_seconds:quietStart===null?0:Math.max(0,through-quietStart)/rate,
      graph_checkpoint_observed:calibrated};
    return {...cached};
  }
  inlet.snapshot=snapshot;
  inlet.markCheckpoint=()=>{requireOpen();if(closed)throw Error('b_no_plc_startup_closed');calibrated=true;};
  inlet.port={onmessage:null,postMessage(message){
    if(message!=='sample')return;
    const value=snapshot();queueMicrotask(()=>{if(!closed)inlet.port.onmessage?.({data:value});});
  },close(){
    if(closed)return;closed=true;for(const buffer of buffers)buffer.fill(0);
  }};
  return inlet;
}

function createGeneratorCheckpoint(ctx,source,own,protect,requireOpen) {
  const analyser=own(ctx.createAnalyser()),size=4096,data=new Float32Array(size);
  analyser.fftSize=size;analyser.smoothingTimeConstant=0;
  source.connect(analyser);const detach=source.disconnect.bind(source,analyser);protect(analyser);
  let closed=false,timer=null,rejectPending=null,endUpper=null;
  function scan(){
    requireOpen();if(closed||ctx.state!=='running'||analyser.fftSize!==size)
      throw Error('b_no_plc_startup_closed');
    const before=Math.round(ctx.currentTime*48000);analyser.getFloatTimeDomainData(data);
    const after=Math.round(ctx.currentTime*48000);
    if(data.some(value=>!Number.isFinite(value)))throw Error('b_no_plc_startup_checkpoint');
    if(after<before||after-before>256)return null;
    return {before,after};
  }
  const probeValue=(i,attempt=0)=>(i<320?.125:i<640?-.125:.25)*(attempt+1);
  function wait(marker,attempt=0){const started=Math.round(ctx.currentTime*48000);return new Promise((resolve,reject)=>{
    rejectPending=reject;
    function poll(){try{
      const clock=scan();
      if(clock&&marker){
        for(let at=0;at+960<=size;at++){
          let found=true;for(let i=0;i<960;i++)if(Math.abs(data[at+i]-probeValue(i,attempt))>1e-6){found=false;break;}
          if(found){endUpper=clock.after-size+at+960+512;rejectPending=null;
            resolve({marker_observed:true});return;}
        }
        if(clock.before-started>=3840){rejectPending=null;resolve({marker_observed:false});return;}
      }else if(clock&&endUpper!==null&&clock.before>=endUpper+512){
        let quiet=true;for(let i=size-512;i<size;i++)if(Math.abs(data[i])>1e-6)quiet=false;
        if(quiet){rejectPending=null;resolve({clock_frame:clock.before,generator_marker_observed:true,
          probe_tail_quiet:true});return;}
      }
      timer=setTimeout(poll,5);
    }catch(error){rejectPending=null;reject(error);}}
    poll();
  });}
  function close(){if(closed)return;closed=true;clearTimeout(timer);
    rejectPending?.(Error('b_no_plc_startup_closed'));rejectPending=null;data.fill(0);
    try{detach();}catch{}}
  return {waitMarker:attempt=>wait(true,attempt),waitQuiet:()=>wait(false),probeValue,close};
}

function createHistoryStartup(writer,ctx,probe,requireOpen,openGate) {
  let complete=false,closed=false,timer=null,abortReject=null;
  const receipt={version:'native-history-gated-v2',status:'not_started',
    primer_consumption_verified:false,remote_drain_verified:false};
  function check(){if(closed)throw Error('b_no_plc_startup_closed');requireOpen();}
  function close(){closed=true;clearTimeout(timer);abortReject?.(Error('b_no_plc_startup_closed'));probe.close();}
  async function prepare(audio){
    check();if(complete)return;
    const frames=audio.numberOfFrames,channels=audio.numberOfChannels;
    if(audio.sampleRate!==48000||!Number.isInteger(frames)||frames<1||frames>5760||
      ![1,2].includes(channels)||!Number.isSafeInteger(audio.timestamp))throw Error('b_no_plc_startup_format');
    const allocated=[];
    function make(count,timestamp,attempt=null){const pcm=new Float32Array(count*channels);
      if(attempt!==null)for(let ch=0;ch<channels;ch++)for(let i=0;i<count;i++)pcm[ch*count+i]=probe.probeValue(i,attempt);
      const value=new AudioData({format:'f32-planar',sampleRate:48000,numberOfFrames:count,
        numberOfChannels:channels,timestamp,data:pcm});pcm.fill(0);allocated.push(value);return value;}
    receipt.status='pending';
    const abort=new Promise((_,reject)=>{abortReject=reject;});
    timer=setTimeout(()=>abortReject?.(Error('b_no_plc_startup_timeout')),500);
    try{
      async function write(value){await Promise.race([writer.ready,abort]);check();
        await Promise.race([writer.write(value),abort]);check();}
      await write(make(frames,audio.timestamp-Math.ceil((frames+3840)*1000000/48000)));
      receipt.checkpoint_stage='marker_wait';
      let markerObserved=false;
      // Format adoption may discard an initial buffer. Only an exact, unique
      // native source marker ACK can advance this bounded muted handshake.
      for(let attempt=0;attempt<3;attempt++){
        await write(make(960,audio.timestamp-(4-attempt)*20000,attempt));
        receipt.write_accepted=true;receipt.probe_attempts=attempt+1;
        const result=await Promise.race([probe.waitMarker(attempt),abort]);check();
        if(result?.marker_observed===true){markerObserved=true;break;}
      }
      if(!markerObserved)throw Error('b_no_plc_startup_checkpoint');
      receipt.marker_observed=true;
      await write(make(960,audio.timestamp-20000));
      receipt.checkpoint_stage='quiet_tail_wait';
      const checkpoint=await Promise.race([probe.waitQuiet(),abort]);check();
      if(checkpoint?.generator_marker_observed!==true||checkpoint?.probe_tail_quiet!==true||
        !Number.isSafeInteger(checkpoint.clock_frame))
        throw Error('b_no_plc_startup_checkpoint');
      openGate();
      receipt.checkpoint_stage='gate_open_wait';
      const opened=Math.round(ctx.currentTime*48000);
      while(Math.round(ctx.currentTime*48000)<opened+256){
        await Promise.race([new Promise(resolve=>setTimeout(resolve,5)),abort]);check();
      }
      Object.assign(receipt,{status:'native_generator_checkpoint_observed',
        context_clock_frame:checkpoint.clock_frame,generator_marker_observed:true,
        probe_tail_quiet:true,probe_output_muted:true,checkpoint_stage:'complete'});complete=true;probe.close();
    }catch(error){receipt.status='failed';probe.close();throw error;}
    finally{for(const value of allocated)value.close();clearTimeout(timer);abortReject=null;}
  }
  return {prepare,close,accept(){return false;},receipt};
}
"""
