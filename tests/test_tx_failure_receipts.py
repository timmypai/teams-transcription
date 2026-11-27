"""Diagnostic-only TX receipts at the real JS and Python policy boundaries.

Node supplies synthetic WebRTC objects, not real media or a Teams service.
"""
import json
import shutil
import subprocess
import unittest

from teams_transcription import b_browser_tx as tx
from teams_transcription.b_dual_policy import DUAL_CHECK_JS, OwnedSenderDualPolicy
from teams_transcription.diagnostics import response_start_failure, response_start_receipt, response_policy_failure_receipt
from teams_transcription.b_no_plc_history import NO_PLC_RECEIVER_JS


NODE = r"""
const fs=require('node:fs'),vm=require('node:vm');
const {scripts,mode}=JSON.parse(fs.readFileSync(0,'utf8'));
let guard,state,checks=0,replacements=0;
const track=()=>({kind:'audio',enabled:false,readyState:'live',id:'private-id',
  addEventListener(){},removeEventListener(){},stop(){this.readyState='ended';}});
const original=track(),output=track(),input=track();
const sender={track:original,async replaceTrack(t){this.track=t;replacements++;
  if(mode==='after_replace_track')state.control.readyState='closed';}};
const pc={connectionState:'connected',getSenders:()=>[sender],
  getTransceivers:()=>[{sender,stopped:false,currentDirection:'sendrecv',receiver:{track:input}}]};
const receiver={track:input};
const relay={connectionState:'connected',iceGatheringState:'complete',localDescription:{sdp:'synthetic'},
  getReceivers:()=>[receiver],getTransceivers:()=>[{receiver,setCodecPreferences(){}}],
  addEventListener(){},async setRemoteDescription(){this.ontrack({track:input,receiver});},
  async createAnswer(){return {};},async setLocalDescription(){},
  close(){this.connectionState='closed';}};
const capture={peers:[pc],createNativeOutputPeer:()=>relay};
const document={},location={href:'https://synthetic.invalid/private'};
const sandbox={document,location,performance:{now:()=>0},setInterval:f=>(guard=f,1),
  clearInterval(){},addEventListener(){},removeEventListener(){},console};
sandbox.window=sandbox;
sandbox.__meeting=capture;sandbox.__bMeetingId='one';
vm.createContext(sandbox);
const run=(name,args)=>vm.runInContext('('+scripts[name]+')',sandbox)(args);
(async()=>{
  await run('BIND_TX_JS',{owner:'owner',meetingId:'one',peerIndex:0,senderIndex:0});
  const binding=sandbox.__bTXBindings.get('owner');
  const decoder={receiver,inputTrack:input,track:output,ctx:{currentTime:1},valid(){
    checks++;
    if(mode==='final_verify' && checks===2)queueMicrotask(()=>state.control.readyState='closed');
    return true;
  },close(){}};
  state={control:{readyState:'open'}};
  // Real target ownership, guard and revoke closure; only decoder/WebRTC are fixtures.
  sandbox.createNoPlcReceiver=async()=>decoder;
  sandbox.RTCRtpReceiver={getCapabilities:()=>({codecs:[{mimeType:'audio/opus',clockRate:48000,channels:1}]})};
  await run('TARGET_ANSWER_JS',{owner:'owner',sdp:'synthetic'});
  state=sandbox.__bTX;checks=0;
  relay.ondatachannel({channel:{label:'native-output-owner',readyState:'open',close(){this.readyState='closed';}}});
  if(mode.startsWith('replacement_')) {
    await run('START_TX_JS',{owner:'owner'});
    const policyId='policy';
    await run('DUAL_CHECK_JS',{policyId,meetingId:'one',authorize:true,initialized:false,
      expected:{owner:'owner',meeting_id:'one',track_id:output.id},receipt:true});
    const name=mode.slice('replacement_'.length);
    const replacement=name==='null'?null:name==='original'?original:track();
    if(name==='other')replacement.kind='video';
    if(name==='ended')replacement.readyState='ended';
    if(name==='enabled')replacement.enabled=true;
    sender.track=replacement;guard();
    // A later mutation must not rewrite the event-time receipt.
    if(replacement) {replacement.enabled=false;replacement.readyState='ended';}
    const receipt=await run('DUAL_CHECK_JS',{policyId,meetingId:'one',authorize:false,initialized:true,receipt:true});
    await run('STOP_TX_JS',{owner:'owner'});
    process.stdout.write(JSON.stringify({receipt,ended:output.readyState==='ended',
      foreignPreserved:sender.track===replacement}));return;
  }
  if(mode==='before_replace_track')state.control.readyState='closed';
  if(mode==='fault_state_current')sandbox.__bTX={};
  if(mode==='fault_receiver_track_match')receiver.track=track();
  if(mode==='fault_relay_connected')relay.connectionState='disconnected';
  if(mode==='fault_decoder_valid')decoder.valid=()=>false;
  if(mode==='fault_source_invalidated')state.revoke('source_invalidated');
  if(mode==='fault_short_circuit') {
    sandbox.__bTX={};decoder.valid=()=>{throw new Error('must not be re-evaluated');};
  }
  let error=null;
  try{await run('START_TX_JS',{owner:'owner'});}catch(e){error=e.message;}
  const pre={muted:state.muted,enabled:output.enabled,replacements};
  await run('STOP_TX_JS',{owner:'owner'});
  process.stdout.write(JSON.stringify({error,pre,ended:output.readyState==='ended',
    restored:sender.track===original,remaining:!!sandbox.__bTX}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


def replay(mode):
    executable=shutil.which('node')
    if executable is None:
        raise RuntimeError('Node is required for the diagnostic acceptance fixture')
    result=subprocess.run([executable,'-e',NODE],input=json.dumps({'mode':mode,
        'scripts':{**{key:getattr(tx,key) for key in ('BIND_TX_JS','START_TX_JS','STOP_TX_JS')},
            'TARGET_ANSWER_JS':tx.TARGET_ANSWER_JS.replace(NO_PLC_RECEIVER_JS,''),
            'DUAL_CHECK_JS':DUAL_CHECK_JS}}),
        text=True,capture_output=True,timeout=10)
    if result.returncode:
        raise AssertionError(result.stderr)
    return json.loads(result.stdout)


class TXFailureReceiptTests(unittest.TestCase):
    def test_three_install_checkpoints_keep_mute_restore_and_first_error(self):
        for checkpoint in ('before_replace_track','after_replace_track','final_verify'):
            with self.subTest(checkpoint=checkpoint):
                actual=replay(checkpoint)
                receipt=response_start_failure(RuntimeError('Error: '+actual['error']),
                    'native_output_attach',output_step='sender_install')
                self.assertEqual(receipt.get('checkpoint'),checkpoint)
                self.assertEqual(receipt['first_failed_condition'],'control_open')
                self.assertIsNone(receipt['first_revoke_reason'])
                self.assertIsNone(receipt['source_failure_group'])
                self.assertEqual(receipt['output_error_code'],'target_not_ready')
                self.assertEqual(actual['pre'],{'muted':True,'enabled':False,
                    'replacements':0 if checkpoint=='before_replace_track' else 1})
                self.assertTrue(actual['ended'])
                self.assertTrue(actual['restored'])
                self.assertFalse(actual['remaining'])

    def test_healthy_install_keeps_initial_mute_and_has_no_failure(self):
        actual=replay('healthy')
        self.assertIsNone(actual['error'])
        self.assertEqual(actual['pre'],{'muted':True,'enabled':False,'replacements':1})
        self.assertTrue(actual['ended'])
        self.assertTrue(actual['restored'])

    def test_first_false_condition_is_not_a_cleanup_snapshot_or_recheck(self):
        for mode,condition,reason in [('state_current','state_current',None),
                ('receiver_track_match','receiver_track_match',None),
                ('relay_connected','relay_connected',None),('decoder_valid','decoder_valid',None),
                ('source_invalidated','not_disposed','source_invalidated'),
                ('short_circuit','state_current',None)]:
            with self.subTest(mode=mode):
                actual=replay('fault_'+mode)
                receipt=response_start_failure(RuntimeError('Error: '+actual['error']),
                    'native_output_attach',output_step='sender_install')
                self.assertIn('first_failed_condition',receipt)
                self.assertEqual(receipt['first_failed_condition'],condition)
                self.assertEqual(receipt['first_revoke_reason'],reason)
                self.assertEqual(receipt['checkpoint'],'before_replace_track')
                self.assertTrue(actual['ended'])

    def test_guard_replacement_is_captured_before_revoke_and_latched(self):
        for name,kind,enabled,ready in [('null',None,None,None),
                ('original','original',False,'live'),('audio','other_audio',False,'live'),
                ('enabled','other_audio',True,'live'),('ended','other_audio',False,'ended'),
                ('other','other',False,'live')]:
            with self.subTest(replacement=name):
                actual=replay('replacement_'+name)
                receipt=actual['receipt']
                self.assertFalse(receipt['accepted'])
                self.assertEqual(receipt['reason'],'owned_identity')
                self.assertEqual(receipt['detail'],'revoked_guard_sender_track_changed')
                self.assertEqual(receipt.get('replacement_track'),kind)
                self.assertEqual(receipt.get('replacement_enabled'),enabled)
                self.assertEqual(receipt.get('replacement_ready_state'),ready)
                self.assertTrue(actual['ended'])
                self.assertTrue(actual['foreignPreserved'])


SOURCE_NODE = r"""
const {scripts,mode}=JSON.parse(require('node:fs').readFileSync(0,'utf8'));
const vm=require('node:vm');let guard;
const track=()=>({kind:'audio',enabled:true,readyState:'live',addEventListener(){},removeEventListener(){}});
const input=track(),output=track(),receiver={track:input};
const pc={remoteDescription:{},connectionState:'connected',getReceivers:()=>[receiver]};
const decoder={track:output,receiver,valid:()=>true,closing:Promise.resolve(),stats:()=>({input:{frames:0}})};
const live={pc,noPlc:decoder};
const control={readyState:'open',addEventListener(){},send(){},close(){this.readyState='closed';}};
const relay={connectionState:'connected',iceGatheringState:'complete',localDescription:{sdp:'synthetic'},
  createDataChannel:()=>control,addTrack(){},addEventListener(){},
  async setLocalDescription(){},async createOffer(){return {};},close(){this.connectionState='closed';}};
const sandbox={document:{},location:{href:'synthetic'},__live:live,RTCPeerConnection:function(){return relay;},
  MediaStream:function(){},addEventListener(){},removeEventListener(){},
  setInterval:f=>(guard=f,1),clearInterval(){}};sandbox.window=sandbox;
vm.createContext(sandbox);
const run=name=>vm.runInContext('('+scripts[name]+')',sandbox)({owner:'owner'});
(async()=>{
  await run('SOURCE_OFFER_JS');
  if(mode==='receiver_track')input.enabled=false;
  if(mode==='decoder')decoder.valid=()=>false;
  if(mode==='source_peer')pc.connectionState='failed';
  if(mode==='source_ownership')sandbox.__live={pc};
  if(mode==='source_document')sandbox.location.href='new';
  if(mode==='source_relay_control')control.readyState='closed';
  if(mode==='source_relay_control') {try{run('SOURCE_CHECK_JS');}catch{}}
  else if(mode!=='healthy')guard();
  const first=await run('SOURCE_STOP_JS');
  process.stdout.write(JSON.stringify({first,disposed:sandbox.__bNativeTXSource===undefined,
    relayClosed:relay.connectionState==='closed',controlClosed:control.readyState==='closed'}));
})().catch(e=>{console.error(e);process.exitCode=1;});
"""


class SourceFailureGroupTests(unittest.TestCase):
    def test_source_records_only_observed_first_failure_before_cleanup(self):
        for group in ('source_ownership','source_document','source_peer','receiver_track',
                'decoder','source_relay_control','healthy'):
            with self.subTest(group=group):
                result=subprocess.run(['node','-e',SOURCE_NODE],input=json.dumps({'mode':group,
                    'scripts':{key:getattr(tx,key) for key in ('SOURCE_OFFER_JS','SOURCE_CHECK_JS','SOURCE_STOP_JS')}}),
                    text=True,capture_output=True,timeout=10)
                self.assertEqual(result.returncode,0,result.stderr)
                actual=json.loads(result.stdout)
                self.assertEqual(actual.get('first'),{'source_failure_group':None if group=='healthy' else group})
                self.assertTrue(actual['disposed'])
                self.assertTrue(actual['relayClosed'])
                self.assertTrue(actual['controlClosed'])


class ReceiptBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_policy_receipt_crosses_python_and_persistence_allowlists(self):
        for name in ('null','original','enabled','ended','other'):
            with self.subTest(name=name):
                wire=replay('replacement_'+name)['receipt']
                class Frame:
                    async def evaluate(self,*args):return wire
                policy=OwnedSenderDualPolicy(Frame(),'one',clock=lambda:0)
                self.assertFalse(await policy.check())
                snapshot=policy.failure_snapshot()
                self.assertEqual(snapshot['reason'],'owned_identity')
                self.assertEqual(snapshot['detail'],'revoked_guard_sender_track_changed')
                safe=response_policy_failure_receipt(snapshot)
                for key in ('replacement_track','replacement_enabled','replacement_ready_state'):
                    self.assertIn(key,safe)
                    self.assertEqual(safe[key],wire[key])
                snapshot['replacement_track']='private-content'
                self.assertNotEqual(policy.failure_snapshot()['replacement_track'],'private-content')

    async def test_policy_rejects_non_allowlisted_extra_and_malformed_values(self):
        base={'accepted':False,'reason':'owned_identity','detail':'revoked_guard_sender_track_changed',
            'replacement_track':'other_audio','replacement_enabled':True,'replacement_ready_state':'live'}
        for extra in ({'replacement_track':'https://private.invalid'}, {'replacement_enabled':1},
                {'replacement_ready_state':['live']}, {'sdp':'private-media'}, {'label':'private'},
                {'detail':'sender_track_changed'}, {'accepted':True,'reason':'none'}):
            with self.subTest(extra=extra):
                class Frame:
                    async def evaluate(self,*args):return {**base,**extra}
                policy=OwnedSenderDualPolicy(Frame(),'one',clock=lambda:0)
                self.assertFalse(await policy.check())
                self.assertEqual(policy.failure_snapshot()['reason'],'invalid_result')
                self.assertNotIn('replacement_track',policy.failure_snapshot())
        unsafe={'reason':'owned_identity','detail':'revoked_guard_sender_track_changed',
            'replacement_track':['private'],'replacement_enabled':'true','replacement_ready_state':{},
            'sdp':'private','label':'private'}
        safe=response_policy_failure_receipt(unsafe)
        self.assertEqual({k:v for k,v in safe.items() if k.startswith('replacement_')},
            {'replacement_track':None,'replacement_enabled':None,'replacement_ready_state':None})
        self.assertNotIn('private',json.dumps(safe))
        safe=response_policy_failure_receipt({**unsafe,'detail':'sender_track_changed'})
        self.assertFalse(any(k.startswith('replacement_') for k in safe))

    async def test_start_preserves_target_error_and_only_uses_owned_latched_source_group(self):
        for reason,source_group,expected in [('source_invalidated','decoder','decoder'),
                ('source_disconnected','source_relay_control','source_relay_control'),
                ('relay_disconnected',None,None),('invalidated','decoder',None),
                ('source_invalidated','private-content',None)]:
            with self.subTest(reason=reason,source_group=source_group):
                class Page:
                    def __init__(self,target):self.target=target;self.context=object();self.calls=[]
                    def is_closed(self):return False
                    async def evaluate(self,script,args=None):
                        self.calls.append(script)
                        if script==tx.BIND_TX_JS:return {'owner':args['owner'],'meeting_id':'one'}
                        if script==tx.START_TX_JS:
                            raise RuntimeError('Error: Native TX disconnected or ownership/meeting binding mismatch'
                                '/tx_ready/before_replace_track/not_disposed/'+reason)
                        if script==tx.STOP_TX_JS:return {'cleaned':True}
                        if script==tx.SOURCE_STOP_JS:return {'source_failure_group':source_group}
                        return 'synthetic' if script in (tx.SOURCE_OFFER_JS,tx.TARGET_ANSWER_JS) else {}
                target,source=Page(True),Page(False)
                sink=tx.BrowserTXSink(target,meeting_id='one')
                await sink.bind();await sink.attach_live(source)
                with self.assertRaises(RuntimeError):await sink.start()
                self.assertIn('first_revoke_reason',sink.startup_error)
                self.assertEqual(sink.startup_error['first_revoke_reason'],reason)
                self.assertEqual(sink.startup_error['source_failure_group'],expected)
                self.assertEqual(sink.cleanup_status,'cleaned')
                self.assertFalse(sink.started)
                self.assertEqual(source.calls.count(tx.SOURCE_STOP_JS),1)
                self.assertEqual(target.calls.count(tx.STOP_TX_JS),1)
                self.assertEqual(response_start_receipt(sink.startup_error),sink.startup_error)

    def test_startup_diagnostics_drop_unapproved_values_and_wrong_scope(self):
        base={'phase':'native_output_attach','output_step':'sender_install',
            'output_error_code':'target_not_ready','exception_type':'Error',
            'checkpoint':'private','first_failed_condition':[], 'first_revoke_reason':{},
            'source_failure_group':'private','media':'private'}
        safe=response_start_receipt(base)
        for key in ('checkpoint','first_failed_condition','first_revoke_reason','source_failure_group'):
            self.assertIn(key,safe)
            self.assertIsNone(safe[key])
        self.assertNotIn('private',json.dumps(safe))
        for changed in ({'phase':'ready'},{'output_step':'target_ready'},{'output_error_code':'source_disconnected'}):
            safe=response_start_receipt({**base,**changed})
            self.assertNotIn('checkpoint',safe)
        # Unknown exception text is not a diagnostic protocol and is never persisted.
        for suffix in ('before_replace_track/control_open/private','before_replace_track/private/none',
                'private/control_open/none','before_replace_track/control_open/none/private'):
            error=RuntimeError('Error: Native TX disconnected or ownership/meeting binding mismatch/tx_ready/'+suffix)
            safe=response_start_failure(error,'native_output_attach',output_step='sender_install')
            self.assertNotIn('checkpoint',safe)
            self.assertNotIn('private',json.dumps(safe))
