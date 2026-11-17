// Experimental synthetic-only observer. Not selected by production code.
// The native generator->destination sound path is unchanged; this reads a clone.
function createOfflineNativeMeter(ctx,track,onFailure) {
  const node=ctx.createGain(),scratch=new Float32Array(5760);
  let closed=false,reader,clone,pump,resolvePull,rejectPull,firstPull;
  const sample={frames:0,energy:0,duration:0},observations={frames:0,timestamp_regressions:0};
  let lastTimestamp=null;
  node.port={onmessage:null,postMessage(message){
    if(message!=='sample')return;
    queueMicrotask(()=>{if(!closed)node.port.onmessage?.({data:{...sample}});});
  },close(){
    if(closed)return;
    closed=true;rejectPull?.(Error('offline_meter_closed'));
    clone?.stop();scratch.fill(0);
    const cancel=reader?reader.cancel().catch(()=>{}):Promise.resolve();
    node.closing=Promise.allSettled([cancel,pump]).then(()=>{try{reader?.releaseLock();}catch{}});
  }};
  node.afterWritePull=()=>{
    if(closed)throw Error('offline_meter_closed');
    if(firstPull)return firstPull;
    // There is no pre-existing processor queue: construction follows the write.
    clone=track.clone();
    const processor=new MediaStreamTrackProcessor({track:clone,maxBufferSize:1});
    reader=processor.readable.getReader();
    firstPull=new Promise((resolve,reject)=>{resolvePull=resolve;rejectPull=reject;});
    pump=(async()=>{
      while(!closed){
        if(node.beforeSyntheticRead)await node.beforeSyntheticRead();
        const {done,value:audio}=await reader.read();
        if(closed){audio?.close();return;}
        if(done)throw Error('offline_meter_ended');
        try{
          if(audio.sampleRate!==48000||![1,2].includes(audio.numberOfChannels)||
            !Number.isInteger(audio.numberOfFrames)||audio.numberOfFrames<1||audio.numberOfFrames>5760||
            !Number.isSafeInteger(audio.timestamp))throw Error('offline_meter_format');
          // Synthetic harness only; never installed or selected by the app.
          node.observeSynthetic?.(audio);
          let energy=0;
          for(let channel=0;channel<audio.numberOfChannels;channel++){
            audio.copyTo(scratch,{planeIndex:channel,format:'f32-planar'});
            for(let i=0;i<audio.numberOfFrames;i++)energy+=scratch[i]*scratch[i];
            scratch.fill(0,0,audio.numberOfFrames);
          }
          if(!Number.isFinite(energy))throw Error('offline_meter_energy');
          sample.frames+=audio.numberOfFrames;sample.duration=sample.frames/48000;
          sample.energy+=energy/audio.numberOfChannels/48000;
          if(lastTimestamp!==null&&audio.timestamp<=lastTimestamp)observations.timestamp_regressions++;
          lastTimestamp=audio.timestamp;observations.frames=sample.frames;
          if(resolvePull&&sample.frames>=256){
            resolvePull({captured_frames:sample.frames,timestamp_us:audio.timestamp});
            resolvePull=null;rejectPull=null;
          }
        }finally{audio.close();}
      }
    })().catch(()=>{if(!closed){rejectPull?.(Error('offline_meter_failed'));onFailure();}});
    return firstPull;
  };
  node.captureObservations=()=>({...observations});
  return node;
}

function createOfflineNativeStartup(writer,ctx,meter,requireOpen) {
  let complete=false,closed=false,abortReject,timer;
  const receipt={version:'native-capture-after-write-v1',status:'not_started'};
  const check=()=>{if(closed)throw Error('offline_startup_closed');requireOpen();};
  function close(){closed=true;clearTimeout(timer);abortReject?.(Error('offline_startup_closed'));}
  async function prepare(audio){
    check();if(complete)return;
    const frames=audio.numberOfFrames,channels=audio.numberOfChannels;
    if(audio.sampleRate!==48000||!Number.isInteger(frames)||frames<1||frames>5760||
      ![1,2].includes(channels)||!Number.isSafeInteger(audio.timestamp))throw Error('offline_startup_format');
    const silence=new AudioData({format:'f32-planar',sampleRate:48000,numberOfFrames:frames,
      numberOfChannels:channels,timestamp:audio.timestamp-Math.ceil(frames*1000000/48000),
      data:new Float32Array(frames*channels)});
    receipt.status='pending';
    const abort=new Promise((_,reject)=>{abortReject=reject;});
    timer=setTimeout(()=>abortReject?.(Error('offline_startup_timeout')),500);
    try{
      await Promise.race([writer.ready,abort]);check();
      await Promise.race([writer.write(silence),abort]);check();
      receipt.write_accepted=true;
      const captured=await Promise.race([meter.afterWritePull(),abort]);check();
      Object.assign(receipt,{status:'native_capture_observed',
        after_write_capture_frames:captured.captured_frames,
        after_write_capture_timestamp_us:captured.timestamp_us,
        clock:'actual captured AudioData; not absolute render frame or remote drain'});
      complete=true;
    }catch(error){receipt.status='failed';throw error;}
    finally{silence.close();clearTimeout(timer);abortReject=null;}
  }
  return {prepare,close,accept(){return false;},receipt};
}
