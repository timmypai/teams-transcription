"""Pinned event and PCM observations only; no real browser, model or audio."""
import ast
import asyncio
import contextlib
import hashlib
import inspect
import json
import struct
import subprocess
import tempfile
import textwrap
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

from teams_transcription.browser import CAPTURE_JS, LivePeer, PEER_JS
from teams_transcription.live import LiveProvider, TranscriptNormalizer
from teams_transcription.rpc import CodexRPC
from teams_transcription.rx_audio_stats import RxAudioSnapshot
from teams_transcription.storage import Session
from test_rx_contract import config


PRIVATE = 'PRIVATE_EVENT_TOKEN https://teams.microsoft.com/meet/secret SDP v=0'


class EventObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_thread_response_mode_is_observed_without_requesting_a_mode(self):
        for mode in ('legacy', 'paginated', PRIVATE, None):
            with self.subTest(mode_known=mode in ('legacy', 'paginated')), tempfile.TemporaryDirectory() as root:
                rpc = CodexRPC(root)
                rpc.request = AsyncMock(return_value={'thread':{'id':'fixture', 'historyMode':mode,
                                                               'payload':PRIVATE}})
                self.assertEqual(await rpc.thread(), 'fixture')
                self.assertNotIn('historyMode', rpc.request.call_args.args[1])
                snapshot = RxAudioSnapshot(Session(root).create(config())).persist(
                    phase='live_start', queue_depth=0, provider=SimpleNamespace(rpc=rpc))
                self.assertEqual(snapshot['server_events']['thread_history_mode'],
                                 mode if mode in ('legacy', 'paginated') else 'unknown')
                self.assertNotIn('PRIVATE', json.dumps(snapshot))

    async def test_reader_counts_final_notifications_before_queue_or_scope(self):
        with tempfile.TemporaryDirectory() as root:
            rpc = CodexRPC(root)
            stream = asyncio.StreamReader()
            rpc.process = SimpleNamespace(stdout=stream)
            for method in ('thread/realtime/transcript/done', 'thread/realtime/item/completed', PRIVATE):
                stream.feed_data((json.dumps({'method': method, 'params': {'threadId': PRIVATE,
                    'text': PRIVATE}}) + '\n').encode())
            stream.feed_eof()
            await rpc._read()
            self.assertEqual(rpc.notification_counts,
                             {'notifications': 3, 'transcript_done': 1, 'item_completed': 1})
            self.assertEqual(rpc.notifications.qsize(), 4)  # Includes existing synthetic client/exit.
            self.assertNotIn('PRIVATE', json.dumps(rpc.notification_counts))

    async def test_scope_diagnostics_do_not_accept_unbound_finals(self):
        saved = []
        reached = asyncio.Event()
        async def final(value):
            saved.append(value)
            reached.set()
        provider = LiveProvider(None, '/tmp/unused-final-fixture', AsyncMock(), AsyncMock(),
                                on_final=final, on_provisional=AsyncMock())
        provider.thread_id = 'own'
        provider.receiver = asyncio.create_task(provider._receive())
        try:
            for binding in ({}, {'threadId': None}, {'threadId': PRIVATE}, {'threadId': []}):
                await provider.rpc.notifications.put({'method': 'thread/realtime/transcript/done',
                    'params': {**binding, 'role': 'user', 'text': PRIVATE}})
            for method in ('thread/realtime/itemAdded', 'thread/realtime/item/transcript/delta', PRIVATE):
                await provider.rpc.notifications.put({'method': method, 'params': {'threadId': 'own'}})
            await provider.rpc.notifications.put({'method': 'thread/realtime/transcript/done',
                'params': {'threadId': 'own', 'role': 'user', 'text': '合成官方終稿'}})
            await asyncio.wait_for(reached.wait(), 3)
            counts = provider.received_events
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]['text'], '合成官方終稿')
            self.assertEqual(counts['rpc_notifications_seen'], 8)
            self.assertEqual(counts['rpc_done_seen_before_scope'], 5)
            self.assertEqual(counts['rpc_transcript_done'], 1)
            self.assertEqual(counts['rpc_final_scope_rejected'], 4)
            for reason in ('missing', 'null', 'foreign', 'invalid'):
                self.assertEqual(counts['rpc_scope_rejected_' + reason], 1)
            self.assertEqual((counts['rpc_item_added'], counts['rpc_item_transcript_delta'],
                              counts['rpc_unknown_method']), (1, 1, 1))
            self.assertNotIn('PRIVATE', json.dumps(counts))
        finally:
            provider.receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await provider.receiver

    async def test_delta_then_close_remains_provisional_and_observations_persist(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            provider = LiveProvider(None, root, AsyncMock(), AsyncMock(),
                on_final=AsyncMock(), on_provisional=lambda value: asyncio.to_thread(session.save_provisional, value))
            provider.thread_id = 'own'
            await provider._provisional_delta({'threadId': 'own', 'role': 'user', 'delta': '合成增量'})
            await provider._source('thread/realtime/item/completed', {'threadId': 'own',
                'item': {'type': 'transcriptSegment', 'id': 'core-sealed', 'realtimeSessionId': 'fixture',
                         'role': 'user', 'text': '合成增量'}})
            provider.closing = True
            await provider._closed('rpc', 'thread/realtime/closed', 'requested')
            provider.rpc.notification_counts['transcript_done'] = 1
            provider.rpc.notifications.put_nowait({'method': 'unused', 'params': {}})
            provider.receiver = asyncio.create_task(asyncio.sleep(0))
            await provider.receiver
            snapshot = RxAudioSnapshot(session).persist(phase='cleanup', queue_depth=0, provider=provider)
            self.assertEqual(snapshot['server_events']['rpc_reader_counts']['transcript_done'], 1)
            self.assertEqual(snapshot['server_events']['rpc_queue_depth'], 1)
            self.assertTrue(snapshot['server_events']['receiver_done'])
            self.assertEqual(snapshot['server_events']['counts']['normalizer_unconfirmed_canonical'], 1)
            self.assertEqual(snapshot['server_events']['normalized_finals'], 0)
            self.assertEqual(session.entries(include_unfinalized=False), [])
            provider.on_final.assert_not_awaited()
            entry,=session.entries()
            self.assertEqual(entry['text'],'合成增量')
            self.assertFalse(entry['provider_final'])
            self.assertEqual(session.status()['rx_audio'], snapshot)


class PcmObservationTests(unittest.TestCase):
    def test_zero_runs_cross_packet_boundaries_and_do_not_infer_finality(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            stats = RxAudioSnapshot(session)
            for samples in ((0, 32767, 0, 0), (0, 0), (-32768, 0, 0, 0)):
                data = struct.pack('<' + 'h' * len(samples), *samples)
                nonzero = stats.consumed(data)
                stats.push_attempted(data)
                stats.push_returned(data, nonzero)
            self.assertEqual(stats.push['returned_frames'], 10)
            self.assertEqual(stats.push['returned_last_nonzero_frame'], 7)
            self.assertEqual(stats.push['returned_trailing_exact_zero_frames'], 3)
            self.assertEqual(stats.push['returned_max_exact_zero_frames'], 4)
            snapshot = stats.persist(phase='rx_capture', queue_depth=0, provider=None)
            self.assertEqual(snapshot['live_push'], stats.push)
            self.assertEqual(session.entries(include_unfinalized=False), [])
            self.assertIsNone(snapshot['server_events']['rpc_reader_counts'])
            self.assertIsNone(snapshot['server_events']['receiver_done'])

    def test_transport_filters_every_new_field_and_missing_observation_is_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            stats = RxAudioSnapshot(Session(root).create(config()))
            stats.observe_transport({'sent_frames': 4, 'audio_context_state': [PRIVATE],
                'audio_context_sample_rate': True, 'sender_track_sample_rate': PRIVATE,
                'sender_track_channels': -1, 'scheduled_queue_ms': float('inf'),
                'buffer_gap_count': 2, 'buffer_gap_frames': 2400, 'max_buffer_gap_frames': 1200,
                'datachannel_delivery': {'messages': 3, 'json_errors': 1, 'binding_failed': PRIVATE,
                    'payload': PRIVATE}, 'url': PRIVATE})
            value = stats.transport
            self.assertEqual(value['audio_context_state'], 'unknown')
            for key in ('audio_context_sample_rate', 'sender_track_sample_rate',
                        'sender_track_channels', 'scheduled_queue_ms'):
                self.assertIsNone(value[key])
            self.assertEqual(value['datachannel_delivery']['messages'], 3)
            self.assertIsNone(value['datachannel_delivery']['binding_failed'])
            self.assertNotIn('PRIVATE', json.dumps(value))


NODE = r'''
const assert = require('node:assert/strict');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const buffers = [], sources = [], calls = [];
global.window = global;
global.AudioContext = class {
  constructor(options) { this.sampleRate = options.sampleRate; this.currentTime = 0; this.state = 'running'; }
  async resume() {}
  createMediaStreamDestination() { return {stream:{getAudioTracks:()=>[{getSettings:()=>({sampleRate:48000,channelCount:2})}]}}; }
  createBuffer(channels, frames, rate) {
    const samples = new Float32Array(frames);
    const buffer = {channels,frames,rate,duration:frames/rate,getChannelData:()=>samples,samples};
    buffers.push(buffer); return buffer;
  }
  createBufferSource() {
    const source = {connect(destination){ assert(destination.stream); },start(when){ this.when=when; }};
    sources.push(source); return source;
  }
};
global.RTCPeerConnection = class {
  constructor(options) { this.options=options;this.iceGatheringState='complete';this.connectionState='connected'; }
  getConfiguration(){ return this.options; }
  addEventListener(){}
  addTrack(){}
  createDataChannel(){ return this.dc={readyState:'open'}; }
  async createOffer(){ return {type:'offer',sdp:'synthetic-offline'}; }
  async setLocalDescription(value){ this.localDescription=value; }
  async getStats(){ return new Map(); }
};
window.liveEvent = value => { calls.push(value); return Promise.resolve(); };
(async () => {
  await eval('('+input.peer+')')([]);
  const first = Buffer.alloc(8); first.writeInt16LE(-32768,0); first.writeInt16LE(32767,2);
  __live.push(first.toString('base64'));
  __live.push(Buffer.alloc(4).toString('base64'));
  __live.ctx.currentTime = 1;
  __live.push(Buffer.alloc(6).toString('base64'));
  assert.equal(buffers.length,3); assert(buffers.every(b=>b.channels===1 && b.rate===24000));
  assert.equal(buffers[0].samples[0],-1); assert.equal(buffers[0].samples[1],32767/32768);
  assert(buffers[1].samples.every(x=>x===0)); assert.equal(__live.sentFrames,9);
  assert.equal(sources[0].when,.06);
  assert.equal(sources[1].when,.06+4/24000); assert.equal(sources[2].when,1.06);
  // No commit/end_turn message is emitted by the sender or by a zero buffer.
  assert.equal(calls.length,0);
  const delta = {type:'input_transcript.added',item:{text:'synthetic-private-marker'}};
  await __live.dc.onmessage({data:JSON.stringify(delta)});
  assert.deepEqual(calls,[delta]);
  if(input.instrumented) {
    const bad = '{synthetic-private-marker';
    assert.throws(()=>__live.dc.onmessage({data:bad}),SyntaxError);
    window.liveEvent = ()=>Promise.reject(new Error('synthetic-private-marker'));
    await assert.rejects(__live.dc.onmessage({data:'{}'}));
    await Promise.resolve();
    assert.deepEqual(__live.delivery,{messages:3,json_errors:1,binding_attempted:2,binding_returned:1,binding_failed:1});
    assert.equal(__live.bufferGapCount,1); assert.equal(__live.bufferGapFrames,23994);
    assert.equal(__live.maxBufferGapFrames,23994);
    const stats = await eval('('+input.stats+')')();
    assert.equal(stats.audio_context_sample_rate,24000);
    assert.equal(stats.sender_track_sample_rate,48000); assert.equal(stats.sender_track_channels,2);
    assert.equal(stats.scheduled_queue_ms,60); assert.deepEqual(stats.datachannel_delivery,__live.delivery);
  }
  console.log(JSON.stringify({buffers:buffers.map(b=>({channels:b.channels,frames:b.frames,rate:b.rate})),
    starts:sources.map(s=>s.when),sent_frames:__live.sentFrames}));
})().catch(()=>{ console.error('offline peer assertion failed'); process.exitCode=1; });
'''


def reviewed_base():
    return json.loads((Path(__file__).parent / 'fixtures/final-event-base-42.json').read_text())


class PeerJavascriptObservationTests(unittest.TestCase):
    def test_actual_peer_pcm_scheduling_matches_reviewed_base_without_browser(self):
        base = reviewed_base()
        original = base['peer_js']
        self.assertEqual(hashlib.sha256(original.encode()).hexdigest(), base['peer_sha256'])
        stats = next(n.value for n in ast.walk(ast.parse(textwrap.dedent(inspect.getsource(LivePeer.stats))))
                     if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.startswith('async ()'))
        results = []
        for peer, instrumented in ((original, False), (PEER_JS, True)):
            result = subprocess.run(['node', '-e', NODE], input=json.dumps(
                {'peer':peer, 'stats':stats, 'instrumented':instrumented}), text=True,
                capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('synthetic-private-marker', result.stdout + result.stderr)
            results.append(json.loads(result.stdout))
        self.assertEqual(results[0], results[1])

