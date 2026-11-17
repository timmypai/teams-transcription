"""Same SQLite source + independent series revision; no media/model/network."""
import copy
import json
import unittest

from teams_transcription.b_context import MeetingQuestionContext, ContextFailure
from teams_transcription.b_response import MeetingMinutesTool, MeetingStoreMinutesReader
from teams_transcription.response_runtime import _AsyncStore
from teams_transcription.storage import Session
import test_postmeeting_store as fixtures


class SeriesQuestionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = fixtures.PostmeetingPublicationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.previous,self.previous_session = self.fixture.capture()
        self.fixture.publish(task={},decision={})
        event = dict(event_id='current',recurring_event_id='parent',original_start='2026-11-02T10:00:00Z',
            start_at='2026-11-02T10:00:00+00:00',end_at='2026-11-02T11:00:00+00:00',
            title='Synthetic current',status='confirmed',link_status='valid',all_day=False,
            join_url='https://teams.microsoft.com/meet/12345')
        self.current = self.store.sync_occurrences(self.fixture.calendar['id'],[event])[0]
        self.lease = self.store.claim_run(self.current['id'],'fixture')
        self.session = Session(self.fixture.tmp.name,self.lease['session_id'],lease=self.lease).create(
            dict(provider='mock',source='synthetic',prior_context=self.store.get_run(self.lease['session_id'])['prior_context']))
        self.session.store.record_increment(self.session.ident,dict(capture_id='b'*32,receipt_sequence=1,
            part_index=0,text='Current source remains unchanged.',received_at='2026-11-02T10:30:00Z'))
        self.async_store = _AsyncStore(self.fixture.tmp.name)
        self.reader = MeetingStoreMinutesReader(self.async_store)
        self.context = MeetingQuestionContext(MeetingMinutesTool(self.session.ident,self.reader))
        self.context.bind_session('thread-one')

    def send(self, plan):
        self.context.record_sent(plan,len(plan.payload.encode())+15,session_id='thread-one')
        self.context.commit(plan,session_id='thread-one')

    def publish_correction(self):
        state = self.store.series_state(self.current['series_id'])
        base = self.store.list_postmeeting_snapshots(self.previous_session)[0]['entries'][0]['id']
        self.store.add_postmeeting_correction(self.previous_session,[dict(correction_of=base,
            text='Report complete. Budget changed to twelve.',timestamp='2026-11-02T10:31:00Z',source_revision=1)],
            reason='Synthetic late correction')
        lease = self.store.claim_postmeeting('fixture-host')
        correction = self.store.postmeeting_input(lease)['source']['entries'][-1]['id']
        self.store.publish_postmeeting(lease,self.fixture.completed(lease,
            task=dict(id=state['open_tasks'][0]['id'],status='completed',sources=[correction]),
            decision=dict(id=state['conclusions'][0]['id'],text='Budget now twelve',sources=[correction])))

    async def test_series_publication_refreshes_next_question_without_changing_a_version(self):
        first = await self.context.prepare('Latest conclusion and work?',session_id='thread-one')
        first_wire = json.loads(first.payload)
        self.assertEqual(first_wire['result']['series_snapshot']['revision'],1)
        self.publish_correction()
        self.send(first)  # Publication during this question must not alter its pinned payload.
        self.assertEqual(json.loads(first.payload),first_wire)
        second = await self.context.prepare('Latest conclusion and work?',session_id='thread-one')
        second_wire = json.loads(second.payload)
        self.assertEqual(first_wire['result']['version'],second_wire['result']['version'])
        self.assertEqual(second_wire['result']['series_snapshot']['revision'],2)
        self.assertNotEqual(first_wire['result']['snapshot_sha256'],second_wire['result']['snapshot_sha256'])
        readings = second_wire['selected_source_reading']
        self.assertIn('Budget now twelve',[row['text'] for row in readings])
        tasks = [row for row in readings if row.get('historical_context',{}).get('kind')=='series_task']
        self.assertEqual(tasks,[])
        changes = [row for row in readings if row.get('historical_context',{}).get('kind')=='series_change']
        self.assertEqual([row['historical_context']['status'] for row in changes],['completed'])
        self.assertNotIn('Budget approved',str(readings))
        self.assertEqual([row['text'] for row in readings if row['provenance']=='sqlite_source'],
            ['Current source remains unchanged.'])
        self.assertFalse(second_wire['result']['history_status']['previous_transcript_included'])
        self.send(second)

    async def test_reader_rejects_foreign_series_state_in_bound_snapshot(self):
        snapshot = await self.async_store.read(self.session.ident)
        snapshot['series_context']['state']['series_id']='foreign-series'
        async def forged(_):
            return snapshot
        self.reader.store = type('ForgedStore',(),{'read':staticmethod(forged)})()
        with self.assertRaisesRegex(ValueError,'series'):
            await self.reader(self.session.ident)

    async def test_bound_question_rejects_series_switch_or_disappearing_identity(self):
        first = await self.context.prepare('Work?',session_id='thread-one')
        self.send(first)
        value = await self.reader(self.session.ident)
        for replacement in ({**value['series_snapshot'],'series_id':'foreign-series'},None):
            forged = copy.deepcopy(value)
            if replacement is None:
                forged.pop('series_snapshot')
            else:
                forged['series_snapshot']=replacement
            self.context.tool.reader = lambda _:forged
            with self.assertRaisesRegex(ContextFailure,'context_series_changed'):
                await self.context.prepare('Work?',session_id='thread-one')
            self.assertFalse(self.context.status()['pending'])

    async def test_series_rollback_and_same_revision_mutation_fail_even_when_a_advances(self):
        self.publish_correction()
        first = await self.context.prepare('Work?',session_id='thread-one')
        self.send(first)
        value = await self.reader(self.session.ident)
        mutated = copy.deepcopy(value)
        mutated['version']+=1
        mutated['series_snapshot']['sha256']='a'*64
        self.context.tool.reader = lambda _:mutated
        with self.assertRaisesRegex(ContextFailure,'context_series_same_revision_conflict'):
            await self.context.prepare('Work?',session_id='thread-one')
        rolled_back = copy.deepcopy(value)
        rolled_back['version']+=1
        rolled_back['series_snapshot']['revision']=1
        self.context.tool.reader = lambda _:rolled_back
        with self.assertRaisesRegex(ContextFailure,'context_series_revision_rollback'):
            await self.context.prepare('Work?',session_id='thread-one')

    async def test_same_a_and_series_revision_reject_different_records(self):
        first = await self.context.prepare('Work?',session_id='thread-one')
        self.send(first)
        value = await self.reader(self.session.ident)
        value['records'][0]['text']='Mutated at the same versions.'
        self.context.tool.reader = lambda _:value
        with self.assertRaisesRegex(ContextFailure,'context_same_version_conflict'):
            await self.context.prepare('Work?',session_id='thread-one')

    async def test_invalidated_pending_series_read_cannot_publish_or_commit(self):
        import asyncio
        entered,release = asyncio.Event(),asyncio.Event()
        original = self.context.tool.reader
        async def delayed(meeting):
            value = await original(meeting)
            entered.set()
            await release.wait()
            return value
        self.context.tool.reader = delayed
        task = asyncio.create_task(self.context.prepare('Work?',session_id='thread-one'))
        await entered.wait()
        self.context.invalidate('thread-one')
        self.publish_correction()
        release.set()
        with self.assertRaisesRegex(ContextFailure,'context_prepare_invalidated'):
            await task
        self.assertFalse(self.context.status()['pending'])
        self.assertEqual(self.context.status()['sent_bytes'],0)
        self.context.tool.reader = original
        retry = await self.context.prepare('Work?',session_id='thread-one')
        self.assertEqual(retry.result['series_snapshot']['revision'],2)
        with self.assertRaisesRegex(ContextFailure,'context_session_changed'):
            self.context.record_sent(retry,1,session_id='foreign-thread')

    async def test_bound_series_reader_never_includes_other_series_or_old_summary_stack(self):
        result = await self.reader(self.session.ident)
        state = self.store.series_state(self.current['series_id'])
        self.assertEqual(result['series_snapshot']['series_id'],self.current['series_id'])
        self.assertEqual([row['text'] for row in result['records']],
            ['Current source remains unchanged.','Budget approved','Prepare report'])
        # The current meeting plus latest materialized state are present exactly once.
        self.assertFalse(any(row['source_id'].startswith('history:') for row in result['records']))
        for row in result['records'][1:]:
            self.assertEqual(row['locator']['series_revision'],state['revision'])
            self.assertEqual(row['locator']['source_refs'][0]['occurrence_id'],self.previous['id'])
            self.assertEqual(row['locator']['source_refs'][0]['snapshot_revision'],1)

    async def test_native_question_delivery_uses_new_series_revision_and_one_marker_each(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from teams_transcription.b_native import NativeLiveResponder
        from teams_transcription.b_native_output import NativeTrackOutput
        from teams_transcription.b_question import FramedQuestion
        sink = AsyncMock()
        sink.observe.return_value = dict(frames=0,packets=0,energy=0.,duration=0.,
            source_frames=0,source_packets=0,source_energy=0.,source_duration=0.,connected=True)
        output = NativeTrackOutput(self.session.ident,sink)
        live = NativeLiveResponder(None,self.fixture.tmp.name,self.session.ident,output,
            MeetingMinutesTool(self.session.ident,self.reader),question_timeout=2,stop_timeout=.01)
        live.thread_id='owned-thread'
        live.context.bind_session(live.thread_id)
        live.started=True
        live.state='sleeping'
        live.peer=AsyncMock()
        live.rpc=AsyncMock()
        live.rpc.notifications=asyncio.Queue()
        live.rpc.process=SimpleNamespace(returncode=None)
        live._answer=asyncio.get_running_loop().create_future()
        async def begin(state):
            live.state=state
        live._begin_native_speech=AsyncMock(side_effect=begin)
        messages=[]
        try:
            for sequence in (1,2):
                start=live.peer.send_event.await_count
                await live._start_application_request(FramedQuestion('question','Latest work?',sequence))
                await live._tool_task
                events=[call.args[0]['content'][0]['text']
                    for call in live.peer.send_event.await_args_list[start:]]
                self.assertEqual(events[-1],'RESULT_COMPLETE')
                self.assertEqual(events.count('RESULT_COMPLETE'),1)
                messages.append(json.loads(''.join(events[:-1])))
                if sequence==1:
                    self.publish_correction()
            self.assertEqual([value['result']['series_snapshot']['revision'] for value in messages],[1,2])
            self.assertEqual(messages[0]['result']['version'],messages[1]['result']['version'])
            self.assertIsNone(live.context_failure_code)
            self.assertEqual([row['phase'] for row in live.status()['tool_deliveries']],['delivered','delivered'])
            self.assertNotEqual(messages[0]['result']['snapshot_sha256'],messages[1]['result']['snapshot_sha256'])
        finally:
            await live.close()

    async def test_first_series_publication_replaces_admission_fallback_at_same_source_version(self):
        fixture = fixtures.PostmeetingPublicationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        occurrence,previous = fixture.capture()
        event = dict(event_id='next',recurring_event_id='parent',original_start='2026-11-02T10:00:00Z',
            start_at='2026-11-02T10:00:00+00:00',end_at='2026-11-02T11:00:00+00:00',
            title='Synthetic',status='confirmed',link_status='valid',all_day=False,
            join_url='https://teams.microsoft.com/meet/12345')
        next_occurrence = fixture.store.sync_occurrences(fixture.calendar['id'],[event])[0]
        capture = fixture.store.claim_run(next_occurrence['id'],'fixture')
        reader = MeetingStoreMinutesReader(_AsyncStore(fixture.tmp.name))
        context = MeetingQuestionContext(MeetingMinutesTool(capture['session_id'],reader))
        context.bind_session('thread')
        first = await context.prepare('Work?',session_id='thread')
        self.assertEqual(first.result['series_snapshot']['revision'],0)
        context.record_sent(first,len(first.payload.encode())+15,session_id='thread')
        context.commit(first,session_id='thread')
        fixture.publish(task={})
        second = await context.prepare('Work?',session_id='thread')
        self.assertEqual(second.result['series_snapshot']['revision'],1)
        self.assertEqual(first.result['version'],second.result['version'])
        self.assertIn('Prepare report',second.payload)
