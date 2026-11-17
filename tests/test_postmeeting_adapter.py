"""Synthetic RPC boundary contracts; these are not real Codex acceptance."""
import asyncio
import json
import hashlib
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from teams_transcription import postmeeting_adapter as adapter
from teams_transcription.summary import SECTIONS
from teams_transcription.meeting_store import canonical_json


def fixture_input():
    return {'job_id':'job-one','session_id':'session-one','occurrence_id':'occurrence-one',
        'series_id':'series-one','pipeline_version':'1','source':{
            'source_sha256':'fixture-source','source_version':3,
            'entries':[{'id':'S0001','text':'Finish the report.'}]},
        'prior':{'revision':0,'conclusions':[],'open_tasks':[],'closed_tasks':[]}}


def fixture_result():
    summary = {section:[] for section in SECTIONS}
    summary['待辦'] = [dict(text='Finish the report.',sources=['S0001'],owner='',date='')]
    return {'summary':summary,'conclusions':[],'tasks':[
        dict(id=None,text='Finish the report.',owner='',date='',status='open',sources=['S0001'])]}


class ControlledRPC:
    def __init__(self, *, value=None, finish=True, status='completed', account='chatgpt',
                 provider='openai', model='gpt-6.1-sol', tool=False, cleanup_error=False):
        self.state_dir = Path('/controlled-postmeeting')
        self.timeout = 1
        self.notifications = asyncio.Queue()
        self.value = fixture_result() if value is None else value
        self.finish,self.status,self.account,self.provider = finish,status,account,provider
        self.model,self.tool,self.cleanup_error = model,tool,cleanup_error
        self.requests = []
        self.closed = False

    async def start(self):
        return self

    async def close(self):
        self.closed = True
        if self.cleanup_error:
            raise RuntimeError('private cleanup body')

    async def request(self, method, params):
        self.requests.append((method,params))
        if method=='account/read':
            return {'account':{'type':self.account}}
        if method=='thread/start':
            return {'thread':{'id':'owned-thread'},'model':self.model,'modelProvider':self.provider}
        if method=='turn/start':
            await self.notifications.put({'method':'item/completed','params':{
                'threadId':'foreign-thread','turnId':'owned-turn',
                'item':{'type':'agentMessage','text':'private foreign content'}}})
            await self.notifications.put({'method':'turn/completed','params':{
                'threadId':'owned-thread','turn':{'id':'foreign-turn','status':'completed'}}})
            if self.tool:
                await self.notifications.put({'method':'item/started','params':{
                    'threadId':'owned-thread','turnId':'owned-turn',
                    'item':{'type':'commandExecution','id':'forbidden-command'}}})
            await self.notifications.put({'method':'item/completed','params':{
                'threadId':'owned-thread','turnId':'owned-turn',
                'item':{'type':'agentMessage','id':'answer-one','phase':'final_answer',
                    'text':self.value if isinstance(self.value,str) else json.dumps(self.value)}}})
            if self.finish:
                await self.notifications.put({'method':'turn/completed','params':{
                    'threadId':'owned-thread','turn':{'id':'owned-turn','status':self.status}}})
            return {'turn':{'id':'owned-turn'}}
        return {}


class PostmeetingAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def generate(self, rpc, value=None):
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc), \
                patch.object(adapter,'_check_codex_version',new=AsyncMock()):
            return await adapter.generate_postmeeting(value or fixture_input(),'/controlled-postmeeting')

    async def test_bound_completed_turn_yields_actual_host_receipt_after_cleanup(self):
        rpc = ControlledRPC()
        completed = await self.generate(rpc)
        self.assertIsInstance(completed,adapter.CompletedPostmeeting)
        self.assertEqual(completed.result,fixture_result())
        self.assertEqual((completed.model,completed.thread_id,completed.turn_id),
            ('gpt-6.1-sol','owned-thread','owned-turn'))
        self.assertEqual(completed.input_sha256,
            hashlib.sha256(canonical_json(fixture_input()).encode()).hexdigest())
        self.assertTrue(rpc.closed)
        self.assertNotIn('private foreign content',json.dumps(completed.result))
        config = next(params for method,params in rpc.requests if method=='thread/start')
        self.assertEqual((config['approvalPolicy'],config['sandbox']),('never','read-only'))
        self.assertEqual(config['config']['mcp_servers'],{})
        self.assertFalse(config['config']['features']['shell_tool'])
        self.assertEqual(config['config']['web_search'],'disabled')

    async def test_receipt_requires_own_completed_turn_and_cleanup(self):
        for options,code in (({'status':'failed'},'postmeeting_codex_failed'),
                ({'account':'apiKey'},'postmeeting_codex_auth_required'),
                ({'provider':'foreign'},'postmeeting_codex_failed'),
                ({'model':'gpt-live-1-codex'},'postmeeting_codex_failed'),
                ({'tool':True},'postmeeting_codex_tool_rejected'),
                ({'cleanup_error':True},'postmeeting_codex_cleanup_unknown')):
            with self.subTest(options=options):
                rpc = ControlledRPC(**options)
                with self.assertRaises(adapter.PostmeetingAdapterError) as raised:
                    await self.generate(rpc)
                self.assertEqual(raised.exception.code,code)
                self.assertTrue(rpc.closed)
                self.assertNotIn('private',str(raised.exception))

    async def test_uncompleted_candidate_times_out_and_interrupts_owned_turn(self):
        rpc = ControlledRPC(finish=False)
        with patch.object(adapter,'POSTMEETING_INACTIVITY_SECONDS',.01):
            with self.assertRaisesRegex(adapter.PostmeetingAdapterError,'postmeeting_codex_timeout'):
                await self.generate(rpc)
        self.assertIn(('turn/interrupt',{'threadId':'owned-thread','turnId':'owned-turn'}),rpc.requests)
        self.assertTrue(rpc.closed)

    async def test_foreign_citation_or_model_metadata_in_result_is_rejected(self):
        value = fixture_result()
        value['tasks'][0]['sources'] = ['FOREIGN']
        for candidate in (value,{**fixture_result(),'model':'fabricated-model'},
                '{"summary":{},"summary":{},"conclusions":[],"tasks":[]}',
                '{"summary":NaN,"conclusions":[],"tasks":[]}'):
            with self.subTest(candidate=candidate):
                with self.assertRaisesRegex(adapter.PostmeetingAdapterError,'postmeeting_codex_failed'):
                    await self.generate(ControlledRPC(value=candidate))

    async def test_wrong_cli_version_never_starts_rpc(self):
        with patch.object(adapter,'_check_codex_version',new=AsyncMock(side_effect=RuntimeError('private version'))), \
                patch('teams_transcription.rpc.CodexRPC') as rpc:
            with self.assertRaisesRegex(adapter.PostmeetingAdapterError,'postmeeting_codex_failed'):
                await adapter.generate_postmeeting(fixture_input(),'/controlled-postmeeting')
        rpc.assert_not_called()

    async def test_one_shot_claims_once_and_publishes_only_after_provider_cleanup(self):
        class Store:
            def __init__(self):
                self.claims,self.publications = 0,[]
            def claim_postmeeting(self,owner,lease_seconds=90):
                self.claims += 1
                return {'id':'lease'}
            def postmeeting_input(self,lease):
                return fixture_input()
            def heartbeat_postmeeting(self,lease,lease_seconds=90):
                pass
            def publish_postmeeting(self,lease,completed):
                self.publications.append(completed)
                return {'state':'published','revision':1}
            def fail_postmeeting(self,lease,code):
                raise AssertionError('valid turn must not fail')
        store,rpc = Store(),ControlledRPC()
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc), \
                patch.object(adapter,'_check_codex_version',new=AsyncMock()):
            result = await adapter.process_postmeeting_once(store,'/controlled-postmeeting')
        self.assertEqual(result,{'state':'published','revision':1})
        self.assertEqual(store.claims,1)
        self.assertEqual(len(store.publications),1)
        self.assertTrue(rpc.closed)

    async def test_cancelled_invocation_interrupts_and_cleans_up_without_publication(self):
        started = asyncio.Event()
        class Store:
            def __init__(self):
                self.failed,self.published = [],[]
            def claim_postmeeting(self,owner,seconds):
                return {'id':'lease'}
            def postmeeting_input(self,lease):
                return fixture_input()
            def heartbeat_postmeeting(self,lease,seconds):
                pass
            def publish_postmeeting(self,lease,result):
                self.published.append(result)
            def fail_postmeeting(self,lease,code):
                self.failed.append(code)
        rpc,store = ControlledRPC(finish=False),Store()
        original = rpc.request
        async def request(method,params):
            result = await original(method,params)
            if method=='turn/start':
                started.set()
            return result
        rpc.request = request
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc), \
                patch.object(adapter,'_check_codex_version',new=AsyncMock()):
            work = asyncio.create_task(adapter.process_postmeeting_once(store,'/controlled-postmeeting'))
            await asyncio.wait_for(started.wait(),1)
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await work
        self.assertTrue(rpc.closed)
        self.assertEqual(store.published,[])
        self.assertEqual(store.failed,['postmeeting_cancelled'])
        self.assertTrue(any(method=='turn/interrupt' for method,_ in rpc.requests))

    async def test_lease_loss_cancels_owned_turn_before_release(self):
        class Store:
            def __init__(self):
                self.heartbeats,self.failed,self.published = 0,[],[]
            def claim_postmeeting(self,owner,seconds):
                return {'id':'lease'}
            def postmeeting_input(self,lease):
                return fixture_input()
            def heartbeat_postmeeting(self,lease,seconds):
                self.heartbeats += 1
                if self.heartbeats>1:
                    raise ValueError('stale lease private token')
            def publish_postmeeting(self,lease,result):
                self.published.append(result)
            def fail_postmeeting(self,lease,code):
                self.failed.append(code)
        rpc,store = ControlledRPC(finish=False),Store()
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc), \
                patch.object(adapter,'_check_codex_version',new=AsyncMock()), \
                patch.object(adapter,'LEASE_SECONDS',.03):
            with self.assertRaisesRegex(ValueError,'stale lease'):
                await adapter.process_postmeeting_once(store,'/controlled-postmeeting')
        self.assertTrue(rpc.closed)
        self.assertEqual(store.published,[])
        self.assertEqual(store.failed,['postmeeting_publication_failed'])

    async def test_empty_queue_does_not_start_provider_or_spawn_polling(self):
        class Store:
            calls = 0
            def claim_postmeeting(self,owner,seconds):
                self.calls += 1
                return None
        store = Store()
        with patch('teams_transcription.rpc.CodexRPC') as rpc:
            self.assertIsNone(await adapter.process_postmeeting_once(store,'/controlled-postmeeting'))
        self.assertEqual(store.calls,1)
        rpc.assert_not_called()

    async def test_cancellation_during_claim_releases_known_claim_after_it_finishes(self):
        import threading
        entered,release = threading.Event(),threading.Event()
        class Store:
            failed = []
            def claim_postmeeting(self,owner,seconds):
                entered.set()
                if not release.wait(2):
                    raise RuntimeError('test release missed')
                return {'id':'owned-claim'}
            def fail_postmeeting(self,lease,code):
                self.failed.append((lease['id'],code))
        store = Store()
        work = asyncio.create_task(adapter.process_postmeeting_once(store,'/controlled-postmeeting'))
        try:
            self.assertTrue(await asyncio.to_thread(entered.wait,1))
            work.cancel()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await work
        finally:
            release.set()
            if not work.done():
                work.cancel()
                await asyncio.gather(work,return_exceptions=True)
        self.assertEqual(store.failed,[('owned-claim','postmeeting_cancelled')])

    async def test_caller_mutation_after_start_cannot_change_prompt_or_receipt_binding(self):
        snapshot = fixture_input()
        expected = hashlib.sha256(canonical_json(snapshot).encode()).hexdigest()
        rpc = ControlledRPC()
        async def start():
            snapshot['source']['entries'][0]['text'] = 'mutated after start'
            return rpc
        rpc.start = start
        completed = await self.generate(rpc,snapshot)
        self.assertEqual(completed.input_sha256,expected)
        prompt = next(params for method,params in rpc.requests if method=='turn/start')['input'][0]['text']
        self.assertNotIn('mutated after start',prompt)

    async def test_primary_generation_error_survives_unknown_cleanup(self):
        for options,code in ((dict(account='apiKey'),'postmeeting_codex_auth_required'),
                (dict(status='failed'),'postmeeting_codex_failed'),
                (dict(finish=False),'postmeeting_codex_timeout')):
            with self.subTest(options=options), patch.object(adapter,'POSTMEETING_INACTIVITY_SECONDS',.01):
                rpc=ControlledRPC(cleanup_error=True,**options)
                with self.assertRaises(adapter.PostmeetingAdapterError) as raised:
                    await self.generate(rpc)
                self.assertEqual(raised.exception.code,code)
                self.assertTrue(raised.exception.cleanup_unknown)
