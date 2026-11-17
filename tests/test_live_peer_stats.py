"""Execute actual stats JavaScript offline; Chromium coverage stays in test_browser."""
import ast
import inspect
import json
import subprocess
import textwrap
import unittest

from teams_transcription.browser import LivePeer


NODE = r'''
const assert = require('node:assert/strict');
const stats = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const privateMarker = 'private-sender-error';
const fail = () => { throw new Error(privateMarker); };
const pc = {
  connectionState:'failed', iceConnectionState:'disconnected',
  iceGatheringState:'complete', signalingState:'stable',
  localDescription:{sdp:'a=candidate:1 1 udp 100 192.0.2.10 4567 typ host\r\na=ice-pwd:private-ice-password\r\n'},
  remoteDescription:{sdp:'a=candidate:2 1 tcp 100 198.51.100.1 443 typ relay\r\n'},
  getStats:async () => new Map([['private-id', {type:'candidate-pair',state:'failed',
    nominated:false,bytesSent:0,bytesReceived:0,address:'192.0.2.10'}]])
};
const context = {state:'running',sampleRate:24000,currentTime:1};
const track = {readyState:'live',getSettings:()=>({sampleRate:48000,channelCount:2})};
const destination = {stream:{getAudioTracks:()=>[track]}};
const unknown = {audio_context_state:'unknown',audio_context_sample_rate:null,
  sender_track_sample_rate:null,sender_track_channels:null,scheduled_queue_ms:null};
const active = {...unknown,audio_context_state:'running',audio_context_sample_rate:24000,scheduled_queue_ms:60};
const cases = [
  ['original_missing_sender', {}, unknown],
  ['missing_destination', {ctx:context,next:1.06}, active],
  ['missing_stream', {ctx:context,next:1.06,destination:{}}, active],
  ['missing_tracks_api', {ctx:context,next:1.06,destination:{stream:{}}}, active],
  ['empty_tracks', {ctx:context,next:1.06,destination:{stream:{getAudioTracks:()=>[]}}}, active],
  ['tracks_throw', {ctx:context,next:1.06,destination:{stream:{getAudioTracks:fail}}}, active],
  ['ended_track', {ctx:context,next:1.06,destination:{stream:{getAudioTracks:()=>[{readyState:'ended',getSettings:fail}]}}}, active],
  ['settings_missing', {ctx:context,next:1.06,destination:{stream:{getAudioTracks:()=>[{readyState:'live'}]}}}, active],
  ['settings_throw', {ctx:context,next:1.06,destination:{stream:{getAudioTracks:()=>[{readyState:'live',getSettings:fail}]}}}, active],
  ['closed_context', {ctx:{...context,state:'closed'},next:1.06,destination}, {...unknown,audio_context_state:'closed'}],
  ['invalid_schedule', {ctx:context,next:NaN,destination}, {...active,scheduled_queue_ms:null,sender_track_sample_rate:48000,sender_track_channels:2}],
  ['active_sender', {ctx:context,next:1.06,destination}, {...active,sender_track_sample_rate:48000,sender_track_channels:2}],
  ['native_sender', {inputMode:'native_media',ctx:context,next:100,destination,
    sentFrames:999,bufferGapCount:12,bufferGapFrames:24000,maxBufferGapFrames:24000},
    {...active,scheduled_queue_ms:null,sender_track_sample_rate:48000,sender_track_channels:2}],
  ['native_closed', {inputMode:'native_media',ctx:{...context,state:'closed'},next:100,destination},
    {...unknown,audio_context_state:'closed'}],
];
(async () => {
  const observed = [];
  for(const [name,sender,expected] of cases) {
    global.__live = {pc,dc:{readyState:'connecting'},sentFrames:0,...sender};
    const result = await eval('('+stats+')')();
    const selected = Object.fromEntries(Object.keys(unknown).map(key=>[key,result[key]]));
    assert.deepEqual(selected,expected,name);
    assert.equal(result.input_mode,sender.inputMode || 'pcm');
    if(sender.inputMode==='native_media') {
      assert.equal(result.sent_frames,null);
      assert.equal(result.scheduled_queue_ms,null);
      assert.equal(result.buffer_gap_count,null);
      assert.equal(result.buffer_gap_frames,null);
      assert.equal(result.max_buffer_gap_frames,null);
    }
    assert.deepEqual(result.local_candidates,{count:1,udp:1,tcp:0,relay:0});
    assert.deepEqual(result.remote_candidates,{count:1,udp:0,tcp:1,relay:1});
    assert.equal(result.candidate_pairs[0].state,'failed');
    for(const forbidden of [privateMarker,'192.0.2.10','198.51.100.1','private-id','private-ice-password','sdp'])
      assert(!JSON.stringify(result).includes(forbidden),name);
    observed.push(name);
  }
  console.log(JSON.stringify(observed));
})().catch(error=>{console.error(error.name);process.exitCode=1;});
'''


class LivePeerStatsTests(unittest.TestCase):
    def test_missing_closed_and_failed_sender_observations_preserve_ice_stats(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(LivePeer.stats)))
        script = next(node.value for node in ast.walk(tree)
                      if isinstance(node, ast.Constant) and isinstance(node.value, str)
                      and node.value.startswith('async ()'))
        result = subprocess.run(['node', '-e', NODE], input=json.dumps(script),
                                text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        names = json.loads(result.stdout)
        self.assertEqual(len(names), 14)
        self.assertIn('original_missing_sender', names)
        self.assertIn('closed_context', names)
        self.assertIn('active_sender', names)
        self.assertIn('native_sender', names)
        self.assertIn('native_closed', names)
