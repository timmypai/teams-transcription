"""B input routing evidence is bounded metadata, never transcript content."""
import asyncio
import json
import unittest
from unittest.mock import patch

from teams_transcription.diagnostics import response_progress_diagnostic
from teams_transcription.b_question_input import QuestionTranscriptInput
from test_b_question import Capture


class QuestionFramingProjectionTests(unittest.TestCase):
    def test_current_framer_activity_and_quiet_survive_progress_projection(self):
        value=response_progress_diagnostic({'question_framing':{
            'provenance':'application','boundary':'pcm_quiet_and_stable_delta',
            'heard_activity':True,'capture_recent':False,'observed_quiet_seconds':1.42,
            'silence_seconds':1.5,'text_settle_seconds':.6,'emitted_requests':2,
            'pending':{'characters':11,'has_wake':False,'has_question':False},
            'raw_transcript':'PRIVATE_UTTERANCE','threadId':'PRIVATE_THREAD'}})
        framing=value.get('question_framing')
        self.assertIsNotNone(framing)
        self.assertTrue(framing['heard_activity'])
        self.assertFalse(framing['capture_recent'])
        self.assertEqual(framing['observed_quiet_seconds'],1.42)
        self.assertEqual(framing['emitted_requests'],2)
        self.assertNotIn('PRIVATE',json.dumps(value))

    def test_missing_or_invalid_framing_is_unknown_and_no_private_field_is_copied(self):
        self.assertIsNone(response_progress_diagnostic({})['question_framing'])
        value=response_progress_diagnostic({'question_framing':{
            'heard_activity':1,'capture_recent':'PRIVATE','observed_quiet_seconds':float('nan'),
            'boundary':'PRIVATE','emitted_requests':-1}})['question_framing']
        self.assertIsNone(value['heard_activity'])
        self.assertIsNone(value['capture_recent'])
        self.assertIsNone(value['observed_quiet_seconds'])
        self.assertEqual(value['boundary'],'unknown')


class QuestionTranscriptRoutingTests(unittest.TestCase):
    def setUp(self):
        self.capture=Capture()
        self.router=QuestionTranscriptInput(self.capture.framer,clock=lambda:self.capture.now)
        self.item={'id':'PRIVATE_ITEM','realtimeSessionId':'PRIVATE_SESSION',
            'type':'transcriptSegment','role':'user','text':''}
        self.router.feed('thread/realtime/started',
            {'threadId':'PRIVATE_THREAD','version':'v3','realtimeSessionId':self.item['realtimeSessionId']},
            thread_id='PRIVATE_THREAD',enabled=False,start_issued=True)

    def event(self,kind,**params):
        self.router.feed('thread/realtime/'+kind,{'threadId':'PRIVATE_THREAD',**params},
            thread_id='PRIVATE_THREAD',enabled=True)

    def start(self,**changes):self.event('item/started',item={**self.item,**changes})
    def delta(self,text,**changes):self.event('item/transcript/delta',itemId=self.item['id'],delta=text,**changes)

    def test_canonical_then_flat_mirror_is_not_double_appended(self):
        self.capture.packet(1000);self.start()
        text='小白小白，請問1加1等於多少？'
        self.delta(text);self.event('transcript/delta',role='user',delta=text)
        self.capture.quiet()
        self.assertEqual(self.capture.framer.poll().text,'請問1加1等於多少')
        self.assertIsNone(self.capture.framer.poll())
        self.assertEqual(self.router.status()['counts']['appended'],1)
        self.assertEqual(self.router.status()['counts']['flat_mirror'],1)

    def test_completed_and_flat_done_text_do_not_substitute_for_delta(self):
        self.capture.packet(1000);self.start()
        self.event('item/completed',item={**self.item,'text':'小白小白，問題？'})
        self.event('transcript/done',role='user',text='小白小白，問題？')
        self.capture.quiet();self.assertIsNone(self.capture.framer.poll())
        self.assertEqual(self.router.status()['counts']['appended'],0)

    def test_unknown_or_assistant_item_delta_cannot_authorize_question(self):
        self.capture.packet(1000)
        self.delta('小白小白，問題？')
        self.start(role='assistant')
        self.delta('小白小白，問題？',role='user')
        self.capture.quiet();self.assertIsNone(self.capture.framer.poll())
        self.assertEqual(self.router.status()['counts']['unknown_item'],1)
        self.assertEqual(self.router.status()['counts']['non_user'],1)

    def test_closed_or_orphan_completed_identity_cannot_reopen(self):
        for started in (False,True):
            with self.subTest(started=started):
                self.setUp();self.capture.packet(1000)
                if started:self.start()
                self.event('item/completed',item=self.item)
                self.start();self.delta('小白小白，問題？')
                self.capture.quiet();self.assertIsNone(self.capture.framer.poll())
                self.assertEqual(self.router.status()['counts']['closed_item'],1)

    def test_foreign_thread_session_and_conflicting_role_cannot_rebind_user(self):
        self.capture.packet(1000);self.start()
        self.router.feed('thread/realtime/item/transcript/delta',
            {'threadId':'FOREIGN','itemId':self.item['id'],'delta':'小白小白，問題？'},
            thread_id='PRIVATE_THREAD',enabled=True)
        self.start(id='foreign-session-item',realtimeSessionId='FOREIGN')
        self.event('item/transcript/delta',itemId='foreign-session-item',delta='小白小白，問題？')
        self.start(role='assistant');self.delta('小白小白，問題？')
        self.capture.quiet();self.assertIsNone(self.capture.framer.poll())
        self.assertEqual(self.router.status()['counts']['foreign_thread'],1)
        self.assertEqual(self.router.status()['counts']['foreign_session'],1)
        self.assertEqual(self.router.status()['counts']['closed_item'],1)

    def test_legacy_flat_stream_is_explicit_and_never_mixed_with_later_canonical(self):
        self.capture.packet(1000)
        self.event('transcript/delta',role='user',delta='小白小白，問題？')
        self.start();self.delta('小白小白，問題？')
        self.capture.quiet();self.assertEqual(self.capture.framer.poll().text,'問題')
        self.assertEqual(self.router.status()['mode'],'flat')
        self.assertEqual(self.router.status()['counts']['canonical_after_flat'],2)

    def test_repeated_spoken_question_and_shared_user_item_are_not_text_deduplicated(self):
        self.start()
        for sequence in (1,2):
            self.capture.packet(1000);self.delta('小白、小白：請問１＋１？')
            self.capture.quiet()
            question=self.capture.framer.poll()
            self.assertEqual((question.sequence,question.text),(sequence,'請問1+1'))
        self.assertEqual(self.router.status()['counts']['appended'],2)

    def test_no_recent_local_activity_still_rejects_valid_canonical_text(self):
        self.capture.now=1.
        self.capture.framer.push_pcm(b'\x00\x10'*1200,started_at=.65,ended_at=.7)
        self.start();self.delta('小白小白，問題？')
        self.capture.quiet();self.assertIsNone(self.capture.framer.poll())
        self.assertEqual(self.router.status()['counts']['no_local_activity'],1)

    def test_inactive_input_does_not_bind_or_append(self):
        self.capture.packet(1000)
        self.router.feed('thread/realtime/item/started',{'threadId':'PRIVATE_THREAD','item':self.item},
            thread_id='PRIVATE_THREAD',enabled=False)
        self.delta('小白小白，問題？')
        self.capture.quiet();self.assertIsNone(self.capture.framer.poll())
        self.assertEqual(self.router.status()['identity_count'],0)

    def test_identity_memory_is_bounded_and_capacity_is_not_silently_evicted(self):
        with patch('teams_transcription.b_question_input.ITEM_LIMIT',2):
            self.start(id='one');self.start(id='two')
            with self.assertRaisesRegex(RuntimeError,'response_input_identity_capacity'):
                self.start(id='three')
            self.assertEqual(self.router.status()['identity_count'],2)

    def test_routing_trace_and_projection_are_bounded_and_contain_no_text_or_ids(self):
        self.capture.packet(1000);self.start()
        for _ in range(25):self.delta('PRIVATE_CONTENT')
        status=self.router.status();self.assertEqual(len(status['events']),16)
        status['events'][0]['private']='PRIVATE_EXTRA'
        value=response_progress_diagnostic({'question_input':status})['question_input']
        self.assertEqual(value['mode'],'canonical')
        self.assertEqual(value['counts']['appended'],25)
        self.assertEqual(len(value['events']),16)
        self.assertNotIn('PRIVATE',json.dumps(value))
        self.assertNotIn('PRIVATE',json.dumps(self.router.status()))
        self.assertIsNone(response_progress_diagnostic({'question_input':{'version':True}})['question_input'])

    def test_malformed_identity_and_foreign_session_collision_remain_closed(self):
        self.capture.packet(1000)
        for change in ({'id':[]},{'id':''},{'id':'x'*257},{'role':{}},{'realtimeSessionId':None}):
            self.start(**change)
        self.assertEqual(self.router.status()['identity_count'],0)
        self.start();self.start(realtimeSessionId='FOREIGN')
        self.delta('小白小白，問題？')
        self.capture.quiet();self.assertIsNone(self.capture.framer.poll())
        self.assertEqual(self.router.status()['counts']['closed_item'],1)

    def test_invalid_flat_delta_does_not_select_fallback_stream(self):
        self.capture.packet(1000)
        for value in ('',None,1,[]):self.event('transcript/delta',role='user',delta=value)
        self.assertEqual(self.router.status()['mode'],'unselected')

    def test_only_issued_start_binds_session_even_before_media_ready(self):
        router=QuestionTranscriptInput(self.capture.framer,clock=lambda:self.capture.now)
        params={'threadId':'PRIVATE_THREAD','version':'v3','realtimeSessionId':'PRIVATE_SESSION'}
        router.feed('thread/realtime/started',{**params,'version':'v2'},
            thread_id='PRIVATE_THREAD',enabled=False,start_issued=True)
        self.assertFalse(router.status()['session_bound'])
        router.feed('thread/realtime/started',params,thread_id='PRIVATE_THREAD',enabled=False)
        self.assertFalse(router.status()['session_bound'])
        router.feed('thread/realtime/started',params,thread_id='PRIVATE_THREAD',enabled=False,start_issued=True)
        self.assertTrue(router.status()['session_bound'])
        with self.assertRaisesRegex(RuntimeError,'response_input_session_changed'):
            router.feed('thread/realtime/started',{**params,'realtimeSessionId':'FOREIGN'},
                thread_id='PRIVATE_THREAD',enabled=True,start_issued=True)

    def test_canonical_item_cannot_supply_its_own_session_authority(self):
        router=QuestionTranscriptInput(self.capture.framer,clock=lambda:self.capture.now)
        self.capture.packet(1000)
        router.feed('thread/realtime/item/started',{'threadId':'PRIVATE_THREAD','item':self.item},
            thread_id='PRIVATE_THREAD',enabled=True)
        router.feed('thread/realtime/item/transcript/delta',{'threadId':'PRIVATE_THREAD',
            'itemId':self.item['id'],'delta':'小白小白，問題？'},thread_id='PRIVATE_THREAD',enabled=True)
        self.capture.quiet();self.assertIsNone(self.capture.framer.poll())
        self.assertFalse(router.status()['session_bound'])
        self.assertEqual(router.status()['identity_count'],0)


class CanonicalQuestionInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_b_native import NativeLiveTests
        self.case=NativeLiveTests('runTest')
        await self.case.asyncSetUp()

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_official_final_only_canonical_delta_waits_for_pcm_quiet_then_queries(self):
        case=self.case;live=case.live
        await live.handle_notification('thread/realtime/started',{
            'threadId':'native-thread','version':'v3','realtimeSessionId':'synthetic-live'})
        await case.input_pcm()
        item={'type':'transcriptSegment','id':'synthetic-input-1',
            'realtimeSessionId':'synthetic-live','role':'user','text':''}
        await live.handle_notification('thread/realtime/item/started',
            {'threadId':'native-thread','item':item})
        text='小白小白，請問1加1等於多少？'
        await live.handle_notification('thread/realtime/item/transcript/delta',
            {'threadId':'native-thread','itemId':item['id'],'delta':text})
        await live.handle_notification('thread/realtime/item/completed',
            {'threadId':'native-thread','item':{**item,'text':text}})
        await live.handle_notification('thread/realtime/transcript/done',
            {'threadId':'native-thread','role':'user','text':text})
        self.assertEqual(live.question_receipts()['counts']['received'],0)
        await case.input_pcm(quiet=True,seconds=.5)
        await asyncio.sleep(0)
        self.assertEqual(live.question_receipts()['counts']['received'],0)
        await case.input_pcm(quiet=True,seconds=1.1)
        await case.until(lambda:live.state=='answering')
        self.assertEqual(live.question_receipts()['counts']['admitted'],1)
        self.assertEqual(case.reads,['one'])

    async def test_real_rpc_receive_loop_routes_canonical_before_flat_without_repetition(self):
        case=self.case;live=case.live
        await case.input_pcm()
        item={'type':'transcriptSegment','id':'synthetic-input-1',
            'realtimeSessionId':'synthetic-live','role':'user','text':''}
        case.notify('thread/realtime/started',version='v3',realtimeSessionId='synthetic-live')
        case.notify('thread/realtime/item/started',item=item)
        text='小白小白，預算多少？'
        case.notify('thread/realtime/item/transcript/delta',itemId=item['id'],delta=text)
        case.notify('thread/realtime/transcript/delta',role='user',delta=text)
        await case.until(lambda:live.question_input.status()['counts']['flat_mirror']==1)
        self.assertEqual(live.question_framer.pending_status()['characters'],len(text))
        await case.input_pcm(quiet=True,seconds=1.6)
        await case.until(lambda:live.state=='answering')
        self.assertEqual(live.question_receipts()['counts']['admitted'],1)
        self.assertEqual(case.reads,['one'])

    async def test_identity_capacity_failure_keeps_output_muted_and_is_observable(self):
        case=self.case;live=case.live
        await live.handle_notification('thread/realtime/started',{
            'threadId':'native-thread','version':'v3','realtimeSessionId':'synthetic-live'})
        with patch('teams_transcription.b_question_input.ITEM_LIMIT',1):
            item={'type':'transcriptSegment','id':'synthetic-input-1',
                'realtimeSessionId':'synthetic-live','role':'user','text':''}
            case.notify('thread/realtime/item/started',item=item)
            await case.until(lambda:live.question_input.status()['identity_count']==1)
            case.notify('thread/realtime/item/started',item={**item,'id':'synthetic-input-2'})
            await case.until(lambda:live.state=='failed')
        self.assertFalse(case.output.active or case.output.enabled)
        self.assertEqual(case.reads,[])
        self.assertEqual(live.question_input.status()['counts']['identity_capacity'],1)

    async def test_actual_started_session_rejects_a_stale_first_user_item(self):
        case=self.case;live=case.live
        await live.handle_notification('thread/realtime/started',{
            'threadId':'native-thread','version':'v3','realtimeSessionId':'actual-session'})
        await case.input_pcm()
        item={'type':'transcriptSegment','id':'old-input',
            'realtimeSessionId':'stale-session','role':'user','text':''}
        await live.handle_notification('thread/realtime/item/started',{'threadId':'native-thread','item':item})
        await live.handle_notification('thread/realtime/item/transcript/delta',{
            'threadId':'native-thread','itemId':item['id'],'delta':'小白小白，舊問題？'})
        self.assertEqual(live.question_framer.pending_status()['characters'],0)
