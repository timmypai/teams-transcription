"""Real SQLite + host adapter with synthetic external RPC, never automatic E2E."""
import unittest
from unittest.mock import AsyncMock,patch

from teams_transcription.postmeeting_adapter import process_postmeeting_once, PostmeetingAdapterError
from teams_transcription.summary import SECTIONS
import test_postmeeting_store as store_fixtures
import test_postmeeting_adapter as rpc_fixtures


class PostmeetingHostContractTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # This suite injects the external RPC; its version subprocess must use
        # the same controlled seam as test_postmeeting_adapter, not host Codex.
        self.version_check=AsyncMock()
        version=patch('teams_transcription.postmeeting_adapter._check_codex_version',self.version_check)
        version.start();self.addCleanup(version.stop)
        self.fixture = store_fixtures.PostmeetingStoreTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.occurrence,self.session_id = self.fixture.capture()
        self.source_id = self.store.list_postmeeting_snapshots(self.session_id)[0]['entries'][0]['id']

    def rpc(self, **options):
        summary = {section:[] for section in SECTIONS}
        summary['待辦'] = [dict(text='Prepare report',owner='',date='',sources=[self.source_id])]
        value = dict(summary=summary,conclusions=[],tasks=[dict(id=None,
            text='Prepare report',owner='',date='',status='open',sources=[self.source_id])])
        return rpc_fixtures.ControlledRPC(value=value,**options)

    async def test_explicit_host_invocation_publishes_once_without_rejoining_or_old_worker(self):
        rpc = self.rpc()
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc):
            result = await process_postmeeting_once(self.store,self.fixture.tmp.name)
            self.assertEqual(result['series_revision'],1)
            self.assertIsNone(await process_postmeeting_once(self.store,self.fixture.tmp.name))
        self.assertTrue(rpc.closed)
        self.version_check.assert_awaited_once_with('codex')
        state = self.store.series_state(self.occurrence['series_id'])
        self.assertEqual([task['text'] for task in state['open_tasks']],['Prepare report'])
        self.assertEqual(len(self.store.list_postmeeting_notifications()),1)
        self.assertEqual(len(self.store.list_runs()),1)
        self.assertEqual(len(self.store.list_summary_jobs()),1)
        self.assertEqual(self.store.list_postmeeting_jobs(self.session_id)[0]['state'],'published')

    async def test_unknown_external_cleanup_remains_blocked_without_publication_or_retry(self):
        rpc = self.rpc(cleanup_error=True)
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc):
            with self.assertRaisesRegex(PostmeetingAdapterError,'postmeeting_codex_cleanup_unknown'):
                await process_postmeeting_once(self.store,self.fixture.tmp.name)
            self.assertIsNone(await process_postmeeting_once(self.store,self.fixture.tmp.name))
        self.assertEqual(self.store.list_postmeeting_jobs(self.session_id)[0]['state'],'blocked')
        self.assertIsNone(self.store.series_state(self.occurrence['series_id']))
        self.assertEqual(self.store.list_postmeeting_notifications(),[])

    async def test_cancellation_preserves_unknown_cleanup_as_durable_block(self):
        import asyncio
        started=asyncio.Event()
        rpc=self.rpc(finish=False,cleanup_error=True)
        request=rpc.request
        async def observed(method,params):
            result=await request(method,params)
            if method=='turn/start':
                started.set()
            return result
        rpc.request=observed
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc):
            work=asyncio.create_task(process_postmeeting_once(self.store,self.fixture.tmp.name))
            await asyncio.wait_for(started.wait(),1)
            work.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await work
        job,=self.store.list_postmeeting_jobs(self.session_id)
        self.assertEqual((job['state'],job['error_code']),('blocked','postmeeting_codex_cleanup_unknown'))
        self.assertIsNone(self.store.claim_postmeeting('retry'))
        self.assertEqual(self.store.list_postmeeting_notifications(),[])

    async def test_heartbeat_failure_preserves_primary_error_and_blocks_unknown_cleanup(self):
        import asyncio
        from teams_transcription import postmeeting_adapter as adapter
        started=asyncio.Event()
        rpc=self.rpc(finish=False,cleanup_error=True)
        request=rpc.request
        async def observed(method,params):
            result=await request(method,params)
            if method=='turn/start':
                started.set()
            return result
        rpc.request=observed
        original_clock=self.store._lease_clock
        failed=False
        def unavailable_once():
            nonlocal failed
            if started.is_set() and not failed:
                failed=True
                raise OSError('synthetic lease clock failure')
            return original_clock()
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc), \
                patch.object(self.store,'_lease_clock',side_effect=unavailable_once), \
                patch.object(adapter,'LEASE_SECONDS',1):
            with self.assertRaisesRegex(OSError,'synthetic lease clock failure'):
                await process_postmeeting_once(self.store,self.fixture.tmp.name)
        self.assertTrue(rpc.closed)
        job,=self.store.list_postmeeting_jobs(self.session_id)
        self.assertEqual((job['state'],job['error_code']),('blocked','postmeeting_codex_cleanup_unknown'))
        self.assertIsNone(self.store.claim_postmeeting('retry'))

    async def test_expired_lease_still_records_settled_unknown_cleanup(self):
        import asyncio
        from teams_transcription import postmeeting_adapter as adapter
        started=asyncio.Event()
        rpc=self.rpc(finish=False,cleanup_error=True)
        request=rpc.request
        async def observed(method,params):
            result=await request(method,params)
            if method=='turn/start':
                started.set()
            return result
        rpc.request=observed
        clock=self.store._lease_clock
        def expired_after_start():
            boot,now=clock()
            return boot,now+10 if started.is_set() else now
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc), \
                patch.object(self.store,'_lease_clock',side_effect=expired_after_start), \
                patch.object(adapter,'LEASE_SECONDS',1):
            with self.assertRaisesRegex(ValueError,'stale_postmeeting_lease'):
                await process_postmeeting_once(self.store,self.fixture.tmp.name)
            job,=self.store.list_postmeeting_jobs(self.session_id)
            self.assertEqual((job['state'],job['error_code']),('blocked','postmeeting_codex_cleanup_unknown'))
            self.assertIsNone(self.store.claim_postmeeting('retry'))
        self.assertEqual(self.store.list_postmeeting_notifications(),[])

    async def test_generation_auth_error_survives_while_unknown_cleanup_blocks_store(self):
        rpc=self.rpc(account='apiKey',cleanup_error=True)
        with patch('teams_transcription.rpc.CodexRPC',return_value=rpc):
            with self.assertRaises(PostmeetingAdapterError) as raised:
                await process_postmeeting_once(self.store,self.fixture.tmp.name)
        self.assertEqual(raised.exception.code,'postmeeting_codex_auth_required')
        self.assertTrue(raised.exception.cleanup_unknown)
        job,=self.store.list_postmeeting_jobs(self.session_id)
        self.assertEqual((job['state'],job['error_code']),('blocked','postmeeting_codex_cleanup_unknown'))
        self.assertIsNone(self.store.claim_postmeeting('retry'))
        self.assertEqual(self.store.list_postmeeting_notifications(),[])
