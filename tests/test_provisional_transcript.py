"""Synthetic ordered RPC previews; no Live, browser, meeting or audio device."""
import asyncio
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from teams_transcription.live import LiveProvider
from teams_transcription.meeting_store import MeetingStore, MeetingStoreError

from teams_transcription.provisional import ProvisionalTranscript, MAX_PROVISIONAL_BYTES
from teams_transcription.rx_audio_stats import RxAudioSnapshot
from teams_transcription.storage import Session
from teams_transcription.worker import run_session
from test_rx_contract import Fixture, config, native_transport
from test_meeting_store import event
from lifecycle_probe import LifecycleProbe, session_events


def preview(text='未確認的合成文字', ident='a', revision=1):
    return {'provisional_id':'provisional:'+ident*32, 'revision':revision,
            'text':text, 'truncated':False}


class ProvisionalStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store=MeetingStore(self.temp.name)
        self.store.create('a')
        self.store.create('b')

    def test_saved_preview_is_scoped_and_never_provider_final(self):
        self.store.save_provisional('a',preview())
        result=self.store.read('a')
        self.assertEqual(result['events'],[])
        self.assertEqual(result['provisionals'][0]['text'],preview()['text'])
        self.assertEqual(result['completeness'],'partial')
        self.assertFalse(result['sources'][0]['provider_final'])
        self.assertEqual(MeetingStore(self.temp.name).read('a')['provisionals'],result['provisionals'])
        self.assertEqual(self.store.read('b')['sources'],[])

    def test_revision_resend_reordering_and_conflict(self):
        first=self.store.save_provisional('a',preview())
        self.assertEqual(self.store.save_provisional('a',preview())['version'],first['version'])
        self.store.save_provisional('a',preview('最新',revision=3))
        self.store.save_provisional('a',preview('延遲舊版',revision=2))
        self.assertEqual(self.store.read('a')['provisionals'][0]['text'],'最新')
        with self.assertRaisesRegex(MeetingStoreError,'provisional_revision_conflict'):
            self.store.save_provisional('a',preview('衝突',revision=3))

    def test_stop_fail_and_restart_preserve_unfinalized_text(self):
        for status in ('stopped','failed','completed'):
            ident=status
            self.store.create(ident)
            self.store.save_provisional(ident,preview())
            self.store.finalize(ident,status)
            result=MeetingStore(self.temp.name).read(ident)
            self.assertEqual(result['events'],[])
            self.assertEqual(result['provisionals'][0]['text'],preview()['text'])
            with self.assertRaisesRegex(MeetingStoreError,'meeting_finalized'):
                self.store.save_provisional(ident,preview('較新',revision=2))

    def test_resolution_requires_same_meeting_durable_final_and_cannot_reopen(self):
        self.store.save_provisional('a',preview())
        resolved={**preview('',revision=2),'resolved':True,'final_event_id':'final'}
        self.store.append_final('b',event('final'))
        with self.assertRaisesRegex(MeetingStoreError,'provisional_final_missing'):
            self.store.save_provisional('a',resolved)
        self.store.append_final('a',event('final',text='官方終稿更正文字'))
        self.store.save_provisional('a',resolved)
        self.store.save_provisional('a',preview('延遲舊狀態'))
        result=self.store.read('a')
        self.assertEqual(result['provisionals'],[])
        self.assertEqual(result['events'][0]['text'],'官方終稿更正文字')
        with self.assertRaisesRegex(MeetingStoreError,'provisional_already_resolved'):
            self.store.save_provisional('a',preview('企圖重開',revision=3))

    

    def test_bounded_pending_segments_and_invalid_payloads(self):
        for ident in '01234567':
            self.store.save_provisional('a',preview(ident,ident))
        with self.assertRaisesRegex(MeetingStoreError,'provisional_capacity_or_identity'):
            self.store.save_provisional('a',preview('第九段','8'))
        for changed in ({'text':'a'*(MAX_PROVISIONAL_BYTES+1)}, {'revision':True},
                        {'source_role':'assistant'}, {'provisional_id':'../b'}, {'truncated':1}):
            with self.assertRaises(MeetingStoreError):
                self.store.save_provisional('b',{**preview(),**changed})

    def test_utf8_limit_freezes_prefix_without_splicing_after_loss(self):
        normal=ProvisionalTranscript()
        normal.current=normal.delta('中'*(MAX_PROVISIONAL_BYTES//3+1))
        self.assertLessEqual(len(normal.current['text'].encode()),MAX_PROVISIONAL_BYTES)
        self.assertTrue(normal.current['truncated'])
        self.assertIsNone(normal.delta('後續文字'))
        self.store.save_provisional('a',normal.current)
        self.assertTrue(self.store.read('a')['provisionals'][0]['truncated'])

    


class ProvisionalProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.session=Session(self.temp.name).create(config())
        async def final(value): self.session.append_final(value)
        async def provisional(value): self.session.save_provisional(value)
        self.provider=LiveProvider(None,'/tmp/unused-provisional',AsyncMock(),AsyncMock(),
                                   on_final=final,on_provisional=provisional)
        self.provider.thread_id='own'
        self.provider.rpc=SimpleNamespace(notifications=asyncio.Queue())
        self.receiver=asyncio.create_task(self.provider._receive())

    async def asyncTearDown(self):
        self.receiver.cancel()
        await asyncio.gather(self.receiver,return_exceptions=True)
        self.temp.cleanup()

    async def notify(self,method='thread/realtime/transcript/delta',**params):
        await self.provider.rpc.notifications.put({'method':method,'params':{'threadId':'own',**params}})
        await asyncio.sleep(.01)

    def result(self): return MeetingStore(self.temp.name).read(self.session.ident)

    async def test_exact_deltas_preserve_spaces_without_double_channel_append(self):
        await self.provider._data_event({'type':'input_transcript.added','item':{'text':'第一句 '}})
        for part in ('第一句 ','第二句','。'):
            await self.notify(role='user',delta=part)
            await self.provider._data_event({'type':'input_transcript.added','item':{'text':part}})
        self.assertEqual(self.result()['provisionals'][0]['text'],'第一句 第二句。')
        self.assertEqual(self.result()['events'],[])
        self.assertEqual(self.provider.source_count,0)

    async def test_roles_foreign_scope_missing_thread_and_shapes_are_rejected(self):
        for role in ('assistant','tool',None):
            await self.notify(role=role,delta='不可收入')
        await self.notify(threadId='foreign',role='user',delta='別場秘密')
        await self.notify(threadId=None,role='user',delta='無thread')
        for delta in (None,12,[],{},''):
            await self.notify(role='user',delta=delta)
        await self.provider._data_event({'type':'output_transcript.added','role':'user','item':{'text':'助理'}})
        self.assertEqual(self.result()['provisionals'],[])
        self.assertIsNone(self.provider.error)

    async def test_same_channel_repetition_is_preserved_not_guessed_as_resend(self):
        await self.notify(role='user',delta='是')
        await self.notify(role='user',delta='是')
        self.assertEqual(self.result()['provisionals'][0]['text'],'是是')

    async def test_idless_out_of_order_observation_is_not_reconstructed(self):
        await self.notify(role='user',delta='乙')
        await self.notify(role='user',delta='甲')
        self.assertEqual(self.result()['provisionals'][0]['text'],'乙甲')
        self.assertEqual(self.result()['events'],[])

    async def test_real_rpc_final_replaces_preview_and_assistant_done_cannot(self):
        await self.notify(role='user',delta='尚未完成')
        await self.notify('thread/realtime/transcript/done',role='assistant',text='回答')
        self.assertEqual(len(self.result()['provisionals']),1)
        await self.notify('thread/realtime/transcript/done',role='user',text='正式終稿更正文字')
        result=self.result()
        self.assertEqual(result['provisionals'],[])
        self.assertEqual(result['events'][0]['text'],'正式終稿更正文字')
        await self.notify(role='user',delta='下一段未定稿')
        self.assertEqual(len(self.result()['provisionals']),1)

    async def test_rpc_done_requires_exact_event_thread_even_without_pending(self):
        invalid_scopes=({}, {'threadId':None}, {'threadId':'foreign'})
        for scope in invalid_scopes:
            with self.subTest(scope=scope):
                params={**scope,'role':'user','text':'不可寫入的終稿'}
                await self.provider._source('thread/realtime/transcript/done',params)
                await self.provider.rpc.notifications.put({'method':'thread/realtime/transcript/done','params':params})
                await asyncio.sleep(.01)
                self.assertEqual(self.result()['events'],[])
        await self.notify(role='user',delta='自己的暫定文字')
        for scope in invalid_scopes:
            with self.subTest(pending_scope=scope):
                await self.provider.rpc.notifications.put({'method':'thread/realtime/transcript/done',
                    'params':{**scope,'role':'user','text':'不可解除預覽'}})
                await asyncio.sleep(.01)
                self.assertEqual(self.result()['events'],[])
                self.assertEqual(self.result()['provisionals'][0]['text'],'自己的暫定文字')
        self.assertFalse(self.provider.rpc_transcripts)

    async def test_canonical_started_final_and_known_correction_require_exact_thread(self):
        item={'id':'bound-item','type':'transcriptSegment','role':'user','text':'本場終稿'}
        for scope in ({}, {'threadId':None}, {'threadId':'foreign'}):
            for method in ('thread/realtime/item/started','thread/realtime/item/completed'):
                with self.subTest(scope=scope,method=method):
                    params={**scope,'item':item}
                    await self.provider._source(method,params)
                    await self.provider.rpc.notifications.put({'method':method,'params':params})
                    await asyncio.sleep(.01)
                    self.assertEqual(self.result()['events'],[])
                    self.assertFalse(self.provider.normalizer.item_mode)
        await self.notify('thread/realtime/item/completed',item=item)
        await self.notify(role='user',delta='尚未結束的新段落')
        for scope in ({}, {'threadId':None}, {'threadId':'foreign'}):
            params={**scope,'item':{**item,'text':'未授權的更正','revision':1}}
            await self.provider._source('thread/realtime/item/completed',params)
            await self.provider.rpc.notifications.put({'method':'thread/realtime/item/completed','params':params})
            await asyncio.sleep(.01)
            self.assertEqual(self.result()['events'][0]['text'],'本場終稿')
            self.assertEqual(self.result()['provisionals'][0]['text'],'尚未結束的新段落')

    async def test_unscoped_rpc_copy_cannot_resolve_bound_dc_final(self):
        await self.notify(role='user',delta='未定稿')
        await self.provider._data_event({'type':'turn.done','turn':{'role':'user','transcript':'正式稿'}})
        for scope in ({}, {'threadId':None}, {'threadId':'foreign'}):
            params={**scope,'role':'user','text':'正式稿'}
            await self.provider._source('thread/realtime/transcript/done',params)
            await self.provider.rpc.notifications.put({'method':'thread/realtime/transcript/done','params':params})
            await asyncio.sleep(.01)
            self.assertEqual(len(self.result()['events']),1)
            self.assertEqual(self.result()['provisionals'][0]['text'],'未定稿')
        await self.notify('thread/realtime/transcript/done',role='user',text='正式稿')
        self.assertEqual(self.result()['provisionals'],[])

    async def test_dc_final_waits_for_rpc_part_boundary_before_resolving(self):
        await self.notify(role='user',delta='部分文字')
        await self.provider._data_event({'type':'turn.done','turn':{'role':'user','transcript':'正式文字'}})
        self.assertEqual(len(self.result()['provisionals']),1)
        await self.notify('thread/realtime/transcript/done',role='user',text='正式文字')
        self.assertEqual(self.result()['provisionals'],[])
        self.assertEqual(len(self.result()['events']),1)

    async def test_canonical_start_cannot_resolve_and_correction_keeps_new_pending(self):
        await self.notify(role='user',delta='第一段')
        item={'id':'a','type':'transcriptSegment','role':'user','text':'終稿'}
        await self.notify('thread/realtime/item/started',item=item)
        self.assertEqual(len(self.result()['provisionals']),1)
        await self.notify('thread/realtime/item/completed',item=item)
        # Canonical alone may be Core's session-close seal, not backend done.
        self.assertEqual(len(self.result()['provisionals']),1)
        self.assertEqual(self.result()['events'],[])
        await self.notify('thread/realtime/transcript/done',role='user',text='終稿')
        self.assertEqual(self.result()['provisionals'],[])
        await self.notify('thread/realtime/item/completed',item=item)
        await self.notify(role='user',delta='新段')
        await self.notify('thread/realtime/item/completed',item={**item,'text':'舊段更正','revision':1})
        self.assertEqual(self.result()['provisionals'][0]['text'],'新段')

    async def test_restart_new_provider_does_not_append_to_previous_pending(self):
        await self.notify(role='user',delta='重啟前')
        second=LiveProvider(None,'/tmp/unused-second',AsyncMock(),AsyncMock(),
                            on_provisional=self.provider.on_provisional)
        second.thread_id='new'
        await second._provisional_delta({'threadId':'new','role':'user','delta':'重啟後'})
        self.assertEqual([p['text'] for p in self.result()['provisionals']],['重啟前','重啟後'])
        self.assertEqual(self.result()['events'],[])

    async def test_thread_change_cannot_append_to_or_resolve_old_preview(self):
        await self.notify(role='user',delta='前thread')
        self.provider.thread_id='new'
        await self.provider._source('thread/realtime/transcript/done',{'role':'user','text':'新thread終稿'})
        self.assertEqual(self.result()['provisionals'][0]['text'],'前thread')
        await self.provider._provisional_delta({'threadId':'new','role':'user','delta':'新thread'})
        self.assertEqual([p['text'] for p in self.result()['provisionals']],['前thread','新thread'])

    async def test_invalid_canonical_shape_and_invalid_rpc_done_do_not_resolve(self):
        await self.notify(role='user',delta='原暫定文字')
        for item in (None,[],{}, {'role':'assistant','type':'transcriptSegment','text':'助理'}):
            await self.notify('thread/realtime/item/completed',item=item)
        await self.provider._data_event({'type':'turn.done','turn':{'role':'user','transcript':'正式稿'}})
        await self.notify('thread/realtime/transcript/done',role='user',text='正式稿',revision=True)
        self.assertEqual(self.result()['provisionals'][0]['text'],'原暫定文字')
        self.assertIsNone(self.provider.error)

    async def test_diagnostics_exclude_preview_but_downstream_preserves_unfinalized_quality(self):
        await self.notify(role='user',delta='暫定私人逐字內容')
        snapshot=RxAudioSnapshot(self.session).persist(phase='rx_capture',queue_depth=0,provider=self.provider)
        self.assertNotIn('暫定私人逐字內容',str(snapshot))
        self.assertEqual(self.session.entries(include_unfinalized=False),[])
        self.assertEqual(self.session.entries()[0]['source_state'],'legacy_provisional')
        self.assertFalse(self.session.entries()[0]['provider_final'])
        from teams_transcription.response_runtime import _AsyncStore
        response=await _AsyncStore(self.temp.name).read(self.session.ident)
        self.assertIn('暫定私人逐字內容',''.join(row['text'] for row in response['sources']))
        self.assertEqual(response['transcript_state']['quality'],'includes_unfinalized')
        diagnostics=json.dumps({'status':self.session.status(),'events':session_events(self.session)},ensure_ascii=False)
        self.assertNotIn('暫定私人逐字內容',diagnostics)

    async def test_close_disconnect_and_timeout_do_not_make_final(self):
        await self.notify(role='user',delta='中斷前')
        await self.provider.drain(.001)
        await self.notify('thread/realtime/closed',reason='expired')
        self.assertEqual(self.result()['events'],[])
        self.assertEqual(self.result()['provisionals'][0]['text'],'中斷前')

    async def test_session_close_canonical_seal_cannot_promote_pending_text(self):
        await self.notify(role='user',delta='未完成的段落')
        await self.notify('thread/realtime/closed',reason='requested')
        item={'id':'close-seal','type':'transcriptSegment','role':'user','text':'未完成的段落'}
        await self.notify('thread/realtime/item/completed',item=item)
        self.assertEqual(self.result()['events'],[])
        self.assertEqual(self.result()['provisionals'][0]['text'],'未完成的段落')


class ProvisionalWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_owner_stop_with_real_reader_full_queue_and_slow_write(self):
        # This local stdio child only emits synthetic JSON. Live.start/model,
        # browser, microphone and network are never invoked.
        child_code='''import json,sys
for i in range(41):
 print(json.dumps({'method':'thread/realtime/transcript/delta','params':{'threadId':'own','role':'user','delta':'x'}}),flush=True)
for line in sys.stdin:
 msg=json.loads(line)
 if msg.get('method')=='thread/realtime/stop':
  print(json.dumps({'id':msg['id'],'result':{}}),flush=True)
'''
        for expire in (False,True):
            with self.subTest(cleanup_deadline_expires=expire), tempfile.TemporaryDirectory() as root:
                probe=LifecycleProbe()
                limits=dict(capture_seconds=10,total_seconds=15,
                            cleanup_seconds=1.5 if expire else 3,ready_ttl_seconds=5)
                session=Session(root).create(config(rx_test_contract=limits))
                session.save_provisional(preview('停止前已保存的預覽'))
                prepared,capturing,queue_full=asyncio.Event(),asyncio.Event(),asyncio.Event()
                entered,exited,receiver_stopped=asyncio.Event(),asyncio.Event(),asyncio.Event()
                release=threading.Event()
                loop=asyncio.get_running_loop()
                async def captured(*_, _capturing=capturing):
                    _capturing.set()
                fixture=Fixture(emit_final=False,source=captured)
                original_update=Session.update
                def updated(self, _update=original_update, _ident=session.ident, _prepared=prepared, **values):
                    result=_update(self,**values)
                    if self.ident==_ident and values.get('state')=='prepared':
                        _prepared.set()
                    return result
                class ObservedQueue(asyncio.Queue):
                    async def put(self,value,_full=queue_full):
                        await super().put(value)
                        if self.full(): _full.set()
                original=Session.save_provisional
                def slow_write(self,value,entered=entered,release=release,exited=exited,loop=loop,original=original):
                    loop.call_soon_threadsafe(entered.set)
                    try:
                        if not release.wait(15): raise TimeoutError('fixture release')
                        return original(self,value)
                    finally:
                        loop.call_soon_threadsafe(exited.set)
                async def start(provider,fixture=fixture,expire=expire,queue_type=ObservedQueue,stopped=receiver_stopped):
                    fixture.provider=provider
                    # Legacy preview cancellation still has a bounded lifecycle;
                    # production lossless increment queue drain has its own actual
                    # reader acceptance test in test_durable_delta.
                    provider.on_increment = provider.on_capture_end = None
                    provider.thread_id='own'
                    provider.peer=SimpleNamespace(request_session_close=AsyncMock(return_value=False),
                        push=AsyncMock(),close=AsyncMock(),
                        stats=AsyncMock(return_value=native_transport()))
                    provider.rpc.timeout=30 if expire else .2
                    provider.rpc.notifications=queue_type(maxsize=32)
                    provider.rpc.process=await asyncio.create_subprocess_exec(sys.executable,'-u','-c',child_code,
                        stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
                    provider.rpc.tasks=[asyncio.create_task(provider.rpc._read()),asyncio.create_task(provider.rpc._stderr())]
                    provider.receiver=asyncio.create_task(provider._receive())
                    provider.receiver.add_done_callback(lambda _, stopped=stopped: stopped.set())
                    provider.stage='connected'
                task=None
                try:
                    with probe.record_failure(session,fixture), fixture.install(), \
                            patch('teams_transcription.worker.LiveProvider',LiveProvider), \
                            patch.object(LiveProvider,'start',start), patch.object(Session,'save_provisional',slow_write), \
                            patch.object(Session,'update',updated):
                        from teams_transcription.rx_contract import confirm_prepared
                        task=asyncio.create_task(run_session(session,install_signals=False))
                        await probe.wait(prepared.wait(),'wait_owner_ready')
                        ready=session.status()['rx_prepared']
                        confirm_prepared(session,ready['generation'],session.config()['rx_owner_principal'])
                        await probe.wait(capturing.wait(),'wait_capture')
                        await probe.wait(entered.wait(),'wait_disk_entry')
                        provider=fixture.provider
                        await probe.wait(queue_full.wait(),'wait_queue_full')
                        self.assertEqual(provider.rpc.notifications.qsize(),32)
                        stopped_at=time.monotonic()
                        probe.phase='owner_stop'
                        session.stop()
                        if not expire:
                            # Request times out behind the full queue. close()
                            # must cancel receive, then drain the shielded write.
                            await probe.wait(receiver_stopped.wait(),'wait_receiver_stop')
                            self.assertTrue(provider.receiver.done())
                            self.assertFalse(exited.is_set())
                            release.set()
                        result=await probe.wait(task,'wait_runner',limits['cleanup_seconds']+2)
                        probe.phase='verify_cleanup'
                        self.assertLess(time.monotonic()-stopped_at,limits['cleanup_seconds']+1)
                        status=session.status()
                        self.assertEqual(result,1 if expire else 0)
                        self.assertEqual(status['cleanup_status'],'unknown' if expire else 'cleaned')
                        self.assertEqual(status['state'],'failed' if expire else 'stopped')
                        self.assertIsNotNone(provider.rpc.process.returncode)
                        self.assertTrue(provider.receiver.done())
                        self.assertTrue(all(value.done() for value in provider.rpc.tasks))
                        self.assertEqual(provider.rpc.pending,{})
                        before=MeetingStore(root).read(session.ident)
                        self.assertEqual(before['events'],[])
                        self.assertEqual(before['provisionals'][0]['text'],'停止前已保存的預覽')
                        if expire:
                            self.assertEqual(status['error'],'rx_cleanup_unknown')
                            self.assertFalse(exited.is_set())
                            release.set()
                            await probe.wait(exited.wait(),'wait_late_write')
                            # A previously entered write cannot be interrupted;
                            # the finalized store rejects its late publication.
                            self.assertEqual(MeetingStore(root).read(session.ident)['version'],before['version'])
                        diagnostics=json.dumps({'status':session.status(),'events':session_events(session)},ensure_ascii=False)
                        self.assertNotIn('停止前已保存的預覽',diagnostics)
                finally:
                    release.set()
                    with probe.record_failure(session,fixture):
                        try:
                            if task and not task.done():
                                task.cancel()
                                await asyncio.gather(task,return_exceptions=True)
                            provider=getattr(fixture,'provider',None)
                            if provider:
                                if provider.receiver and not provider.receiver.done():
                                    provider.receiver.cancel()
                                    await asyncio.gather(provider.receiver,return_exceptions=True)
                                await provider.rpc.close()
                        finally:
                            # A cancelled to_thread task does not join its
                            # thread. Join this case before deleting its files
                            # or creating the next case's closure/event cells.
                            if entered.is_set():
                                await probe.wait(exited.wait(),'wait_late_write')

    async def test_scoped_session_rejects_provisional_after_attempt_closes(self):
        from teams_transcription.rx_contract import ScopedSession,RxContractError
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(config())
            scoped=ScopedSession(session,SimpleNamespace(writes_open=False))
            with self.assertRaisesRegex(RxContractError,'rx_attempt_closed'):
                await asyncio.to_thread(scoped.save_provisional,preview())
            self.assertEqual(MeetingStore(root).read(session.ident)['provisionals'],[])

    async def test_real_rpc_reader_backpressures_bounded_live_queue_without_loss(self):
        provider=LiveProvider(None,'/tmp/unused-queue',AsyncMock(),AsyncMock())
        rpc=provider.rpc
        self.assertEqual(rpc.notifications.maxsize,32)
        stream=asyncio.StreamReader()
        rpc.process=SimpleNamespace(stdout=stream)
        messages=[{'method':'thread/realtime/transcript/delta',
                   'params':{'threadId':'fixture','role':'user','delta':str(i)}} for i in range(35)]
        future=asyncio.get_running_loop().create_future()
        rpc.pending[1]=future
        stream.feed_data(('\n'.join(json.dumps(m) for m in messages)+
                          '\n'+json.dumps({'id':1,'result':{'fixture':True}})+'\n').encode())
        stream.feed_eof()
        task=asyncio.create_task(rpc._read())
        try:
            await asyncio.sleep(.01)
            self.assertEqual(rpc.notifications.qsize(),32)
            self.assertFalse(future.done())
            received=[await asyncio.wait_for(rpc.notifications.get(),1) for _ in range(35)]
            await asyncio.wait_for(task,1)
            self.assertEqual(received,messages)
            self.assertEqual(future.result(),{'fixture':True})
        finally:
            if not task.done(): task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def test_cancel_during_disk_write_drains_without_promoting_or_logging_text(self):
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(config(rx_test_contract=None))
            fixture=Fixture(emit_final=False)
            entered,release=threading.Event(),threading.Event()
            original=Session.save_provisional
            def blocked(self,value):
                entered.set()
                if not release.wait(2): raise TimeoutError('fixture release')
                return original(self,value)
            with fixture.install(), patch.object(Session,'save_provisional',blocked):
                from teams_transcription import worker
                live_class=worker.LiveProvider
                async def start(self):
                    await self.on_provisional(preview('取消期間的暫定文字'))
                with patch.object(live_class,'start',start):
                    task=asyncio.create_task(run_session(session,install_signals=False))
                    try:
                        self.assertTrue(await asyncio.wait_for(asyncio.to_thread(entered.wait,1),1.5))
                        task.cancel()
                        release.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    finally:
                        release.set()
                        if not task.done():
                            task.cancel()
                            await asyncio.gather(task,return_exceptions=True)
            result=MeetingStore(root).read(session.ident)
            self.assertEqual(result['events'],[])
            self.assertEqual(result['provisionals'][0]['text'],'取消期間的暫定文字')
            self.assertEqual(result['status'],'stopped')
            self.assertNotIn('取消期間的暫定文字',json.dumps(session_events(session),ensure_ascii=False))

    
