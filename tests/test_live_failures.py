"""Public notification contracts only; never read native logs or connect to Live."""
import asyncio
import json
import unittest
from unittest.mock import AsyncMock

from teams_transcription.live import LiveProvider


class LiveFailureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = AsyncMock()
        self.provider = LiveProvider(None,'/tmp/unused-live-failure',AsyncMock(),self.events)
        self.provider.thread_id = 'own-thread'
        self.provider.stage = 'connected'
        self.provider.answer_future = asyncio.get_running_loop().create_future()
        self.provider.rpc = AsyncMock()
        self.provider.rpc.notifications = asyncio.Queue()
        self.receiver = asyncio.create_task(self.provider._receive())

    async def asyncTearDown(self):
        self.receiver.cancel()
        await asyncio.gather(self.receiver,return_exceptions=True)
        future = self.provider.answer_future
        if future.done() and not future.cancelled(): future.exception()
        else: future.cancel()

    async def notify(self,method,**params):
        await self.provider.rpc.notifications.put(dict(method=method,params={'threadId':'own-thread',**params}))
        for _ in range(20):
            if self.provider.error: return
            await asyncio.sleep(.001)

    async def test_rpc_expired_preserves_first_cause_without_payload(self):
        await self.notify('thread/realtime/closed',reason='expired',usage={'secret':'private'})
        await self.notify('thread/realtime/error',message='WebSocket Connection reset without closing handshake; private')
        self.assertEqual(self.provider.error,'live_session_expired')
        self.assertEqual(self.provider.failure['reason'],'expired')
        self.assertEqual(self.provider.failure['channel'],'rpc')
        self.assertEqual(self.events.await_count,1)
        self.assertNotIn('private',json.dumps(self.events.await_args.args))

    async def test_unknown_close_reason_is_not_persisted_or_inferred(self):
        await self.notify('thread/realtime/closed',reason='private arbitrary reason')
        self.assertEqual(self.provider.error,'live_session_closed')
        self.assertEqual(self.provider.failure['reason'],'unknown')
        self.assertNotIn('private',str(self.provider.failure))

    async def test_rpc_reset_signature_is_classified_without_message(self):
        await self.notify('thread/realtime/error',message='WebSocket Connection reset without closing handshake; private')
        self.assertEqual(self.provider.error,'live_transport_reset')
        self.assertEqual(self.provider.failure['type'],'thread/realtime/error')
        self.assertNotIn('private',str(self.provider.failure))

    async def test_arbitrary_error_does_not_claim_transport_loss(self):
        await self.notify('thread/realtime/error',message='arbitrary private error expired')
        self.assertEqual(self.provider.error,'live_realtime_error')
        self.assertEqual(self.provider.failure['reason'],'unknown')

    async def test_client_exit_is_separate_from_server_close(self):
        await self.notify('client/exit',error='private stderr')
        self.assertEqual(self.provider.error,'live_app_server_exit')
        self.assertNotIn('private',str(self.events.await_args.args))

    async def test_datachannel_expired_only_when_event_is_actually_delivered(self):
        await self.provider._data_event(dict(type='session.closed',reason='expired',usage={'secret':'private'}))
        self.assertEqual(self.provider.error,'live_session_expired')
        self.assertEqual(self.provider.failure['channel'],'datachannel')
        self.assertNotIn('private',str(self.events.await_args.args))

    async def test_unsupported_rpc_wire_event_is_not_fabricated_as_notification(self):
        await self.notify('session.closed',reason='expired')
        self.assertIsNone(self.provider.error)
        self.events.assert_not_awaited()

    async def test_foreign_thread_close_cannot_close_this_provider(self):
        await self.notify('thread/realtime/closed',threadId='other-thread',reason='expired')
        self.assertIsNone(self.provider.error)

    async def test_local_stop_ack_does_not_become_failure(self):
        self.provider.closing = True
        await self.notify('thread/realtime/closed',reason='requested')
        self.assertIsNone(self.provider.error)
        self.events.assert_not_awaited()

    async def test_requested_native_close_ack_keeps_later_real_error(self):
        self.provider.peer.request_session_close = AsyncMock(return_value=True)
        await self.provider.drain(0)
        await self.provider.peer.callback({'type':'session.closed','reason':'client_request'})
        self.assertIsNone(self.provider.error)
        self.assertEqual(self.provider.received_events['datachannel_closed'],1)
        await self.provider.peer.callback({'type':'error','error':{'message':'private failure'}})
        with self.assertRaisesRegex(RuntimeError,'live_realtime_error'):
            await self.provider.drain(0)
        self.assertEqual(self.provider.failure['channel'],'datachannel')
        self.assertNotIn('private',json.dumps(self.provider.failure))


    async def test_native_close_ack_can_arrive_before_dispatch_returns(self):
        async def dispatch(**kwargs):
            await self.provider.peer.callback({'type':'session.closed','reason':'client_request'})
            return True
        self.provider.peer.request_session_close=AsyncMock(side_effect=dispatch)
        await self.provider.drain(0)
        self.assertIsNone(self.provider.error)
        self.events.assert_not_awaited()


    async def test_closing_flag_without_own_request_cannot_authorize_native_ack(self):
        self.provider.closing=True
        await self.provider.peer.callback({'type':'session.closed','reason':'client_request'})
        self.assertEqual(self.provider.error,'live_session_closed')
        self.assertEqual(self.provider.failure['reason'],'client_request')


    async def test_requested_stop_keeps_explicit_close_failures(self):
        for reason in ('expired','transport_closed','private-unknown',None):
            with self.subTest(reason=reason):
                provider=LiveProvider(None,'/tmp/unused-close-reason',AsyncMock(),AsyncMock())
                provider.peer.request_session_close=AsyncMock(return_value=True)
                await provider.drain(0)
                await provider.peer.callback({'type':'session.closed','reason':reason})
                expected='live_session_expired' if reason=='expired' else 'live_session_closed'
                with self.assertRaisesRegex(RuntimeError,expected):
                    await provider.drain(0)
                self.assertEqual(provider.error,expected)
                self.assertNotIn('private',json.dumps(provider.failure))

    async def test_real_provider_error_during_input_drain_is_not_a_stop_ack(self):
        async def finish_input(**kwargs):
            await self.provider._data_event({'type':'error', 'error':{'message':'synthetic failure'}})
            return True

        self.provider.peer = AsyncMock()
        self.provider.peer.request_session_close.side_effect = finish_input
        with self.assertRaisesRegex(RuntimeError, 'live_realtime_error'):
            await self.provider.drain(.001)
        self.assertEqual(self.provider.error, 'live_realtime_error')
        self.assertEqual(self.provider.failure['channel'], 'datachannel')

    async def test_new_provider_error_during_media_close_is_not_success(self):
        async def close_media():
            await self.provider._data_event({'type':'error', 'error':{'message':'synthetic failure'}})

        self.provider.peer = AsyncMock()
        self.provider.peer.request_session_close.return_value = False
        self.provider.peer.close.side_effect = close_media
        self.provider.rpc.process = None
        self.provider.receiver = self.receiver
        with self.assertRaisesRegex(RuntimeError, 'live_realtime_error'):
            await self.provider.close()
        self.provider.peer.close.assert_awaited_once()
        self.provider.rpc.close.assert_awaited_once()

    async def test_known_provider_error_at_close_entry_surfaces_after_cleanup(self):
        await self.notify('thread/realtime/error', message='synthetic failure')
        self.provider.peer = AsyncMock()
        self.provider.peer.request_session_close.return_value = False
        self.provider.rpc.process = None
        self.provider.receiver = self.receiver
        with self.assertRaisesRegex(RuntimeError, 'live_realtime_error'):
            await self.provider.close()
        self.provider.peer.close.assert_awaited_once()
        self.provider.rpc.close.assert_awaited_once()
        self.assertTrue(self.receiver.done())

    async def test_malformed_notification_has_safe_protocol_classification(self):
        await self.provider.rpc.notifications.put(dict(method='thread/realtime/error',params='private'))
        await asyncio.sleep(.01)
        self.assertEqual(self.provider.error,'live_notification_invalid')
        self.assertNotIn('private',str(self.events.await_args.args))

    async def test_actual_media_readiness_requires_peer_and_datachannel(self):
        self.provider.peer=AsyncMock()
        self.provider.peer.stats.return_value=dict(connection='connected',data_channel='open')
        self.assertTrue(await self.provider.ready())
        self.provider.peer.stats.return_value=dict(connection='connected',data_channel='closed',payload='private')
        self.assertFalse(await self.provider.ready())
        self.assertEqual(self.provider.error,'live_media_not_ready')
        self.assertNotIn('private',str(self.events.await_args.args))

    async def test_native_readiness_uses_handshake_and_definite_source_latch_not_optional_stats(self):
        class Source:
            native_audio_failed = False
        self.provider.media_source = Source()
        self.provider.peer = AsyncMock()
        self.provider.peer.stats.side_effect = TimeoutError('private optional snapshot')
        self.assertTrue(await self.provider.ready())
        self.provider.peer.stats.assert_not_awaited()
        self.assertIsNone(self.provider.error)
        self.provider.media_source.native_audio_failed = True
        self.assertFalse(await self.provider.ready())
        self.assertIsNone(self.provider.error)
        self.provider.media_source.native_audio_failed = False
        self.provider.stage = 'media_connect'
        self.assertFalse(await self.provider.ready())
        self.provider.peer.stats.assert_not_awaited()

    async def test_event_roles_and_existing_datachannel_filter_are_observable(self):
        await self.notify('thread/realtime/item/started',role='user',item={'role':'user','type':'audio','private':'payload'})
        await self.provider._data_event({'type':'conversation.item.input_audio_transcription.completed',
            'role':'user', 'item_id':'fixture', 'transcript':'controlled synthetic text'})
        await self.notify('thread/realtime/item/completed',item={'role':'assistant','type':'transcriptSegment','text':'fixture'})
        await self.notify('thread/realtime/transcript/done',role='user',itemId='final',text='controlled synthetic text')
        counts=self.provider.received_events
        self.assertTrue(self.provider.rpc_transcripts)
        self.assertEqual(counts['rpc_item_started'],1)
        self.assertEqual(counts['rpc_item_completed'],1)
        self.assertEqual(counts['rpc_role_user'],2)
        self.assertEqual(counts['rpc_role_assistant'],1)
        self.assertEqual(counts['datachannel_transcript_completed'],1)
        self.assertEqual(counts['datachannel_transcript_filtered'],1)
        self.assertEqual(counts['datachannel_role_user'],1)
        self.assertEqual(counts['normalizer_filtered_events'],2)
        self.assertEqual(self.provider.source_count,1)
        self.assertNotIn('private',json.dumps(counts))

    async def test_speech_counts_require_actual_datachannel_callback_and_ignore_foreign_rpc(self):
        await self.provider._data_event({'type':'input_audio_buffer.speech_started','payload':'private'})
        await self.provider._data_event({'type':'input_audio_buffer.speech_stopped','payload':'private'})
        await self.notify('thread/realtime/transcript/done',threadId='other-thread',role='user',text='private')
        counts=self.provider.received_events
        self.assertEqual(counts['datachannel_speech_started'],1)
        self.assertEqual(counts['datachannel_speech_stopped'],1)
        self.assertEqual(counts['datachannel_role_unknown'],2)
        self.assertEqual(counts['rpc_events'],0)
        self.assertEqual(self.provider.source_count,0)
        self.assertNotIn('private',json.dumps(counts))
