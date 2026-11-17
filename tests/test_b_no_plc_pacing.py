"""Public write-pacer seam; synthetic clocks/writers, never a playback ACK."""
import json
import shutil
import subprocess
import unittest

from teams_transcription.b_no_plc_pacing import PACING_HELPER_JS, with_clock_pacing


@unittest.skipUnless(shutil.which('node'), 'Node is unavailable')
class NoPlcWritePacerTests(unittest.TestCase):
    def node(self, code):
        result = subprocess.run(['node', '-e', PACING_HELPER_JS + code],
            capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        return json.loads(result.stdout)

    def test_burst_has_at_most_two_twenty_ms_packets_of_estimated_lead(self):
        result = self.node(r'''
(async()=>{
 const ctx={sampleRate:48000,state:'running',currentTime:0},writes=[];
 global.setTimeout=fn=>{queueMicrotask(()=>{ctx.currentTime+=.005;fn();});return 1;};
 global.clearTimeout=()=>{};
 const audio={sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:123};
 const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:async a=>{
   writes.push({at:ctx.currentTime,same:a===audio,timestamp:a.timestamp});}},ctx,()=>{});
 for(let i=0;i<6;i++)await pacer.write(audio);
 pacer.close();console.log(JSON.stringify({writes,audio}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(len(result['writes']), 6)
        for row, expected in zip(result['writes'], [0, 0, .02, .04, .06, .08]):
            self.assertAlmostEqual(row['at'], expected)
            self.assertTrue(row['same'])
            self.assertEqual(row['timestamp'], 123)

    def test_close_rejects_pending_ready_without_late_write(self):
        result = self.node(r'''
(async()=>{
 const ctx={sampleRate:48000,state:'running',currentTime:0};
 let release,writes=0;
 const writer={ready:new Promise(r=>release=r),write:async()=>{writes++;}};
 const pacer=createNoPlcWritePacer(writer,ctx,()=>{});
 const pending=pacer.write({sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0});
 pacer.close();pacer.close();let error;
 try{await pending;}catch(e){error=e.message;}
 release();await new Promise(setImmediate);
 console.log(JSON.stringify({error,writes}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result, {'error': 'b_no_plc_pacing_closed', 'writes': 0})

    def test_initial_open_failure_remains_first_failure(self):
        result = self.node(r'''
(async()=>{
 const first=Error('owned_source_failed'),ctx={sampleRate:48000,state:'running',currentTime:0};
 let fail=true,writes=0;
 const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:async()=>writes++},ctx,
   ()=>{if(fail)throw first;});
 const errors=[];
 try{await pacer.ready();}catch(e){errors.push(e===first);}
 fail=false;ctx.currentTime=NaN;
 try{await pacer.write({sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0});}
 catch(e){errors.push(e===first);}
 pacer.close();console.log(JSON.stringify({errors,writes}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result, {'errors': [True, True], 'writes': 0})

    def test_stalled_write_reanchors_without_catchup_burst(self):
        result = self.node(r'''
(async()=>{
 const ctx={sampleRate:48000,state:'running',currentTime:0},writes=[];
 global.setTimeout=fn=>{queueMicrotask(()=>{ctx.currentTime+=.005;fn();});return 1;};
 global.clearTimeout=()=>{};
 const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:async()=>{
   writes.push(ctx.currentTime);if(writes.length===1)ctx.currentTime=.5;}},ctx,()=>{});
 for(let i=0;i<5;i++)await pacer.write({sampleRate:48000,numberOfFrames:960,numberOfChannels:2,timestamp:i*20000});
 pacer.close();console.log(JSON.stringify(writes));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        for value, expected in zip(result, [0, .5, .52, .54, .56]):
            self.assertAlmostEqual(value, expected)

    def test_one_large_packet_sets_lead_without_rewriting_audio(self):
        result = self.node(r'''
(async()=>{
 const ctx={sampleRate:48000,state:'running',currentTime:0},writes=[];
 global.setTimeout=fn=>{queueMicrotask(()=>{ctx.currentTime+=.005;fn();});return 1;};
 global.clearTimeout=()=>{};
 const audio=Object.freeze({sampleRate:48000,numberOfFrames:5760,numberOfChannels:2,timestamp:-40000});
 const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:async a=>{
   writes.push({at:ctx.currentTime,same:a===audio,timestamp:a.timestamp});}},ctx,()=>{});
 await pacer.write(audio);await pacer.write(audio);pacer.close();console.log(JSON.stringify(writes));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(len(result), 2)
        self.assertAlmostEqual(result[0]['at'], 0)
        self.assertAlmostEqual(result[1]['at'], .12)
        self.assertTrue(all(row['same'] and row['timestamp'] == -40000 for row in result))

    def test_close_cancels_standalone_ready(self):
        result = self.node(r'''
(async()=>{
 const pacer=createNoPlcWritePacer({ready:new Promise(()=>{}),write:()=>{throw Error('unexpected');}},
   {sampleRate:48000,state:'running',currentTime:0},()=>{});
 const pending=pacer.ready();pacer.close();let error;
 try{await pending;}catch(e){error=e.message;}
 console.log(JSON.stringify({error}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result['error'], 'b_no_plc_pacing_closed')

    def test_close_cancels_write_and_late_completion_cannot_resume(self):
        result = self.node(r'''
(async()=>{
 let release,writes=0,entered;
 const called=new Promise(r=>entered=r);
 const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:()=>{
   writes++;entered();return new Promise(r=>release=r);}},
   {sampleRate:48000,state:'running',currentTime:0},()=>{});
 const audio={sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0};
 const pending=pacer.write(audio);await called;pacer.close();let error,next;
 try{await pending;}catch(e){error=e.message;}
 release();await new Promise(setImmediate);
 try{await pacer.write(audio);}catch(e){next=e.message;}
 console.log(JSON.stringify({error,next,writes}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result, {'error': 'b_no_plc_pacing_closed',
            'next': 'b_no_plc_pacing_closed', 'writes': 1})

    def test_close_cancels_single_clock_wait_and_stale_timer(self):
        result = self.node(r'''
(async()=>{
 const ctx={sampleRate:48000,state:'running',currentTime:0};
 const timers=new Map();let id=0,writes=0,stale;
 global.setTimeout=(fn,ms)=>{if(ms!==5)throw Error('unexpected_wait');
   stale=fn;timers.set(++id,fn);return id;};
 global.clearTimeout=n=>timers.delete(n);
 const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:async()=>writes++},ctx,()=>{});
 const audio={sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0};
 await pacer.write(audio);await pacer.write(audio);
 const pending=pacer.write(audio);await new Promise(setImmediate);
 const before=timers.size;pacer.close();pacer.close();let error;
 try{await pending;}catch(e){error=e.message;}
 ctx.currentTime=10;stale();await new Promise(setImmediate);
 console.log(JSON.stringify({before,after:timers.size,error,writes}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result, {'before': 1, 'after': 0,
            'error': 'b_no_plc_pacing_closed', 'writes': 2})

    def test_context_stop_during_clock_wait_fails_without_another_write(self):
        result = self.node(r'''
(async()=>{
 const ctx={sampleRate:48000,state:'running',currentTime:0};let writes=0,waits=0;
 global.setTimeout=fn=>{waits++;queueMicrotask(()=>{ctx.state='suspended';fn();});return 1;};
 global.clearTimeout=()=>{};
 const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:async()=>writes++},ctx,()=>{});
 const audio={sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0};
 await pacer.write(audio);await pacer.write(audio);let error;
 try{await pacer.write(audio);}catch(e){error=e.message;}
 pacer.close();console.log(JSON.stringify({error,writes,waits}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result, {'error': 'b_no_plc_pacing_clock', 'writes': 2, 'waits': 1})

    def test_invalid_frames_and_nonfinite_or_reversed_clock_never_write(self):
        result = self.node(r'''
(async()=>{
 const errors=[];
 const audio={sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0};
 for(const patch of [{numberOfFrames:0},{numberOfFrames:5761},{numberOfFrames:1.5},
   {sampleRate:44100},{numberOfChannels:3},{timestamp:NaN}]){
   const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:()=>{throw Error('unexpected_write');}},
     {sampleRate:48000,state:'running',currentTime:0},()=>{});
   try{await pacer.write({...audio,...patch});}catch(e){errors.push(e.message);}pacer.close();
 }
 for(const patch of [{currentTime:NaN},{currentTime:Infinity},{currentTime:-1},
   {currentTime:'0'},{state:'closed'},{sampleRate:44100}]){
   const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:()=>{throw Error('unexpected_write');}},
     {sampleRate:48000,state:'running',currentTime:0,...patch},()=>{});
   try{await pacer.write(audio);}catch(e){errors.push(e.message);}pacer.close();
 }
 const ctx={sampleRate:48000,state:'running',currentTime:1};let writes=0;
 const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:async()=>writes++},ctx,()=>{});
 await pacer.write(audio);ctx.currentTime=.5;
 try{await pacer.write(audio);}catch(e){errors.push(e.message);}pacer.close();
 console.log(JSON.stringify({errors,writes}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result['errors'], ['b_no_plc_pacing_format'] * 6 + ['b_no_plc_pacing_clock'] * 7)
        self.assertEqual(result['writes'], 1)

    def test_writer_failure_is_original_and_not_swallowed_by_close(self):
        result = self.node(r'''
(async()=>{
 const first=Error('writer_failed'),errors=[];
 const pacer=createNoPlcWritePacer({ready:Promise.resolve(),write:()=>{throw first;}},
   {sampleRate:48000,state:'running',currentTime:0},()=>{});
 const audio={sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0};
 try{await pacer.write(audio);}catch(e){errors.push(e===first);}
 pacer.close();
 try{await pacer.ready();}catch(e){errors.push(e===first);}
 console.log(JSON.stringify(errors));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result, [True, True])

    def test_ready_stall_and_new_helper_do_not_reuse_old_schedule(self):
        result = self.node(r'''
(async()=>{
 const ctx={sampleRate:48000,state:'running',currentTime:0};let writes=0,reads=0;
 const times=[];
 const writer={get ready(){reads++;if(reads===2)ctx.currentTime=5;return Promise.resolve();},
   write:async()=>{writes++;times.push(ctx.currentTime);}};
 const audio={sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0};
 const first=createNoPlcWritePacer(writer,ctx,()=>{});
 await first.write(audio);await first.write(audio);first.close();
 ctx.currentTime=0;
 const next=createNoPlcWritePacer(writer,ctx,()=>{});
 await next.ready();await next.write(audio);await next.write(audio);next.close();
 console.log(JSON.stringify({times,writes}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result, {'times': [0, 5, 0, 0], 'writes': 4})

    def test_concurrent_call_cannot_queue_a_second_packet(self):
        result = self.node(r'''
(async()=>{
 let release,writes=0;
 const pacer=createNoPlcWritePacer({ready:new Promise(r=>release=r),write:async()=>writes++},
   {sampleRate:48000,state:'running',currentTime:0},()=>{});
 const audio={sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0};
 const pending=pacer.write(audio);let error;
 try{await pacer.write(audio);}catch(e){error=e.message;}
 release();await pending;pacer.close();console.log(JSON.stringify({error,writes}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result, {'error': 'b_no_plc_pacing_concurrent', 'writes': 1})

    def test_ready_rejection_remains_original_after_close(self):
        result = self.node(r'''
(async()=>{
 const first=Error('ready_failed');let writes=0;
 const pacer=createNoPlcWritePacer({get ready(){return Promise.reject(first);},write:async()=>writes++},
   {sampleRate:48000,state:'running',currentTime:0},()=>{});
 const errors=[];
 try{await pacer.ready();}catch(e){errors.push(e===first);}
 pacer.close();
 try{await pacer.write({sampleRate:48000,numberOfFrames:960,numberOfChannels:1,timestamp:0});}
 catch(e){errors.push(e===first);}
 console.log(JSON.stringify({errors,writes}));
})().catch(e=>{console.error(e);process.exitCode=1;});''')
        self.assertEqual(result, {'errors': [True, True], 'writes': 0})


class WritePacerTransformTests(unittest.TestCase):
    def test_startup_then_pacing_then_history_preserves_both_close_hooks(self):
        from teams_transcription.b_no_plc_startup import LEGACY_NO_PLC_RECEIVER_JS, with_startup_primer
        from teams_transcription.b_no_plc_history import with_render_history
        source = with_clock_pacing(with_startup_primer(LEGACY_NO_PLC_RECEIVER_JS))
        target = with_render_history(source)
        for script in (source, target):
            self.assertIn('await startup.prepare(audio);requireOpen();', script)
            self.assertIn('await writePacer.ready();requireOpen();', script)
            self.assertIn('await writePacer.write(audio);requireOpen();', script)
            self.assertIn('startup?.close();writePacer?.close();', script)
            self.assertIn('const timestamp=clock(frame.timestamp);', script)
            if shutil.which('node'):
                check = subprocess.run(['node', '-e', 'new Function(' + json.dumps(script) + ');'],
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(check.returncode, 0, check.stderr[-1000:])

    def test_legacy_duplicate_or_drifted_source_is_refused(self):
        from teams_transcription.b_no_plc import NO_PLC_RECEIVER_JS as legacy
        from teams_transcription.b_no_plc_startup import with_startup_primer
        startup = with_startup_primer(legacy)
        for value in (legacy, with_clock_pacing(startup),
                startup.replace('await writer.write(audio);', 'await another.write(audio);')):
            with self.subTest(source_bytes=len(value)):
                with self.assertRaisesRegex(ValueError, 'pacing_receiver_source_drift'):
                    with_clock_pacing(value)
