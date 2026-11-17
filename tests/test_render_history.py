"""Deterministic history/lease evidence checks; browser proof lives in evidence."""
import json
import re
import subprocess
import unittest

from teams_transcription.b_render_history import HISTORY_METER_JS
from teams_transcription.b_no_plc_history import with_render_history
from teams_transcription.b_no_plc_startup import NO_PLC_RECEIVER_JS
from teams_transcription.diagnostics import response_progress_diagnostic, render_history_diagnostic


FAKE_CONTEXT = r"""
const analysers=[],signals=[null,null,null];let stopped=0;
const node=()=>({connect(){},disconnect(){}});
const ctx={state:'running',sampleRate:48000,currentTime:0,
 createGain:()=>({...node(),gain:{value:0,setValueAtTime(){}}}),
 createChannelSplitter:node,createChannelMerger:node,
 createOscillator:()=>({...node(),frequency:{value:0},start(){},stop(){stopped++;}}),
 createAnalyser(){const lane=analysers.length;
  const a={...node(),getFloatTimeDomainData(buffer){buffer.fill(0);signals[lane]?.(buffer);}};
  analysers.push(a);return a;}};
const meter=createRenderHistory(ctx,n=>n,()=>{},()=>{});
"""


class RenderHistoryTests(unittest.TestCase):
    def node(self, code):
        result = subprocess.run(['node', '--unhandled-rejections=strict', '-e',
            HISTORY_METER_JS + code], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout)

    def test_initial_zero_duplicate_clock_and_long_gap_never_credit_quiet(self):
        result = self.node(FAKE_CONTEXT + r'''
const initial=meter.snapshot();ctx.currentTime=1;meter.snapshot();
for(let i=1;i<=20;i++){ctx.currentTime=1+i*.05;meter.snapshot();}
const proven=meter.snapshot(),duplicate=meter.snapshot();
ctx.currentTime+=1;const gap=meter.snapshot();meter.port.close();meter.port.close();
let closedError;try{meter.snapshot();}catch(e){closedError=e.message;}
console.log(JSON.stringify({initial,proven,duplicate,gap,closedError}));''')
        self.assertFalse(result['initial']['coverage_valid'])
        self.assertEqual(result['initial']['quiet_seconds'], 0)
        self.assertGreaterEqual(result['proven']['quiet_seconds'], .8)
        self.assertFalse(result['duplicate']['fresh'])
        self.assertEqual(result['proven']['quiet_seconds'], result['duplicate']['quiet_seconds'])
        self.assertFalse(result['gap']['coverage_valid'])
        self.assertEqual(result['gap']['quiet_seconds'], 0)
        self.assertGreater(result['gap']['coverage_epoch'], result['proven']['coverage_epoch'])
        self.assertEqual(result['closedError'], 'b_no_plc_graph_mutated')

    def test_antiphase_and_one_threshold_block_are_not_averaged_away(self):
        result = self.node(FAKE_CONTEXT + r'''
signals[0]=b=>b.fill(8/32768,4096,4224);
signals[1]=b=>b.fill(-8/32768,4096,4224);
const value=meter.snapshot();meter.port.close();console.log(JSON.stringify(value));''')
        self.assertTrue(result['active'])
        self.assertAlmostEqual(result['peak_rms'], 8 / 32768)
        self.assertEqual(result['quiet_seconds'], 0)
        self.assertEqual(result['latest_active_block_min_frame'], -32768 + 4096 - 512)

    def test_cross_channel_read_spanning_multiple_quanta_is_unknown(self):
        result = self.node(FAKE_CONTEXT + r'''
ctx.currentTime=1;meter.snapshot();ctx.currentTime=1.05;
signals[1]=()=>{ctx.currentTime+=.02;};
const value=meter.snapshot();meter.port.close();console.log(JSON.stringify(value));''')
        self.assertFalse(result['coverage_valid'])
        self.assertEqual(result['quiet_seconds'], 0)

    def test_suspension_does_not_leave_reusable_quiet(self):
        result = self.node(FAKE_CONTEXT + r'''
ctx.currentTime=1;meter.snapshot();for(let i=1;i<=20;i++){
 ctx.currentTime=1+i*.05;meter.snapshot();}
ctx.state='suspended';let error;try{meter.snapshot();}catch(e){error=e.message;}
ctx.state='running';const resumed=meter.snapshot();meter.port.close();
console.log(JSON.stringify({error,resumed}));''')
        self.assertEqual(result['error'], 'b_no_plc_graph_mutated')
        self.assertFalse(result['resumed']['coverage_valid'])
        self.assertEqual(result['resumed']['quiet_seconds'], 0)

    def test_muted_marker_is_consumed_before_opening_gate_and_real_timestamp_is_unchanged(self):
        result = self.node(r'''
let closed=0;global.AudioData=class{constructor(config){this.config={...config,data:config.data.slice()};}close(){closed++;}};
(async()=>{const order=[];
 let time=0;const ctx={get currentTime(){return time+=.01;}};
 const probe={probeValue:i=>.125,waitMarker:async()=>{order.push('marker_seen');return {marker_observed:true};},
 waitQuiet:async()=>{order.push('tail_quiet');return {clock_frame:2048,generator_marker_observed:true,probe_tail_quiet:true};},close(){}};
 const gate=createHistoryStartup({ready:Promise.resolve(),write:async a=>{
  order.push(a.config.data.every(v=>v===0)?'silent_write':'muted_probe_write');}}, ctx,probe,()=>{},()=>order.push('gate_open'));
 const real={sampleRate:48000,numberOfChannels:2,numberOfFrames:960,timestamp:20000};
 await gate.prepare(real);await gate.prepare(real);gate.close();
 console.log(JSON.stringify({order,closed,realTimestamp:real.timestamp,receipt:gate.receipt}));})();''')
        self.assertEqual(result['order'], ['silent_write', 'muted_probe_write', 'marker_seen', 'silent_write', 'tail_quiet', 'gate_open'])
        self.assertEqual(result['closed'], 3)
        self.assertEqual(result['realTimestamp'], 20000)
        self.assertEqual(result['receipt']['status'], 'native_generator_checkpoint_observed')
        self.assertFalse(result['receipt']['primer_consumption_verified'])
        self.assertFalse(result['receipt']['remote_drain_verified'])

    def test_close_during_checkpoint_cannot_be_overwritten_by_late_success(self):
        result = self.node(r'''
let closed=0;global.AudioData=class{close(){closed++;}};
(async()=>{let gate;
 gate=createHistoryStartup({ready:Promise.resolve(),write:async()=>{}},{},
 {probeValue:()=>.125,close(){},waitMarker:()=>new Promise(resolve=>queueMicrotask(()=>{
  gate.close();resolve({marker_observed:true});}))},()=>{},()=>{throw Error('must_not_open');});
 try{await gate.prepare({sampleRate:48000,numberOfChannels:1,numberOfFrames:960,timestamp:0});}
 catch(e){console.log(JSON.stringify({error:e.message,closed,status:gate.receipt.status}));}})();''')
        self.assertEqual(result, {'error': 'b_no_plc_startup_closed', 'closed': 2, 'status': 'failed'})

    def test_bounded_unique_probe_attempts_cannot_open_without_exact_ack(self):
        result = self.node(r'''
global.AudioData=class{constructor(config){this.config=config;}close(){}};
(async()=>{const rows=[];
 for(const accepted of [false,true]){let writes=0,opened=0,attempts=[];let t=0;
  const probe={probeValue:(i,a)=>.125*(a+1),close(){},waitMarker:async a=>{
    attempts.push(a);return {marker_observed:accepted&&a===2};},
    waitQuiet:async()=>({clock_frame:4000,generator_marker_observed:true,probe_tail_quiet:true})};
  const gate=createHistoryStartup({ready:Promise.resolve(),write:async()=>writes++},
    {get currentTime(){return t+=.01;}},probe,()=>{},()=>opened++);
  let error=null;try{await gate.prepare({sampleRate:48000,numberOfChannels:1,numberOfFrames:960,timestamp:0});}
  catch(e){error=e.message;}finally{gate.close();}
  rows.push({writes,opened,attempts,error,status:gate.receipt.status});
 }console.log(JSON.stringify(rows));})();''')
        self.assertEqual(result[0], {'writes': 4, 'opened': 0, 'attempts': [0, 1, 2],
            'error': 'b_no_plc_startup_checkpoint', 'status': 'failed'})
        self.assertEqual(result[1], {'writes': 5, 'opened': 1, 'attempts': [0, 1, 2],
            'error': None, 'status': 'native_generator_checkpoint_observed'})

    def test_nonfinite_native_probe_history_cannot_be_a_marker(self):
        result = self.node(FAKE_CONTEXT + r'''
(async()=>{signals[2]=b=>b.fill(NaN);
 const probe=createGeneratorCheckpoint(ctx,node(),n=>n,()=>{},()=>{});
 let error;try{await probe.waitMarker(0);}catch(e){error=e.message;}finally{probe.close();meter.port.close();}
 console.log(JSON.stringify({error}));})();''')
        self.assertEqual(result, {'error': 'b_no_plc_startup_checkpoint'})

    def test_versioned_target_removes_loading_without_modifying_legacy_source(self):
        result = with_render_history(NO_PLC_RECEIVER_JS)
        self.assertNotIn('new Blob', result)
        self.assertNotIn('audioWorklet.addModule', result)
        self.assertIn('audioWorklet.addModule', NO_PLC_RECEIVER_JS)
        with self.assertRaisesRegex(ValueError, 'source_drift'):
            with_render_history(NO_PLC_RECEIVER_JS.replace("setupStep='worklet_url'", 'changed'))

    def test_actual_receiver_stats_and_guard_do_not_consume_the_public_sample_twice(self):
        source = with_render_history(NO_PLC_RECEIVER_JS)
        method = re.search(r'stats\(\)\{.*?return \{input:\{\.\.\.input\},render:closed\?'
            r'\{\.\.\.render\}:meter.snapshot\(\)\};\}', source).group()
        start = source.index('  function requestMeter()')
        end = source.index('\n  async function actualCodec', start)
        guard = re.search(r'timer=setInterval\((.*?),50\);', source).group(1)
        result = self.node(FAKE_CONTEXT + r'''
let closed=false,meterPending=false,render={},input={};function valid(){return true;}
''' + source[start:end] + '\nconst tick=' + guard + ';\nconst bridge={' + method + r'''};
meter.port.onmessage=e=>{meterPending=false;render=e.data;};
(async()=>{ctx.currentTime=1;const first=bridge.stats();await Promise.resolve();
 ctx.currentTime=1.05;tick();const next=bridge.stats();await Promise.resolve();
 meter.port.close();console.log(JSON.stringify({first:first.render.fresh,next:next.render.fresh}));})();''')
        self.assertEqual(result, {'first': True, 'next': True})

    def test_diagnostics_keep_counter_domain_and_do_not_invent_render_activity_frames(self):
        result = response_progress_diagnostic({'tx': {'counter_domain': 'target_decoded_input_v2',
            'frames': 960, 'energy': .1, 'duration': .02}, 'speech_completion': {
            'counter_domain': 'target_decoded_input_v2', 'activity_frames': None,
            'render_activity_observed': True, 'frames': 960}})
        self.assertEqual(result['tx']['counter_domain'], 'target_decoded_input_v2')
        self.assertIsNone(result['speech']['activity_frames'])
        self.assertTrue(result['speech']['render_activity_observed'])

    def test_history_diagnostic_rejects_content_and_nonfinite_values(self):
        result = render_history_diagnostic({'kind': 'native-history-v1', 'peak_rms': float('nan'),
            'clock_frame': 'PRIVATE-CONTENT', 'active': 'PRIVATE-CONTENT',
            'token': 'PRIVATE-CONTENT', 'quiet_seconds': float('inf')})
        self.assertNotIn('PRIVATE-CONTENT', json.dumps(result))
        self.assertIsNone(result['peak_rms'])
        self.assertIsNone(result['active'])

    def test_installed_image_probe_keeps_fixture_bytes_literal_and_does_not_override_package(self):
        import ast
        from tools.compose_smoke import history_probe_script
        tool = "print('FIXTURE_SOURCE')\n"
        fixture = "const quoted='TOOL_SOURCE';\n"
        script = history_probe_script(tool, fixture)
        tree = ast.parse(script)
        strings = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
        self.assertIn(tool, strings)
        self.assertIn(fixture, strings)
        self.assertNotIn('sys.path', script)
        compile(script, '<synthetic-image-probe>', 'exec')


if __name__ == '__main__':
    unittest.main()
