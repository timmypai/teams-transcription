// Controlled JS host proves connections and cleanup; no browser/media hardware.
const fs=require('fs'), assert=require('assert');
const code=JSON.parse(fs.readFileSync(0,'utf8'));
let assertions=0;
const check=(v)=>{assert.ok(v); assertions++;};
async function settled(out) {
  const deadline=performance.now()+2000;
  while(out.pcmBridge.queuedPackets) {
    assert.ok(performance.now()<deadline,'Serialized PCM bridge did not drain');
    await new Promise(resolve=>setTimeout(resolve,5));
  }
}
async function scenario(mode='ok') {
  const edges=[], log=[], nodes=[], players=[], errors=[], packets=[];
  class Node {
    constructor(kind){this.kind=kind;this.gain={value:1};this.disconnected=false;nodes.push(this);}
    connect(target){edges.push([this,target]);log.push('connect:'+this.kind+':'+target.kind);return target;}
    disconnect(){this.disconnected=true;}
  }
  class Context {
    constructor(){this.sampleRate=48000;this.destination=new Node('destination');this.state='suspended';this.sinkId='';}
    async setSinkId(sink){
      log.push('setSink:none');
      if(mode==='reject')throw Error('unsupported-private-label');
      if(['NotAllowedError','SecurityError','NotSupportedError'].includes(mode)) {
        const error=Error('private-device-label');error.name=mode;throw error;
      }
      this.sinkId=mode==='wrong'?'default':sink;
    }
    createGain(){return new Node('gain');}
    createMediaStreamSource(){return new Node('source');}
    createScriptProcessor(){return new Node('processor');}
    async resume(){this.state='running';}
    async close(){this.state='closed';log.push('close');}
  }
  if(mode==='unsupported') Context.prototype.setSinkId=undefined;
  const track={kind:'audio',stop(){this.stopped=true;}};
  const oscillator=new Node('oscillator');oscillator.stop=function(){this.stopped=true;};
  const inputGain=new Node('silentgain'); inputGain.gain.value=0;
  const uplink={stop(){this.stopped=true;}};
  const pc={close(){this.closed=true;}};
  const state={pc,silentInput:{oscillator,gain:inputGain,active:true},
    ctx:{async close(){this.closed=true;}},destination:{stream:{getTracks:()=>[uplink]}}};
  const env={window:{isSecureContext:mode!=='insecure',__live:state,responseAudio:async p=>packets.push(p),responseAudioError:async e=>errors.push(e)},
    AudioContext:mode==='audio_missing'?undefined:Context,MediaStream:class {constructor(tracks){this.tracks=tracks;}},
    document:{createElement(){const p={async play(){this.played=true;},pause(){this.paused=true;}};players.push(p);return p;}},
    btoa:s=>Buffer.from(s,'binary').toString('base64')};
  async function evaluate(js){return await Function(...Object.keys(env),'return ('+js+')()')(...Object.values(env));}
  if(mode!=='ok') {
    const label={reject:'silent_sink_rejected_other',wrong:'silent_sink_not_verified',
      unsupported:'silent_sink_api_missing',insecure:'audio_context_not_secure',audio_missing:'audio_context_missing',
      NotAllowedError:'silent_sink_rejected_not_allowed',SecurityError:'silent_sink_rejected_security',
      NotSupportedError:'silent_sink_rejected_not_supported'}[mode];
    await assert.rejects(()=>evaluate(code.render),new RegExp(label));
    check(!edges.length);
    check(['insecure','audio_missing'].includes(mode) ? log.length===0 : log.at(-1)==='close');
    return;
  }
  const receipt=await evaluate(code.render),out=state.output;
  check(receipt.sink==='none');check(out.ctx.sinkId.type==='none');
  check(log[0]==='setSink:none');check(out.renderGain.gain.value===1);
  await pc.ontrack({track});
  const n=[...out.nodes][0];
  check(edges.some(([a,b])=>a===n.source&&b===out.renderGain));
  check(edges.some(([a,b])=>a===out.renderGain&&b===out.ctx.destination));
  check(edges.some(([a,b])=>a===n.source&&b===n.processor));
  check(edges.some(([a,b])=>a===n.processor&&b===n.tapMute));
  check(n.tapMute.gain.value===0);check(n.player.muted&&n.player.played);
  const samples=new Float32Array([0,.5,-.5]);
  n.processor.onaudioprocess({inputBuffer:{getChannelData:()=>{throw Error('idle waveform was read');}}});
  await new Promise(resolve=>setImmediate(resolve));
  check(packets.length===0); // No application capture admission yet.
  out.pcmBridge.setCapture(true,1);
  n.processor.onaudioprocess({inputBuffer:{getChannelData:()=>samples}});
  await new Promise(resolve=>setImmediate(resolve));
  check(packets.length===1&&packets[0].sample_rate===48000);
  check(Buffer.from(packets[0].data,'base64').readInt16LE(2)===16384);
  await pc.ontrack({track});check(out.nodes.size===1);
  const bridge=out.pcmBridge;
  check(bridge.maxBytes===4_320_000);
  check(bridge.maxPackets===Math.ceil(4_320_000/(2048*2)));
  let release, active=0, maximum=0;
  const held=new Promise(resolve=>{release=resolve;});
  env.window.responseAudio=async packet=>{
    active++;maximum=Math.max(maximum,active);packets.push(packet);
    try {await held;} finally {active--;}
  };
  bridge.setCapture(true,2);
  for(let i=1;i<=12;i++)n.processor.onaudioprocess({
    inputBuffer:{getChannelData:()=>new Float32Array(2048).fill(i/32)}});
  check(packets.length===2&&bridge.queuedPackets===12);
  check(bridge.queuedBytes===12*2048*2&&out.dropped===0&&errors.length===0);
  release();await settled(out);
  check(maximum===1&&packets.length===13);
  check(packets.slice(1).every((p,i)=>Buffer.from(p.data,'base64').readInt16LE(0)===(i+1)*1024));
  let staleRelease;
  const staleHeld=new Promise(resolve=>{staleRelease=resolve;});
  env.window.responseAudio=async p=>{packets.push(p);await staleHeld;};
  bridge.setCapture(true,3);
  for(let i=0;i<4;i++)n.processor.onaudioprocess({inputBuffer:{getChannelData:()=>samples}});
  bridge.setCapture(false,4);bridge.setCapture(true,5);
  n.processor.onaudioprocess({inputBuffer:{getChannelData:()=>samples}});
  staleRelease();await settled(out);
  check(packets.length===15&&packets.at(-2).capture_generation===3&&packets.at(-1).capture_generation===5);
  let overflowRelease;
  const overflowHeld=new Promise(resolve=>{overflowRelease=resolve;});
  env.window.responseAudio=async p=>{packets.push(p);await overflowHeld;};
  bridge.setCapture(true,6);
  const fullBlock=new Float32Array(2048).fill(.125),before=packets.length;
  for(let i=0;i<=bridge.maxPackets;i++)n.processor.onaudioprocess({
    inputBuffer:{getChannelData:()=>fullBlock}});
  check(bridge.failed&&!out.captureEnabled&&out.dropped===1);
  check(errors.filter(e=>e==='output_backpressure').length===1);
  check(bridge.queuedBytes===4096&&bridge.queuedPackets===1&&packets.length===before+1);
  check(bridge.queuedBytes<=bridge.maxBytes);
  assert.throws(()=>bridge.setCapture(true,7),/output_backpressure/);
  overflowRelease();await settled(out);
  check(packets.length===before+1&&bridge.queuedBytes===0);
  out.ctx.sinkId='default';n.processor.onaudioprocess({inputBuffer:{getChannelData:()=>samples}});
  check(out.renderGain.gain.value===0&&errors.includes('silent_sink_changed'));
  out.ctx.sinkId={type:'none'};
  await evaluate(code.close);
  check(out.nodes.size===0&&out.tracks.size===0);check(n.processor.onaudioprocess===null);
  check(n.source.disconnected&&n.processor.disconnected&&n.tapMute.disconnected);
  check(n.player.paused&&n.player.srcObject===null);check(track.stopped&&uplink.stopped);
  check(oscillator.stopped&&oscillator.disconnected&&inputGain.disconnected);
  check(!state.silentInput.active&&pc.closed);check(out.ctx.state==='closed'&&state.ctx.closed);
  check(out.renderGain.disconnected&&out.renderGain.gain.value===0);
}
async function sustainedBridge() {
  let now=0,sent=0;
  const out={ctx:{sampleRate:48000},closing:false,captureEnabled:false,
    captureGeneration:0,frames:0,dropped:0};
  const errors=[],block=new Float32Array(2048).fill(.125);
  const env={out,performance:{now:()=>now},
    btoa:s=>Buffer.from(s,'binary').toString('base64'),
    setTimeout:()=>{throw Error('On-time producer must not wait for pacing');},
    clearTimeout:()=>{},
    window:{responseAudio:async packet=>{
      check(packet.capture_generation===1&&packet.sample_rate===48000);
      check(Buffer.from(packet.data,'base64').length===4096);sent++;
    },responseAudioError:label=>errors.push(label)}};
  Function(...Object.keys(env),'('+code.bridge+')(out)')(...Object.values(env));
  const bridge=out.pcmBridge;
  bridge.setCapture(true,1);
  const blocks=Math.ceil(120*48000/2048);
  for(let i=0;i<blocks;i++) {
    now+=50;
    bridge.capture({inputBuffer:{getChannelData:()=>block}});
    await Promise.resolve();await Promise.resolve();
    check(bridge.queuedBytes===0&&bridge.queuedPackets===0&&!bridge.sending);
  }
  check(sent===blocks&&out.frames>60*48000);
  check(!bridge.failed&&out.captureEnabled&&out.dropped===0&&errors.length===0);
  check(bridge.maxBytes===4_320_000);
  bridge.setCapture(false,2);
  bridge.capture({inputBuffer:{getChannelData:()=>{throw Error('Revoked waveform read');}}});
  check(sent===blocks);
  return out.frames;
}
(async()=>{
  await scenario(); await scenario('reject'); await scenario('wrong'); await scenario('unsupported');
  for (const mode of ['insecure','audio_missing','NotAllowedError','SecurityError','NotSupportedError'])
    await scenario(mode);
  const sustainedFrames=await sustainedBridge();
  const log=[];let oscillator,gain,track;
  class N {
    constructor(kind){this.kind=kind;this.gain={value:1};}
    connect(target){log.push(this.kind+'->'+target.kind);}
    start(){log.push('oscillator_start');}
  }
  class C {
    async resume(){}
    createMediaStreamDestination(){track={kind:'audio'};return {kind:'streamDestination',stream:{getAudioTracks:()=>[track]}};}
    createOscillator(){return oscillator=new N('oscillator');}
    createGain(){return gain=new N('gain');}
  }
  class PC {
    constructor(){this.iceGatheringState='complete';}
    getConfiguration(){return {iceServers:[]};}
    addEventListener(){}
    addTrack(t){check(t===track&&gain.gain.value===0);log.push('addTrack');}
    createDataChannel(){return {};}
    async createOffer(){log.push('offer');return {sdp:'controlled-no-network'};}
    async setLocalDescription(s){this.localDescription=s;}
  }
  const env={AudioContext:C,RTCPeerConnection:PC,window:{},setTimeout:()=>0};
  const offer=await Function(...Object.keys(env),'return ('+code.silent+')()')(...Object.values(env));
  check(offer==='controlled-no-network');check(log.includes('oscillator->gain'));
  check(log.includes('gain->streamDestination'));
  check(log.indexOf('oscillator_start')<log.indexOf('addTrack'));
  check(log.indexOf('addTrack')<log.indexOf('offer'));
  check(env.window.__live.silentInput.active);
  const win={isSecureContext:true};win.top=win;
  class A {}; A.prototype.setSinkId=()=>{};
  const trustedEnv={window:win,location:{origin:'http://127.0.0.1'},AudioContext:A};
  const trusted=()=>Function(...Object.keys(trustedEnv),'return ('+code.trusted+')()')(...Object.values(trustedEnv));
  check(Object.values(trusted()).every(v=>v===true));
  trustedEnv.location.origin='https://foreign.invalid';check(!trusted().trusted_origin);
  trustedEnv.location.origin='http://127.0.0.1';win.top={};check(!trusted().trusted_origin);win.top=win;
  win.isSecureContext=false;check(!trusted().secure_context);win.isSecureContext=true;
  A.prototype.setSinkId=undefined;check(!trusted().set_sink_id_present);
  trustedEnv.AudioContext=undefined;check(!trusted().audio_context_present&&!trusted().set_sink_id_present);
  console.log(JSON.stringify({assertions,host:'controlled_node_graph_not_browser',
    models_called:0,sustained_frames:sustainedFrames}));
})().catch(e=>{console.error(e);process.exitCode=1;});
