"""Offline measurement contracts; real browser evidence is a separate command."""
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
from decoder_continuity_check import instrument_quality, BASELINE_RECEIVER_JS
from teams_transcription.b_no_plc_history import NO_PLC_RECEIVER_JS


class DecoderContinuityTests(unittest.TestCase):
    def node(self, code):
        result = subprocess.run(['node', '-e',
            (ROOT / 'tools/decoder_audio_check.js').read_text() + code],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout)

    def test_full_timeline_requires_unique_exact_overlap_and_preserves_every_sample(self):
        value = self.node('''
const t=createSyntheticTimeline(40000),a=Float32Array.from({length:32768},(_,i)=>(i+1)/100000);
t.ingest(a,40000,100);const b=new Float32Array(32768);b.set(a.subarray(256));
for(let i=0;i<256;i++)b[32768-256+i]=(32769+i)/100000;
t.ingest(b,40256,106);const valid=t.summary(),first=t.pcm[0],last=t.pcm[33023];
b[100]+=1;t.ingest(b,40512,112);const invalid=t.summary();t.close();
console.log(JSON.stringify({valid,invalid,first,last,cleared:t.pcm.every(x=>x===0)}));''')
        self.assertTrue(value['valid']['coverage_verified'])
        self.assertEqual(value['valid']['frames'], 33024)
        self.assertAlmostEqual(value['first'], .00001)
        self.assertAlmostEqual(value['last'], .33024)
        self.assertFalse(value['invalid']['coverage_verified'])
        self.assertEqual(value['invalid']['unknown_reason'], 'unmatched_overlap')
        self.assertTrue(value['cleared'])

    def test_full_reference_distinguishes_extra_zeros_from_missing_payload_and_unknown(self):
        value = self.node('''
const ref=Float32Array.from({length:1920},(_,i)=>(i+1)/10000);
const run=(gap,known=true)=>{
 const data=new Float32Array(2048+gap);data.set(ref.subarray(0,960),32);
 data.set(ref.subarray(960),992+gap);
 return analyseSyntheticTimeline({pcm:data,rows:[{end:data.length,wall_ms:100}],
 summary:()=>({coverage_verified:known,frames:data.length})},ref,960,
 [{index:0,at_ms:0},{index:1,at_ms:20}],0);
};console.log(JSON.stringify({exact:run(0),gap:run(128),unknown:run(0,false)}));''')
        self.assertTrue(value['exact']['all_payload_samples_verified'])
        self.assertEqual(value['exact']['outside_payload_max_abs'], 0)
        self.assertFalse(value['gap']['all_payload_samples_verified'])
        self.assertEqual(value['gap']['matched_packets'], 2)
        self.assertEqual(value['gap']['missing_packets'], [])
        self.assertEqual(value['gap']['inter_packet_gaps'],
            [{'after_packet': 0, 'frames': 128, 'all_zero': True}])
        self.assertIsNone(value['unknown']['all_payload_samples_verified'])
        self.assertIsNone(value['unknown']['missing_packets'])

    def test_render_clock_evidence_maps_identified_samples_without_guessing_missing_clock(self):
        value = self.node('''
const reference=Float32Array.from({length:1920},(_,i)=>(i+1)/4096);
const pcm=new Float32Array(2048);pcm.set(reference,128);
const result=analyseSyntheticTimeline({pcm,rows:[{end:2048,wall_ms:110,clock_frame:3000}],
 summary:()=>({coverage_verified:true,frames:2048})},reference,960,
 [{index:0,at_ms:0},{index:1,at_ms:20}],50);
console.log(JSON.stringify(result.packet_clocks));''')
        self.assertEqual(value, [
            {'packet': 0, 'render_frame': 128, 'observed_ms': 110, 'arrival_ms': 50, 'context_frame': 1080},
            {'packet': 1, 'render_frame': 1088, 'observed_ms': 110, 'arrival_ms': 70, 'context_frame': 2040}])

    def test_full_reference_search_honours_tolerance_and_rejects_nonfinite_samples(self):
        value = self.node('''
const ref=Float32Array.from({length:1920},(_,i)=>(i+1)/10000);
const run=invalid=>{
 const pcm=ref.slice();pcm[512]+=5e-7;if(invalid)pcm[1000]=NaN;
 return analyseSyntheticTimeline({pcm,rows:[{end:1920,wall_ms:100}],
 summary:()=>({coverage_verified:true,frames:1920})},ref,960,
 [{index:0,at_ms:0},{index:1,at_ms:20}],0);
};console.log(JSON.stringify({tolerance:run(false),invalid:run(true)}));''')
        self.assertTrue(value['tolerance']['all_payload_samples_verified'])
        self.assertEqual(value['tolerance']['missing_packets'], [])
        self.assertIsNone(value['invalid']['all_payload_samples_verified'])
        self.assertEqual(value['invalid']['unknown_reason'], 'nonfinite_samples')

    def test_exact_packet_search_catches_non_grid_offset_and_rejects_partial_or_wrong_lane(self):
        value = self.node('''
const ref=new Float32Array([.1,.2,.3,.4]),right=new Float32Array([-.1,-.2,-.3,-.4]);
const pcm=new Float32Array(20),r=new Float32Array(20);pcm.set(ref,7);r.set(right,7);
const exact=findFullSyntheticPacket(ref,right,pcm,r,0,4,20);
r[9]=0;const wrongLane=findFullSyntheticPacket(ref,right,pcm,r,0,4,20);
pcm[10]=0;const partial=findFullSyntheticPacket(ref,null,pcm,null,0,4,20);
console.log(JSON.stringify({exact,wrongLane,partial}));''')
        self.assertEqual(value, {'exact': 7, 'wrongLane': None, 'partial': None})

    def test_known_delivery_counterexamples_and_bounded_invalid_mode(self):
        value = self.node('''
let invalid=false;try{syntheticDeliveryPlan('arbitrary',16);}catch{invalid=true;}
const long=syntheticDeliveryPlan('long_paced',500),jitter=syntheticDeliveryPlan('long_jitter',500);
const beta=syntheticDeliveryPlan('beta_long',1500);
const paced=syntheticDeliveryPlan('paced',16),burst=syntheticDeliveryPlan('burst',16),
reordered=syntheticDeliveryPlan('reordered',16);
console.log(JSON.stringify({invalid,betaLast:beta[1499],longLast:long[499],jitterLast:jitter[499],paced:paced.slice(0,4),burst:burst.slice(0,4),
order:reordered.map(x=>x.index),counts:[paced.length,burst.length,reordered.length]}));''')
        self.assertTrue(value['invalid'])
        self.assertEqual(value['betaLast'], {'index': 1499, 'at_ms': 29980})
        self.assertEqual(value['longLast'], {'index': 499, 'at_ms': 9980})
        self.assertEqual(value['jitterLast'], {'index': 499, 'at_ms': 9967})
        self.assertEqual(value['counts'], [16, 16, 16])
        self.assertEqual([x['at_ms'] for x in value['paced']], [0, 20, 40, 60])
        self.assertEqual([x['at_ms'] for x in value['burst']], [0, 0, 0, 60])
        self.assertEqual(value['order'], [0, 2, 1] + list(range(3, 16)))

    def test_instrumentation_refuses_source_drift_or_unrecognized_experiment(self):
        with self.assertRaisesRegex(ValueError, 'source_drift'):
            instrument_quality(BASELINE_RECEIVER_JS.replace('await writer.ready;', ''), 'paced')
        with self.assertRaisesRegex(ValueError, 'delivery_plan'):
            instrument_quality(NO_PLC_RECEIVER_JS, 'arbitrary')
        with self.assertRaisesRegex(ValueError, 'playout_candidate'):
            instrument_quality(NO_PLC_RECEIVER_JS, 'paced', 'arbitrary')
        for mode in ('paced', 'burst', 'reordered', 'writer_stall'):
            instrument_quality(BASELINE_RECEIVER_JS, mode)
            # Parse actual candidate source; browser A/B establishes its audio
            # behavior separately, without treating a Node mock as playback.
            result = subprocess.run(['node', '--check'],
                input=instrument_quality(BASELINE_RECEIVER_JS, mode, 'clock_lead_40ms'),
                text=True, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr[-2000:])
            production = subprocess.run(['node', '--check'],
                input=instrument_quality(NO_PLC_RECEIVER_JS, mode, 'production'),
                text=True, capture_output=True, timeout=10)
            self.assertEqual(production.returncode, 0, production.stderr[-2000:])
