"""Native QA integration only: mock media/RPC, real scoped context/tool reads."""
import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from teams_transcription.b_native_output import NativeTrackOutput
from teams_transcription.b_native import NativeLiveResponder
from teams_transcription.b_question import FramedQuestion
from teams_transcription.b_response import MeetingMinutesTool
from context_fixtures import context_record,context_snapshot


class NativeSnapshotContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.reads = []
        self.sequence = 0
        self.records = [context_record('history','歷史：上次保留舊版。',provenance='synthetic_history'),
            context_record('event-one','預算十萬元。')]
        self.version = 1
        def read(meeting_id):
            self.reads.append(meeting_id)
            return context_snapshot(meeting_id,self.records,version=self.version)
        self.sink = AsyncMock()
        self.sink.observe.return_value = dict(frames=0, packets=0, energy=0., duration=0.,
            source_frames=0, source_packets=0, source_energy=0., source_duration=0., connected=True)
        self.output = NativeTrackOutput('one', self.sink)
        self.live = NativeLiveResponder(None, '/tmp/native-context-unused', 'one', self.output,
            MeetingMinutesTool('one', read), question_timeout=2, stop_timeout=.01)
        self.live.thread_id = 'owned-thread'
        self.live.context.bind_session(self.live.thread_id)
        self.live.started = True
        self.live.state = 'sleeping'
        self.live.peer = AsyncMock()
        self.live.rpc = AsyncMock()
        self.live.rpc.notifications = asyncio.Queue()
        self.live.rpc.process = SimpleNamespace(returncode=None)
        self.live._answer = asyncio.get_running_loop().create_future()
        async def begin(state):
            self.live.state = state
        self.live._begin_native_speech = AsyncMock(side_effect=begin)

    async def asyncTearDown(self):
        await self.live.close()

    def decode_payload(self, events):
        wire = ''.join(event['content'][0]['text'] for event in events)
        self.assertTrue(wire.endswith('\n'))
        payload = json.loads(wire)
        self.assertEqual(payload['kind'], 'application_question')
        self.assertIs(payload['provider_final'], False)
        return payload

    def payload(self, start=0):
        events = [call.args[0] for call in self.live.peer.send_event.await_args_list[start:]]
        self.assertEqual(events[-1]['content'][0]['text'], 'RESULT_COMPLETE')
        self.assertEqual(sum(event['content'][0]['text'] == 'RESULT_COMPLETE'
            for event in events), 1)
        return self.decode_payload(events[:-1]), events

    def use_large_context(self):
        self.records = [context_record(f'large-{number}', f'預算{number}：' + '中' * 1800)
            for number in range(5)]

    async def test_one_large_json_returns_before_one_marker_admission_and_commit(self):
        self.use_large_context()
        returned = []
        async def sending(event, *, capture_output=False):
            self.assertFalse(self.output.active)
            self.assertEqual(self.live.context.status()['committed_records'], 0)
            returned.append(event)
            await asyncio.sleep(0)
        async def begin(state):
            self.assertEqual(state, 'answering')
            self.assertNotEqual(returned[-1]['content'][0]['text'], 'RESULT_COMPLETE')
            payload = self.decode_payload(returned)
            self.assertEqual(payload['request_id'], self.live._answer_scope['request_id'])
            self.assertEqual(payload['question'], '預算多少？')
            self.assertEqual(self.live.context.status()['committed_records'], 0)
            self.live.state = state
        self.live.peer.send_event.side_effect = sending
        self.live._begin_native_speech.side_effect = begin
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.send()
            commit.assert_called_once()
        payload, events = self.payload()
        self.assertEqual({row['record_index'] for row in payload['selected_source_reading']},
            set(range(1, 6)))
        self.assertEqual([row['text'] for row in payload['selected_source_reading']],
            [row['text'] for row in self.records])
        for event in events:
            self.assertEqual(event['channel'], 'commentary')
            self.assertLessEqual(len(event['content'][0]['text'].encode()), 500)
        self.assertEqual(sum(event['content'][0]['text'] == 'RESULT_COMPLETE' for event in events), 1)
        wire_bytes = sum(len(event['content'][0]['text'].encode()) for event in events)
        receipt = self.live.status()['tool_deliveries'][-1]
        self.assertGreater(receipt['payload_bytes'], 12 * 1024)
        self.assertEqual(receipt['payload_bytes'] + 15, wire_bytes)
        self.assertEqual(receipt['sent_bytes'], wire_bytes)
        self.assertTrue(receipt['marker_returned'])
        self.assertEqual(receipt['selected_ref_count'], len(payload['selected_source_reading']))
        self.assertEqual(receipt['phase'], 'delivered')
        self.assertEqual(self.live.context.status()['sent_bytes'], wire_bytes)
        self.assertEqual(self.live.context.status()['committed_records'], 5)
        self.assertEqual(self.reads, ['one'])
        self.live._begin_native_speech.assert_awaited_once_with('answering')

    async def test_failure_after_streaming_progress_has_no_marker_tx_or_commit(self):
        self.use_large_context()
        returned = []
        async def sending(event, *, capture_output=False):
            if len(returned) == 3:
                raise RuntimeError('controlled failure after data chunks')
            returned.append(event['content'][0]['text'])
        self.live.peer.send_event.side_effect = sending
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.send()
            commit.assert_not_called()
        self.assertEqual(len(returned), 3)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(''.join(returned))
        attempted = [call.args[0]['content'][0]['text']
            for call in self.live.peer.send_event.await_args_list]
        self.assertNotIn('RESULT_COMPLETE', attempted)
        receipt = self.live.status()['tool_deliveries'][-1]
        self.assertGreater(receipt['payload_bytes'], len(''.join(returned).encode()))
        self.assertEqual(receipt['sent_bytes'], len(''.join(returned).encode()))
        self.assertEqual(self.live.context.status()['sent_bytes'], receipt['sent_bytes'])
        self.assertFalse(receipt['marker_returned'])
        self.assertEqual(receipt['phase'], 'failed')
        self.assertEqual(self.live.context.status()['committed_records'], 0)
        self.assertFalse(self.output.active)
        self.live._begin_native_speech.assert_not_awaited()
        self.assertEqual(self.reads, ['one'])
        self.assertEqual(self.live.state, 'sleeping')
        self.assertIsNone(self.live.error)
        self.assertIsNone(self.live._request_id)
        self.assertIsNone(self.live._answer_scope)

    async def test_cancellation_after_streaming_progress_has_no_marker_tx_or_commit(self):
        self.use_large_context()
        entered, release = asyncio.Event(), asyncio.Event()
        returned = []
        async def sending(event, *, capture_output=False):
            if len(returned) == 3:
                entered.set()
                await release.wait()
            returned.append(event['content'][0]['text'])
        self.live.peer.send_event.side_effect = sending
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.live._start_application_request(
                FramedQuestion('question', '預算多少？', 1))
            task = self.live._tool_task
            try:
                await asyncio.wait_for(entered.wait(), 1)
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            finally:
                release.set()
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            commit.assert_not_called()
        self.assertEqual(len(returned), 3)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(''.join(returned))
        self.assertNotEqual(self.live.peer.send_event.await_args_list[-1].args[0]['content'][0]['text'],
            'RESULT_COMPLETE')
        receipt = self.live.status()['tool_deliveries'][-1]
        self.assertEqual(receipt['phase'], 'cancelled')
        self.assertFalse(receipt['marker_returned'])
        self.assertEqual(receipt['sent_bytes'], len(''.join(returned).encode()))
        self.assertEqual(self.live.context.status()['sent_bytes'], receipt['sent_bytes'])
        self.assertEqual(self.live.context.status()['committed_records'], 0)
        self.assertFalse(self.output.active)
        self.live._begin_native_speech.assert_not_awaited()
        self.assertEqual(self.reads, ['one'])

    async def test_output_admission_failure_after_complete_payload_never_commits(self):
        self.use_large_context()
        self.live._begin_native_speech.side_effect = RuntimeError('controlled output admission failure')
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.send()
            commit.assert_not_called()
        events = [call.args[0] for call in self.live.peer.send_event.await_args_list]
        self.decode_payload(events)
        self.assertFalse(any(event['content'][0]['text'] == 'RESULT_COMPLETE' for event in events))
        receipt = self.live.status()['tool_deliveries'][-1]
        self.assertEqual(receipt['sent_bytes'], receipt['payload_bytes'])
        self.assertFalse(receipt['marker_returned'])
        self.assertEqual(receipt['phase'], 'failed')
        self.assertEqual(self.live.context.status()['committed_records'], 0)
        self.assertFalse(self.output.active)

    async def send(self, question='預算多少？'):
        self.sequence += 1
        await self.live._start_application_request(
            FramedQuestion('question', question, self.sequence))
        if self.live._tool_task:
            await self.live._tool_task

    async def until(self, condition):
        async with asyncio.timeout(1):
            while not condition():
                await asyncio.sleep(0)

    async def test_each_question_reads_current_minutes_and_commits_after_marker(self):
        commits = []
        actual_commit = self.live.context.commit
        def commit(plan, *, session_id):
            commits.append(self.live.peer.send_event.await_args_list[-1].args[0])
            return actual_commit(plan, session_id=session_id)
        with patch.object(self.live.context, 'commit', side_effect=commit) as spy:
            await self.send()
            first, events = self.payload()
            self.assertEqual(spy.call_count, 1)
            self.assertEqual(commits[0]['content'][0]['text'], 'RESULT_COMPLETE')
            start = len(events)
            await self.send()
            second, _ = self.payload(start)
        self.assertEqual(self.reads, ['one', 'one'])
        self.assertEqual(first['result']['meeting_id'], 'one')
        self.assertEqual([row['text'] for row in first['selected_source_reading']],
            [row['text'] for row in self.records])
        self.assertEqual(second['selected_source_reading'], first['selected_source_reading'])
        self.assertNotEqual(first['request_id'], second['request_id'])
        self.assertTrue(second['result']['available'])
        returned_bytes = sum(len(call.args[0]['content'][0]['text'].encode('utf-8'))
                             for call in self.live.peer.send_event.await_args_list)
        self.assertEqual(self.live.context.status()['sent_bytes'], returned_bytes)
        self.assertFalse(self.live.context.status()['model_consumption_verified'])

    async def test_delivery_receipt_hashes_identify_actual_current_payload_without_content(self):
        import hashlib
        await self.send()
        payload, events = self.payload()
        text = ''.join(event['content'][0]['text'] for event in events[:-1])
        receipt = self.live.status()['tool_deliveries'][-1]
        self.assertEqual(receipt['payload_sha256'], hashlib.sha256(text.encode()).hexdigest())
        self.assertEqual(receipt['snapshot_sha256'], payload['result']['snapshot_sha256'])
        self.assertEqual(receipt['source_version'], payload['result']['version'])
        self.assertTrue(receipt['marker_returned'])
        self.assertEqual(receipt['phase'], 'delivered')
        self.assertFalse(receipt['model_consumption_verified'])
        self.assertNotIn('selected_source_reading', receipt)
        self.assertFalse(self.output.enabled)
        self.sink.start.assert_not_awaited()
        self.sink.set_muted.assert_not_awaited()
        self.live.rpc.request.assert_not_awaited()

    async def test_every_source_is_delivered_and_counted_above_old_record_limit(self):
        self.records = [context_record(f'source-{index}', f'原文{index}。')
            for index in range(600)]
        for _ in range(2):
            start = self.live.peer.send_event.await_count
            await self.send()
            payload, _ = self.payload(start)
            readings = payload['selected_source_reading']
            self.assertEqual([row['text'] for row in readings],
                [row['text'] for row in self.records])
            self.assertEqual([row['record_index'] for row in readings],
                list(range(1, 601)))
            receipt = self.live.status()['tool_deliveries'][-1]
            self.assertEqual(receipt['selected_ref_count'], 600)
            self.assertTrue(receipt['selected_indexes_truncated'])
            self.assertEqual(self.live.context.status()['committed_records'], 600)
            self.assertFalse(self.live.context.status()['model_consumption_verified'])
        self.assertEqual(self.reads, ['one', 'one'])

    async def test_fragment_projection_delivers_each_source_once_with_full_host_receipt(self):
        import tempfile
        from teams_transcription.meeting_store import MeetingStore
        from teams_transcription.b_response import MeetingStoreMinutesReader
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('one')
            store.record_increment('one', {
                'capture_id': 'a' * 32, 'receipt_sequence': 1, 'part_index': 0,
                'text': '代號', 'parts': ['代號', '松', '風'],
                'received_at': '2026-10-14T01:00:00Z'})
            store.append_final('one', {'event_id': 'budget', 'revision': 1,
                'text': '預算十萬元。', 'sequence': 1,
                'timestamp': '2026-10-14T01:00:01Z'})
            reads = self.reads
            source_texts = []
            class Source:
                def read(self, meeting):
                    reads.append(meeting)
                    snapshot = store.read(meeting)
                    source_texts[:] = [source['text'] for source in snapshot['sources']]
                    return snapshot
            self.live.tool.reader = MeetingStoreMinutesReader(Source())
            for _ in range(2):
                start = self.live.peer.send_event.await_count
                await self.send(question='代號和預算是多少？')
                payload, _ = self.payload(start)
                readings = payload['selected_source_reading']
                self.assertCountEqual([row['text'] for row in readings],
                    ['代號松風', '預算十萬元。'])
                projection = next(row for row in readings if 'record_index_range_inclusive' in row)
                self.assertFalse(projection['provider_final'])
                self.assertFalse(projection['speech_continuity_verified'])
                self.assertEqual(projection['provenance'], 'sqlite_source')
                self.assertEqual(projection['quality'], 'unfinalized')
                covered = []
                for row in readings:
                    if 'record_index_range_inclusive' in row:
                        first, last = row['record_index_range_inclusive']
                        indexes = range(first, last+1)
                        covered.extend(indexes)
                        self.assertEqual(row['text'], ''.join(
                            source_texts[index - 1] for index in indexes))
                    else:
                        covered.append(row['record_index'])
                        self.assertEqual(row['text'], source_texts[row['record_index'] - 1])
                self.assertEqual(covered, [1, 2, 3, 4])
                receipt = self.live.status()['tool_deliveries'][-1]
                self.assertEqual(receipt['selected_record_indexes'], covered)
                self.assertEqual(receipt['selected_ref_count'], 4)
                self.assertEqual(self.live.context.status()['committed_records'], 4)
            self.assertEqual(self.reads, ['one', 'one'])


    async def test_long_framed_question_reads_sqlite_sends_complete_context_and_remains_cancellable(self):
        import tempfile
        from teams_transcription.meeting_store import MeetingStore
        from teams_transcription.b_response import MeetingStoreMinutesReader
        self.live._begin_native_speech = NativeLiveResponder._begin_native_speech.__get__(self.live)
        self.output.start_output()
        question = '請按本場紀錄逐項核對完整問題內容。' * 160 + '藍天專案的預算是多少？'
        self.assertGreater(len(question), 2000)
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('one')
            store.append_final('one', {'event_id':'budget', 'revision':1,
                'text':'藍天專案預算十萬元。', 'sequence':1,
                'timestamp':'2026-10-11T00:00:00Z'})
            reads = self.reads
            class Source:
                def read(self, meeting):
                    reads.append(meeting)
                    return store.read(meeting)
            self.live.tool.reader = MeetingStoreMinutesReader(Source())
            await self.send(question)
            first, events = self.payload()
            self.assertEqual(first['question'], question)
            self.assertIn('藍天專案預算十萬元', json.dumps(first['selected_source_reading'],
                ensure_ascii=False))
            self.assertTrue(first['selected_source_reading'])
            self.assertEqual(self.live.status()['tool_deliveries'][-1]['selected_ref_count'],
                len(first['selected_source_reading']))
            self.assertEqual(self.live.status()['tool_deliveries'][-1]['phase'], 'delivered')
            self.assertTrue(self.output.active)
            self.assertTrue(self.output.enabled)
            self.assertIsNone(self.live.error)
            self.assertTrue(all(len(event['content'][0]['text'].encode()) <= 500 for event in events))
            await self.live.handle_notification('thread/realtime/itemAdded', {
                'threadId':'owned-thread', 'item':{'type':'response.cancelled'}})
            self.assertEqual(self.live.state, 'sleeping')
            self.assertFalse(self.output.active)
            self.assertTrue(self.output.enabled)
            self.assertIsNone(self.live._request_id)
            self.assertIsNone(self.live._answer_scope)
            self.assertFalse(self.live.context.status()['pending'])
            store.append_final('one', {'event_id':'budget', 'revision':2,
                'text':'藍天專案預算十二萬元。', 'sequence':1,
                'timestamp':'2026-10-11T00:00:00Z'})
            start = self.live.peer.send_event.await_count
            await self.send(question)
            current, current_events = self.payload(start)
            self.assertNotEqual(first['request_id'], current['request_id'])
            self.assertGreater(current['result']['version'], first['result']['version'])
            wire = ''.join(event['content'][0]['text'] for event in current_events)
            self.assertIn('藍天專案預算十二萬元', wire)
            self.assertNotIn('藍天專案預算十萬元', wire)
            self.assertEqual(self.reads, ['one', 'one'])
            self.assertTrue(self.output.active)
            self.assertIsNone(self.live.error)
            await self.live.handle_notification('thread/realtime/itemAdded', {
                'threadId':'owned-thread', 'item':{'type':'response.cancelled'}})
            self.assertFalse(self.output.active)

    async def test_slow_prepare_has_no_outer_fifteen_second_cutoff(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def read(meeting):
            self.reads.append(meeting)
            entered.set()
            await release.wait()
            return context_snapshot(meeting, self.records, version=self.version)
        self.live.tool.reader = read
        original_timeout = asyncio.timeout
        def accelerate_old_cutoff(seconds):
            return original_timeout(0 if seconds == 15 else seconds)
        with patch('teams_transcription.b_native.asyncio.timeout', side_effect=accelerate_old_cutoff):
            await self.live._start_application_request(FramedQuestion('question', '預算多少？', 1))
            task = self.live._tool_task
            try:
                await asyncio.wait_for(entered.wait(), 1)
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                self.assertEqual(self.live.state, 'thinking')
                self.assertFalse(self.output.active)
                self.assertIsNone(self.live.error)
                self.live.peer.send_event.assert_not_awaited()
                release.set()
                await task
            finally:
                release.set()
        payload, _ = self.payload()
        self.assertEqual(payload['question'], '預算多少？')
        self.assertEqual(self.reads, ['one'])
        self.assertEqual(self.live.state, 'answering')

    async def assert_read_failure_allows_fresh_question(self, error):
        self.output.start_output()
        async def read(meeting):
            self.reads.append(meeting)
            if len(self.reads) == 1:
                raise error
            return context_snapshot(meeting, [context_record('new', '新預算十二萬元。')], version=2)
        self.live.tool.reader = read
        await self.send()
        self.assertEqual(self.live.state, 'sleeping')
        self.assertIsNone(self.live.error)
        self.assertIsNone(self.live.context_failure_code)
        self.assertIsNone(self.live._request_id)
        self.assertIsNone(self.live._answer_scope)
        self.assertFalse(self.live.context.status()['pending'])
        self.assertEqual(self.live.context.status()['committed_records'], 0)
        self.assertFalse(self.output.active)
        self.assertTrue(self.output.enabled)
        self.live.peer.send_event.assert_not_awaited()
        await asyncio.sleep(0)
        self.assertEqual(self.reads, ['one'], 'Failed questions must never auto-retry')
        await self.send(question='最新預算？')
        payload, _ = self.payload()
        self.assertEqual(payload['request_id'], 'question-2')
        self.assertEqual(payload['result']['version'], 2)
        self.assertIn('新預算十二萬元', json.dumps(payload['selected_source_reading'], ensure_ascii=False))
        self.assertEqual(self.reads, ['one', 'one'])
        self.assertEqual(self.live.state, 'answering')
        self.assertIsNone(self.live.error)
        self.assertEqual([row['phase'] for row in self.live.status()['tool_deliveries']],
            ['failed', 'delivered'])

    async def test_reader_io_failure_invalidates_only_its_question_without_retry(self):
        await self.assert_read_failure_allows_fresh_question(OSError('controlled unavailable SQLite read'))

    async def test_reader_timeout_invalidates_only_its_question_without_retry(self):
        await self.assert_read_failure_allows_fresh_question(TimeoutError('controlled reader timeout'))

    async def test_reader_integrity_error_still_fails_channel_closed(self):
        self.output.start_output()
        def invalid_read(meeting):
            self.reads.append(meeting)
            raise ValueError('controlled invalid scoped source contract')
        self.live.tool.reader = invalid_read
        await self.send()
        self.assertEqual(self.live.state, 'failed')
        self.assertEqual(self.live.context_failure_code, 'context_failure')
        self.assertFalse(self.output.enabled)
        self.assertFalse(self.output.active)
        self.assertIsNone(self.live._answer_scope)
        self.live.peer.send_event.assert_not_awaited()
        await self.send(question='下一題也不能繞過完整性錯誤')
        self.assertEqual(self.reads, ['one'])

    async def test_same_version_conflict_is_fatal_not_a_recoverable_read_failure(self):
        await self.send()
        sent = self.live.peer.send_event.await_count
        self.records[1] = context_record('event-one', '未升版本卻變更預算。')
        await self.send()
        self.assertEqual(self.live.state, 'failed')
        self.assertEqual(self.live.context_failure_code, 'context_same_version_conflict')
        self.assertEqual(self.live.peer.send_event.await_count, sent)
        self.assertIsNone(self.live._answer_scope)
        self.assertFalse(self.output.enabled)
        self.assertFalse(self.live.context.status()['pending'])

    async def test_version_rollback_is_fatal_not_a_recoverable_read_failure(self):
        await self.send()
        sent = self.live.peer.send_event.await_count
        self.version = 0
        await self.send()
        self.assertEqual(self.live.state, 'failed')
        self.assertEqual(self.live.context_failure_code, 'context_version_rollback')
        self.assertEqual(self.live.peer.send_event.await_count, sent)
        self.assertIsNone(self.live._answer_scope)
        self.assertFalse(self.output.enabled)
        self.assertFalse(self.live.context.status()['pending'])

    async def test_corrected_and_appended_minutes_arrive_with_unchanged_history(self):
        await self.send()
        start = self.live.peer.send_event.await_count
        self.records[1] = context_record('event-one','預算二十萬元。',revision=2)
        self.records.append(context_record('event-two','週五交付。'))
        self.version += 1
        await self.send()
        result, _ = self.payload(start)
        readings = result['selected_source_reading']
        self.assertEqual([row['text'] for row in readings],
            ['歷史：上次保留舊版。', '預算二十萬元。', '週五交付。'])
        self.assertEqual([row['record_index'] for row in readings], [1, 2, 3])
        self.assertEqual(readings[0]['provenance'], 'synthetic_history')
        self.assertEqual(self.live.context.status()['committed_records'], 3)

    async def test_budget_ten_to_twelve_current_fact_same_scope_no_stale_wire(self):
        await self.send()
        first,_=self.payload()
        first_reading=next(r for r in first['selected_source_reading'] if '十萬元' in r['text'])
        start=self.live.peer.send_event.await_count
        self.records[1] = context_record('event-one','預算十二萬元。',revision=2);self.version=2
        await self.send()
        second,events=self.payload(start)
        wire=''.join(e['content'][0]['text'] for e in events)
        self.assertIn('預算十二萬元',wire);self.assertNotIn('預算十萬元',wire)
        current=next(r for r in second['selected_source_reading'] if '十二萬元' in r['text'])
        self.assertEqual(current['record_index'],first_reading['record_index'])
        self.assertEqual(second['selected_source_reading'][0],first['selected_source_reading'][0])
        self.assertEqual(second['result']['meeting_id'],'one');self.assertEqual(second['result']['version'],2)
        self.assertEqual(self.reads,['one','one'])
        self.assertEqual(events[-1]['content'][0]['text'],'RESULT_COMPLETE')
        self.assertTrue(all('delegation_item_id' not in event for event in events))
        self.assertEqual(self.live.status()['tool_deliveries'][-1]['selected_record_indexes'],
            [row['record_index'] for row in second['selected_source_reading']])
        self.assertFalse(self.live.context.status()['model_consumption_verified'])

    async def test_all_wire_chunks_are_supported_owned_500_utf8_bytes(self):
        self.records.append(context_record('extended','會議😀中文預算。'*35))
        await self.send()
        header, events = self.payload()
        self.assertGreater(len(events), 2)
        self.assertEqual(events[-1]['content'][0]['text'], 'RESULT_COMPLETE')
        for event in events:
            self.assertEqual(set(event), {'type', 'channel', 'content'})
            self.assertEqual(event['type'], 'session.context.append')
            self.assertEqual(event['channel'], 'commentary')
            self.assertLessEqual(len(event['content'][0]['text'].encode('utf-8')), 500)
        self.assertEqual(header['kind'], 'application_question')
        self.assertEqual(header['request_id'], self.live._answer_scope['request_id'])
        self.assertEqual(header['question'], '預算多少？')
        self.assertIs(header['provider_final'], False)

    async def test_current_tiny_receipt_fact_survives_native_tool_wire_and_repeat(self):
        import tempfile
        from teams_transcription.meeting_store import MeetingStore
        from teams_transcription.b_response import MeetingStoreMinutesReader
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('one', {'available':True, 'session_id':'previous',
                'summary':{'重點':[{'text':'先前紀錄。', 'sources':[]}],
                    '決策':[], '待辦':[], '未決問題':[]},
                'open_tasks':[{'id':'pending-task', 'text':'核對舊檔。', 'owner':None,
                    'date':None, 'status':'open', 'origin_session_id':'previous', 'source_refs':[]}]})
            class Source:
                def read(self, meeting):
                    self_read_calls.append(meeting)
                    return store.read(meeting)
            self_read_calls = []
            self.live.tool.reader = MeetingStoreMinutesReader(Source())
            fragments = ['好。'] * 30 + ['代號', '：', '藍天', '預算', '一', '萬', '兩千', '元。']
            fragments[20] = '測試資訊'
            fragments[27] = '測試資訊'
            for receipt, text in enumerate(fragments, 1):
                store.record_increment('one', {'capture_id':'b'*32,
                    'receipt_sequence':receipt, 'part_index':0, 'text':text,
                    'received_at':'2026-10-11T00:00:00Z'})
                if receipt == 13:
                    await self.send(question='目前提供的測試資訊是什麼？')
            start = self.live.peer.send_event.await_count
            await self.send(question='剛才提供的測試資訊是什麼？')
            payload, events = self.payload(start)
            self.assertIn('代號：藍天預算一萬兩千元。',
                ''.join(row['text'] for row in payload['selected_source_reading']))
            self.assertEqual(events[-1]['content'][0]['text'], 'RESULT_COMPLETE')
            self.assertTrue(all('delegation_item_id' not in event for event in events))
            self.assertTrue(all(len(event['content'][0]['text'].encode()) <= 500 for event in events))
            start = self.live.peer.send_event.await_count
            await self.send(question='剛才提供的測試資訊是什麼？')
            repeated, repeat_events = self.payload(start)
            fact_indexes = set(range(31, 39))
            self.assertIn('代號：藍天預算一萬兩千元。',
                ''.join(row['text'] for row in repeated['selected_source_reading']))
            represented = set()
            for row in repeated['selected_source_reading']:
                if 'record_index_range_inclusive' in row:
                    first, last = row['record_index_range_inclusive']
                    represented.update(range(first, last+1))
                else:
                    represented.add(row['record_index'])
            self.assertTrue(fact_indexes.issubset(represented))
            self.assertEqual(self_read_calls, ['one', 'one', 'one'])
            self.assertEqual(repeat_events[-1]['content'][0]['text'], 'RESULT_COMPLETE')
            self.assertFalse(self.live.context.status()['model_consumption_verified'])


    async def test_audio_error_during_marker_return_never_commits_snapshot(self):
        async def fail_during_marker(event, *, capture_output=False):
            if event['content'][0]['text'] == 'RESULT_COMPLETE':
                await self.live._audio_error('controlled output failure during marker')
        self.live.peer.send_event.side_effect = fail_during_marker
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.send()
            commit.assert_not_called()
        self.assertEqual(self.live.state, 'failed')
        self.assertTrue(self.live.error)
        self.assertFalse(self.output.enabled)
        status = self.live.context.status()
        self.assertEqual(status['committed_records'], 0)
        self.assertFalse(status['pending'])
        self.assertGreater(status['sent_bytes'], 0)

    async def test_audio_error_on_last_data_chunk_never_begins_answer(self):
        plan = await self.live.context.prepare('預算多少？', session_id=self.live.thread_id,
            request_id='question-1')
        from teams_transcription.b_native import utf8_chunks
        last_chunk = list(utf8_chunks(plan.payload))[-1]
        self.live.context.invalidate(self.live.thread_id)
        async def fail_after_data(event, *, capture_output=False):
            if event['content'][0]['text'] == last_chunk:
                await self.live._audio_error('controlled data send output failure')
        self.live.peer.send_event.side_effect = fail_after_data
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.send()
            commit.assert_not_called()
        self.live._begin_native_speech.assert_not_awaited()
        self.assertFalse(any(call.args[0]['content'][0]['text'] == 'RESULT_COMPLETE'
                             for call in self.live.peer.send_event.await_args_list))
        self.assertEqual(self.live.context.status()['committed_records'], 0)

    async def test_new_request_during_marker_send_does_not_commit_previous_request(self):
        entered, release = asyncio.Event(), asyncio.Event()
        first = True
        async def block_first_marker(event, *, capture_output=False):
            nonlocal first
            if first and event['content'][0]['text'] == 'RESULT_COMPLETE':
                first = False
                entered.set()
                await release.wait()
        self.live.peer.send_event.side_effect = block_first_marker
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            self.sequence += 1
            await self.live._start_application_request(
                FramedQuestion('question', '預算多少？', self.sequence))
            previous = self.live._tool_task
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await self.send(question='誰負責？')
                release.set()
                await asyncio.gather(previous, return_exceptions=True)
            finally:
                release.set()
            self.assertEqual(commit.call_count, 1)
        self.assertEqual(self.reads, ['one', 'one'])
        self.assertEqual(self.live._begin_native_speech.await_count, 2)
        self.assertEqual(self.live.state, 'answering')

    async def test_marker_failure_is_not_committed_or_answered(self):
        async def failing(event, *, capture_output=False):
            if event['content'][0]['text'] == 'RESULT_COMPLETE':
                raise RuntimeError('controlled final marker failure')
        self.live.peer.send_event.side_effect = failing
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.send()
            commit.assert_not_called()
        self.assertEqual(self.live.state, 'sleeping')
        self.live._begin_native_speech.assert_awaited_once_with('answering')
        data_events = [call.args[0] for call in self.live.peer.send_event.await_args_list[:-1]]
        self.decode_payload(data_events)
        receipt = self.live.status()['tool_deliveries'][-1]
        self.assertEqual(receipt['sent_bytes'], receipt['payload_bytes'])
        self.assertFalse(receipt['marker_returned'])
        self.assertEqual(receipt['phase'], 'failed')
        self.assertEqual(self.live.context.status()['committed_records'], 0)
        self.assertFalse(self.output.active)
        self.assertIsNone(self.live.error)
        self.assertIsNone(self.live._request_id)
        self.assertIsNone(self.live._answer_scope)
        self.live.peer.send_event.side_effect = None
        start = self.live.peer.send_event.await_count
        await self.send(question='最新預算？')
        current, _ = self.payload(start)
        self.assertEqual(current['request_id'], 'question-2')
        self.assertEqual(self.reads, ['one', 'one'])
        self.assertEqual(self.live.state, 'answering')

    async def test_cancellation_before_first_chunk_returns_invalidates_without_commit(self):
        entered, blocked = asyncio.Event(), asyncio.Event()
        async def sending(event, *, capture_output=False):
            entered.set()
            await blocked.wait()
        self.live.peer.send_event.side_effect = sending
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.live._start_application_request(
                FramedQuestion('question', '預算多少？', 1))
            await asyncio.wait_for(entered.wait(), 1)
            task = self.live._tool_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            commit.assert_not_called()
        self.assertFalse(any(call.args[0]['content'][0]['text'] == 'RESULT_COMPLETE'
            for call in self.live.peer.send_event.await_args_list))
        self.assertEqual(self.live.context.status()['committed_records'], 0)
        self.live._begin_native_speech.assert_not_awaited()
        receipt = self.live.status()['tool_deliveries'][-1]
        self.assertEqual(receipt['sent_bytes'], 0)
        self.assertFalse(receipt['marker_returned'])
        self.assertEqual(receipt['phase'], 'cancelled')
        self.assertFalse(self.output.active)

    async def test_scope_change_during_prepare_does_not_send_or_commit(self):
        async def read(_):
            self.live.thread_id = 'different-thread'
            return context_snapshot('one',self.records,version=self.version)
        self.live.tool.reader = read
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.send()
            commit.assert_not_called()
        self.live.peer.send_event.assert_not_awaited()

    async def test_rebound_context_during_prepare_is_not_invalidated_as_old_session(self):
        async def read(_):
            self.live.thread_id = 'new-thread'
            self.live.context.bind_session('new-thread')
            return context_snapshot('one',self.records,version=self.version)
        self.live.tool.reader = read
        await self.send()
        self.live.peer.send_event.assert_not_awaited()
        self.assertEqual(self.live.context.session_id, 'new-thread')
        self.assertFalse(self.live.context.status()['pending'])

    async def test_thread_change_while_payload_send_waits_has_no_marker_commit_or_tx(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def block(event, *, capture_output=False):
            entered.set()
            await release.wait()
        self.output.start_output()
        self.live.peer.send_event.side_effect = block
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.live._start_application_request(
                FramedQuestion('question', '預算多少？', 1))
            task = self.live._tool_task
            try:
                await asyncio.wait_for(entered.wait(), 1)
                self.live.thread_id = 'replacement-thread'
                self.live.context.bind_session('replacement-thread')
                release.set()
                await task
            finally:
                release.set()
            commit.assert_not_called()
        self.assertFalse(any(call.args[0]['content'][0]['text'] == 'RESULT_COMPLETE'
            for call in self.live.peer.send_event.await_args_list))
        self.live._begin_native_speech.assert_not_awaited()
        self.assertFalse(self.output.active)
        self.assertEqual(self.live.context.session_id, 'replacement-thread')
        receipt = self.live.status()['tool_deliveries'][-1]
        self.assertGreater(receipt['sent_bytes'], 0)
        self.assertLess(receipt['sent_bytes'], receipt['payload_bytes'])
        self.assertFalse(receipt['marker_returned'])
        self.assertEqual(self.live.context.status()['committed_records'], 0)

    async def test_stop_while_query_waits_cannot_send_marker_commit_or_tx(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_read(meeting):
            self.reads.append(meeting)
            entered.set()
            await release.wait()
            return context_snapshot(meeting, self.records, version=self.version)
        self.live.tool.reader = delayed_read
        self.output.start_output()
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.live._start_application_request(
                FramedQuestion('question', '預算多少？', 1))
            await asyncio.wait_for(entered.wait(), 1)
            stopping = asyncio.create_task(self.live.close())
            try:
                await self.until(lambda:self.live._closing)
                release.set()
                await stopping
            finally:
                release.set()
                await asyncio.gather(stopping, return_exceptions=True)
            commit.assert_not_called()
        self.live.peer.send_event.assert_not_awaited()
        self.live._begin_native_speech.assert_not_awaited()
        self.assertFalse(self.output.active)
        self.assertFalse(self.output.enabled)

    async def test_new_request_while_query_waits_returns_only_fresh_request_context(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_first_read(meeting):
            self.reads.append(meeting)
            if len(self.reads) == 1:
                entered.set()
                await release.wait()
                return context_snapshot(meeting, [context_record('old', '舊預算十萬元。')], version=1)
            return context_snapshot(meeting, [context_record('new', '新預算十二萬元。')], version=2)
        self.live.tool.reader = delayed_first_read
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            self.sequence += 1
            await self.live._start_application_request(
                FramedQuestion('question', '預算多少？', self.sequence))
            old = self.live._tool_task
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await self.send(question='最新預算？')
                release.set()
                await asyncio.gather(old, return_exceptions=True)
            finally:
                release.set()
            self.assertEqual(commit.call_count, 1)
        payload, events = self.payload()
        self.assertEqual(payload['result']['version'], 2)
        wire = ''.join(event['content'][0]['text'] for event in events)
        self.assertIn('新預算十二萬元', wire)
        self.assertNotIn('舊預算十萬元', wire)
        self.assertEqual(sum(event['content'][0]['text'] == 'RESULT_COMPLETE' for event in events), 1)
        self.assertEqual(self.reads, ['one', 'one'])
        self.live._begin_native_speech.assert_awaited_once_with('answering')

    async def test_marker_cancellation_keeps_authorization_but_requires_a_fresh_request(self):
        self.output.start_output()
        self.live._begin_native_speech = NativeLiveResponder._begin_native_speech.__get__(self.live)
        entered, blocked = asyncio.Event(), asyncio.Event()
        async def sending(event, *, capture_output=False):
            if event['content'][0]['text'] == 'RESULT_COMPLETE':
                entered.set()
                await blocked.wait()
        self.live.peer.send_event.side_effect = sending
        with patch.object(self.live.context, 'commit', wraps=self.live.context.commit) as commit:
            await self.live._start_application_request(
                FramedQuestion('question', '預算多少？', 1))
            await asyncio.wait_for(entered.wait(), 1)
            task = self.live._tool_task
            await self.live.handle_notification('thread/realtime/itemAdded', {
                'threadId':'owned-thread', 'item':{'type':'response.cancelled'}})
            await asyncio.gather(task, return_exceptions=True)
            commit.assert_not_called()
        self.assertFalse(self.output.active)
        self.sink.pause.assert_awaited()
        self.assertIsNone(self.live._request_id)
        self.assertFalse(self.live.context.status()['pending'])
        self.live.peer.send_event.side_effect = None
        await self.live._start_application_request(FramedQuestion('question', '最新預算？', 2))
        await self.live._tool_task
        self.assertTrue(self.output.active)
        self.assertEqual(self.reads, ['one', 'one'])
        self.assertEqual([row['phase'] for row in self.live.status()['tool_deliveries']],
                         ['cancelled', 'delivered'])

    async def test_new_capture_during_request_cleanup_rejects_superseded_question(self):
        entered, release = asyncio.Event(), asyncio.Event()
        cancel = self.live._cancel_request
        async def delayed_cleanup():
            await cancel()
            entered.set()
            await release.wait()
        self.live._cancel_request = delayed_cleanup
        request = asyncio.create_task(self.live._start_application_request(
            FramedQuestion('question', '上一段問題', 1)))
        try:
            await asyncio.wait_for(entered.wait(), 1)
            ended = self.live.clock()
            self.assertTrue(self.live.push_rx('one', b'\x00\x10' * 480,
                started_at=ended-.02, ended_at=ended))
            release.set()
            self.assertFalse(await request)
            self.assertEqual(self.reads, [])
            self.live.peer.send_event.assert_not_awaited()
            self.assertIsNone(self.live._request_id)
        finally:
            release.set()
            await asyncio.gather(request, return_exceptions=True)
            self.live._cancel_request = cancel

    async def test_no_data_and_foreign_minutes_never_reuse_previous_answer(self):
        await self.send()
        self.live.tool.reader = lambda meeting:context_snapshot(meeting,[],version=2)
        start = self.live.peer.send_event.await_count
        await self.send()
        result, _ = self.payload(start)
        self.assertFalse(result['result']['available'])
        self.assertEqual(result['selected_source_reading'], [])
        self.live.tool.reader = lambda _:context_snapshot('other',[context_record('secret','secret')])
        start = self.live.peer.send_event.await_count
        await self.send()
        self.assertEqual(self.live.peer.send_event.await_count, start)
        self.assertEqual(self.live.state, 'failed')

    async def test_provider_receiver_events_do_not_start_local_queries(self):
        self.live.receiver = asyncio.create_task(self.live._receive())
        item = {'type':'handoff_request', 'handoff_id':'provider',
            'item_id':'provider', 'input_transcript':'小白小白，預算多少？'}
        for thread in ('foreign', 'owned-thread', 'owned-thread'):
            self.live.rpc.notifications.put_nowait({'method':'thread/realtime/itemAdded',
                'params':{'threadId':thread, 'item':item}})
        await self.until(lambda:self.live.rpc.notifications.empty())
        self.assertEqual(self.reads, [])
        self.assertEqual(self.live.tool_calls, 0)
        self.live.peer.send_event.assert_not_awaited()

    async def test_start_binds_actual_thread_without_output_authorization(self):
        version = SimpleNamespace(returncode=0,
            communicate=AsyncMock(return_value=(b'codex-cli 0.159.0-alpha.3\n', b'')))
        async def base_start(_):
            self.live.thread_id = 'new-started-thread'
        with patch('teams_transcription.b_native.asyncio.create_subprocess_exec',
                   new=AsyncMock(return_value=version)), \
             patch('teams_transcription.b_live.LiveSpeechChannel.start', new=base_start), \
             patch.object(self.live.context, 'bind_session', wraps=self.live.context.bind_session) as bind:
            await self.live.start()
        bind.assert_called_once_with('new-started-thread')
        self.assertEqual(self.live.state, 'sleeping')
        self.assertFalse(self.output.enabled)
        self.sink.start.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
