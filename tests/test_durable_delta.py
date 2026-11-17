"""Durable delta-only acceptance: synthetic RPC/text; no browser or model call."""
import asyncio
import json
import hashlib
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch



from teams_transcription.b_response import MeetingMinutesTool, MeetingStoreMinutesReader

from teams_transcription.live import LiveProvider
from teams_transcription.meeting_store import MeetingStore, MeetingStoreError


from teams_transcription.response_runtime import _AsyncStore
from teams_transcription.rpc import CodexRPC
from teams_transcription.rx_contract import confirm_prepared
from teams_transcription.storage import Session
from teams_transcription.worker import run_session


from test_rx_contract import Fixture, config, native_transport
from lifecycle_probe import session_events



CAPTURE = 'a' * 32
RECEIVED = '2026-10-10T15:00:00+00:00'


def increment(text='尚未定稿的原文', *, capture=CAPTURE, receipt=1, part=0, **source):
    return dict(capture_id=capture, receipt_sequence=receipt, part_index=part,
                text=text, received_at=RECEIVED, **source)


def segment_text(snapshot):
    return ''.join(value['text'] for value in snapshot['segments'])


class DurableDeltaStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = MeetingStore(self.tmp.name)
        self.store.create('one')
        self.store.create('other')

    def test_idless_repetition_and_receipt_order_are_preserved(self):
        for number, text in enumerate(('乙', '甲', '是', '是'), 1):
            self.store.record_increment('one', increment(text, receipt=number))
        value = self.store.read('one')
        self.assertEqual(segment_text(value), '乙甲是是')
        self.assertEqual(value['events'], [])
        state = value['transcript_state']
        self.assertTrue(state['saved'])
        self.assertEqual(state['ordering'], 'receipt_order')
        self.assertEqual(state['deduplication'], 'source_identity_only')
        self.assertEqual(state['provider_final_count'], 0)
        self.assertEqual(state['quality'], 'includes_unfinalized')
        self.assertFalse(state['meeting_ended'])
        self.assertEqual(self.store.read('other')['segments'], [])

    def test_local_receipt_retry_is_idempotent_and_conflict_is_rejected(self):
        first = self.store.record_increment('one', increment())
        retry = self.store.record_increment('one', increment())
        self.assertEqual(first['version'], retry['version'])
        self.assertEqual(segment_text(self.store.read('one')),increment()['text'])
        with self.assertRaises(MeetingStoreError):
            self.store.record_increment('one', increment('同一receipt不同文字'))

    def test_explicit_source_identity_revision_and_restart(self):
        source = dict(source_event_id='known-source', source_revision=1,
                      source_sequence=7, source_timestamp='2026-10-10T14:59:00Z')
        self.store.record_increment('one', increment('第一版本', **source))
        self.store = MeetingStore(self.tmp.name)
        self.store.record_increment('one', increment('第一版本', capture='b'*32, **source))
        self.assertEqual(segment_text(self.store.read('one')), '第一版本')
        self.store.record_increment('one', increment('更正版本', receipt=2,
            **{**source, 'source_revision':2}))
        value = self.store.read('one')
        self.assertEqual(segment_text(value), '更正版本')
        self.assertEqual(value['segments'][0]['source_revision'], 2)
        self.assertEqual([row['text'] for row in value['increments']], ['第一版本', '更正版本'])
        self.store.record_increment('one', increment('第一版本', capture='c'*32, **source))
        self.assertEqual(segment_text(self.store.read('one')), '更正版本')
        self.store.record_increment('other', increment('別場同ID', **source))
        self.assertEqual(segment_text(self.store.read('other')), '別場同ID')

    def test_application_seal_late_restart_and_terminal_are_not_final(self):
        self.store.record_increment('one', increment('結束前'))
        self.store.end_capture('one', dict(capture_id=CAPTURE, reason='application_stop'))
        self.store = MeetingStore(self.tmp.name)
        self.store.record_increment('one', increment('晚到片段', receipt=2))
        value = self.store.read('one')
        self.assertEqual(segment_text(value), '結束前晚到片段')
        self.assertTrue(any(item['late'] for item in value['segments']))
        self.assertTrue(all(not item['provider_final'] for item in value['segments']))
        self.assertEqual(value['events'], [])
        self.store.finalize('one', 'stopped')
        value = MeetingStore(self.tmp.name).read('one')
        self.assertTrue(value['transcript_state']['meeting_ended'])
        self.assertEqual(value['transcript_state']['provider_final_count'], 0)
        self.assertEqual(value['transcript_state']['quality'], 'includes_unfinalized')
        with self.assertRaisesRegex(MeetingStoreError, 'meeting_finalized'):
            self.store.record_increment('one', increment('結束後新資料', receipt=3))

    def test_stop_error_completion_keep_same_saved_unfinalized_source(self):
        for state in ('stopped', 'failed', 'completed'):
            with self.subTest(state=state):
                self.store.create(state)
                self.store.record_increment(state, increment('已收到原文'))
                self.store.finalize(state, state)
                value = MeetingStore(self.tmp.name).read(state)
                self.assertEqual(segment_text(value), '已收到原文')
                self.assertEqual(value['events'], [])
                self.assertTrue(value['transcript_state']['saved'])
                self.assertTrue(value['transcript_state']['meeting_ended'])
                self.assertEqual(value['transcript_state']['provider_final_count'], 0)

    

    def test_invalid_increment_does_not_write_or_accept_guessed_source_identity(self):
        invalid = ({'text':'中'*2731}, {'receipt_sequence':True}, {'part_index':-1},
                   {'capture_id':'../other'}, {'received_at':'2026-10-10T15:00:00'},
                   {'source_event_id':'id-only'}, {'source_revision':1},
                   {'source_role':'assistant'})
        for changed in invalid:
            with self.subTest(changed=tuple(changed)), self.assertRaises(MeetingStoreError):
                self.store.record_increment('one', {**increment(), **changed})
        self.assertEqual(self.store.read('one')['segments'], [])

    def test_historical_provisional_remains_unfinalized_and_queryable(self):
        session = Session(self.tmp.name).create(config())
        text = '歷史42/43未定稿原文，仍非model final。'
        session.save_provisional(dict(provisional_id='provisional:'+'e'*32,
            revision=12, text=text, truncated=False))
        session.finalize_minutes('stopped')
        recovered = Session(self.tmp.name, session.ident)
        value = MeetingStore(self.tmp.name).read(session.ident)
        self.assertEqual(value['events'], [])
        self.assertEqual(value['provisionals'][0]['revision'], 12)
        self.assertEqual(value['provisionals'][0]['text'], text)
        self.assertEqual(value['transcript_state']['provider_final_count'], 0)
        self.assertEqual(recovered.entries(include_unfinalized=False), [])
        self.assertIn(text, ''.join(item['text'] for item in recovered.entries()))

    def test_whole_multichunk_delta_batch_is_atomic_validated_and_retryable(self):
        parts = [letter * 8192 for letter in 'abcd']
        value = {**increment(parts[0]), 'parts':parts}
        with self.assertRaises(MeetingStoreError):
            self.store.record_increment('one', {**value, 'parts':[parts[0], 'x'*8193, parts[2], parts[3]]})
        self.assertEqual(self.store.read('one')['increments'], [])
        first = self.store.record_increment('one', value)
        first=self.store.read('one')
        self.assertEqual(segment_text(first), ''.join(parts))
        self.assertEqual(len(first['increments']), 4)
        self.assertEqual(len({item['changed_version'] for item in first['increments']}), 1)
        retry = MeetingStore(self.tmp.name).record_increment('one', value)
        self.assertEqual(first['version'], retry['version'])
        self.assertEqual(segment_text(self.store.read('one')),''.join(parts))


class DurableDeltaProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.session = Session(self.tmp.name).create(config())
        self.persisted = asyncio.Queue()
        async def save(value):
            self.session.record_increment(value)
            self.persisted.put_nowait('delta')
        async def final(value):
            self.session.append_final(value)
            self.persisted.put_nowait('final')
        async def ended(value): self.session.end_capture(value)
        self.provider = LiveProvider(None, '/tmp/unused-delta', AsyncMock(), AsyncMock(),
            on_final=final, on_increment=save, on_capture_end=ended)
        self.provider.thread_id = 'own'
        self.provider.rpc = SimpleNamespace(notifications=asyncio.Queue())
        self.receiver = asyncio.create_task(self.provider._receive())

    async def asyncTearDown(self):
        self.receiver.cancel()
        await asyncio.gather(self.receiver, return_exceptions=True)
        self.tmp.cleanup()

    async def notify(self, text, **params):
        await self.provider.rpc.notifications.put(dict(method='thread/realtime/transcript/delta',
            params=dict(threadId='own', role='user', delta=text, **params)))
        await asyncio.wait_for(self.provider.rpc.notifications.join(), 2)
        count = 0
        while not self.persisted.empty():
            self.assertEqual(self.persisted.get_nowait(), 'delta')
            count += 1
        self.assertGreaterEqual(count, 1)

    async def test_delta_only_beyond_64k_and_tail_reaches_all_source_views(self):
        parts = [('原文%02d：'%n) + '中文😀 '*1500 + '\n' for n in range(7)]
        parts.append('保存尾端：下次會議核對交付日期。')
        for text in parts:
            await self.notify(text)
        value = MeetingStore(self.tmp.name).read(self.session.ident)
        expected = ''.join(parts)
        self.assertGreater(len(expected.encode()), 65536)
        self.assertEqual(segment_text(value), expected)
        self.assertTrue(all(len(item['text'].encode()) <= 8192 for item in value['segments']))
        self.assertEqual(value['events'], [])
        self.assertEqual(''.join(entry['text'] for entry in self.session.entries()), expected)
        self.assertTrue(all(entry['source_state'] == 'application_segment' for entry in self.session.entries()))
        self.assertEqual(self.session.entries(include_unfinalized=False), [])
        reader = MeetingStoreMinutesReader(_AsyncStore(self.tmp.name))
        tool=MeetingMinutesTool(self.session.ident,reader)
        result=await tool.dispatch(tool.name,{'query':'保存尾端'})
        self.assertTrue(result['available'])
        self.assertTrue(any('保存尾端' in line['text'] for line in result['context']))
        self.assertEqual(result['transcript_state']['provider_final_count'], 0)
        self.assertFalse((self.session.path/'audio.wav').exists())

    async def test_actual_rpc_scope_filter_and_idless_repeat_preserved(self):
        for params in (dict(threadId='other',role='user'), dict(role='assistant',threadId='own'),
                       dict(threadId=None,role='user')):
            await self.provider.rpc.notifications.put(dict(method='thread/realtime/transcript/delta',
                params={**params,'delta':'不得入場'}))
        await self.notify('是')
        await self.notify('是')
        value = MeetingStore(self.tmp.name).read(self.session.ident)
        self.assertEqual(segment_text(value), '是是')
        self.assertEqual(value['events'], [])
        self.assertIsNone(self.provider.error)

    async def test_flat_rpc_never_invents_source_clock_identity_or_revision(self):
        await self.notify('原文', source_event_id='not-supported-by-pinned-schema',
            source_revision=42, source_sequence=17, source_timestamp='2020-01-01T00:00:00Z')
        value = MeetingStore(self.tmp.name).read(self.session.ident)
        for segment in value['segments']:
            self.assertIsNone(segment['source_event_id'])
            self.assertIsNone(segment['source_revision'])
            self.assertIsNone(segment['source_timestamp'])
            self.assertIsNone(segment['source_sequence'])
            self.assertNotEqual(segment['timestamp'], '2020-01-01T00:00:00Z')

    async def test_real_done_remains_provider_final_and_new_delta_is_unfinalized(self):
        await self.notify('尚未完成')
        await self.provider.rpc.notifications.put(dict(method='thread/realtime/transcript/done',
            params=dict(threadId='own',role='user',text='來源明確終稿')))
        self.assertEqual(await asyncio.wait_for(self.persisted.get(), 1), 'final')
        await self.notify('下一段未定稿')
        value = MeetingStore(self.tmp.name).read(self.session.ident)
        self.assertEqual(value['events'][0]['text'], '來源明確終稿')
        self.assertEqual(value['transcript_state']['provider_final_count'], 1)
        self.assertIn('下一段未定稿', segment_text(value))
        self.assertTrue(all(not item['provider_final'] for item in value['segments']))

    async def test_restart_different_capture_retains_prior_original_text(self):
        await self.notify('重啟前')
        second = LiveProvider(None, '/tmp/unused-second', AsyncMock(), AsyncMock(),
            on_increment=self.provider.on_increment, on_capture_end=self.provider.on_capture_end)
        second.thread_id = 'new'
        await second._provisional_delta(dict(threadId='new',role='user',delta='重啟後'))
        self.assertEqual(await asyncio.wait_for(self.persisted.get(), 1), 'delta')
        value = MeetingStore(self.tmp.name).read(self.session.ident)
        self.assertEqual(segment_text(value), '重啟前重啟後')
        self.assertEqual(value['events'], [])

    async def assert_transport_end_retains_accepted_delta(self, method, **params):
        await self.notify('斷線前原文')
        await self.provider.rpc.notifications.put(dict(method=method,
            params=dict(threadId='own', **params)))
        await asyncio.wait_for(self.provider.rpc.notifications.join(), 2)
        self.assertIsNotNone(self.provider.error)
        # The already accepted ordered notification tail still belongs to this
        # capture. Closing the provider seals the application boundary only.
        await self.notify('已接收晚到尾文')
        self.provider.receiver = self.receiver
        self.provider.peer = SimpleNamespace(request_session_close=AsyncMock(return_value=False), close=AsyncMock())
        self.provider.rpc.process = None
        self.provider.rpc.close = AsyncMock()
        # Surface the real failure only after draining and closing owned work.
        with self.assertRaisesRegex(RuntimeError, self.provider.error):
            await self.provider.close()
        value = MeetingStore(self.tmp.name).read(self.session.ident)
        self.assertEqual(segment_text(value), '斷線前原文已接收晚到尾文')
        self.assertEqual(value['events'], [])
        self.assertEqual(value['transcript_state']['provider_final_count'], 0)
        self.assertFalse(value['transcript_state']['meeting_ended'])
        self.assertTrue(all(item['capture_state'] == 'sealed' for item in value['segments']))
        expected_reason = 'provider_closed' if method.endswith('/closed') else 'provider_error'
        self.assertTrue(all(item['capture_end_reason'] == expected_reason for item in value['segments']))
        self.assertTrue(any(item['late'] for item in value['segments']))
        self.assertTrue(self.receiver.done())
        self.provider.rpc.close.assert_awaited_once()

    async def test_actual_rpc_error_flush_is_saved_and_never_provider_final(self):
        await self.assert_transport_end_retains_accepted_delta('thread/realtime/error',
            message='synthetic realtime error')

    async def test_actual_rpc_closed_flush_is_saved_and_never_meeting_end(self):
        await self.assert_transport_end_retains_accepted_delta('thread/realtime/closed', reason='expired')

    async def test_provider_final_on_media_end_is_saved_before_rpc_reader_stops(self):
        await self.notify('已收到的增量')
        sideband_stopped = False
        async def stop_sideband(method, params):
            nonlocal sideband_stopped
            sideband_stopped = True
            self.receiver.cancel()
            return {}
        async def end_media():
            if not sideband_stopped:
                await self.provider.rpc.notifications.put(dict(
                    method='thread/realtime/transcript/done',
                    params=dict(threadId='own', role='user', text='真正的 provider 結尾')))
        self.provider.receiver = self.receiver
        self.provider.peer = SimpleNamespace(request_session_close=AsyncMock(return_value=False), close=end_media)
        self.provider.rpc.process = SimpleNamespace(returncode=None)
        self.provider.rpc.request = stop_sideband
        self.provider.rpc.close = AsyncMock()
        await self.provider.close()
        value = MeetingStore(self.tmp.name).read(self.session.ident)
        self.assertEqual([event['text'] for event in value['events']], ['真正的 provider 結尾'])
        self.assertEqual(value['transcript_state']['provider_final_count'], 1)
        self.assertEqual(segment_text(value), '已收到的增量')
        self.assertTrue(all(segment['capture_state'] == 'sealed' for segment in value['segments']))

    async def test_drain_requests_provider_end_before_media_and_preserves_true_final(self):
        await self.notify('已保存的增量')
        media_open = True
        async def finish_input(**kwargs):
            if media_open:
                await self.provider.rpc.notifications.put(dict(
                    method='thread/realtime/transcript/done',
                    params=dict(threadId='own', role='user', text='provider 明確結尾')))
            return media_open
        async def end_media():
            nonlocal media_open
            media_open = False
        self.provider.receiver = self.receiver
        self.provider.peer = SimpleNamespace(request_session_close=finish_input, close=end_media)
        self.provider.rpc.process = None
        self.provider.rpc.close = AsyncMock()
        await self.provider.drain(.02)
        await self.provider.close()
        finals = self.session.entries(include_unfinalized=False)
        self.assertEqual([item['text'] for item in finals], ['provider 明確結尾'])
        self.assertEqual(MeetingStore(self.tmp.name).read(self.session.ident)['transcript_state']['provider_final_count'], 1)


    async def test_canonical_session_close_seal_without_backend_done_is_not_provider_final(self):
        await self.notify('尚無backend done的原文')
        await self.provider.rpc.notifications.put(dict(method='thread/realtime/item/completed',
            params=dict(threadId='own', item=dict(id='close-seal',type='transcriptSegment',
                role='user',text='尚無backend done的原文'))))
        await asyncio.wait_for(self.provider.rpc.notifications.join(), 2)
        value = MeetingStore(self.tmp.name).read(self.session.ident)
        self.assertEqual(value['events'], [])
        self.assertEqual(value['transcript_state']['provider_final_count'], 0)
        self.assertEqual(segment_text(value), '尚無backend done的原文')
        self.assertEqual(self.provider.source_count, 0)
        self.assertIsNone(self.provider.error)

    async def test_real_bounded_rpc_reader_close_drains_parsed_text_and_reaps_owned_child(self):
        # This child is only a local JSON emitter. It has no Codex, auth, audio,
        # browser, network, or model. The production RPC reader/close are real.
        child_code = '''import json,sys
for n in range(41):
 print(json.dumps({'method':'thread/realtime/transcript/delta','params':{
  'threadId':'own','role':'user','delta':'片%02d\\n'%n}}),flush=True)
for line in sys.stdin:
 message=json.loads(line)
 if message.get('method')=='thread/realtime/stop':
  print(json.dumps({'id':message['id'],'result':{}}),flush=True)
'''
        self.receiver.cancel()
        await asyncio.gather(self.receiver, return_exceptions=True)
        entered, release, full, stop_attempted = (asyncio.Event() for _ in range(4))
        original_save = self.provider.on_increment
        async def gated_save(value):
            if not entered.is_set():
                entered.set()
                await release.wait()
            await original_save(value)
        self.provider.on_increment = gated_save
        class ObservedQueue(asyncio.Queue):
            async def put(queue, value):
                await super().put(value)
                if queue.full(): full.set()
        rpc = CodexRPC(self.session.path/'local-json-runtime', timeout=.5)
        rpc.notifications = ObservedQueue(maxsize=32)
        rpc.process = await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', child_code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        rpc.tasks = [asyncio.create_task(rpc._read()), asyncio.create_task(rpc._stderr())]
        self.provider.rpc = rpc
        self.provider.peer = SimpleNamespace(request_session_close=AsyncMock(return_value=False), close=AsyncMock())
        self.receiver = asyncio.create_task(self.provider._receive())
        self.provider.receiver = self.receiver
        original_request = rpc.request
        async def request(method, params):
            if method == 'thread/realtime/stop': stop_attempted.set()
            return await original_request(method, params)
        rpc.request = request
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(full.wait(), 2)
            self.assertEqual(rpc.notifications.qsize(), 32)
            closing = asyncio.create_task(self.provider.close())
            await asyncio.wait_for(stop_attempted.wait(), 2)
            release.set()
            await asyncio.wait_for(closing, 4)
            value = MeetingStore(self.tmp.name).read(self.session.ident)
            self.assertEqual(segment_text(value), ''.join('片%02d\n'%n for n in range(41)))
            self.assertEqual(value['events'], [])
            self.assertEqual(value['transcript_state']['provider_final_count'], 0)
            self.assertTrue(all(item['capture_state'] == 'sealed' for item in value['segments']))
            self.assertTrue(self.receiver.done())
            self.assertTrue(all(task.done() for task in rpc.tasks))
            self.assertIsNotNone(rpc.process.returncode)
            self.assertEqual(rpc.pending, {})
            # RPC.close may enqueue its owned child's exit notification after
            # the transcript receiver is cancelled. No source text may remain.
            tail = []
            while not rpc.notifications.empty():
                tail.append(rpc.notifications.get_nowait())
                rpc.notifications.task_done()
            self.assertTrue(all(message.get('method') == 'client/exit' for message in tail))
            self.assertFalse((self.session.path/'audio.wav').exists())
        finally:
            release.set()
            if closing and not closing.done():
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
            if not self.receiver.done():
                self.receiver.cancel()
                await asyncio.gather(self.receiver, return_exceptions=True)
            await rpc.close()

    async def test_multichunk_rpc_ingress_survives_blocked_view_and_close_timeout(self):
        # A single 32 KiB provider delta must be committed once as a batch.
        # The consumer's view publication is deliberately blocked indefinitely;
        # the existing two-second close limit must not discard the batch suffix.
        child_code = '''import json,sys
def send(params):
 print(json.dumps({'method':'thread/realtime/transcript/delta','params':params}),flush=True)
send({'threadId':'own','role':'user','delta':'a'*8192+'b'*8192+'c'*8192+'d'*8192})
send({'threadId':'foreign','role':'user','delta':'FOREIGN_SECRET'})
send({'threadId':'own','role':'assistant','delta':'ASSISTANT_REPLY'})
send({'role':'user','delta':'UNBOUND_SECRET'})
for n in range(36):
 print(json.dumps({'method':'synthetic/control','params':{'threadId':'own'}}),flush=True)
for line in sys.stdin:
 message=json.loads(line)
 if message.get('method')=='thread/realtime/stop':
  print(json.dumps({'id':message['id'],'result':{}}),flush=True)
'''
        self.receiver.cancel()
        await asyncio.gather(self.receiver, return_exceptions=True)
        saved, view_entered, view_release, queue_full = (asyncio.Event() for _ in range(4))
        batches = []
        async def save(value):
            batches.append(value)
            self.session.record_increment(value, publish=False)
            saved.set()
        async def view():
            view_entered.set()
            await view_release.wait()
            self.session.store.transcript_state(self.session.ident)
        async def ended(value): self.session.end_capture(value)
        provider = LiveProvider(None, '/tmp/unused-slow-delta', AsyncMock(), AsyncMock(),
            on_increment=save, on_capture_end=ended)
        provider.on_source_view = view
        provider.thread_id = 'own'
        provider.peer = SimpleNamespace(request_session_close=AsyncMock(return_value=False), close=AsyncMock())
        rpc = provider.rpc  # Retain the constructor's real ingress binding.
        rpc.timeout = .2
        class ObservedQueue(asyncio.Queue):
            async def put(queue, value):
                await super().put(value)
                if queue.full(): queue_full.set()
        rpc.notifications = ObservedQueue(maxsize=32)
        rpc.process = await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', child_code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        rpc.tasks = [asyncio.create_task(rpc._read()), asyncio.create_task(rpc._stderr())]
        receiver = asyncio.create_task(provider._receive())
        provider.receiver = receiver
        closing = None
        try:
            await asyncio.wait_for(saved.wait(), 2)
            await asyncio.wait_for(view_entered.wait(), 2)
            await asyncio.wait_for(queue_full.wait(), 2)
            self.assertEqual(rpc.notifications.qsize(), 32)
            self.assertEqual(len(batches), 1)
            self.assertEqual(batches[0]['parts'], [letter*8192 for letter in 'abcd'])
            # The full original is durable before the consumer can publish a view.
            self.assertEqual(segment_text(MeetingStore(self.tmp.name).read(self.session.ident)),
                ''.join(letter*8192 for letter in 'abcd'))
            closing = asyncio.create_task(provider.close())
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(closing, 4)
            value = MeetingStore(self.tmp.name).read(self.session.ident)
            expected = ''.join(letter*8192 for letter in 'abcd')
            self.assertEqual(segment_text(value), expected)
            self.assertEqual(value['events'], [])
            self.assertEqual(value['transcript_state']['provider_final_count'], 0)
            self.assertEqual(len(value['increments']), 4)
            self.assertTrue(all(len(item['text'].encode()) <= 8192 for item in value['increments']))
            for secret in ('FOREIGN_SECRET', 'ASSISTANT_REPLY', 'UNBOUND_SECRET'):
                self.assertNotIn(secret,json.dumps(value,ensure_ascii=False))
            self.assertTrue(receiver.done())
            self.assertTrue(all(task.done() for task in rpc.tasks))
            self.assertIsNotNone(rpc.process.returncode)
            self.assertEqual(rpc.pending, {})
            provider.peer.close.assert_awaited_once()
        finally:
            view_release.set()
            if closing and not closing.done():
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
            if not receiver.done():
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            await rpc.close()

    async def test_explicit_final_rpc_ingress_survives_blocked_view_queue_and_close_timeout(self):
        child_code = '''import json,sys
def send(method,params):
 print(json.dumps({'method':method,'params':params}),flush=True)
send('thread/realtime/transcript/delta',{'threadId':'own','role':'user','delta':'尚未定稿原文'})
send('thread/realtime/transcript/done',{'threadId':'foreign','role':'user','text':'FOREIGN_FINAL_SECRET'})
send('thread/realtime/transcript/done',{'role':'user','text':'UNBOUND_FINAL_SECRET'})
send('thread/realtime/transcript/done',{'threadId':'own','role':'assistant','text':'ASSISTANT_FINAL_REPLY'})
send('thread/realtime/transcript/done',{'threadId':'own','role':'user','text':'官方明確終稿'})
for n in range(36):
 print(json.dumps({'method':'synthetic/control','params':{'threadId':'own'}}),flush=True)
for line in sys.stdin:
 message=json.loads(line)
 if message.get('method')=='thread/realtime/stop':
  print(json.dumps({'id':message['id'],'result':{}}),flush=True)
'''
        self.receiver.cancel()
        await asyncio.gather(self.receiver, return_exceptions=True)
        view_entered, view_release, final_saved, queue_full = (asyncio.Event() for _ in range(4))
        accepted_finals = []
        async def save(value): self.session.record_increment(value, publish=False)
        async def final(value):
            self.session.append_final(value)
            accepted_finals.append(value)
            final_saved.set()
        async def view():
            view_entered.set()
            await view_release.wait()
            self.session.store.transcript_state(self.session.ident)
        async def ended(value): self.session.end_capture(value)
        provider = LiveProvider(None, '/tmp/unused-final-ingress', AsyncMock(), AsyncMock(),
            on_increment=save, on_final=final, on_capture_end=ended)
        provider.on_source_view = view
        provider.thread_id = 'own'
        provider.peer = SimpleNamespace(request_session_close=AsyncMock(return_value=False), close=AsyncMock())
        rpc = provider.rpc
        rpc.timeout = .2
        class ObservedQueue(asyncio.Queue):
            async def put(queue, value):
                await super().put(value)
                if queue.full(): queue_full.set()
        rpc.notifications = ObservedQueue(maxsize=32)
        rpc.process = await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', child_code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        rpc.tasks = [asyncio.create_task(rpc._read()), asyncio.create_task(rpc._stderr())]
        receiver = asyncio.create_task(provider._receive())
        provider.receiver = receiver
        closing = None
        try:
            await asyncio.wait_for(view_entered.wait(), 2)
            await asyncio.wait_for(final_saved.wait(), 2)
            await asyncio.wait_for(queue_full.wait(), 2)
            self.assertEqual(rpc.notifications.qsize(), 32)
            self.assertEqual(len(accepted_finals), 1)
            self.assertEqual(provider.source_count, 1)
            value = MeetingStore(self.tmp.name).read(self.session.ident)
            self.assertEqual([event['text'] for event in value['events']], ['官方明確終稿'])
            self.assertEqual(segment_text(value), '尚未定稿原文')
            self.assertEqual(value['transcript_state']['provider_final_count'], 1)
            self.assertEqual(value['transcript_state']['quality'], 'includes_unfinalized')
            closing = asyncio.create_task(provider.close())
            with self.assertRaises(TimeoutError):
                await asyncio.wait_for(closing, 4)
            value = MeetingStore(self.tmp.name).read(self.session.ident)
            self.assertEqual([event['text'] for event in value['events']], ['官方明確終稿'])
            self.assertEqual(value['transcript_state']['provider_final_count'], 1)
            self.assertEqual(provider.source_count, 1)
            for secret in ('FOREIGN_FINAL_SECRET', 'UNBOUND_FINAL_SECRET', 'ASSISTANT_FINAL_REPLY'):
                self.assertNotIn(secret,json.dumps(value,ensure_ascii=False))
            self.assertTrue(receiver.done())
            self.assertTrue(all(task.done() for task in rpc.tasks))
            self.assertIsNotNone(rpc.process.returncode)
            self.assertEqual(rpc.pending, {})
        finally:
            view_release.set()
            if closing and not closing.done():
                closing.cancel()
                await asyncio.gather(closing, return_exceptions=True)
            if not receiver.done():
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
            await rpc.close()





class DurableDeltaWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_delta_only_normal_stop_error_and_cleanup_tail_are_saved(self):
        for mode in ('normal', 'stop', 'error'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as root:
                session = Session(root).create(config())
                # Keep a fake remote-track source alive until stop_capture;
                # Fixture.Meeting.close records that cleanup only for a live source.
                async def source(callback, owner):
                    await asyncio.Event().wait()
                fixture = Fixture(emit_final=False, source=source)
                prepared = asyncio.Event()
                original_update = Session.update
                def update(current, **values):
                    result = original_update(current, **values)
                    if current.ident == session.ident and values.get('state') == 'prepared':
                        prepared.set()
                    return result
                class SyntheticLive(LiveProvider):
                    async def start(current):
                        fixture.provider = current
                        fixture.live_starts += 1
                        current.thread_id = 'own'
                        current.peer = SimpleNamespace(request_session_close=AsyncMock(return_value=False),
                            push=AsyncMock(), close=AsyncMock(),
                            stats=AsyncMock(return_value=native_transport()))
                        current.rpc = SimpleNamespace(process=None, close=AsyncMock())
                        current.once = False
                        current.stage = 'connected'
                    async def ready(current):
                        if not await super().ready():
                            return False
                        if not current.once:
                            current.once = True
                            await current._provisional_delta(dict(threadId='own', role='user', delta='錄製期間原文'))
                            if mode == 'stop': session.stop()
                            if mode == 'error': raise RuntimeError('synthetic controlled delivery error')
                        return True
                    async def drain(current, seconds):
                        await current._provisional_delta(dict(threadId='own', role='user', delta='drain晚到原文'))
                    async def close(current):
                        await current._provisional_delta(dict(threadId='own', role='user', delta='cleanup尾文'))
                        await super().close()
                        fixture.closed.append('live')
                task = None
                try:
                    with fixture.install() as summary, patch('teams_transcription.worker.LiveProvider', SyntheticLive), \
                            patch.object(Session, 'update', update):
                        task = asyncio.create_task(run_session(session, install_signals=False))
                        await asyncio.wait_for(prepared.wait(), 2)
                        generation = session.status()['rx_prepared']['generation']
                        confirm_prepared(session, generation, session.config()['rx_owner_principal'])
                        self.assertEqual(await asyncio.wait_for(task, 3), 1 if mode == 'error' else 0)
                        summary.assert_not_called()
                    value = MeetingStore(root).read(session.ident)
                    self.assertIn('錄製期間原文', segment_text(value))
                    self.assertIn('cleanup尾文', segment_text(value))
                    if mode == 'normal': self.assertIn('drain晚到原文', segment_text(value))
                    self.assertEqual(value['events'], [])
                    self.assertTrue(value['transcript_state']['saved'])
                    self.assertTrue(value['transcript_state']['meeting_ended'])
                    self.assertEqual(value['transcript_state']['provider_final_count'], 0)
                    self.assertEqual(session.status()['state'], dict(normal='completed',stop='stopped',error='failed')[mode])
                    self.assertEqual(session.status()['cleanup_status'], 'cleaned')
                    self.assertTrue(all(name in fixture.closed for name in ('capture','meeting','live','browser')))
                    self.assertFalse((session.path/'audio.wav').exists())
                    self.assertFalse((session.path/'source.wav').exists())
                    self.assertNotIn('錄製期間原文', json.dumps(session_events(session),ensure_ascii=False))
                finally:
                    if task and not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
