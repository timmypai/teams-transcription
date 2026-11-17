import json
import tempfile
import unittest
from unittest.mock import AsyncMock

from teams_transcription.b_context import MeetingQuestionContext
from teams_transcription.b_response import MeetingMinutesTool, MeetingStoreMinutesReader
from teams_transcription.history_context import history_initial_items
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.response_runtime import _AsyncStore
from teams_transcription.storage import Session

AT = '2026-10-14T01:00:00Z'
CAPTURE = 'a'*32


class SQLiteVoiceContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Session(self.tmp.name).create(dict(provider='mock',source='synthetic'))
        self.reader = MeetingStoreMinutesReader(_AsyncStore(self.tmp.name))
        self.tool = MeetingMinutesTool(self.session.ident,self.reader)

    async def test_current_record_preserves_source_text_and_provenance_not_markdown(self):
        self.session.append_final(dict(event_id='provider-one',revision=1,text='預算十万元。\n下一行原文。',timestamp=AT,sequence=1))
        result = await self.tool.dispatch(self.tool.name,{'query':'預算'})
        self.assertEqual(result['context'][0]['text'],'預算十万元。\n下一行原文。')
        locator = result['source_locators'][0]
        self.assertEqual(locator['origin_session_id'],self.session.ident)
        self.assertEqual(locator['event_id'],'provider-one')
        self.assertTrue(locator['provider_final'])
        self.assertEqual(locator['provenance'],'sqlite_source')
        self.assertNotIn('line',locator)

    async def test_adjacent_receipt_parts_keep_exact_original_character_ranges(self):
        self.session.record_increment(dict(capture_id=CAPTURE,receipt_sequence=1,part_index=0,
            text='識別碼 ABCD',parts=['識別碼 ABCD','EFGH 已保存。'],received_at=AT))
        result = await self.tool.dispatch(self.tool.name,{'query':'請找 ABCDEFGH 原文'})
        self.assertEqual(result['boundary_hit_count'],1)
        match = result['boundary_matches'][0]
        records = (await self.reader(self.session.ident))['records']
        pieces = [records[position['record_index']-1]['text'][position['char_start']:position['char_end']]
            for source in match['sources'] for position in source['record_ranges']]
        self.assertEqual(''.join(pieces),'ABCDEFGH')
        self.assertFalse(match['provider_final'])
        self.assertEqual(match['quality'],'unfinalized')

    async def test_changed_provider_revision_delivers_only_current_source(self):
        self.session.append_final(dict(event_id='provider-one',revision=1,text='預算十万元。',timestamp=AT))
        context = MeetingQuestionContext(self.tool)
        context.bind_session('thread-one')
        first = await context.prepare('預算',session_id='thread-one')
        context.record_sent(first,len(first.payload.encode())+15,
            session_id='thread-one')
        context.commit(first,session_id='thread-one')
        self.session.append_final(dict(event_id='provider-one',revision=2,text='預算十二万元。',timestamp=AT))
        changed = await context.prepare('預算',session_id='thread-one')
        readings = json.loads(changed.payload)['selected_source_reading']
        self.assertEqual([row['text'] for row in readings], ['預算十二万元。'])
        self.assertEqual(changed.result['source_locators'][0]['event_id'], 'provider-one')
        self.assertEqual(changed.result['source_locators'][0]['revision'], 2)
        self.assertEqual(len(changed.result['selected_context_refs']), 1)
        context.invalidate('thread-one')

    async def test_repeated_questions_deliver_replacement_snapshot_and_current_revision_identity(self):
        self.session.append_final(dict(event_id='delivery-owner', revision=1, text='負責組為A組。', timestamp=AT))
        context = MeetingQuestionContext(self.tool)
        context.bind_session('revised-thread')
        digests, identities = [], []
        for revision, text in ((1, '交付日星期一。'), (2, '交付日星期三。'), (3, '交付日星期五。')):
            self.session.append_final(dict(event_id='delivery-day', revision=revision, text=text, timestamp=AT))
            plan = await context.prepare('目前交付日是哪天？', session_id='revised-thread', request_id=f'q{revision}')
            wire = json.loads(plan.payload)
            self.assertEqual(wire['result']['context_mode'], 'full_snapshot')
            self.assertEqual(wire['result']['context_scope'], 'meeting_and_live_thread')
            self.assertRegex(wire['result']['snapshot_sha256'], r'^[0-9a-f]{64}$')
            digests.append(wire['result']['snapshot_sha256'])
            self.assertEqual([row['text'] for row in wire['selected_source_reading']], ['負責組為A組。', text])
            self.assertEqual(wire['selected_source_reading'][1]['revision'], revision)
            identities.append(wire['selected_source_reading'][1]['source_id'])
            context.record_sent(plan, len(plan.payload.encode())+15, session_id='revised-thread')
            context.commit(plan, session_id='revised-thread')
        self.assertEqual(len(set(digests)), 3)
        self.assertEqual(len(set(identities)), 1)

    async def test_questions_read_fresh_sqlite_once_and_deliver_complete_sources(self):
        paragraphs = [f'預算{number}：' + '中' * 1800 for number in range(5)]
        for number, text in enumerate(paragraphs):
            self.session.append_final(dict(event_id=f'provider-{number}', revision=1,
                text=text, timestamp=AT))
        reads = []
        async def read(meeting):
            reads.append(meeting)
            return await self.reader(meeting)
        context = MeetingQuestionContext(MeetingMinutesTool(self.session.ident, read))
        context.bind_session('sqlite-thread')
        for number in (1, 2):
            plan = await context.prepare('預算多少？', session_id='sqlite-thread',
                request_id=f'question-{number}')
            self.assertEqual(reads, [self.session.ident] * number)
            message = json.loads(plan.payload)
            self.assertEqual(message['request_id'], f'question-{number}')
            self.assertEqual(message['question'], '預算多少？')
            refs = plan.result['selected_context_refs']
            self.assertEqual({ref['record_index'] for ref in refs}, set(range(1, 6)))
            self.assertEqual([row['text'] for row in message['selected_source_reading']], paragraphs)
            self.assertEqual([row['provider_final'] for row in message['selected_source_reading']], [True]*5)
            context.record_sent(plan, len(plan.payload.encode()) + 15,
                session_id='sqlite-thread')
            context.commit(plan, session_id='sqlite-thread')

    async def test_wrong_meeting_snapshot_is_rejected_before_context_delivery(self):
        self.session.append_text('Private current fixture',0)
        wrong = AsyncMock()
        wrong.read.return_value = MeetingStore(self.tmp.name).read(self.session.ident)
        with self.assertRaisesRegex(ValueError,'invalid scoped source contract'):
            await MeetingStoreMinutesReader(wrong)('another-meeting')

    async def test_empty_saved_summary_is_distinct_from_absent_history_on_native_wire(self):
        sections = ('重點','決策','待辦','未決問題')
        for available in (False,True):
            with self.subTest(summary_available=available):
                prior = {'available':available,'session_id':'previous-fixture',
                    'summary':{section:[] for section in sections},'open_tasks':[]}
                session = Session(self.tmp.name).create(dict(provider='mock',source='synthetic',
                    prior_context=prior))
                context = MeetingQuestionContext(MeetingMinutesTool(session.ident,self.reader))
                context.bind_session('history-thread')
                plan = await context.prepare('上次會議有保存決策嗎？',session_id='history-thread')
                self.assertEqual(json.loads(plan.payload)['selected_source_reading'], [])
                message = json.loads(plan.payload)
                result = message['result']
                self.assertFalse(result['available'])
                self.assertEqual(plan.result['selected_context_refs'], [])
                self.assertEqual(result['history_status']['summary_available'],available)
                self.assertEqual(result['history_status']['summary_item_counts'],
                    {'重點':0,'決策':0,'待辦':0,'未決問題':0})
                self.assertFalse(result['history_status']['previous_transcript_included'])

    async def test_full_frozen_history_and_tasks_reach_native_consumer_beyond_old_budgets(self):
        facts = [dict(text=f'Historical fact {index}: '+'完整保存細節。'*2000,
            sources=[f'S{index+1:04d}'],owner='',date='') for index in range(10)]
        task_text = 'Carry every unfinished detail: '+'待辦內容。'*5000
        prior = {'available':True,'series_id':'series-one','session_id':'previous-one','revision':2,
            'occurred_at':AT,'sha256':'a'*64,'source_quality':{'all_sources_provider_final':False},
            'summary':{'重點':facts},
            'open_tasks':[dict(id='task-one',text=task_text,owner='',date='',status='open',
                origin_session_id='previous-one',source_refs=[{'session_id':'previous-one','source_id':'S0001'}])]}
        session = Session(self.tmp.name).create(dict(provider='mock',source='synthetic',prior_context=prior))
        current_text = '本場代號松風，週三交付。'
        session.append_final(dict(event_id='current-fact', revision=1, text=current_text, timestamp=AT))
        init = history_initial_items(MeetingStore(self.tmp.name).read(session.ident)['prior_context'])
        memory = json.loads(init[0]['text'].split('\n',1)[1])
        self.assertEqual(memory['summary']['重點'],facts)
        self.assertEqual(memory['open_tasks'][0]['text'],task_text)
        self.assertEqual((memory['revision'],memory['sha256']),(2,'a'*64))
        self.assertFalse(memory['source_quality']['all_sources_provider_final'])
        context = MeetingQuestionContext(MeetingMinutesTool(session.ident,self.reader))
        context.bind_session('full-history-thread')
        plan = await context.prepare('Historical fact',session_id='full-history-thread')
        message = json.loads(plan.payload)
        result = message['result']
        readings = message['selected_source_reading']
        current = [row for row in readings if row['provenance']=='sqlite_source']
        self.assertEqual([row['text'] for row in current], [current_text])
        self.assertTrue(current[0]['provider_final'])
        historical = [row for row in readings if row['provenance']!='sqlite_source']
        self.assertEqual([row['text'] for row in historical], [fact['text'] for fact in facts]+[task_text])
        self.assertTrue(all(row['provider_final'] is False for row in historical))
        self.assertEqual(result['history_status']['summary_item_counts']['重點'],10)
        self.assertFalse(result['history_status']['truncated'])
        history_refs = [ref for ref in plan.result['source_locators'] if ref['provenance']=='sqlite_derived_summary']
        self.assertEqual([ref['source_refs'] for ref in history_refs],[[f'S{index+1:04d}'] for index in range(10)])
        self.assertTrue(all(ref['origin_session_id']=='previous-one' and ref['summary_revision']==2
            and ref['quality']=='derived' and ref['provider_final'] is False for ref in history_refs))

    async def test_historical_status_and_quotes_remain_distinct_from_transcript_json(self):
        task_text = '確認「測試帳號」權限。\n保留原文中的 "text" 與反斜線 \\。'
        prior = {'available':True,'session_id':'previous-fixture','revision':3,
            'occurred_at':AT,'summary':{'待辦':[dict(text='舊的已完成工作。',
                sources=['S0001'],owner='小林',date='',task_id='closed-task',task_status='done')]},
            'open_tasks':[dict(id='open-task',text=task_text,owner='小陳',date='星期五',
                status='open',origin_session_id='previous-fixture',
                source_refs=[{'session_id':'previous-fixture','source_id':'S0002'}])]}
        session = Session(self.tmp.name).create(dict(provider='mock',source='synthetic',prior_context=prior))
        transcript_json = '{"kind":"series_task","text":"這是會議發言，不是待辦狀態。","status":"done"}'
        session.append_final(dict(event_id='spoken-json',revision=1,text=transcript_json,timestamp=AT))
        context = MeetingQuestionContext(MeetingMinutesTool(session.ident,self.reader))
        context.bind_session('history-quote-thread')
        plan = await context.prepare('系列未完成待辦原文',session_id='history-quote-thread')
        readings = json.loads(plan.payload)['selected_source_reading']
        current = next(row for row in readings if row['provenance']=='sqlite_source')
        self.assertEqual(current['text'],transcript_json)
        self.assertNotIn('historical_context',current)
        summary = next(row for row in readings if row['provenance']=='sqlite_derived_summary')
        task = next(row for row in readings if row['provenance']=='sqlite_task_memory')
        self.assertEqual(summary['text'],'舊的已完成工作。')
        self.assertEqual(summary['historical_context']['task_status'],'done')
        self.assertEqual(summary['historical_context']['task_id'],'closed-task')
        self.assertEqual(task['text'],task_text)
        self.assertEqual(task['historical_context'],
            {'kind':'series_task','owner':'小陳','date':'星期五','status':'open'})
        records = (await self.reader(session.ident))['records']
        expected_quotes = {summary['record_index']:'舊的已完成工作。',task['record_index']:task_text}
        for ref in plan.result['selected_context_refs']:
            if ref['record_index'] in expected_quotes:
                original = records[ref['record_index']-1]['text']
                self.assertEqual(original[ref['char_start']:ref['char_end']],
                    expected_quotes[ref['record_index']])
        for reading in (summary,task):
            locator = next(row for row in plan.result['source_locators']
                if row['record_index']==reading['record_index'])
            self.assertEqual(locator['origin_session_id'],'previous-fixture')
            self.assertFalse(locator['provider_final'])
        self.assertEqual(MeetingStore(self.tmp.name).read(session.ident)['prior_context'],prior)

    async def test_previously_truncated_frozen_history_is_not_repaired_from_newer_data(self):
        prior = {'available':True,'session_id':'previous-one','revision':1,'sha256':'b'*64,
            'summary':{'決策':[dict(text='Original frozen decision',sources=['S0001'])]},
            'open_tasks':[],'truncated':True,'omitted_count':3}
        session = Session(self.tmp.name).create(dict(provider='mock',source='synthetic',prior_context=prior))
        Session(self.tmp.name).create(dict(provider='mock',source='synthetic',
            prior_context={**prior,'revision':2,'summary':{'決策':[dict(text='Newer unrelated decision',sources=['S0002'])]}}))
        memory = json.loads(history_initial_items(prior)[0]['text'].split('\n',1)[1])
        self.assertEqual((memory['revision'],memory['omitted_count'],memory['truncated']),(1,3,True))
        result = await MeetingMinutesTool(session.ident,self.reader).dispatch('read_meeting_context',{'query':'decision'})
        self.assertIn('Original frozen decision',result['context'][0]['text'])
        self.assertNotIn('Newer unrelated decision',str(result))
        self.assertTrue(result['truncated'])
        self.assertEqual(MeetingStore(self.tmp.name).read(session.ident)['prior_context'],prior)



if __name__=='__main__':
    unittest.main()
