"""Synthetic-only tool seams; Node mocks do not prove browser audio behavior."""
import json
from pathlib import Path
import subprocess
import unittest

from teams_transcription.b_no_plc_startup import NO_PLC_RECEIVER_JS
from tools.decoder_offline_check import audio_case_safe, case_verified, native_meter_candidate


HELPER = Path(__file__).resolve().parents[1] / 'tools/decoder_native_meter_candidate.js'
FIXTURE = HELPER.with_name('decoder_audio_check.js')


class OfflineDecoderCandidateTests(unittest.TestCase):
    def node(self, code):
        result = subprocess.run(['node', '--unhandled-rejections=strict', '-e',
            HELPER.read_text() + code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout)

    def test_constructed_decoder_with_failed_cleanup_or_extra_request_cannot_pass(self):
        case = {'expected_result_matched': True, 'setup_passed': True,
            'intercepted_requests': 1, 'cleanup': {'receiver_ended': True,
                'context_closed': True, 'decoder_invalid': True}}
        self.assertTrue(case_verified(case))
        for key in case['cleanup']:
            broken = {**case, 'cleanup': {**case['cleanup'], key: False}}
            self.assertFalse(case_verified(broken), key)
        self.assertFalse(case_verified({**case, 'intercepted_requests': 2}))

    def test_audio_experiment_cannot_complete_with_native_writes_or_incomplete_cleanup(self):
        case = {'mode': 'mono', 'phase': 'complete', 'native_writes': 0,
            'intercepted_requests': 1, 'cleanup': dict.fromkeys(('input_track_ended',
                'decoder_invalid', 'repeat_close_same_promise', 'stream_cancelled', 'context_closed'), True)}
        self.assertTrue(audio_case_safe(case))
        self.assertFalse(audio_case_safe({**case, 'native_writes': 1}))
        for key in case['cleanup']:
            self.assertFalse(audio_case_safe({**case, 'cleanup': {**case['cleanup'], key: False}}), key)

    def test_full_packet_comparison_rejects_single_missing_stereo_sample_and_incomplete_window(self):
        result = self.node(FIXTURE.read_text() + r'''
const ref=new Float32Array(960).fill(.1),right=new Float32Array(960).fill(-.2);
const capture=ref.slice(),captureRight=right.slice();
const good=compareSyntheticPacket(ref,right,capture,captureRight,0,0,960,960);
captureRight[900]=0;
const anchorUnchanged=captureRight.subarray(512,768).every((v,i)=>v===right[i+512]);
const bad=compareSyntheticPacket(ref,right,capture,captureRight,0,0,960,960);
const missing=compareSyntheticPacket(ref,right,capture,captureRight,1,0,960,960);
console.log(JSON.stringify({good,bad,missing,anchorUnchanged}));''')
        self.assertTrue(result['good']['verified'])
        self.assertTrue(result['anchorUnchanged'])
        self.assertFalse(result['bad']['verified'])
        self.assertEqual(result['bad']['mismatched_samples'], 1)
        self.assertFalse(result['missing']['verified'])
        self.assertIsNone(result['missing']['max_abs_error'])

    def test_native_capture_starts_after_silent_write_and_keeps_real_timestamp(self):
        result = self.node(r'''
global.AudioData=class{constructor(c){this.config=c;}close(){this.closed=true;}};
(async()=>{let written,closed=false;const order=[];
 const gate=createOfflineNativeStartup({ready:Promise.resolve(),write:async a=>{
   order.push('write');written=a;await Promise.resolve();order.push('write_done');}}, {},
 {afterWritePull:async()=>{order.push('capture');return {captured_frames:480,timestamp_us:12345};}},()=>{});
 const real={sampleRate:48000,numberOfChannels:2,numberOfFrames:960,timestamp:70000};
 await gate.prepare(real);await gate.prepare(real);gate.close();
 console.log(JSON.stringify({order,realTimestamp:real.timestamp,
   primer:written.config.timestamp,silent:written.config.data.every(x=>x===0),
   closed:written.closed,receipt:gate.receipt}));})();''')
        self.assertEqual(result['order'], ['write', 'write_done', 'capture'])
        self.assertEqual(result['realTimestamp'], 70000)
        self.assertEqual(result['primer'], 50000)
        self.assertTrue(result['silent'] and result['closed'])
        self.assertEqual(result['receipt']['status'], 'native_capture_observed')
        self.assertEqual(result['receipt']['after_write_capture_timestamp_us'], 12345)
        self.assertNotIn('pull_checkpoint_frame', result['receipt'])

    def test_timeout_preserves_failure_and_closes_primer(self):
        result = self.node(r'''
let fire,closed=0;global.setTimeout=fn=>{fire=fn;return 1;};global.clearTimeout=()=>{};
global.AudioData=class{close(){closed++;}};
(async()=>{const gate=createOfflineNativeStartup({ready:Promise.resolve(),write:async()=>{}},{},
 {afterWritePull:()=>{queueMicrotask(()=>fire());return new Promise(()=>{});}},()=>{});
 try{await gate.prepare({sampleRate:48000,numberOfChannels:1,numberOfFrames:480,timestamp:0});}
 catch(e){console.log(JSON.stringify({error:e.message,closed,status:gate.receipt.status}));}})();''')
        self.assertEqual(result, {'error': 'offline_startup_timeout', 'closed': 1, 'status': 'failed'})

    def test_close_during_write_prevents_capture_and_late_completion(self):
        result = self.node(r'''
let closed=0;global.AudioData=class{close(){closed++;}};
(async()=>{let gate,capture=0;
 gate=createOfflineNativeStartup({ready:Promise.resolve(),write:()=>new Promise(()=>{
   queueMicrotask(()=>gate.close());})},{},{afterWritePull:()=>{capture++;}},()=>{});
 try{await gate.prepare({sampleRate:48000,numberOfChannels:1,numberOfFrames:480,timestamp:0});}
 catch(e){console.log(JSON.stringify({error:e.message,closed,capture,status:gate.receipt.status}));}})();''')
        self.assertEqual(result, {'error': 'offline_startup_closed', 'closed': 1,
            'capture': 0, 'status': 'failed'})

    def test_processor_constructor_failure_has_no_unhandled_rejection_and_stops_clone(self):
        result = self.node(r'''
let stopped=0;global.MediaStreamTrackProcessor=class{constructor(){throw Error('fixture_constructor');}};
const meter=createOfflineNativeMeter({createGain:()=>({})},{clone:()=>({stop(){stopped++;}})},()=>{});
let caught;try{meter.afterWritePull();}catch(e){caught=e.message;}
meter.port.close();Promise.resolve(meter.closing).then(()=>setImmediate(()=>{
 console.log(JSON.stringify({caught,stopped}));}));''')
        self.assertEqual(result, {'caught': 'fixture_constructor', 'stopped': 1})

    def test_candidate_refuses_source_drift_and_does_not_change_app_literal(self):
        original = NO_PLC_RECEIVER_JS
        candidate = native_meter_candidate(original)
        self.assertEqual(NO_PLC_RECEIVER_JS, original)
        self.assertNotIn("setupStep='worklet_module'", candidate)
        self.assertIn("setupStep='worklet_module'", original)
        with self.assertRaisesRegex(ValueError, 'source_drift'):
            native_meter_candidate(original.replace("setupStep='worklet_url'", 'changed'))

    def test_actual_sample_counters_and_owned_cleanup_for_mono_and_stereo(self):
        result = self.node(r'''
(async()=>{const rows=[];for(const channels of [1,2]){
 let delivered=false,finish,framesClosed=0,cloneStopped=0,released=0,failed=0;
 const audio={sampleRate:48000,numberOfChannels:channels,numberOfFrames:480,timestamp:20000,
   copyTo(buffer){buffer.fill(.5,0,480);},close(){framesClosed++;}};
 global.MediaStreamTrackProcessor=class{constructor(){this.readable={getReader:()=>({
   read:()=>{if(!delivered){delivered=true;return Promise.resolve({value:audio,done:false});}
     return new Promise(resolve=>{finish=resolve;});},
   cancel:async()=>finish?.({done:true}),releaseLock(){released++;}})};}};
 const meter=createOfflineNativeMeter({createGain:()=>({})},
   {clone:()=>({stop(){cloneStopped++;}})},()=>failed++);
 const pull=await meter.afterWritePull();
 const sample=await new Promise(resolve=>{meter.port.onmessage=e=>resolve(e.data);
   meter.port.postMessage('sample');});
 meter.port.close();await meter.closing;
 rows.push({channels,pull,sample,framesClosed,cloneStopped,released,failed});
}console.log(JSON.stringify(rows));})();''')
        for row in result:
            self.assertEqual(row['pull'], {'captured_frames': 480, 'timestamp_us': 20000})
            self.assertEqual(row['sample']['frames'], 480)
            self.assertAlmostEqual(row['sample']['duration'], .01)
            self.assertAlmostEqual(row['sample']['energy'], .0025)
            self.assertEqual([row[k] for k in ('framesClosed', 'cloneStopped', 'released', 'failed')],
                [1, 1, 1, 0])


if __name__ == '__main__':
    unittest.main()
