import asyncio
import json
import unittest

from teams_transcription.b_context import MeetingQuestionContext, ContextFailure
from teams_transcription.b_response import MeetingMinutesTool
from context_fixtures import context_record, context_snapshot


class ContextTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.version = 1
        self.records = [context_record('history', '上次結論保留舊版。', provenance='sqlite_derived_summary'),
            context_record('speech1', '預算十萬元。', provenance='sqlite_source')]
        self.reads = 0
        async def read(meeting):
            self.reads += 1
            return context_snapshot(meeting, self.records, version=self.version)
        self.context = MeetingQuestionContext(MeetingMinutesTool('one', read))
        self.context.bind_session('live-1')

    @staticmethod
    def required(plan):
        return len(plan.payload.encode()) + len('RESULT_COMPLETE')

    @staticmethod
    def readings(plan):
        return json.loads(plan.payload)['selected_source_reading']

    def sent(self, plan):
        self.context.record_sent(plan, self.required(plan), session_id='live-1')
        self.context.commit(plan, session_id='live-1')

    async def test_every_question_reads_once_and_delivers_all_current_source_text(self):
        for number in (1, 2):
            plan = await self.context.prepare('預算多少？', session_id='live-1', request_id=f'q{number}')
            message = json.loads(plan.payload)
            self.assertEqual(self.reads, number)
            self.assertEqual([row['text'] for row in self.readings(plan)],
                [row['text'] for row in self.records])
            self.assertEqual([row['provenance'] for row in self.readings(plan)],
                ['sqlite_derived_summary', 'sqlite_source'])
            self.assertEqual(message['request_id'], f'q{number}')
            self.assertEqual(message['question'], '預算多少？')
            self.assertEqual(len(plan.result['selected_context_refs']), 2)
            self.assertFalse(plan.result['omitted_for_budget'])
            self.sent(plan)
        self.assertEqual(self.context.status()['committed_records'], 2)
        self.assertFalse(self.context.status()['model_consumption_verified'])

    async def test_wire_instruction_distinguishes_verbatim_quote_and_normal_answers(self):
        plan = await self.context.prepare('目前預算多少？', session_id='live-1')
        instruction = json.loads(plan.payload)['instruction']
        self.assertNotIn('Quote the supported answer once;', instruction)
        self.assertIn('If verbatim quotation is requested', instruction)
        self.assertIn('otherwise answer naturally', instruction)
        self.assertIn('prior assistant answers are not sources', instruction)

    async def test_latest_revision_and_deletion_replace_previous_snapshot_naturally(self):
        first = await self.context.prepare('預算', session_id='live-1')
        self.sent(first)
        self.version += 1
        self.records[1] = context_record('speech1', '預算十二萬元。', revision=2)
        revised = await self.context.prepare('預算', session_id='live-1')
        self.assertEqual([row['text'] for row in self.readings(revised)],
            ['上次結論保留舊版。', '預算十二萬元。'])
        self.assertEqual(revised.result['source_locators'][1]['revision'], 2)
        self.sent(revised)
        self.version += 1
        self.records.pop(1)
        removed = await self.context.prepare('預算', session_id='live-1')
        self.assertEqual([row['text'] for row in self.readings(removed)], ['上次結論保留舊版。'])
        self.assertEqual([row['source_id'] for row in removed.result['source_locators']], ['history'])

    async def test_full_large_sources_and_duplicate_text_keep_unique_citation_mapping(self):
        text = '完整原文。' * 2000
        self.records = [context_record(f'source-{index}', text) for index in range(520)]
        self.records[-1]['locator']['source_refs'] = [f'citation-{index}' for index in range(1000)]
        plan = await self.context.prepare('未命中查詢', session_id='live-1')
        self.assertEqual([row['text'] for row in self.readings(plan)], [text] * 520)
        self.assertEqual(len(plan.result['source_locators'][-1]['source_refs']), 1000)
        for index, (ref, locator) in enumerate(zip(plan.result['selected_context_refs'],
                plan.result['source_locators'], strict=True), 1):
            self.assertEqual(ref, {'record_index': index, 'char_start': 0, 'char_end': len(text)})
            self.assertEqual(locator['source_id'], f'source-{index-1}')
        self.sent(plan)
        self.assertEqual(self.context.status()['committed_records'], 520)
        self.assertIsNone(self.context.status()['total_limit_bytes'])

    def receipt(self, ident, text, capture, sequence, part=0, **metadata):
        row = context_record(ident, text, provenance='sqlite_source')
        row['locator'].update(capture_id=capture, receipt_sequence=sequence, part_index=part,
            source_state='application_segment', provider_final=False, quality='unfinalized',
            ordering='receipt_order', source_event_id=None, source_revision=None)
        row['locator'].update(metadata)
        return row

    async def test_fragment_projection_reads_each_source_once_without_inventing_continuity(self):
        self.records = [self.receipt('one', '松', 'a'*32, 1),
            self.receipt('two', '風', 'a'*32, 2),
            self.receipt('gap', '錯', 'a'*32, 4),
            self.receipt('other', '跨', 'b'*32, 1),
            self.receipt('next-part', '場', 'b'*32, 1, 1),
            self.receipt('missing-part', '缺', 'b'*32, 1, 3),
            self.receipt('revision', '改', 'b'*32, 1, 4, source_revision=2),
            context_record('final', '獨立定稿。')]
        plan = await self.context.prepare('剛才的資訊', session_id='live-1')
        readings = self.readings(plan)
        self.assertEqual([row['text'] for row in readings], ['松風', '錯', '跨場', '缺', '改', '獨立定稿。'])
        represented = []
        for reading in readings:
            if 'record_index_range_inclusive' in reading:
                start, end = reading['record_index_range_inclusive']
                indexes = range(start, end+1)
                self.assertEqual(reading['text'],
                    ''.join(self.records[index-1]['text'] for index in indexes))
                self.assertIs(reading['provider_final'], False)
                self.assertIs(reading['speech_continuity_verified'], False)
                self.assertEqual(reading['quality'], 'unfinalized')
                represented.extend(indexes)
            else:
                represented.append(reading['record_index'])
        self.assertEqual(represented, list(range(1, len(self.records)+1)))
        self.assertEqual(len(plan.result['selected_context_refs']), len(self.records))
        for reading in plan.result['selected_source_reading']:
            if 'record_ranges' in reading:
                self.assertEqual(reading['text'], ''.join(
                    self.records[ref['record_index']-1]['text'][ref['char_start']:ref['char_end']]
                    for ref in reading['record_ranges']))

    async def test_partial_send_and_invalidated_plan_never_commit(self):
        plan = await self.context.prepare('預算', session_id='live-1')
        with self.assertRaisesRegex(ContextFailure, 'context_send_incomplete'):
            self.context.commit(plan, session_id='live-1')
        payload_bytes = len(plan.payload.encode())
        self.context.record_sent(plan, payload_bytes, session_id='live-1')
        with self.assertRaisesRegex(ContextFailure, 'context_send_incomplete'):
            self.context.commit(plan, session_id='live-1')
        self.assertEqual(self.context.status()['committed_records'], 0)
        self.context.invalidate('live-1')
        retry = await self.context.prepare('預算', session_id='live-1')
        self.assertEqual(self.readings(retry), self.readings(plan))
        with self.assertRaises(ContextFailure):
            self.context.record_sent(plan, 1, session_id='live-1')
        self.context.record_sent(retry, self.required(retry)-1, session_id='live-1')
        with self.assertRaises(ContextFailure):
            self.context.commit(retry, session_id='live-1')
        self.context.record_sent(retry, 1, session_id='live-1')
        self.context.commit(retry, session_id='live-1')
        self.assertEqual(self.context.status()['sent_bytes'], payload_bytes+self.required(retry))
        self.assertEqual(self.context.status()['committed_records'], 2)
        with self.assertRaises(ContextFailure):
            self.context.commit(retry, session_id='live-1')

    async def test_wrong_thread_and_wrong_meeting_cannot_deliver(self):
        first = await self.context.prepare('預算', session_id='live-1')
        self.context.bind_session('live-2')
        with self.assertRaises(ContextFailure):
            self.context.record_sent(first, self.required(first), session_id='live-1')
        with self.assertRaises(ContextFailure):
            self.context.commit(first, session_id='live-2')
        current = await self.context.prepare('預算', session_id='live-2')
        self.assertEqual(self.readings(current), self.readings(first))
        self.assertEqual(self.context.status()['sent_bytes'], 0)
        self.context.invalidate('live-2')
        self.context.tool.reader = lambda _: context_snapshot('other', self.records)
        with self.assertRaisesRegex(ContextFailure, 'invalid_context_snapshot'):
            await self.context.prepare('預算', session_id='live-2')

    async def test_same_version_conflict_rollback_and_ambiguous_source_are_rejected(self):
        first = await self.context.prepare('預算', session_id='live-1')
        self.sent(first)
        self.records[1] = context_record('speech1', '預算八萬元。')
        with self.assertRaisesRegex(ContextFailure, 'same_version_conflict'):
            await self.context.prepare('預算', session_id='live-1')
        self.version = 0
        with self.assertRaisesRegex(ContextFailure, 'version_rollback'):
            await self.context.prepare('預算', session_id='live-1')
        self.version = 2
        self.records.append(context_record('duplicate-source', '偽裝相同來源。'))
        self.records[-1]['logical_id'] = 'speech1'
        with self.assertRaisesRegex(ContextFailure, 'ambiguous'):
            await self.context.prepare('預算', session_id='live-1')

    async def test_concurrent_or_invalidated_read_cannot_publish_a_plan(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def blocked(meeting):
            entered.set()
            await release.wait()
            return context_snapshot(meeting, self.records)
        self.context.tool.reader = blocked
        pending = asyncio.create_task(self.context.prepare('預算', session_id='live-1'))
        await entered.wait()
        try:
            with self.assertRaisesRegex(ContextFailure, 'context_plan_pending'):
                await self.context.prepare('預算', session_id='live-1')
            self.context.invalidate('live-1')
        finally:
            release.set()
        with self.assertRaisesRegex(ContextFailure, 'context_prepare_invalidated'):
            await pending
        self.assertFalse(self.context.status()['pending'])

    async def test_empty_snapshot_retains_truthful_availability(self):
        self.records = []
        plan = await self.context.prepare('預算', session_id='live-1')
        self.assertFalse(json.loads(plan.payload)['result']['available'])
        self.assertEqual(self.readings(plan), [])
        self.assertEqual(plan.result['selected_context_refs'], [])


if __name__ == '__main__':
    unittest.main()
