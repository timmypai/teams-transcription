"""First decoder setup cause survives cleanup; no model, network or media claims."""
import json
import subprocess
import unittest

from teams_transcription.b_no_plc_startup import NO_PLC_RECEIVER_JS


class DecoderSetupFailureTests(unittest.TestCase):
    def test_actual_receiver_retains_fixed_stage_type_and_first_reason(self):
        harness=r'''
const assert=require('node:assert/strict');
const script=JSON.parse(process.argv[1]);
eval(script);
function track(){return {kind:'audio',readyState:'live',enabled:true,
 addEventListener(){},removeEventListener(){},stop(){this.readyState='ended';},clone(){return track();}};}
(async()=>{
 for(const failure of ['browser_api','context_resume','worklet_module']){
  const input=track(),contexts=[];let cancelled=0,aborted=0;
  const reader={async cancel(){cancelled++;},releaseLock(){}};
  const writer={async abort(){aborted++;},releaseLock(){}};
  const receiver={track:input,createEncodedStreams(){return {
    readable:{getReader:()=>reader},writable:{getWriter:()=>writer}};}};
  const node=stream=>({mediaStream:stream,connect(){},disconnect(){}});
  global.MediaStream=class{constructor(tracks){this.tracks=tracks;}getAudioTracks(){return this.tracks;}};
  global.MediaStreamTrackGenerator=class{constructor(){Object.assign(this,track());
    this.writable={getWriter:()=>writer};}};
  global.AudioWorkletNode=function(){};global.EncodedAudioChunk=function(){};
  global.AudioDecoder=failure==='browser_api'?undefined:function(){};
  global.AudioContext=class{
    constructor(){this.sampleRate=48000;this.state='running';contexts.push(this);
      this.audioWorklet={async addModule(){const e=Error('PRIVATE-WORKLET-URL');e.name='AbortError';throw e;}};}
    async resume(){if(failure==='context_resume'){
      const e=Error('PRIVATE-DEVICE-PATH');e.name='NotSupportedError';throw e;}}
    createMediaStreamSource(stream){return node(stream);}
    createMediaStreamDestination(){return {...node(),stream:new MediaStream([track()])};}
    async close(){this.state='closed';}
  };
  let caught;const callback=[];
  try{await createNoPlcReceiver(receiver,e=>callback.push(e.message));}catch(e){caught=e;}
  assert.ok(caught);assert.equal(input.readyState,'ended');
  const expected={stage:failure,exception_type:failure==='browser_api'?'Error':
    failure==='context_resume'?'NotSupportedError':'AbortError',
    reason:failure==='browser_api'?'api_unsupported':'unknown'};
  const {environment,...detail}=caught.bNoPlcSetup;
  assert.deepEqual(detail,expected);
  assert.deepEqual(environment,{secure_context:null,cross_origin_isolated:null,meta_csp_present:null,receiver_encoded_streams:true,
    audio_decoder:failure!=='browser_api',encoded_audio_chunk:true,track_generator:true,
    audio_worklet_node:true,audio_context:true,context_audio_worklet:failure==='browser_api'?null:true,
    context_state:failure==='browser_api'?'unknown':'running',sample_rate:failure==='browser_api'?null:48000});
  assert.ok(!JSON.stringify(caught.bNoPlcSetup).includes('PRIVATE'));
  assert.ok(contexts.every(c=>c.state==='closed'));
  assert.equal(cancelled,failure==='browser_api'?0:1);
  assert.equal(aborted,failure==='browser_api'?0:failure==='context_resume'?1:2);
  assert.deepEqual(callback,[failure==='browser_api'?'b_no_plc_api_unsupported':'b_no_plc_setup_failed']);
 }
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        result=subprocess.run(['node','-e',harness,json.dumps(NO_PLC_RECEIVER_JS)],
            capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr[-3000:])
