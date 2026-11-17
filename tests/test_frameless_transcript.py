"""Pinned v3 shapes as synthetic fixtures, no Live/Teams or raw payload dumps."""
import asyncio
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from teams_transcription.live import LiveProvider, TranscriptNormalizer
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.rx_audio_stats import LIVE_EVENT_KEYS, RxAudioSnapshot
from teams_transcription.storage import Session
from test_rx_contract import config


def dc(text='合成發話', role='user'):
    # protocol_frameless_bidi.rs::parse_turn_done, rust-v0.159.0-alpha.3.
    return {'type':'turn.done', 'turn':{'role':role, 'transcript':text}}


def rpc(text='合成發話', role='user'):
    # bespoke_event_handling.rs maps InputTranscriptDone to this role/text.
    return {'role':role, 'text':text}


class FramelessNormalizerTests(unittest.TestCase):
    def test_exact_official_shape_accepts_only_nested_user_final(self):
        normal = TranscriptNormalizer()
        event = normal.final_event('turn.done', dc('  合成終稿  '), 1, 'own')
        self.assertEqual((event['text'],event['source_role'],event['revision']), ('合成終稿','user',0))
        for value in (dc(role='assistant'), dc(role='tool'),
                      {'type':'turn.done','role':'user','turn':{'transcript':'合成'}},
                      {'type':'turn.done','role':'user','turn':{'role':'assistant','transcript':'合成'}},
                      {'turn':None}, {'turn':[]}, {'turn':{'role':'user','transcript':12}},
                      dc(''), dc('  ')):
            with self.subTest(value_type=type(value.get('turn')).__name__):
                self.assertIsNone(normal.final_event('turn.done', value, 1.1, 'own'))

    def test_rpc_dc_copies_are_paired_in_either_arrival_order(self):
        for order in (('turn.done','thread/realtime/transcript/done'),
                      ('thread/realtime/transcript/done','turn.done')):
            with self.subTest(first=order[0]):
                normal = TranscriptNormalizer()
                values = {'turn.done':dc(), 'thread/realtime/transcript/done':rpc()}
                first = normal.final_event(order[0], values[order[0]], 1, 'own')
                second = normal.final_event(order[1], values[order[1]], 1.1, 'own')
                self.assertIsNotNone(first)
                self.assertIsNone(second)

    def test_interleaved_copies_match_text_and_keep_first_arrival_order(self):
        normal = TranscriptNormalizer()
        first = normal.final_event('turn.done', dc('甲'), 1, 'own')
        second = normal.final_event('turn.done', dc('乙'), 1.1, 'own')
        self.assertIsNone(normal.final_event('thread/realtime/transcript/done', rpc('乙'), 1.2, 'own'))
        self.assertIsNone(normal.final_event('thread/realtime/transcript/done', rpc('甲'), 1.3, 'own'))
        self.assertNotEqual(first['event_id'], second['event_id'])
        self.assertEqual(normal.sequence, 2)

    def test_identical_repeated_speech_is_preserved_one_for_one(self):
        for method, value, copy_method, copy_value in (
            ('turn.done',dc(), 'thread/realtime/transcript/done',rpc()),
            ('thread/realtime/transcript/done',rpc(), 'turn.done',dc())):
            with self.subTest(method=method):
                normal = TranscriptNormalizer()
                first = normal.final_event(method,value,1,'own')
                second = normal.final_event(method,value,1.1,'own')
                self.assertNotEqual(first['event_id'],second['event_id'])
                self.assertIsNone(normal.final_event(copy_method,copy_value,1.2,'own'))
                self.assertIsNone(normal.final_event(copy_method,copy_value,1.3,'own'))
                self.assertEqual(normal.sequence,2)

    def test_copy_window_and_thread_are_bounded_not_global_text_dedup(self):
        for at, thread in ((2.1,'own'), (1.1,'other')):
            with self.subTest(at=at,thread=thread):
                normal = TranscriptNormalizer()
                first = normal.final_event('turn.done',dc(),1,'own')
                second = normal.final_event('thread/realtime/transcript/done',rpc(),at,thread)
                self.assertNotEqual(first['event_id'],second['event_id'])
        normal = TranscriptNormalizer()
        for index in range(140):
            normal.final_event('turn.done',dc(str(index)),index*.001,'own')
        self.assertEqual(len(normal.frameless_copies),128)
        self.assertNotIn('合成發話',str(normal.frameless_copies))
        normal.final_event('turn.done',dc('新發話'),2,'own')
        self.assertEqual(len(normal.frameless_copies),1)

    def test_invalid_revision_does_not_consume_a_valid_counterpart(self):
        normal = TranscriptNormalizer()
        normal.final_event('turn.done',dc(),1,'own')
        self.assertIsNone(normal.final_event('thread/realtime/transcript/done',
                                           {**rpc(),'revision':'invalid'},1.1,'own'))
        self.assertIsNone(normal.final_event('thread/realtime/transcript/done',rpc(),1.2,'own'))
        self.assertEqual(normal.sequence,1)

    def test_canonical_priority_and_stable_revision_identity_are_preserved(self):
        normal = TranscriptNormalizer()
        item = {'id':'stable','realtimeSessionId':'session', 'type':'transcriptSegment',
                'role':'user','text':'第一版'}
        first = normal.final_event('thread/realtime/item/completed',{'item':item},1,'own')
        self.assertIsNone(normal.final_event('turn.done',dc('第一版'),1.1,'own'))
        corrected = normal.final_event('thread/realtime/item/completed',
                                      {'item':{**item,'text':'更正版'}},1.2,'own')
        self.assertEqual(first['event_id'],corrected['event_id'])
        self.assertEqual(corrected['revision'],1)

    def test_canonical_user_start_is_not_proof_of_a_received_final(self):
        normal = TranscriptNormalizer()
        item = {'id':'stable','type':'transcriptSegment','role':'user'}
        normal.final_event('thread/realtime/item/started',{'item':item},1,'own')
        first = normal.final_event('turn.done',dc(),1.1,'own')
        self.assertIsNotNone(first)
        self.assertIsNone(normal.final_event('thread/realtime/item/completed',
                                           {'item':{**item,'text':'合成發話'}},1.2,'own'))

    def test_canonical_final_rejects_delayed_or_interleaved_idless_dc(self):
        for at, interleaved in ((2.1,False),(1.2,True)):
            with self.subTest(at=at,interleaved=interleaved):
                normal=TranscriptNormalizer()
                item={'id':'a','type':'transcriptSegment','role':'user','text':'甲','revision':0}
                first=normal.final_event('thread/realtime/item/completed',{'item':item},1,'own')
                if interleaved:
                    normal.final_event('thread/realtime/item/completed',
                                       {'item':{**item,'id':'b','text':'乙'}},1.1,'own')
                self.assertIsNone(normal.final_event('turn.done',dc('甲'),at,'own'))
                self.assertIsNotNone(first)
                self.assertEqual(normal.sequence,2 if interleaved else 1)

    def test_canonical_corrections_own_identity_and_reject_all_dc_texts(self):
        normal=TranscriptNormalizer()
        item={'id':'a','type':'transcriptSegment','role':'user','text':'原稿','revision':0}
        first=normal.final_event('thread/realtime/item/completed',{'item':item},1,'own')
        corrected=normal.final_event('thread/realtime/item/completed',
                                    {'item':{**item,'text':'更正','revision':1}},2,'own')
        self.assertEqual(first['event_id'],corrected['event_id'])
        self.assertEqual(corrected['revision'],1)
        for text in ('原稿','更正','不同的新發話'):
            self.assertIsNone(normal.final_event('turn.done',dc(text),5,'own'))
        repeated=normal.final_event('thread/realtime/item/completed',
                                   {'item':{**item,'id':'b','text':'更正'}},6,'own')
        self.assertNotEqual(repeated['event_id'],first['event_id'])

    def test_invalid_canonical_completed_does_not_disable_dc(self):
        item={'id':'a','type':'transcriptSegment','role':'user','text':'合成發話'}
        for changed in ({'role':'assistant'},{'text':''},{'text':None},
                        {'revision':'invalid'},{'revision':-1}):
            with self.subTest(changed=changed):
                normal=TranscriptNormalizer()
                self.assertIsNone(normal.final_event('thread/realtime/item/completed',
                                                     {'item':{**item,**changed}},1,'own'))
                self.assertNotIn('own',normal.canonical_final_threads)
                self.assertIsNotNone(normal.final_event('turn.done',dc(),2.1,'own'))

    def test_dc_first_canonical_handover_activates_final_authority(self):
        normal=TranscriptNormalizer()
        first=normal.final_event('turn.done',dc(),1,'own')
        item={'id':'a','type':'transcriptSegment','role':'user','text':'合成發話'}
        self.assertIsNone(normal.final_event('thread/realtime/item/completed',{'item':item},1.1,'own'))
        self.assertIn('own',normal.canonical_final_threads)
        self.assertIsNone(normal.final_event('turn.done',dc(),2.1,'own'))
        corrected=normal.final_event('thread/realtime/item/completed',
                                    {'item':{**item,'text':'更正'}},3,'own')
        self.assertEqual(corrected['event_id'],first['event_id'])

    def test_canonical_final_authority_is_thread_scoped(self):
        normal=TranscriptNormalizer()
        item={'id':'a','type':'transcriptSegment','role':'user','text':'合成發話'}
        normal.final_event('thread/realtime/item/completed',{'item':item},1,'own')
        self.assertIsNotNone(normal.final_event('turn.done',dc(),2.1,'other'))
        self.assertIsNone(normal.final_event('turn.done',dc('不同發話'),2.2,'own'))


class FramelessProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.finals = AsyncMock()
        self.text = AsyncMock()
        self.events = AsyncMock()
        self.provider = LiveProvider(None,'/tmp/unused-frameless-fixture',self.text,
                                     self.events,on_final=self.finals)
        self.provider.thread_id = 'own-thread'
        self.provider.rpc = SimpleNamespace(notifications=asyncio.Queue())
        self.receiver = asyncio.create_task(self.provider._receive())

    async def asyncTearDown(self):
        self.receiver.cancel()
        await asyncio.gather(self.receiver,return_exceptions=True)

    async def notify(self, method, **params):
        await self.provider.rpc.notifications.put(
            {'method':method,'params':{'threadId':'own-thread',**params}})
        for _ in range(20):
            if self.provider.rpc.notifications.empty():
                break
            await asyncio.sleep(.001)
        await asyncio.sleep(.005)

    async def test_dc_and_rpc_deltas_are_observable_without_promoting_partial_text(self):
        await self.provider._data_event({'type':'input_transcript.added','item':{'text':'合成增量'}})
        await self.provider._data_event({'type':'output_transcript.added','item':{'text':'合成回應'}})
        await self.notify('thread/realtime/transcript/delta',role='user',delta='合成增量')
        await self.notify('thread/realtime/transcript/delta',role='assistant',delta='合成回應')
        counts=self.provider.received_events
        self.assertEqual(counts['datachannel_input_transcript_delta'],1)
        self.assertEqual(counts['datachannel_output_transcript_delta'],1)
        self.assertEqual(counts['rpc_transcript_delta'],2)
        self.assertEqual(counts['rpc_role_user'],1)
        self.assertEqual(counts['rpc_role_assistant'],1)
        self.finals.assert_not_awaited()
        self.text.assert_not_awaited()
        self.assertEqual(self.provider.source_count,0)
        self.assertFalse(self.provider.rpc_transcripts)

    async def test_turn_role_cannot_be_forged_by_top_level_role(self):
        await self.provider._data_event(dc())
        await self.provider._data_event({**dc('合成回答','assistant'),'role':'user'})
        await self.provider._data_event({'type':'turn.done','role':'user','turn':{'transcript':'合成'}})
        counts=self.provider.received_events
        self.assertEqual(counts['datachannel_turn_done'],3)
        self.assertEqual(counts['datachannel_turn_role_user'],1)
        self.assertEqual(counts['datachannel_turn_role_assistant'],1)
        self.assertEqual(counts['datachannel_turn_role_unknown'],1)
        self.assertEqual(self.finals.await_count,1)
        self.assertEqual(self.finals.await_args.args[0]['source_role'],'user')
        self.assertIsNone(self.provider.error)

    async def test_user_rpc_start_or_delta_cannot_suppress_first_dc_final(self):
        await self.notify('thread/realtime/item/started',role='user',item={'type':'audio'})
        self.assertTrue(self.provider.rpc_transcripts)
        await self.notify('thread/realtime/transcript/delta',role='user',delta='合成')
        await self.provider._data_event(dc())
        self.assertEqual(self.finals.await_count,1)

    async def test_provider_rpc_dc_copies_and_assistant_rpc_are_filtered(self):
        await self.notify('thread/realtime/transcript/done',**rpc())
        await self.provider._data_event(dc())
        await self.notify('thread/realtime/transcript/done',**rpc('合成回答','assistant'))
        self.assertEqual(self.finals.await_count,1)
        self.assertEqual(self.provider.received_events['normalizer_filtered_events'],2)
        self.assertEqual(self.provider.received_events['datachannel_turn_role_assistant'],0)

    async def test_malformed_and_foreign_events_cannot_create_source(self):
        for turn in (None, [], 'private-payload', {}, {'role':'user'},
                     {'role':'tool','transcript':'private-payload'}):
            await self.provider._data_event({'type':'turn.done','turn':turn})
        await self.notify('thread/realtime/transcript/delta',threadId='foreign',role='user',delta='private-payload')
        await self.notify('thread/realtime/transcript/done',threadId='foreign',**rpc())
        self.finals.assert_not_awaited()
        self.assertEqual(self.provider.received_events['rpc_events'],0)
        self.assertIsNone(self.provider.error)

    async def test_known_types_and_unknown_types_are_counts_only_in_durable_snapshot(self):
        for kind in ('session.started','session.updated','output_audio.delta','delegation.created',
                     'private-payload-in-type', ['private-payload']):
            await self.provider._data_event({'type':kind,'payload':'private-payload',
                                             'SDP':'private-payload','audio':'private-payload'})
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(config())
            snapshot=RxAudioSnapshot(session).persist(phase='rx_capture',queue_depth=0,provider=self.provider)
            saved=session.status()['rx_audio']
            self.assertEqual(saved,snapshot)
            counts=saved['server_events']['counts']
            for key in ('datachannel_session_started','datachannel_session_updated',
                        'datachannel_output_audio_delta','datachannel_delegation_created'):
                self.assertEqual(counts[key],1)
            self.assertEqual(counts['datachannel_unrecognized_type'],2)
            self.assertEqual(set(counts),set(LIVE_EVENT_KEYS))
            self.assertNotIn('private-payload',json.dumps(saved))
        self.finals.assert_not_awaited()

    async def test_real_store_writes_one_final_per_user_utterance_in_arrival_order(self):
        with tempfile.TemporaryDirectory() as root:
            store=MeetingStore(root)
            store.create('meeting-v3')
            async def save(event):
                store.append_final('meeting-v3',event)
            self.provider.on_final=save
            for text in ('第一句','第二句'):
                await self.provider._data_event(dc(text))
            await self.notify('thread/realtime/transcript/done',**rpc('第二句'))
            await self.notify('thread/realtime/transcript/done',**rpc('第一句'))
            await self.provider._data_event(dc('合成回答','assistant'))
            result=store.read('meeting-v3')
            self.assertEqual([event['text'] for event in result['events']],['第一句','第二句'])
            self.assertEqual(self.provider.source_count,2)

    async def test_durable_canonical_correction_rejects_delayed_dc_copy(self):
        with tempfile.TemporaryDirectory() as root:
            store=MeetingStore(root)
            store.create('canonical')
            async def save(event):
                store.append_final('canonical',event)
            self.provider.on_final=save
            item={'id':'a','type':'transcriptSegment','role':'user','text':'原稿','revision':0}
            await self.notify('thread/realtime/item/completed',item=item)
            # Force the same >1-second gap as the reviewer's counterexample
            # without sleeping or contacting any transport.
            self.provider.normalizer.last['at']-=2
            await self.provider._data_event(dc('原稿'))
            await self.notify('thread/realtime/item/completed',item={**item,'text':'更正','revision':1})
            self.provider.normalizer.last['at']-=2
            await self.provider._data_event(dc('原稿'))
            await self.provider._data_event(dc('更正'))
            result=store.read('canonical')
            self.assertEqual(len(result['events']),1)
            self.assertEqual(result['events'][0]['text'],'更正')
            self.assertEqual(result['events'][0]['revision'],1)
