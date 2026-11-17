"""Offline tests of the explicit versioned initializer; not real WebAudio."""
import json
import re
import subprocess
import unittest

from teams_transcription.b_no_plc import NO_PLC_RECEIVER_JS
from teams_transcription.b_no_plc_startup import STARTUP_HELPER_JS, with_startup_primer


class StartupInitializerTests(unittest.TestCase):
    def node(self, code):
        r=subprocess.run(['node','-e',STARTUP_HELPER_JS+code],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr[-2000:])
        return json.loads(r.stdout)

    def test_actual_format_is_primed_once_and_real_timestamp_is_unchanged(self):
        result=self.node(r'''
let closed=0;global.AudioData=class{constructor(c){this.config=c;}close(){closed++;}};
(async()=>{const rows=[];for(const [channels,frames] of [[1,120],[2,960],[1,2880]]){
 let writes=[],gate;
 const meter={port:{postMessage:d=>queueMicrotask(()=>gate.accept({kind:d.kind,frame:d.target}))}};
 gate=createNoPlcStartup({ready:Promise.resolve(),write:async a=>{writes.push(a.config);}},
 {currentTime:.3},meter,()=>{});
 const original={sampleRate:48000,numberOfChannels:channels,numberOfFrames:frames,timestamp:0};
 await gate.prepare(original);await gate.prepare(original);gate.close();
 rows.push({channels,frames,writes:writes.length,silent:writes[0].data.every(x=>x===0),
  primerTimestamp:writes[0].timestamp,realTimestamp:original.timestamp,receipt:gate.receipt});
}console.log(JSON.stringify({rows,closed}));})();''')
        self.assertEqual(result['closed'],3)
        for row in result['rows']:
            self.assertEqual(row['writes'],1)
            self.assertTrue(row['silent'])
            self.assertEqual(row['realTimestamp'],0)
            self.assertEqual(row['primerTimestamp'],-row['frames']*1000000//48000)
            self.assertEqual(row['receipt']['status'],'render_checkpoint_observed')

    def test_close_during_pending_write_aborts_and_closes_primer(self):
        result=self.node(r'''
let closed=false;global.AudioData=class{close(){closed=true;}};
(async()=>{let gate;
 const writer={ready:Promise.resolve(),write:()=>new Promise(()=>{queueMicrotask(()=>gate.close());})};
 gate=createNoPlcStartup(writer,{currentTime:.1},{port:{postMessage:()=>{throw Error('late_pull');}}},()=>{});
 try{await gate.prepare({sampleRate:48000,numberOfChannels:2,numberOfFrames:960,timestamp:0});}
 catch(e){console.log(JSON.stringify({error:e.message,closed,status:gate.receipt.status}));}})();''')
        self.assertEqual(result,{'error':'b_no_plc_startup_closed','closed':True,'status':'failed'})

    def test_timeout_rejects_pending_checkpoint_without_extending_deadline(self):
        result=self.node(r'''
let fire,cleared=0,closed=0;global.setTimeout=fn=>{fire=fn;return 1;};
global.clearTimeout=()=>{cleared++;};global.AudioData=class{close(){closed++;}};
(async()=>{const gate=createNoPlcStartup({ready:Promise.resolve(),write:async()=>{}},
 {currentTime:.1},{port:{postMessage:()=>queueMicrotask(()=>fire())}},()=>{});
 try{await gate.prepare({sampleRate:48000,numberOfChannels:1,numberOfFrames:480,timestamp:0});}
 catch(e){console.log(JSON.stringify({error:e.message,closed,cleared,status:gate.receipt.status}));}})();''')
        self.assertEqual(result['error'],'b_no_plc_startup_timeout')
        self.assertEqual(result['closed'],1)
        self.assertEqual(result['status'],'failed')
        self.assertGreaterEqual(result['cleared'],1)

    def test_close_during_checkpoint_and_late_reply_cannot_complete_initialization(self):
        result=self.node(r'''
let closed=0;global.AudioData=class{close(){closed++;}};
(async()=>{let gate;
 gate=createNoPlcStartup({ready:Promise.resolve(),write:async()=>{}},{currentTime:.1},
 {port:{postMessage:d=>queueMicrotask(()=>{gate.close();gate.accept({kind:d.kind,frame:d.target});})}},()=>{});
 try{await gate.prepare({sampleRate:48000,numberOfChannels:2,numberOfFrames:960,timestamp:0});}
 catch(e){console.log(JSON.stringify({error:e.message,closed,status:gate.receipt.status}));}})();
''')
        self.assertEqual(result,{'error':'b_no_plc_startup_closed','closed':1,'status':'failed'})

    def test_invalid_format_and_stop_before_prepare_cannot_write(self):
        result=self.node(r'''
let allocations=0,writes=0;global.AudioData=class{constructor(){allocations++;}};
(async()=>{const gate=createNoPlcStartup({write:()=>{writes++;}},{currentTime:0},{},()=>{}),errors=[];
 try{await gate.prepare({sampleRate:44100,numberOfChannels:1,numberOfFrames:960,timestamp:0});}
 catch(e){errors.push(e.message);}
 gate.close();try{await gate.prepare({sampleRate:48000,numberOfChannels:1,numberOfFrames:960,timestamp:0});}
 catch(e){errors.push(e.message);}
 console.log(JSON.stringify({errors,allocations,writes}));})();
''')
        self.assertEqual(result,{'errors':['b_no_plc_startup_format','b_no_plc_startup_closed'],
            'allocations':0,'writes':0})

    def test_versioned_source_and_policy_target_select_their_initializers(self):
        from teams_transcription.b_no_plc_startup import NO_PLC_RECEIVER_JS as current
        from teams_transcription.b_no_plc_history import NO_PLC_RECEIVER_JS as history
        from teams_transcription.b_media_profile import NO_PLC_PEER_JS
        from teams_transcription.b_browser_tx import TARGET_ANSWER_JS
        self.assertEqual(NO_PLC_PEER_JS.count(current),1)
        self.assertEqual(TARGET_ANSWER_JS.count(history),1)
        self.assertEqual(TARGET_ANSWER_JS.count(current),0)
        self.assertIn('await startup.prepare(audio);requireOpen();',current)
        self.assertNotIn('await startup.prepare(audio);requireOpen();',NO_PLC_RECEIVER_JS)

    def test_versioned_source_refuses_drift_and_preserves_legacy(self):
        fixed=with_startup_primer(NO_PLC_RECEIVER_JS)
        self.assertNotIn('startup.prepare(audio)',NO_PLC_RECEIVER_JS)
        self.assertIn('await startup.prepare(audio);requireOpen();',fixed)
        scripts=[fixed]+re.findall(r'new Blob\(\[`(.*?)`',fixed,flags=re.S)
        r=subprocess.run(['node','-e','for(const s of '+json.dumps(scripts)+')new Function(s);'],capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr[-1000:])
        with self.assertRaisesRegex(ValueError,'startup_receiver_source_drift'):
            with_startup_primer('changed receiver')
