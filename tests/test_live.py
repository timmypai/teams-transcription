import asyncio
import unittest
import tempfile
from unittest.mock import AsyncMock

from teams_transcription.live import LiveProvider, TranscriptNormalizer
from teams_transcription.meeting_store import MeetingStore, MeetingStoreError


class LiveFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_flat_first_canonical_alias_survives_restart_without_text_dedup(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('alias-meeting')
            async def persist(event):
                await asyncio.to_thread(store.append_final, 'alias-meeting', event)
            def provider():
                p = LiveProvider(None, root, AsyncMock(), AsyncMock(), on_final=persist,
                    lookup_final=lambda event_id:store.get_final('alias-meeting', event_id),
                    bind_alias=lambda alias_id,event_id:store.bind_final_alias('alias-meeting',alias_id,event_id))
                p.thread_id = 'stable-thread'
                return p
            flat = {'itemId':'same-item', 'role':'user', 'text':'相同來源的逐字稿'}
            canonical = {'item':{'id':'same-item', 'realtimeSessionId':'stable-realtime',
                'type':'transcriptSegment', 'role':'user', 'text':flat['text']}}
            p = provider()
            await p._source('thread/realtime/transcript/done', flat)
            await p._source('thread/realtime/item/completed', canonical)
            initial = store.read('alias-meeting')
            self.assertEqual(len(initial['events']), 1)
            restarted = provider()
            await restarted._source('thread/realtime/item/completed', canonical)
            replay = store.read('alias-meeting')
            self.assertEqual(replay['version'], initial['version'])
            self.assertEqual(replay['sha256'], initial['sha256'])
            await restarted._source('thread/realtime/item/completed', {'item':{**canonical['item'], 'text':'別名更正'}})
            corrected = store.read('alias-meeting')
            self.assertEqual(len(corrected['events']), 1)
            self.assertEqual(corrected['events'][0]['event_id'], initial['events'][0]['event_id'])
            self.assertEqual(corrected['events'][0]['revision'], 1)
            await restarted._source('thread/realtime/item/completed', {'item':{**canonical['item'], 'id':'different-item'}})
            self.assertEqual(len(store.read('alias-meeting')['events']), 2)
            # A different stable source ID with the same text is never a handover.
            other = provider()
            other.thread_id = 'other-thread'
            await other._source('thread/realtime/transcript/done', {**flat, 'itemId':'flat-other'})
            await other._source('thread/realtime/item/completed', {'item':{**canonical['item'], 'id':'canonical-other'}})
            self.assertEqual(len(store.read('alias-meeting')['events']), 4)

    async def test_revision_one_restart_replay_and_correction_use_durable_state(self):
        with tempfile.TemporaryDirectory() as directory:
            store = MeetingStore(directory)
            store.create('meeting-a')
            async def persist(event):
                return await asyncio.to_thread(store.append_final, 'meeting-a', event)
            def provider():
                value = LiveProvider(None, directory, AsyncMock(), AsyncMock(), on_final=persist,
                    lookup_final=lambda event_id: store.get_final('meeting-a', event_id))
                value.thread_id = 'same-thread'
                return value
            params = {'item': {'id':'item-a', 'realtimeSessionId':'same-live',
                              'type':'transcriptSegment', 'role':'user', 'text':'第一版'}}
            method = 'thread/realtime/item/completed'
            first = provider()
            await first._source(method, params)
            params['item']['text'] = '第二版'
            await first._source(method, params)
            initial = store.read('meeting-a')
            self.assertEqual(initial['events'][0]['revision'], 1)
            restarted = provider()
            await restarted._source(method, params)
            replay = store.read('meeting-a')
            self.assertEqual(replay['sha256'], initial['sha256'])
            self.assertEqual(replay['version'], initial['version'])
            await restarted._source(method, {'item': {**params['item'], 'id':'item-b', 'text':'新片段'}})
            params['item']['text'] = '第三版'
            await restarted._source(method, params)
            current = store.read('meeting-a')
            self.assertEqual([e['text'] for e in current['events']], ['第三版','新片段'])
            self.assertEqual(current['events'][0]['revision'], 2)
            self.assertEqual(current['events'][0]['timestamp'], initial['events'][0]['timestamp'])
            self.assertEqual(current['events'][0]['sequence'], initial['events'][0]['sequence'])
            with self.assertRaises(MeetingStoreError):
                await restarted._source(method, {'item': {**params['item'], 'revision':1, 'text':'同舊revision異文'}})
            with self.assertRaisesRegex(ValueError, 'live_final_revision_conflict'):
                await restarted._source(method, {'item': {**params['item'], 'revision':2, 'text':'同現行revision異文'}})
            self.assertEqual(store.get_final('meeting-a', current['events'][0]['event_id'])['revision'], 2)

    async def test_final_callback_preserves_revisions_and_provider_session_identity(self):
        final = AsyncMock()
        text = AsyncMock()
        provider = LiveProvider(None, '/tmp/unused-live-test', text, AsyncMock(), on_final=final)
        provider.thread_id = 'session-a'
        item = {'id': 'speech-a', 'role': 'user', 'type': 'transcriptSegment', 'text': 'draft'}
        await provider._source('thread/realtime/item/completed', {'item': item})
        await provider._source('thread/realtime/item/completed', {'item': item})
        await provider._source('thread/realtime/item/completed', {'item': {**item, 'text': 'corrected'}})
        text.assert_not_awaited()
        self.assertEqual(final.await_count, 2)
        first, correction = [c.args[0] for c in final.await_args_list]
        self.assertEqual(first['event_id'], correction['event_id'])
        self.assertEqual(first['timestamp'], correction['timestamp'])
        self.assertEqual(correction['revision'], 1)
        other = TranscriptNormalizer().final_event('thread/realtime/item/completed', {'item': item}, 1, 'session-b')
        self.assertNotEqual(other['event_id'], first['event_id'])
        restarted = TranscriptNormalizer().final_event('thread/realtime/item/completed', {'item': item}, 1, 'session-a')
        self.assertEqual(restarted['event_id'], first['event_id'])
        normal = TranscriptNormalizer()
        session_one = normal.final_event('thread/realtime/item/completed',
            {'item': {**item, 'realtimeSessionId':'live-session-one'}}, 1, 'same-thread')
        session_two = normal.final_event('thread/realtime/item/completed',
            {'item': {**item, 'text':'other speech', 'realtimeSessionId':'live-session-two'}}, 3, 'same-thread')
        self.assertNotEqual(session_one['event_id'], session_two['event_id'])
        self.assertEqual(session_two['revision'], 0)
        other_thread = normal.final_event('thread/realtime/item/completed',
            {'item': {**item, 'realtimeSessionId':'live-session-one'}}, 5, 'another-thread')
        self.assertNotEqual(session_one['event_id'], other_thread['event_id'])
        self.assertEqual(other_thread['revision'], 0)

    async def test_sdp_acceptance_does_not_claim_media_connection(self):
        events = []

        async def event(kind, data):
            events.append((kind, data))

        provider = LiveProvider(None, '/tmp/unused-live-test', AsyncMock(), event)
        provider.rpc = AsyncMock()
        provider.rpc.notifications = asyncio.Queue()
        provider.rpc.thread.return_value = 'test-thread'

        async def request(method, params):
            if method == 'account/read':
                return {'account': {'type': 'chatgpt'}}
            if method == 'thread/realtime/start':
                await provider.rpc.notifications.put({'method': 'thread/realtime/sdp',
                    'params': {'threadId': 'test-thread', 'sdp': 'private-answer'}})
            return {}

        provider.rpc.request.side_effect = request
        provider.peer = AsyncMock()
        provider.peer.offer.return_value = 'private-offer'
        provider.peer.wait_connected.side_effect = TimeoutError('ICE connection timed out')
        provider.peer.stats.return_value = {'connection': 'failed', 'local_candidates': {'count': 0}}
        try:
            with self.assertRaisesRegex(RuntimeError, 'handshake timed out'):
                await provider.start()
        finally:
            provider.receiver.cancel()
            await asyncio.gather(provider.receiver, return_exceptions=True)
        self.assertEqual([kind for kind, _ in events], ['live_sdp_received', 'live_connection_failed'])
        self.assertEqual(events[-1][1]['stage'], 'media_connect')
        self.assertEqual(events[-1][1]['webrtc']['local_candidates']['count'], 0)
        self.assertNotIn('private-answer', str(events))

    async def test_native_startup_readiness_uses_handshake_not_optional_source_snapshot(self):
        class Source:
            native_audio_failed = False
        source = Source()
        provider = LiveProvider(None, '/tmp/unused-native-start', AsyncMock(), AsyncMock(), media_source=source)
        provider.rpc = AsyncMock()
        provider.rpc.notifications = asyncio.Queue()
        provider.rpc.thread.return_value = 'native-thread'
        async def request(method, params):
            if method == 'account/read':
                return {'account':{'type':'chatgpt'}}
            if method == 'thread/realtime/start':
                await provider.rpc.notifications.put({'method':'thread/realtime/sdp',
                    'params':{'threadId':'native-thread', 'sdp':'synthetic-answer'}})
            return {}
        provider.rpc.request.side_effect = request
        provider.peer = AsyncMock()
        provider.peer.offer.return_value = 'synthetic-offer'
        provider.peer.stats.side_effect = TimeoutError('private optional snapshot')
        waiting, connected = asyncio.Event(), asyncio.Event()
        async def wait_connected(*, timeout):
            waiting.set()
            await connected.wait()
        provider.peer.wait_connected.side_effect = wait_connected
        starting = asyncio.create_task(provider.start())
        try:
            await asyncio.wait_for(waiting.wait(), 1)
            self.assertFalse(starting.done())
            self.assertIsNone(provider.error)
            connected.set()
            await asyncio.wait_for(starting, 1)
            self.assertTrue(await provider.ready())
            provider.peer.wait_connected.assert_awaited_once_with(timeout=20)
            provider.peer.stats.assert_not_awaited()
            self.assertIsNone(provider.error)
            source.native_audio_failed = True
            self.assertFalse(await provider.ready())
            self.assertIsNone(provider.error)
        finally:
            connected.set()
            if not starting.done():
                starting.cancel()
            await asyncio.gather(starting, return_exceptions=True)
            if provider.receiver:
                provider.receiver.cancel()
                await asyncio.gather(provider.receiver, return_exceptions=True)

    async def test_diagnostic_failure_preserves_connection_error(self):
        event = AsyncMock()
        provider = LiveProvider(None, '/tmp/unused-live-test', AsyncMock(), event)
        provider.peer = AsyncMock()
        provider.peer.stats.side_effect = RuntimeError('Browser already closed')
        await provider._connection_failure('media_connect', 'ICE connection timed out')
        diagnostic = event.await_args.args[1]
        self.assertEqual(diagnostic['error'], 'ICE connection timed out')
        self.assertEqual(diagnostic['diagnostic_error'], 'Browser already closed')

    async def test_completed_gather_with_zero_candidates_has_specific_reason(self):
        event = AsyncMock()
        provider = LiveProvider(None, '/tmp/unused-live-test', AsyncMock(), event)
        provider.peer = AsyncMock()
        provider.peer.stats.return_value = {'ice_gathering':'complete','local_candidates':{'count':0}}
        diagnostic = await provider._connection_failure('media_connect', 'Timed out')
        self.assertEqual(diagnostic['reason'], 'no_local_ice_candidates')
        diagnostic = await provider._connection_failure('sdp_answer', 'Timed out')
        self.assertNotIn('reason', diagnostic)
