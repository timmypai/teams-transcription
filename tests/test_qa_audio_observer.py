"""Passive synthetic audio evidence boundaries; no browser/model/meeting."""
import base64
import json
import struct
import tempfile
import unittest
from pathlib import Path


class AudioObservationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='qa-audio-evidence-')
        self.addCleanup(self.temp.cleanup)
        self.directory=Path(self.temp.name)/'capture'

    def pcm(self, kind, values, *, at=0):
        raw=struct.pack('<'+'f'*len(values),*values)
        return {'kind':kind,'channels':1,'frames':len(values),'media_ms':at,
            'observed_ms':at,'data':base64.b64encode(raw).decode()}

    def test_fixed_synthetic_scope_and_private_exclusive_bounded_files(self):
        from teams_transcription.qa_audio_observation import AudioObservation
        with self.assertRaisesRegex(ValueError,'synthetic_scope_required'):
            AudioObservation(self.directory,meeting_id='real-meeting',authorized_synthetic=True)
        self.assertFalse(self.directory.exists())
        observation=AudioObservation(self.directory,meeting_id='synthetic-qa',authorized_synthetic=True,
            audio_byte_cap=32)
        observation.ingest({'elapsed_ms':1,'pcm':[self.pcm('source',[.1]*4),self.pcm('render',[0.]*4)],
            'events':[],'status':{}})
        observation.ingest({'elapsed_ms':2,'pcm':[self.pcm('source',[.1])],'events':[],'status':{}})
        report=observation.close()
        self.assertLessEqual(sum(p.stat().st_size for p in self.directory.glob('*.f32le')),32)
        self.assertTrue(report['truncated'])
        self.assertEqual(self.directory.stat().st_mode & 0o777,0o700)
        for path in self.directory.iterdir():self.assertEqual(path.stat().st_mode & 0o777,0o600)
        with self.assertRaises(FileExistsError):
            AudioObservation(self.directory,meeting_id='synthetic-qa',authorized_synthetic=True)

    def observation(self):
        from teams_transcription.qa_audio_observation import AudioObservation
        return AudioObservation(self.directory,meeting_id='synthetic-qa',authorized_synthetic=True)

    def test_decoded_pause_packet_clock_gap_and_render_zero_have_distinct_evidence(self):
        observation=self.observation()
        observation.ingest({'elapsed_ms':1,'pcm':[self.pcm('source',[.1]*480,at=0),
            self.pcm('source',[0.]*480,at=10),self.pcm('render',[0.]*480,at=10)],
            'events':[{'kind':'packet','rtp_timestamp':1000,'expected_frames':480},
                {'kind':'packet','rtp_timestamp':1480,'expected_frames':480}],'status':{}})
        observation.ingest({'elapsed_ms':50,'pcm':[self.pcm('render',[0.]*480,at=50)],
            'events':[{'kind':'packet','rtp_timestamp':2920,'expected_frames':480},
                {'kind':'stats','packets_lost':-1,'jitter_seconds':.002},
                {'kind':'stats','packets_lost':1,'jitter_seconds':.004}], 'status':{}})
        result=observation.close(poll_wall_ms=3,polls=2)
        self.assertEqual(result['findings']['decoded_silent_frames'],480)
        self.assertEqual(result['findings']['rtp_timestamp_gap_frames'],960)
        self.assertEqual(result['findings']['render_zero_after_source_stall_candidate_frames'],480)
        self.assertEqual(result['reported_loss_delta'],2)
        self.assertEqual(result['reported_jitter_max_seconds'],.004)
        self.assertIsNone(result['underflow_confirmed_ms'])
        self.assertFalse(result['audio_quality_verified'])
        manifest=json.loads((self.directory/'manifest.json').read_text())
        self.assertEqual(manifest['poll_wall_ms'],3)

    def test_unavailable_arrival_loss_jitter_and_hostile_fields_do_not_become_zero_or_leak(self):
        observation=self.observation()
        observation.ingest({'elapsed_ms':1,'pcm':[], 'events':[{'kind':'packet',
            'rtp_timestamp':1,'receive_ms':None,'dequeue_ms':4,'token':'PRIVATE-TOKEN'},
            {'kind':'stats','packets_lost':None,'jitter_seconds':float('nan'),
                'url':'PRIVATE-URL'}],'status':{}})
        result=observation.close()
        events=[json.loads(line) for line in (self.directory/'events.jsonl').read_text().splitlines()]
        self.assertIsNone(events[0]['receive_ms'])
        self.assertEqual(events[0]['dequeue_ms'],4)
        self.assertIsNone(result['reported_loss_delta'])
        self.assertIsNone(result['reported_jitter_max_seconds'])
        self.assertNotIn('PRIVATE-', ''.join(p.read_text() for p in self.directory.glob('*.json*')))

    def test_duration_and_malformed_audio_are_bounded_and_do_not_write_samples(self):
        observation=self.observation()
        observation.ingest({'elapsed_ms':120001,'pcm':[self.pcm('source',[.1])],'events':[],'status':{}})
        bad=self.pcm('source',[float('nan')])
        observation.ingest({'elapsed_ms':1,'pcm':[bad],'events':[],'status':{}})
        result=observation.close()
        self.assertEqual(sum(result['audio_bytes'].values()),0)
        self.assertTrue(result['truncated'])
        self.assertIn('window_cap',result['limit_reasons'])
        self.assertIn('invalid_pcm_record',result['limit_reasons'])


class TapScriptTests(unittest.TestCase):
    def node(self,script):
        import subprocess
        result=subprocess.run(['node','-e',script],capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr[-2000:])
        return json.loads(result.stdout)

    def test_source_tap_copies_before_scratch_clear_and_keeps_audio_clock_and_rtp_metadata(self):
        from teams_transcription.qa_audio_tap import COLLECTOR_JS
        script='global.window={};\n'+COLLECTOR_JS+'''
const o=createQAAudioObservation(),scratch=new Float32Array([.25,-.25]);
const row=o.call('packet',{timestamp:77,data:new ArrayBuffer(10),getMetadata:()=>({receiveTime:2,token:'PRIVATE'})},1);
o.call('source',scratch,2,0,1,row,125);scratch.fill(0);o.call('packetDone',row);
const batch=o.take();console.log(JSON.stringify(batch));
'''
        batch=self.node(script)
        raw=base64.b64decode(batch['pcm'][0]['data'])
        self.assertEqual(struct.unpack('<ff',raw),(.25,-.25))
        self.assertEqual(batch['pcm'][0]['media_ms'],125)
        self.assertEqual(batch['events'][0]['receive_ms'],2)
        self.assertNotIn('PRIVATE',json.dumps(batch))

    def test_render_worklet_one_credit_and_final_partial_flush_do_not_loop_on_ack(self):
        from teams_transcription.qa_audio_tap import WORKLET_JS
        script='''let Processor;let messages=[];global.sampleRate=48000;global.currentFrame=0;
global.AudioWorkletProcessor=class{constructor(){this.port={postMessage:d=>messages.push(d)};}};
global.registerProcessor=(name,type)=>{Processor=type;};
'''+WORKLET_JS+'''
const p=new Processor(),tone=new Float32Array(128).fill(.2);
for(let i=0;i<16;i++){currentFrame=i*128;p.process([[tone]]);}
const first=messages.length;
for(let i=0;i<16;i++){currentFrame=(i+16)*128;p.process([[tone]]);}
const waiting=messages.length;
p.port.onmessage({data:'ack'});const afterAck=messages.length;
p.process([[new Float32Array(128)]]);p.port.onmessage({data:'finish'});
const beforeAck=messages.length;p.port.onmessage({data:'ack'});
const finalCount=messages.length;p.port.onmessage({data:'ack'});
console.log(JSON.stringify({first,waiting,afterAck,beforeAck,finalCount,lastCount:messages.length,
 last:messages.at(-1),sample:messages[0].pcm[0].samples[0]}));
'''
        result=self.node(script)
        self.assertEqual((result['first'],result['waiting'],result['afterAck']),(1,1,2))
        self.assertGreater(result['finalCount'],result['beforeAck'])
        self.assertEqual(result['finalCount'],result['lastCount'])
        self.assertTrue(result['last']['finished'])
        self.assertAlmostEqual(result['sample'],.2,places=6)

    def test_receiver_instrumentation_fails_on_drift_without_editing_original(self):
        from teams_transcription.b_no_plc import NO_PLC_RECEIVER_JS
        from teams_transcription.qa_audio_tap import instrument_receiver
        script=instrument_receiver(NO_PLC_RECEIVER_JS)
        self.assertIn('await writer.write(audio);requireOpen();',script)
        self.assertIn('const {done,value:frame}=await reader.read();',script)
        self.assertNotIn('decoder.flush(',script)
        with self.assertRaisesRegex(ValueError,'pinned_receiver_contract_changed'):
            instrument_receiver(NO_PLC_RECEIVER_JS.replace('await writer.write(audio)','await writer.write(other)'))

    def test_instrumented_script_parses_as_javascript_and_keeps_native_decode_write_calls(self):
        from teams_transcription.b_no_plc import NO_PLC_RECEIVER_JS
        from teams_transcription.qa_audio_tap import instrument_receiver
        script=instrument_receiver(NO_PLC_RECEIVER_JS)
        value=self.node('new Function('+json.dumps(script)+');console.log(JSON.stringify({parsed:true}));')
        self.assertTrue(value['parsed'])
        for call in ('decoder.decode(new EncodedAudioChunk','await writer.ready;',
                'await writer.write(audio);','audio.copyTo(scratch,','inputTrack.stop();'):
            self.assertEqual(script.count(call),NO_PLC_RECEIVER_JS.count(call))


class ObservedProbeTests(unittest.IsolatedAsyncioTestCase):
    async def offline_probe(self,*,accepted=True,slow=False):
        import asyncio
        from qa_c409_fixture import run_c409
        script='''import asyncio,json,sys
from test_qa_audio_observer import ObservedProbeTests
result=asyncio.run(ObservedProbeTests()._offline_probe_in_fixture(**json.loads(sys.argv[1])))
print(json.dumps(result))'''
        process=await asyncio.to_thread(run_c409,script,
            args=(json.dumps({'accepted':accepted,'slow':slow}),),timeout=10)
        self.assertEqual(process.returncode,0,process.stderr)
        return json.loads(process.stdout)

    async def _offline_probe_in_fixture(self,*,accepted=True,slow=False):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch
        from teams_transcription import qa_probe,b_live
        from teams_transcription.qa_audio_probe import run_observed_probe
        from teams_transcription.owned_processes import OwnedProcesses
        calls=[]
        class Page:
            async def evaluate(self,script,finish):
                calls.append('read')
                if slow:await asyncio.Future()
                calls.append('finish' if finish else 'batch')
                return {'elapsed_ms':1,'pcm':[], 'events':[],'status':{},
                    'pending_chunks':0,'pending_events':0,'browser_ms':1}
        class Native:
            def __init__(self,*args,**kwargs):
                self.peer=SimpleNamespace(page=Page(),_document_valid=True)
                self.stop_ack_received=self.stop_closed_received=True
                self.cleanup_status='cleaned'
            async def stop(self):calls.append('stop');return 'stopped'
        class Sink:
            async def pause(self):calls.append('mute');return {'status':'muted'}
        class Browser:
            async def __aexit__(self,*args):calls.append('browser_closed')
        async def base(args):
            native=qa_probe.NativeLiveResponder(None,None,'synthetic-qa',None,None,muted_probe_text_gate=True)
            sink=qa_probe.NoPhysicalSink();await sink.pause()
            await asyncio.sleep(.45 if slow else .01)
            cleanup=await qa_probe.cleanup_probe(native,Browser(),OwnedProcesses(),tasks=[],
                deadline=asyncio.get_running_loop().time()+2)
            return {'accepted':accepted,'checks':[{'exact_source_quote':accepted}],
                'owned_cleanup':cleanup,'base_sentinel':'unchanged'}
        with tempfile.TemporaryDirectory() as directory:
            args=SimpleNamespace(authorize_live=True,qa_start_stage='question',peer_media_profile='legacy',
                private_result=Path(directory)/'qa.json',audio_observer_dir=Path(directory)/'pcm',
                config='existing-private',chromium=None,ice_servers_file=None)
            original=b_live.NO_PLC_PEER_JS
            with patch.object(qa_probe,'NativeLiveResponder',Native), \
                    patch.object(qa_probe,'NoPhysicalSink',Sink), \
                    patch.object(qa_probe,'run_probe',side_effect=base):
                result=await run_observed_probe(args)
            manifest=json.loads((args.audio_observer_dir/'manifest.json').read_text())
            self.assertEqual(b_live.NO_PLC_PEER_JS,original)
        self.assertEqual(calls.count('stop'),1)
        self.assertEqual(calls.count('browser_closed'),1)
        self.assertFalse(result['owned_cleanup']['pending_operation'])
        self.assertEqual(result['owned_cleanup']['pending_close_tasks'],0)
        return result,manifest,calls

    async def test_observer_preserves_base_qa_result_and_stops_before_owned_cleanup(self):
        for accepted in (True,False):
            result,manifest,calls=await self.offline_probe(accepted=accepted)
            self.assertEqual(result['accepted'],accepted)
            self.assertEqual(result['base_sentinel'],'unchanged')
            self.assertLess(calls.index('finish'),calls.index('stop'))
            self.assertGreater(manifest['polls'],0)
            self.assertFalse(result['audio_observer']['audio_quality_verified'])

    async def test_uncertain_browser_read_is_not_retried_and_does_not_replace_qa_outcome(self):
        result,manifest,calls=await self.offline_probe(slow=True)
        self.assertTrue(result['accepted'])
        self.assertEqual(calls.count('read'),1)
        self.assertIn('observer_read_failed',manifest['limit_reasons'])
        self.assertTrue(manifest['truncated'])

    async def test_unapproved_or_nonfixed_probe_fails_before_any_recording_or_model(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from teams_transcription import qa_probe
        from teams_transcription.qa_audio_probe import run_observed_probe
        with tempfile.TemporaryDirectory() as directory:
            for authorization,stage in ((False,'question'),(True,'wake')):
                args=SimpleNamespace(authorize_live=authorization,qa_start_stage=stage,
                    audio_observer_dir=Path(directory)/'pcm')
                with patch.object(qa_probe,'run_probe') as probe:
                    with self.assertRaises(ValueError):await run_observed_probe(args)
                    probe.assert_not_called()
                self.assertFalse(args.audio_observer_dir.exists())

    async def test_original_private_qa_result_cannot_bypass_observer_directory_byte_cap(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from teams_transcription import qa_probe
        from teams_transcription.qa_audio_probe import run_observed_probe
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'observer'
            args=SimpleNamespace(authorize_live=True,qa_start_stage='question',peer_media_profile='legacy',
                private_result=root/'original-qa.json',audio_observer_dir=root)
            with patch.object(qa_probe,'run_probe') as probe:
                with self.assertRaisesRegex(ValueError,'separate_observer_directory_required'):
                    await run_observed_probe(args)
                probe.assert_not_called()
            self.assertFalse(root.exists())
