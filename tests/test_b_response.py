"""Host-bound structured query boundaries; no text-answerer or Markdown contract."""
import tempfile
import unittest

from teams_transcription.b_response import MeetingMinutesTool,MeetingStoreMinutesReader
from teams_transcription.storage import Session
from context_fixtures import context_record,context_snapshot


class ContextToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_saved_source_finality_transition(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create({'provider':'mock','source':'synthetic'})
            session.append_text('預算十萬元',0)
            reader = MeetingStoreMinutesReader(session.store)
            tool = MeetingMinutesTool(session.ident,reader)
            value = await tool.dispatch(tool.name,{'query':'預算？'})
            self.assertEqual(value['context'][0]['text'],'預算十萬元')
            self.assertEqual(value['completeness'],'partial')
            self.assertFalse(value['finalized'])
            session.finalize_minutes('stopped')
            sealed = await tool.dispatch(tool.name,{'query':'預算？'})
            self.assertTrue(sealed['finalized'])
            self.assertEqual(sealed['completeness'],'complete')

    async def test_tool_cannot_select_other_meeting_or_host_path(self):
        value = context_snapshot('one',[context_record('source-one','預算十萬元')])
        tool = MeetingMinutesTool('one',lambda _:value)
        for name,arguments in [('shell',{'query':'x'}),(tool.name,{'meeting_id':'two','query':'x'}),
                (tool.name,{'path':'/tmp/private','query':'x'}),(tool.name,{'query':3})]:
            with self.assertRaises(ValueError):
                await tool.dispatch(name,arguments)
        for wrong in [context_snapshot('two',[context_record('private','其他會議')]),'unscoped',{'meeting_id':'one'}]:
            with self.assertRaises(ValueError):
                await MeetingMinutesTool('one',lambda _:wrong).dispatch(tool.name,{'query':'x'})
        empty = await MeetingMinutesTool('one',lambda _:context_snapshot('one',[])).dispatch(tool.name,{'query':'x'})
        self.assertFalse(empty['available'])

    async def test_all_saved_sources_survive_large_context_in_source_order(self):
        records = [context_record(f'unrelated-{i}','不相關資料'*100) for i in range(100)]
        fact = '本次預算十萬元。' + '完整保存的細節。'*1000 + '最後期限星期五。'
        records.append(context_record('budget-source',fact))
        tool = MeetingMinutesTool('one',lambda _:context_snapshot('one',records))
        value = await tool.dispatch(tool.name,{'query':'預算多少？'})
        self.assertFalse(value['truncated'])
        self.assertEqual([row['text'] for row in value['context']],
            [record['text'] for record in records])
        self.assertEqual([row['record_index'] for row in value['context']],list(range(1,102)))
        self.assertEqual(value['source_locators'][-1]['source_id'],'budget-source')

    async def test_query_longer_than_two_thousand_characters_is_accepted_intact(self):
        query = '保存的原文'*501
        records = [context_record('long-query-source',query)]
        tool = MeetingMinutesTool('one',lambda _:context_snapshot('one',records))
        value = await tool.dispatch(tool.name,{'query':query})
        self.assertEqual(value['context'],[{'record_index':1,'text':query}])
        self.assertGreater(value['query_hit_count'],0)
