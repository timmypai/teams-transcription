// All PCM remains in this synthetic page and is zeroed in finally. No microphone.
function compareSyntheticPacket(reference,right,captured,capturedRight,start,logical,frames,cap) {
  const channels=right?2:1;
  if(start<0||start+frames>cap)return {verified:false,mismatched_samples:frames*channels,max_abs_error:null};
  let mismatches=0,maxError=0;
  for(let i=0;i<frames;i++){
    const left=Math.abs(reference[logical*frames+i]-captured[start+i]);
    maxError=Math.max(maxError,left);if(!Number.isFinite(left)||left>1e-6)mismatches++;
    if(right){const delta=Math.abs(right[logical*frames+i]-capturedRight[start+i]);
      maxError=Math.max(maxError,delta);if(!Number.isFinite(delta)||delta>1e-6)mismatches++;}
  }
  return {verified:mismatches===0,mismatched_samples:mismatches,max_abs_error:maxError};
}

function findFullSyntheticPacket(reference,right,captured,capturedRight,logical,frames,cap) {
  const first=reference[logical*frames];
  for(let at=0;at+frames<=cap;at++){
    if(Math.abs(captured[at]-first)>1e-6)continue;
    if(compareSyntheticPacket(reference,right,captured,capturedRight,
        at,logical,frames,cap).verified)return at;
  }
  return null;
}

function syntheticDeliveryPlan(mode,count) {
  const long=['long_paced','long_jitter','slow_consumer','full_paced','full_burst',
    'full_slow','two_questions','beta_long'].includes(mode);
  if(!['paced','burst','reordered','writer_stall','long_paced','long_jitter',
      'slow_consumer','cancel_wait','full_paced','full_burst','full_slow',
      'two_questions','beta_long'].includes(mode)||count!==(mode==='beta_long'?1500:long?500:16))
    throw Error('fixture_delivery_plan');
  const plan=Array.from({length:count},(_,index)=>({index,at_ms:index*20}));
  if(mode==='burst'||mode==='full_burst')for(const item of plan)item.at_ms=Math.floor(item.index/3)*60;
  if(mode==='reordered'){
    plan[1].at_ms=50;plan[2].at_ms=40;
    plan.sort((a,b)=>a.at_ms-b.at_ms);
  }
  if(mode==='long_jitter')for(const item of plan)item.at_ms=
    Math.floor(item.index/3)*60+[0,7,17][item.index%3];
  return plan;
}

// Synthetic-only stitching of the existing analyser history. No new graph node.
// Every noninitial overlap must be unique and sample-exact; otherwise UNKNOWN.
function createSyntheticTimeline(cap){
  if(!Number.isInteger(cap)||cap<32768||cap>48000*40)throw Error('fixture_timeline_cap');
  const pcm=new Float32Array(cap),previous=new Float32Array(32768),rows=[];
  let used=0,lastClock=null,reason=null,copyMs=0,maxCopyMs=0,skipped=0;
  return {pcm,rows,
    ingest(values,clock,wall){const started=performance.now();
      try{
        if(reason)return;
        if(values.some(v=>!Number.isFinite(v))){reason='nonfinite_history';return;}
        if(lastClock===null){
          if(!values.some(v=>Math.abs(v)>1e-6))return;
          pcm.set(values);previous.set(values);used=values.length;lastClock=clock;
        }else{
          const delta=clock-lastClock;if(delta===0)return;
          if(delta<0||delta>values.length/2){reason='history_poll_gap';return;}
          const candidates=[];
          for(let shift=Math.max(128,delta-512);shift<=Math.min(values.length/2,delta+512);shift+=128){
            let same=true;
            for(let i=0;i<values.length-shift;i++)if(previous[i+shift]!==values[i]){same=false;break;}
            if(same)candidates.push(shift);
          }
          if(candidates.length!==1){reason=candidates.length?'ambiguous_overlap':'unmatched_overlap';return;}
          const shift=candidates[0];if(used+shift>cap){reason='timeline_cap';return;}
          pcm.set(values.subarray(values.length-shift),used);used+=shift;
          previous.set(values);lastClock=clock;
        }
        if(rows.length<2400)rows.push({end:used,wall_ms:wall,clock_frame:clock});
        else reason='timeline_row_cap';
      }finally{const cost=performance.now()-started;copyMs+=cost;maxCopyMs=Math.max(maxCopyMs,cost);}
    },
    summary(){return {frames:used,coverage_verified:reason===null&&used>0,
      unknown_reason:reason,copy_and_overlap_ms:copyMs,max_copy_and_overlap_ms:maxCopyMs,
      skipped_raced_windows:skipped,
      allocated_bytes:pcm.byteLength+previous.byteLength,observation_rows:rows.length};},
    skipRaced(){skipped++;},
    close(){pcm.fill(0);previous.fill(0);rows.length=0;}
  };
}

function analyseSyntheticTimeline(timeline,reference,packetFrames,schedule,epoch){
  const info=timeline.summary();
  if(!info.coverage_verified)return {...info,all_payload_samples_verified:null,
    missing_packets:null,packet_observation_delay_ms:null};
  const captured=timeline.pcm.subarray(0,info.frames),packets=reference.length/packetFrames;
  if(reference.some(v=>!Number.isFinite(v))||captured.some(v=>!Number.isFinite(v)))
    return {...info,coverage_verified:false,unknown_reason:'nonfinite_samples',
      all_payload_samples_verified:null,missing_packets:null,packet_observation_delay_ms:null};
  const keys=new Map(),positions=Array.from({length:packets},()=>[]);
  for(let packet=0;packet<packets;packet++){
    const key=Math.floor(reference[packet*packetFrames+512]*1e6);
    if(!keys.has(key))keys.set(key,[]);keys.get(key).push(packet);
  }
  for(let at=0;at+packetFrames<=captured.length;at++){
    const key=Math.floor(captured[at+512]*1e6);
    // Conservative neighbours preserve the full comparator's 1e-6 tolerance,
    // including quantisation-boundary rounding. This is only a search filter.
    for(let bin=key-2;bin<=key+2;bin++){
      const candidates=keys.get(bin);if(!candidates)continue;
      for(const packet of candidates){
        if(positions[packet].length>=2)continue;
        if(compareSyntheticPacket(reference,null,captured,null,at,packet,packetFrames,captured.length).verified)
          positions[packet].push(at);
      }
    }
  }
  let wholeStart=null;
  for(const start of positions[0]){
    if(start+reference.length>captured.length)continue;
    let same=true;for(let i=0;i<reference.length;i++){
      const delta=Math.abs(reference[i]-captured[start+i]);
      if(!Number.isFinite(delta)||delta>1e-6){same=false;break;}
    }
    if(same){wholeStart=start;break;}
  }
  const unique=positions.every(p=>p.length===1),gaps=[];
  let outside=null;
  if(wholeStart!==null){outside=0;for(let i=0;i<captured.length;i++)
    if(i<wholeStart||i>=wholeStart+reference.length)outside=Math.max(outside,Math.abs(captured[i]));}
  if(unique)for(let i=1;i<packets;i++){
    const start=positions[i-1][0]+packetFrames,end=positions[i][0];
    if(end!==start){let max=0;for(let at=start;at<end;at++)max=Math.max(max,Math.abs(captured[at]));
      gaps.push({after_packet:i-1,frames:end-start,all_zero:end>start&&max===0});}
  }
  const arrivals=new Map(schedule.map(s=>[s.index,epoch+s.at_ms])),delays=[],packetClocks=[];
  for(let i=0;i<packets;i++){
    const at=wholeStart!==null?wholeStart+i*packetFrames:positions[i].length===1?positions[i][0]:null;
    if(at===null)continue;
    const observed=timeline.rows.find(row=>row.end>=at+packetFrames),arrival=arrivals.get(i);
    if(observed&&Number.isFinite(arrival)){
      delays.push(observed.wall_ms-arrival);
      if(packetClocks.length<2000)packetClocks.push({packet:i,render_frame:at,
        observed_ms:observed.wall_ms,arrival_ms:arrival,
        context_frame:Number.isSafeInteger(observed.clock_frame)?
          observed.clock_frame-(observed.end-at):null});
    }
  }
  return {...info,reference_frames:reference.length,
    all_payload_samples_verified:wholeStart!==null,whole_payload_start_frame:wholeStart,
    outside_payload_max_abs:outside,
    matched_packets:positions.filter(p=>p.length>0).length,
    missing_packets:positions.flatMap((p,i)=>p.length?[]:[i]),
    ambiguous_or_duplicate_packets:positions.flatMap((p,i)=>p.length>1?[i]:[]),
    inter_packet_gaps:gaps.slice(0,600),
    packet_clocks:packetClocks,
    packet_observation_delay_ms:delays.length?{count:delays.length,min:Math.min(...delays),
      max:Math.max(...delays),mean:delays.reduce((a,b)=>a+b,0)/delays.length}:null,
    latency_scope:'encoded arrival to first sampled full-packet render observation; includes poll delay; not ear-end latency',
    completeness_scope:'whole reference contiguous match or individual exact packets; ambiguous identities do not prove missing/duplicate samples'};
}

async function runDecoderAudioCheck(mode,history=false,quality=null) {
  const full=['full_paced','full_burst','full_slow','two_questions','beta_long'].includes(quality);
  const long=full||['long_paced','long_jitter','slow_consumer'].includes(quality);
  const channels=mode==='stereo'?2:1,voicePackets=quality==='beta_long'?1500:long?500:history?16:30;
  const packets=history&&mode==='voiced_then_quiet'?voicePackets+50:voicePackets,frames=960,rate=48000;
  const sequence=Array.from({length:packets},(_,i)=>i).filter(i=>mode!=='dropped_packet'||i!==12);
  const cap=rate*4,reference=new Float32Array(packets*frames),captured=new Float32Array(cap);
  const capturedRight=channels===2?new Float32Array(cap):null;
  const referenceRight=channels===2?new Float32Array(packets*frames):null;
  const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
  let encoder,referenceDecoder,bridge,inputTrack,inputWriter,controller,encoded=[],refFrames=0;
  let captureFrames=0,referenceError=false,observerCost=0,observerMax=0;
  let phase='encode',firstError=null,nativeWrites=0,cancelled=false,next=0;
  const timestamps=[],arrivals=[],failures=[],schedule=[];
  const historyEvidence=[];
  const mediaTrace=[],sourcePrefixes=[{frames:0,energy:0,duration:0,packets:0}];
  let delivered=0,lastDeliveryAt=0;
  let lastTimestamp=null,lastFrames=0,timestampGaps=0,timestampRegressions=0;
  let cleanup={};
  const deliveryTimers=[];let deliveryStarted=false;
  const timeline=full?createSyntheticTimeline(rate*(quality==='beta_long'?40:18)):null;let deliveryEpoch=null;
  globalThis.syntheticOffered=0;globalThis.syntheticCloseRequested=false;
  globalThis.syntheticWritesAfterClose=0;let cancelledInPacing=false;
  try {
    const support=await AudioEncoder.isConfigSupported({codec:'opus',sampleRate:rate,
      numberOfChannels:channels,bitrate:channels===2?128000:64000,opus:{frameDuration:20000}});
    if(!support.supported)throw Error('fixture_encoder_unsupported');
    encoder=new AudioEncoder({output:chunk=>{
      const data=new Uint8Array(chunk.byteLength);chunk.copyTo(data);
      encoded.push({data,timestamp:chunk.timestamp});
    },error:()=>{firstError??='fixture_encode_error';}});
    encoder.configure(support.config);
    for(let packet=0;packet<packets;packet++){
      const pcm=new Float32Array(frames*channels);
      for(let ch=0;ch<channels;ch++)for(let i=0;i<frames;i++){
        const n=packet*frames+i,t=n/rate;
        pcm[ch*frames+i]=packet>=voicePackets||quality==='two_questions'&&packet>=200&&packet<215?0:.2*Math.sin(2*Math.PI*((ch?701:311)*t+600*t*t))+
          .07*Math.sin(2*Math.PI*(1277+(ch?113:0))*t);
      }
      const audio=new AudioData({format:'f32-planar',sampleRate:rate,numberOfChannels:channels,
        numberOfFrames:frames,timestamp:packet*20000,data:pcm});
      try{encoder.encode(audio);}finally{audio.close();pcm.fill(0);}
    }
    await encoder.flush();encoder.close();
    if(firstError||encoded.length<packets)throw Error('fixture_encode_count');
    encoded=encoded.slice(0,packets);
    phase='reference_decode';
    referenceDecoder=new AudioDecoder({output:audio=>{
      try{if(refFrames+audio.numberOfFrames>reference.length)throw Error('fixture_reference_cap');
        audio.copyTo(reference.subarray(refFrames,refFrames+audio.numberOfFrames),
          {planeIndex:0,format:'f32-planar'});
        if(referenceRight)audio.copyTo(referenceRight.subarray(refFrames,refFrames+audio.numberOfFrames),
          {planeIndex:1,format:'f32-planar'});
        let energy=0;
        for(let ch=0;ch<channels;ch++){
          const plane=new Float32Array(audio.numberOfFrames);
          audio.copyTo(plane,{planeIndex:ch,format:'f32-planar'});
          for(const v of plane)energy+=v*v;plane.fill(0);
        }
        const prior=sourcePrefixes[sourcePrefixes.length-1];
        sourcePrefixes.push({frames:prior.frames+audio.numberOfFrames,energy:prior.energy+energy/channels/rate,
          duration:prior.duration+audio.numberOfFrames/rate,packets:prior.packets+1});
        refFrames+=audio.numberOfFrames;
      }finally{audio.close();}
    },error:()=>{referenceError=true;}});
    referenceDecoder.configure({codec:'opus',sampleRate:rate,numberOfChannels:channels,
      description:new Uint8Array([79,112,117,115,72,101,97,100,1,channels,0,0,128,187,0,0,0,0,0])});
    for(const i of sequence)referenceDecoder.decode(new EncodedAudioChunk({type:'key',
      timestamp:i*20000,data:encoded[i].data}));
    await referenceDecoder.flush();referenceDecoder.close();
    if(referenceError||refFrames!==sequence.length*frames)throw Error('fixture_reference_decode');
    if(sourcePrefixes.length!==sequence.length+1||sourcePrefixes.some((p,i)=>p.frames!==i*frames))
      throw Error('fixture_reference_packet_boundaries');
    phase='construct';
    inputTrack=new MediaStreamTrackGenerator({kind:'audio'});
    inputWriter=inputTrack.writable.getWriter();
    const readable=new ReadableStream({start(c){controller=c;},async pull(c){
      if(quality){
        if(deliveryStarted)return;
        deliveryStarted=true;const epoch=performance.now();deliveryEpoch=epoch;
        for(const item of syntheticDeliveryPlan(quality,packets))deliveryTimers.push(setTimeout(()=>{
          if(cancelled)return;
          schedule.push({index:item.index,planned_ms:item.at_ms,at_ms:performance.now()-epoch});
          c.enqueue({data:encoded[item.index].data.buffer,timestamp:item.index*frames,
            syntheticIndex:item.index,
            syntheticArrival:performance.now(),getMetadata:()=>({payloadType:111})});
          delivered++;globalThis.syntheticOffered=delivered;lastDeliveryAt=performance.now();
        },item.at_ms));
        return;
      }
      if(next>=packets)return;
      let index=next++;
      if(mode==='dropped_packet'&&index===12)index=next++;
      if(index>0)await sleep(mode==='starvation'&&index===12?160:
        mode==='dropped_packet'&&index===13?40:20);
      if(cancelled)return;
      schedule.push({index,at_ms:performance.now()});
      const timestamp=mode==='bad_timestamp'&&index===4?-1:
        mode==='timestamp_wrap'?(0xffffff00+index*frames)>>>0:index*frames;
      const frame={data:encoded[index].data.buffer,timestamp,getMetadata:()=>({payloadType:111})};
      c.enqueue(frame);
      delivered++;
      if(mode==='duplicate_packet'&&index===12)c.enqueue(frame);
    },cancel(){cancelled=true;}} ,{highWaterMark:0});
    const receiver={track:inputTrack,getParameters:()=>({codecs:[{payloadType:111,
      mimeType:'audio/opus',clockRate:rate,channels}]}),
      createEncodedStreams:()=>({readable,writable:new WritableStream({write(){nativeWrites++;}})})};
    bridge=await createNoPlcReceiver(receiver,error=>{failures.push(error.message);});
    if(!history)bridge.offlineMeter.observeSynthetic=audio=>{
      if(mode==='observer_error')throw Error('fixture_observer_fault');
      if(mode==='stop_in_checkpoint')bridge.close();
      const started=performance.now(),n=audio.numberOfFrames;
      if(captureFrames+n>cap)throw Error('fixture_capture_cap');
      audio.copyTo(captured.subarray(captureFrames,captureFrames+n),{planeIndex:0,format:'f32-planar'});
      if(capturedRight)audio.copyTo(capturedRight.subarray(captureFrames,captureFrames+n),
        {planeIndex:Math.min(1,audio.numberOfChannels-1),format:'f32-planar'});
      if(lastTimestamp!==null){
        const delta=audio.timestamp-lastTimestamp-lastFrames*1000000/rate;
        if(delta>100)timestampGaps++;
        if(audio.timestamp<=lastTimestamp)timestampRegressions++;
      }
      if(timestamps.length<16)timestamps.push(audio.timestamp);
      if(arrivals.length<16)arrivals.push(performance.now());
      lastTimestamp=audio.timestamp;lastFrames=n;captureFrames+=n;
      const cost=performance.now()-started;observerCost+=cost;observerMax=Math.max(observerMax,cost);
    };
    const record=sample=>{
      if(!history||quality)return;
      const prefix=sourcePrefixes[Math.min(delivered,sourcePrefixes.length-1)];
      mediaTrace.push({...sample.input,counter_domain:'target_decoded_input_v2',connected:true,
        source_frames:prefix.frames,source_energy:prefix.energy,source_duration:prefix.duration,
        source_packets:prefix.packets,render_evidence:sample.render});
    };
    const captureHistory=()=>{
      const data=bridge.syntheticRead(),last=(voicePackets-1)*frames;
      let tailReady=false;
      for(let at=0;at+frames<=data[0].length;at++){
        if(Math.abs(data[0][at]-reference[last])>1e-6)continue;
        let same=true;
        for(let i=0;i<frames;i++)if(Math.abs(data[0][at+i]-reference[last+i])>1e-6||
          capturedRight&&Math.abs(data[1][at+i]-referenceRight[last+i])>1e-6){same=false;break;}
        if(same){tailReady=true;break;}
      }
      if(!tailReady){for(const values of data)values.fill(0);return false;}
      captureFrames=data[0].length;captured.set(data[0]);if(capturedRight)capturedRight.set(data[1]);
      for(const values of data)values.fill(0);
      return true;
    };
    phase='play';const start=performance.now();
    while(performance.now()-start<(quality==='beta_long'?35000:long?16500:2500)){
      const sample=bridge.stats(),count=sample.input.packets;
      if(timeline&&bridge.valid()){
        const values=bridge.syntheticRead();
        if(Number.isSafeInteger(values.before)&&Number.isSafeInteger(values.after)&&
            values.after>=values.before&&values.after-values.before<=256)
          timeline.ingest(values[0],values.before,performance.now());
        else timeline.skipRaced();
        for(const lane of values)lane.fill(0);
      }
      record(sample);
      if(failures.length)break;
      if(!bridge.valid())break;
      if(mode==='stop_early'&&count>=5)break;
      if(quality==='cancel_wait'&&globalThis.syntheticPaceWaiting){
        cancelledInPacing=true;globalThis.syntheticCloseRequested=true;
        bridge.close();await bridge.closing;break;
      }
      if(quality==='reordered'&&delivered===packets&&count>=packets-1&&
          performance.now()-lastDeliveryAt>80&&captureFrames===0){
        const data=bridge.syntheticRead();captureFrames=data[0].length;captured.set(data[0]);
        for(const values of data)values.fill(0);
      }
      if(quality!=='reordered'&&history&&count>=voicePackets&&captureFrames===0)captureHistory();
      if(history&&(historyEvidence.length===0||
        sample.render.clock_frame-historyEvidence[historyEvidence.length-1].clock_frame>=2400))
        historyEvidence.push(sample.render);
      if(count===(mode==='dropped_packet'?packets-1:packets)&&(!history||captureFrames>0)){
        if(!history)await sleep(250);break;}
      await sleep(history?20:5);
    }
    if(history&&mode==='stop_early'){bridge.close();await bridge.closing;}
    if(history&&bridge.valid()){
      if(captureFrames===0){bridge.stats();captureHistory();}
      for(let i=0;i<42&&bridge.valid();i++){await sleep(50);const sample=bridge.stats();
        historyEvidence.push(sample.render);record(sample);}
    }
    const stats=bridge.stats(),startup={...bridge.startup};
    phase='align';
    // Same encoded bytes, independent decoder reference. No PCM is exported.
    function correlation(offset,anchor,length){
      let xy=0,xx=0,yy=0;
      for(let i=0;i<length;i++){
        const x=reference[anchor+i],y=captured[offset+i];xy+=x*y;xx+=x*x;yy+=y*y;
      }
      return xx&&yy?xy/Math.sqrt(xx*yy):0;
    }
    const anchors=[];
    // Late-packet rejection changes Opus prediction history. The all-packets
    // reference cannot establish exact render loss for this counterexample.
    const analysisPackets=quality&&(quality==='reordered'||captureFrames===0)?0:
      mode==='voiced_then_quiet'?voicePackets:stats.input.packets;
    const analysisStart=long?Math.max(0,voicePackets-16):0;
    for(let logical=analysisStart;logical<analysisPackets;logical++){
      const packet=sequence[logical],anchor=logical*frames+512,length=256;
      let best=-1,offset=-1;
      for(let at=0;at+length<=captureFrames;at+=8){
        const score=correlation(at,anchor,length);if(score>best){best=score;offset=at;}
      }
      const coarse=offset;
      for(let at=Math.max(0,coarse-8);at<=coarse+8&&at+length<=captureFrames;at++){
        const score=correlation(at,anchor,length);if(score>best){best=score;offset=at;}
      }
      // The coarse correlation can select a nearby chirp period even when an
      // exact complete packet exists. Search the captured samples independently.
      const exact=findFullSyntheticPacket(reference,referenceRight,captured,capturedRight,
        logical,frames,captureFrames);
      if(exact!==null){offset=exact+512;best=1;}
      const start=offset-512;
      const full=compareSyntheticPacket(reference,referenceRight,captured,capturedRight,
        start,logical,frames,captureFrames);
      anchors.push({packet,logical,offset,score:Number(best.toFixed(6)),anchor_matched:best>.999,
        full_packet_verified:full.verified,full_packet_mismatched_samples:full.mismatched_samples,
        full_packet_max_abs_error:full.max_abs_error});
    }
    const matched=anchors.filter(a=>a.anchor_matched);
    let reordered=0,discontinuous=0;
    for(let i=1;i<matched.length;i++){
      if(matched[i].offset<=matched[i-1].offset)reordered++;
      if(matched[i].offset-matched[i-1].offset!==frames*(matched[i].logical-matched[i-1].logical))discontinuous++;
    }
    let energy=0,nonzero=0,zeroRuns=[],run=0;
    for(let i=0;i<captureFrames;i++){
      energy+=captured[i]*captured[i];if(Math.abs(captured[i])>1e-6)nonzero++;
      if(captured[i]===0)run++;else{if(run>=480)zeroRuns.push({start_frame:i-run,frames:run});run=0;}
    }
    if(run>=480)zeroRuns.push({start_frame:captureFrames-run,frames:run});
    let outsidePayload=null;
    if(mode!=='voiced_then_quiet'&&anchors.length===sequence.length&&anchors.every(a=>a.full_packet_verified)){
      const used=new Uint8Array(captureFrames);
      for(const a of anchors)used.fill(1,a.offset-512,a.offset-512+frames);
      let max=0;
      for(let i=0;i<captureFrames;i++)if(!used[i]){
        max=Math.max(max,Math.abs(captured[i]),capturedRight?Math.abs(capturedRight[i]):0);
      }
      used.fill(0);outsidePayload=max;
    }
    const interPacketGaps=[];
    for(let i=1;i<anchors.length;i++){
      const prior=anchors[i-1],current=anchors[i];
      if(!prior.full_packet_verified||!current.full_packet_verified)continue;
      const start=prior.offset-512+frames,end=current.offset-512;
      if(end<=start)continue;
      let max=0;for(let at=start;at<end;at++)max=Math.max(max,Math.abs(captured[at]));
      interPacketGaps.push({after_packet:prior.packet,frames:end-start,
        max_abs_sample:max,all_zero:max===0});
    }
    const quantumAnchors=[];
    if(quality&&quality!=='reordered'&&captureFrames>0)
      for(let index=long?(reference.length-16*frames)/128:0;index<reference.length/128;index++){
      quantumAnchors.push({source_frame:index*128,render_frame:findFullSyntheticPacket(
        reference,null,captured,null,index,128,captureFrames)});
    }
    let starvationGap=null;
    if(mode==='starvation'){
      const before=anchors.find(a=>a.packet===11),after=anchors.find(a=>a.packet===12);
      if(before?.full_packet_verified&&after?.full_packet_verified){
        const start=before.offset-512+frames,end=after.offset-512;
        let max=0;for(let i=start;i<end;i++)max=Math.max(max,Math.abs(captured[i]));
        starvationGap={start_frame:start,frames:Math.max(0,end-start),max_abs_sample:max,
          observed_all_zero:end>start&&max===0,complete_render_coverage_verified:false};
      }
    }
    return {mode,phase:'complete',channels,reference_frames:refFrames,captured_frames:captureFrames,
      captured_energy:energy/rate,captured_nonzero_frames:nonzero,stats,startup,native_writes:nativeWrites,
      errors:failures,anchors,anchor_matched_packets:matched.length,
      render_history_evidence:historyEvidence,render_history_mode:history,
      synthetic_media_trace:mediaTrace,analysis_packet_count:anchors.length,
      analysis_first_packet:analysisStart,
      passive_tail_identity_observed:quality==='reordered'?false:captureFrames>0,
      render_alignment_status:quality==='reordered'?'unknown_different_decoder_history':
        captureFrames>0?'tail_identity_observed':'unknown_no_tail_identity',
      analysis_scope:quality==='reordered'?'unknown: all-packets reference has different Opus history':
        long?'final16 packets only; earlier render is unobserved':
        mode==='voiced_then_quiet'?'first16 voice packets; silence tested by source/render trace':'decoded input packets',
      full_packet_verified_count:anchors.filter(a=>a.full_packet_verified).length,reordered_anchor_pairs:reordered,
      source_sequence:sequence,source_identity_scope:'same decoded packets; stopped runs may truncate queued playback',
      all_decoded_packet_samples_matched:long?null:anchors.length?anchors.every(a=>a.full_packet_verified):null,
      duplicated_samples_verified:false,
      alignment_limit:'per-packet left anchor plus full mono/stereo packet comparison; not exhaustive duplicate-sample search',
      outside_verified_payload_max_abs:outsidePayload,
      inter_packet_gaps:interPacketGaps,
      synthetic_quantum_anchors:quantumAnchors,
      discontinuous_anchor_pairs:discontinuous,zero_runs_ge_10ms:zeroRuns.slice(0,32),
      zero_run_scope:'captured samples only; prefix/primer/tail included; skipped observation unknown',
      starvation_gap:starvationGap,starvation_zero_verified:false,
      audio_timestamps_first16:timestamps,arrival_ms_first16:arrivals,
      timestamp_gap_events:timestampGaps,timestamp_regressions:timestampRegressions,
      delivery_plan:quality,delivery_schedule:schedule,
      synthetic_pump_metrics:quality?globalThis.syntheticPumpMetrics:null,
      synthetic_pump_summary:quality?globalThis.syntheticPumpSummary:null,
      cancelled_in_pacing:cancelledInPacing,writes_after_close:globalThis.syntheticWritesAfterClose,
      long_audio_scope:long?`${voicePackets} packets / ${voicePackets/50} seconds decoded; full_render is separate from final16 anchors`:null,
      explicit_fixture_bytes:{reference:reference.byteLength,capture:captured.byteLength,
        encoded_payload:encoded.reduce((sum,p)=>sum+p.data.byteLength,0),
        history_copy:history?32768*2*4:0},
      full_render:timeline?analyseSyntheticTimeline(timeline,reference,frames,schedule,deliveryEpoch):null,
      observer_copy_ms:observerCost,observer_copy_max_ms:observerMax,
      enqueue_interval_ms:schedule.slice(1).map((r,i)=>r.at_ms-schedule[i].at_ms),
      cleanup};
  }catch(error){
    firstError??=error.message?.startsWith('fixture_')?error.message:'fixture_unclassified';
    return {mode,phase,failure:firstError,exception_type:error.name,errors:failures,cleanup};
  }finally{
    for(const timer of deliveryTimers)clearTimeout(timer);
    bridge?.close();const closing=bridge?.closing;
    bridge?.close();await bridge?.closing;
    try{await inputWriter?.abort();inputWriter?.releaseLock();}catch{}
    inputTrack?.stop();
    if(encoder&&encoder.state!=='closed')encoder.close();
    if(referenceDecoder&&referenceDecoder.state!=='closed')referenceDecoder.close();
    Object.assign(cleanup,{input_track_ended:inputTrack?inputTrack.readyState==='ended':true,
      context_closed:bridge?bridge.ctx.state==='closed':null,
      decoder_invalid:bridge?!bridge.valid():null,repeat_close_same_promise:bridge?bridge.closing===closing:null,
      stream_cancelled:cancelled});
    reference.fill(0);referenceRight?.fill(0);captured.fill(0);capturedRight?.fill(0);
    for(const item of encoded)item.data.fill(0);
    timeline?.close();
  }
}

// Component counterexample: native WebAudio runs while the observer awaits.
// A scheduled 80ms tone is compared with a control, without a decoder/JS clock.
async function runMeterDropCheck(stall) {
  const ctx=new AudioContext({sampleRate:48000});await ctx.resume();
  const oscillator=ctx.createOscillator(),gain=ctx.createGain(),destination=ctx.createMediaStreamDestination();
  let meter,stalled=false,callbackCount=0,observedNonzero=0,observedFrames=0,errors=0;
  let stallStart=null,stallEnd=null,toneStart,toneEnd;
  const cleanup={};
  try{
    oscillator.frequency.value=1000;oscillator.connect(gain);gain.connect(destination);
    const now=ctx.currentTime;gain.gain.setValueAtTime(0,now);
    toneStart=now+.08;toneEnd=now+.16;
    gain.gain.setValueAtTime(.2,toneStart);gain.gain.setValueAtTime(0,toneEnd);oscillator.start();
    meter=createOfflineNativeMeter(ctx,destination.stream.getAudioTracks()[0],()=>errors++);
    meter.observeSynthetic=audio=>{
      const pcm=new Float32Array(audio.numberOfFrames);audio.copyTo(pcm,{planeIndex:0,format:'f32-planar'});
      for(const value of pcm)if(Math.abs(value)>1e-6)observedNonzero++;
      pcm.fill(0);observedFrames+=audio.numberOfFrames;callbackCount++;
    };
    meter.beforeSyntheticRead=async()=>{
      if(stall&&!stalled&&callbackCount>=2){stalled=true;stallStart=ctx.currentTime;
        await new Promise(r=>setTimeout(r,220));stallEnd=ctx.currentTime;}
    };
    await meter.afterWritePull();
    await new Promise(r=>setTimeout(r,400));
    const sample=await new Promise(resolve=>{meter.port.onmessage=e=>resolve(e.data);meter.port.postMessage('sample');});
    return {mode:stall?'meter_stalled':'meter_control',phase:'complete',observed_frames:observedFrames,
      observed_nonzero_frames:observedNonzero,sample,stall_injected:stalled,errors,
      tone_start_context_seconds:toneStart,tone_end_context_seconds:toneEnd,
      stall_start_context_seconds:stallStart,stall_end_context_seconds:stallEnd,
      render_quiet_verified:false,
      scope:'native oscillator capture counterexample; not an Opus/PLC test',cleanup};
  }finally{
    meter?.port.close();await meter?.closing;oscillator.stop();
    oscillator.disconnect();gain.disconnect();for(const t of destination.stream.getTracks())t.stop();
    await ctx.close();cleanup.context_closed=ctx.state==='closed';
    cleanup.tracks_ended=destination.stream.getTracks().every(t=>t.readyState==='ended');
  }
}
