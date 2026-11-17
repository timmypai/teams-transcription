"""Codex summary consumer contracts, using controlled provider notifications."""
import asyncio
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch
from teams_transcription import summary


class FakeRPC:
    def __init__(self, value=None, *, failed=False, finish=True):
        self.notifications = asyncio.Queue()
        self.state_dir = Path('/controlled-summary')
        self.value, self.failed, self.finish = value, failed, finish
        self.requests = []
        self.timeout = 15

    async def start(self):
        return self

    async def close(self):
        pass

    async def request(self, method, params):
        self.requests.append((method, params))
        if method == 'account/read':
            return {'account':{'type':'chatgpt'}}
        if method == 'thread/start':
            return {'thread':{'id':'summary-thread'}, 'model':'gpt-6.1-sol', 'modelProvider':'openai'}
        if method == 'turn/start':
            await self.notifications.put({'method':'item/completed','params':{
                'threadId':'foreign-thread','turnId':'summary-turn',
                'item':{'type':'agentMessage','text':'private foreign message'}}})
            await self.notifications.put({'method':'turn/completed','params':{
                'threadId':'summary-thread','turn':{'id':'foreign-turn','status':'completed'}}})
            if self.value is not None:
                await self.notifications.put({'method':'item/completed','params':{
                    'threadId':'summary-thread','turnId':'summary-turn',
                    'item':{'type':'agentMessage','phase':'final_answer',
                        'text':self.value if isinstance(self.value,str) else json.dumps(self.value,ensure_ascii=False)}}})
            if self.finish:
                await self.notifications.put({'method':'turn/completed','params':{
                    'threadId':'summary-thread','turn':{'id':'summary-turn',
                        'status':'failed' if self.failed else 'completed',
                        'error':{'message':'private provider body'} if self.failed else None}}})
            return {'turn':{'id':'summary-turn'}}
        return {}


class SummaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_scoped_codex_turn_produces_cited_summary(self):
        value = {section:[] for section in summary.SECTIONS}
        value['重點'] = [{'text':'會議十點開始。','sources':['S0001'],'owner':'','date':''}]
        result = await summary.summarize_batch(FakeRPC(value),
            [{'id':'S0001','text':'會議十點開始。'}], None)
        self.assertEqual(result['重點'], value['重點'])
        self.assertEqual(result['model'], 'gpt-6.1-sol')
        self.assertNotIn('private foreign message', json.dumps(result))

    async def test_failed_turn_does_not_return_candidate_or_provider_body(self):
        value = {section:[] for section in summary.SECTIONS}
        with self.assertRaises(summary.SummaryGenerationError) as raised:
            await summary.summarize_batch(FakeRPC(value, failed=True),
                [{'id':'S0001','text':'會議十點開始。'}], None)
        self.assertEqual(raised.exception.code, 'codex_summary_failed')
        self.assertNotIn('private provider body', str(raised.exception))

    async def test_candidate_without_own_completed_turn_is_not_a_summary(self):
        value = {section:[] for section in summary.SECTIONS}
        with self.assertRaises(TimeoutError):
            async with asyncio.timeout(.01):
                await summary.summarize_batch(FakeRPC(value, finish=False),
                    [{'id':'S0001','text':'會議十點開始。'}], None)

    async def test_foreign_citation_is_rejected_before_return(self):
        value = {section:[] for section in summary.SECTIONS}
        value['重點'] = [{'text':'不得引用另一場會議。','sources':['FOREIGN'],'owner':'','date':''}]
        with self.assertRaisesRegex(ValueError, 'citation'):
            await summary.summarize_batch(FakeRPC(value),
                [{'id':'S0001','text':'會議十點開始。'}], None)

    async def test_multiple_progressing_groups_exceed_former_aggregate_deadline(self):
        entries = [{'id':f'S{index:04d}','text':'source'} for index in range(2)]
        rpc = FakeRPC(finish=False)
        producers = []
        original_request = rpc.request

        async def request(method, params):
            ack = await original_request(method, params)
            if method == 'turn/start':
                source = next(entry for entry in entries if entry['id'] in params['input'][0]['text'])
                async def produce():
                    for _ in range(3):
                        await asyncio.sleep(.04)
                        await rpc.notifications.put({'method':'item/agentMessage/delta','params':{
                            'threadId':'summary-thread','turnId':'summary-turn','delta':'progress'}})
                    value = {section:[] for section in summary.SECTIONS}
                    value['重點'] = [{'text':source['text'],'sources':[source['id']],'owner':'','date':''}]
                    await rpc.notifications.put({'method':'item/completed','params':{
                        'threadId':'summary-thread','turnId':'summary-turn',
                        'item':{'type':'agentMessage','text':json.dumps(value)}}})
                    await rpc.notifications.put({'method':'turn/completed','params':{
                        'threadId':'summary-thread','turn':{'id':'summary-turn','status':'completed'}}})
                producers.append(asyncio.create_task(produce()))
            return ack

        rpc.request = request
        session = Mock()
        session.config.return_value = {'provider':'codex'}
        session.summary_source.return_value = {'terminal':True,'entries':entries,
            'version':7,'source_sha256':'controlled-source-snapshot'}
        session.publish_summary.return_value = {'summary_state':'completed'}
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc), \
                patch.object(summary,'_check_codex_version',new=AsyncMock()), \
                patch.object(summary,'summary_groups',return_value=iter([[entry] for entry in entries])), \
                patch.object(summary,'SUMMARY_TIMEOUT',1.12), \
                patch.object(summary,'SUMMARY_CLEANUP_SECONDS',.01):
            # The former shared deadline would be .11 s; both groups take .24 s.
            result = await summary.generate(session,Path('/controlled-summary'),'codex',None)
        await asyncio.gather(*producers)
        self.assertEqual([item['sources'] for item in result['重點']], [['S0000'],['S0001']])
        session.publish_summary.assert_called_once()
        self.assertEqual(session.publish_summary.call_args.kwargs,{
            'expected_version':7,'expected_source_sha256':'controlled-source-snapshot','generation_mode':'codex'})
        self.assertEqual(result['summary_state'],'completed')

    async def test_owned_text_progress_can_outlast_no_progress_interval(self):
        rpc = FakeRPC(finish=False)
        async def progress():
            for _ in range(6):
                await asyncio.sleep(.01)
                await rpc.notifications.put({'method':'item/agentMessage/delta','params':{
                    'threadId':'summary-thread','turnId':'summary-turn','delta':'progress'}})
            value = {section:[] for section in summary.SECTIONS}
            await rpc.notifications.put({'method':'item/completed','params':{
                'threadId':'summary-thread','turnId':'summary-turn',
                'item':{'type':'agentMessage','text':json.dumps(value)}}})
            await rpc.notifications.put({'method':'turn/completed','params':{
                'threadId':'summary-thread','turn':{'id':'summary-turn','status':'completed'}}})
        producer = asyncio.create_task(progress())
        try:
            with patch.object(summary,'SUMMARY_TIMEOUT',.025):
                result = await summary.summarize_batch(rpc,[{'id':'S0001','text':'saved source'}],None)
            self.assertEqual(result['model'],'gpt-6.1-sol')
        finally:
            producer.cancel()
            await asyncio.gather(producer,return_exceptions=True)

    async def test_stalled_summary_times_out_interrupts_and_closes(self):
        rpc = FakeRPC(finish=False)
        rpc.close = AsyncMock()
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc), \
                patch.object(summary,'_check_codex_version',new=AsyncMock()), \
                patch.object(summary,'SUMMARY_TIMEOUT',.02):
            with self.assertRaises(summary.SummaryGenerationError) as raised:
                await summary._generate_codex([{'id':'S0001','text':'saved source'}],
                    Path('/controlled-summary'),'codex',None)
        self.assertEqual(raised.exception.code,'codex_summary_timeout')
        self.assertIn(('turn/interrupt',{'threadId':'summary-thread','turnId':'summary-turn'}),rpc.requests)
        rpc.close.assert_awaited_once()

    async def test_foreign_progress_does_not_keep_stalled_owned_turn_alive(self):
        rpc = FakeRPC(finish=False)
        async def foreign_progress():
            while True:
                for method, thread, turn, delta in (
                        ('item/agentMessage/delta','foreign-thread','summary-turn','not ours'),
                        ('item/agentMessage/delta','summary-thread','foreign-turn','not ours'),
                        ('unrecognized/noise','summary-thread','summary-turn','noise'),
                        ('item/agentMessage/delta','summary-thread','summary-turn','')):
                    await rpc.notifications.put({'method':method,'params':{
                        'threadId':thread,'turnId':turn,'delta':delta}})
                await asyncio.sleep(.005)
        producer = asyncio.create_task(foreign_progress())
        try:
            with patch.object(summary,'SUMMARY_TIMEOUT',.02):
                with self.assertRaises(TimeoutError):
                    await summary.summarize_batch(rpc,[{'id':'S0001','text':'saved source'}],None)
        finally:
            producer.cancel()
            await asyncio.gather(producer,return_exceptions=True)

    async def test_duplicate_or_unknown_item_noise_cannot_hold_summary_lease(self):
        rpc = FakeRPC(finish=False)
        async def noise():
            while True:
                for kind in ('reasoning', 'unknownItem'):
                    for method in ('item/started', 'item/completed'):
                        await rpc.notifications.put({'method':method,'params':{
                            'threadId':'summary-thread','turnId':'summary-turn',
                            'item':{'id':'same-item','type':kind}}})
                await asyncio.sleep(.005)
        with patch.object(summary,'SUMMARY_TIMEOUT',.05):
            task = asyncio.create_task(summary.summarize_batch(
                rpc,[{'id':'S0001','text':'saved source'}],None))
            producer = asyncio.create_task(noise())
            try:
                done,_ = await asyncio.wait({task},timeout=.25)
                self.assertIn(task,done,'Notification noise retained the summary job')
                with self.assertRaises(TimeoutError):
                    await task
                self.assertIn(('turn/interrupt',{
                    'threadId':'summary-thread','turnId':'summary-turn'}),rpc.requests)
            finally:
                producer.cancel()
                task.cancel()
                await asyncio.gather(producer,task,return_exceptions=True)

    def test_explicit_mock_extraction_preserves_original_citations_locally(self):
        entries = [{'id':'S0001','text':'meeting starts at ten'}]
        value = summary.mock_summary(entries)
        self.assertEqual(summary.validate_summary(value, entries), value)
        self.assertEqual(value['重點'][0]['sources'], ['S0001'])
