"""Owned Native lifecycle metadata; fixture media/RPC, no model or browser."""
import asyncio
import hashlib
import json
import unittest

class TurnLifecycleEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from test_b_native import NativeLiveTests
        self.case = NativeLiveTests('runTest')
        await self.case.asyncSetUp()
        self.native = self.case.live

    async def asyncTearDown(self):
        await self.case.asyncTearDown()

    async def test_matching_done_evidence_is_hashed_and_does_not_delay_completion(self):
        await self.case.utterance('小白小白，目前交付日？')
        await self.case.until(lambda:self.native.state == 'answering')
        await self.case.turn('turn.created', 'PRIVATE-TURN')
        await self.case.receive_media(.1, active=True)
        await self.case.turn('turn.done', 'PRIVATE-TURN')
        await self.case.receive_media(.81)
        await self.case.until(lambda:self.native.state == 'sleeping')
        trace = self.native.turn_lifecycle_snapshot()
        self.assertEqual(trace['counts']['dc_turn_created'], 1)
        self.assertEqual(trace['counts']['dc_turn_done'], 1)
        rows = [row for row in trace['events'] if row['event'] == 'turn.done']
        self.assertEqual(rows[-1]['turn_id_sha256'], hashlib.sha256(b'PRIVATE-TURN').hexdigest())
        self.assertTrue(rows[-1]['id_present'])
        self.assertEqual(rows[-1]['action'], 'set')
        self.assertEqual(rows[-1]['reason'], 'all_known_turns_done')
        self.assertEqual(trace['current']['open_turn_count'], 0)
        self.assertTrue(trace['current']['done_is_set'])
        serialized = json.dumps(trace)
        for private in ('PRIVATE-TURN', 'native-thread', self.native._request_id):
            self.assertNotIn(private, serialized)

    async def test_rpc_final_and_long_quiet_leave_missing_or_unknown_dc_done_uncompleted(self):
        await self.case.utterance('小白小白，目前交付日？')
        await self.case.until(lambda:self.native.state == 'answering')
        await self.case.turn('turn.created', 'PRIVATE-OPEN')
        await self.case.receive_media(.1, active=True)
        message = {'method':'thread/realtime/transcript/done', 'params':{
            'threadId':'native-thread', 'role':'assistant', 'text':'PRIVATE-CONTENT'}}
        self.native.observe_notification_receipt(message)
        await self.native.handle_notification(message['method'], message['params'])
        await self.native._data_event({'type':'turn.done','turn':{'role':'assistant'}})
        await self.case.turn('turn.done', 'PRIVATE-UNKNOWN')
        await self.case.receive_media(39.64)
        await asyncio.sleep(.08)
        snapshot = self.native.speech_completion_snapshot()
        self.assertEqual(snapshot['phase'], 'waiting_done')
        self.assertFalse(snapshot['done_is_set'])
        self.assertEqual(snapshot['quiet_received_seconds'], 39.64)
        trace = snapshot['turn_lifecycle']
        self.assertEqual(trace['current']['open_turn_count'], 1)
        self.assertEqual(trace['rpc_transcripts']['assistant_done'], 1)
        self.assertGreaterEqual(trace['counts']['rpc_own_thread'], 1)
        self.assertEqual(trace['events'][-1]['reason'], 'unmatched_done')
        self.assertTrue(any(row['reason'] == 'invalid_id' for row in trace['events']))
        self.assertNotIn('PRIVATE-', json.dumps(trace))

    async def test_flood_is_bounded_detached_and_foreign_or_malformed_values_do_not_leak(self):
        for number in range(100):
            await self.case.turn('turn.created', 'PRIVATE-'+str(number), role='PRIVATE-ROLE')
        for turn in ({'role':'assistant'}, {'id':'PRIVATE-'*100,'role':['PRIVATE-ROLE']},
                {'id':'\ud800','role':'PRIVATE-ROLE'}):
            await self.native._data_event({'type':'turn.done','turn':turn,'text':'PRIVATE-CONTENT'})
        self.native.observe_notification_receipt({'method':'PRIVATE-METHOD',
            'params':{'threadId':'PRIVATE-FOREIGN','token':'PRIVATE-TOKEN'}})
        trace = self.native.turn_lifecycle_snapshot()
        self.assertEqual(len(trace['events']), 64)
        self.assertGreater(trace['events_dropped'], 0)
        self.assertEqual(trace['counts']['dc_received'], 103)
        self.assertGreaterEqual(trace['counts']['rpc_other_scope'], 1)
        self.assertTrue(all(len(row['open_turn_sha256']) <= 8 for row in trace['events']))
        self.assertTrue(all(row['received_ms'] >= 0 for row in trace['events']))
        self.assertNotIn('PRIVATE-', json.dumps(trace))
        trace['counts']['dc_received'] = -1
        trace['events'][0]['reason'] = 'MUTATED'
        trace['events'][0]['open_turn_sha256'].append('MUTATED')
        self.assertEqual(self.native.turn_lifecycle_snapshot()['counts']['dc_received'], 103)
        self.assertNotIn('MUTATED', json.dumps(self.native.turn_lifecycle_snapshot()))

    async def test_new_turn_clear_and_all_open_turns_done_remain_required(self):
        await self.case.utterance('小白小白，目前交付日？')
        await self.case.until(lambda:self.native.state == 'answering')
        await self.case.turn('turn.created', 'first')
        await self.case.receive_media(.1, active=True)
        await self.case.turn('turn.done', 'first')
        await self.case.turn('turn.created', 'second')
        trace = self.native.turn_lifecycle_snapshot()
        self.assertEqual(trace['events'][-1]['reason'], 'new_turn')
        self.assertEqual(trace['events'][-1]['action'], 'clear')
        self.assertFalse(trace['current']['done_is_set'])
        await self.case.receive_media(.81)
        await asyncio.sleep(.08)
        self.assertEqual(self.native.state, 'answering')
        await self.case.turn('turn.done', 'second')
        await self.case.until(lambda:self.native.state == 'sleeping')
        self.assertTrue(self.native.turn_lifecycle_snapshot()['current']['done_is_set'])

    async def test_ingress_counters_saturate_without_affecting_turn_rejection(self):
        self.native._turn_counts['dc_received'] = 2147483647
        await self.case.turn('turn.done', 'unmatched')
        trace = self.native.turn_lifecycle_snapshot()
        self.assertEqual(trace['counts']['dc_received'], 2147483647)
        self.assertTrue(trace['counters_saturated'])
        self.assertFalse(trace['current']['done_is_set'])

    async def test_created_during_snapshot_send_pairs_done_after_output_lease(self):
        # case0006 ordering: created(thinking), lease clear, matching done, RPC final.
        await self.case.utterance('小白小白，目前交付日？')
        await self.case.until(lambda:self.native.state == 'answering')
        await self.case.emit_speech('第一題。')
        await self.case.until(lambda:self.native.state == 'sleeping')
        created = False
        async def send(event):
            nonlocal created
            if event['content'][0]['text'] != 'RESULT_COMPLETE' and not created:
                created = True
                self.assertEqual(self.native.state, 'thinking')
                self.assertFalse(self.native.output.active)
                await self.case.turn('turn.created', 'early-second-answer')
        self.native.peer.send_event.side_effect = send
        await self.case.utterance('小白小白，現在交付日？')
        await self.case.until(lambda:self.native.state == 'answering')
        await self.native._tool_task
        await self.case.receive_media(.1, active=True)
        await self.case.turn('turn.done', 'early-second-answer')
        await self.native.handle_notification('thread/realtime/transcript/done', {
            'threadId':'native-thread','role':'assistant','text':'第二題。'})
        self.assertTrue(self.native.speech_completion_snapshot()['done_is_set'])
        await self.case.receive_media(.81)
        await self.case.until(lambda:self.native.state == 'sleeping')
        self.assertTrue(created)
        self.assertEqual(self.native.turn_lifecycle_snapshot()['current']['open_turn_count'], 0)

    async def hold_snapshot_send(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(event):
            if not entered.is_set() and event['content'][0]['text'] != 'RESULT_COMPLETE':
                entered.set()
                await release.wait()
        self.native.peer.send_event.side_effect = send
        await self.case.utterance('小白小白，目前交付日？')
        await asyncio.wait_for(entered.wait(), 3)
        self.addCleanup(release.set)
        self.assertEqual(self.native.state, 'thinking')
        self.assertFalse(self.native.output.active)
        return release

    async def test_created_and_done_before_lease_survive_clear_and_require_real_media(self):
        release = await self.hold_snapshot_send()
        await self.case.turn('turn.created', 'early-complete')
        await self.case.turn('turn.done', 'early-complete')
        self.assertFalse(self.native.output.active)
        release.set()
        await self.native._tool_task
        self.assertTrue(self.native.speech_completion_snapshot()['done_is_set'])
        await self.case.receive_media(.81)
        await asyncio.sleep(.06)
        self.assertEqual(self.native.state, 'answering')  # Silence alone never completes.
        await self.case.receive_media(.1, active=True)
        await self.case.receive_media(.81)
        await self.case.until(lambda:self.native.state == 'sleeping')

    async def test_early_reverse_wrong_role_unknown_done_and_duplicates_do_not_complete(self):
        release = await self.hold_snapshot_send()
        await self.case.turn('turn.done', 'early')  # Done-before-created is not buffered.
        await self.case.turn('turn.created', 'early', role='user')
        await self.case.turn('turn.created', 'early')
        await self.case.turn('turn.created', 'early')
        await self.case.turn('turn.done', 'unknown')
        await self.case.turn('turn.done', 'early', role='user')
        release.set()
        await self.native._tool_task
        await self.case.receive_media(.1, active=True)
        await self.case.receive_media(.81)
        self.assertFalse(self.native.speech_completion_snapshot()['done_is_set'])
        self.assertEqual(self.native.turn_lifecycle_snapshot()['current']['open_turn_count'], 1)
        await self.case.turn('turn.done', 'early')
        await self.case.turn('turn.done', 'early')  # Duplicate must not reopen or corrupt.
        await self.case.until(lambda:self.native.state == 'sleeping')

    async def test_interleaved_early_and_lease_turns_wait_for_all_known_done(self):
        release = await self.hold_snapshot_send()
        await self.case.turn('turn.created', 'early-prefix')
        await self.case.turn('turn.created', 'early-answer')
        await self.case.turn('turn.done', 'early-answer')
        release.set()
        await self.native._tool_task
        await self.case.turn('turn.created', 'late-answer')
        await self.case.receive_media(.1, active=True)
        await self.case.turn('turn.done', 'late-answer')
        await self.case.receive_media(.81)
        self.assertFalse(self.native.speech_completion_snapshot()['done_is_set'])
        self.assertEqual(self.native.turn_lifecycle_snapshot()['current']['open_turn_count'], 1)
        await self.case.turn('turn.done', 'early-prefix')
        await self.case.until(lambda:self.native.state == 'sleeping')

    async def test_previous_turn_is_rejected_during_snapshot_and_after_lease(self):
        await self.case.utterance('小白小白，目前交付日？')
        await self.case.until(lambda:self.native.state == 'answering')
        await self.case.emit_speech('前題。')
        await self.case.until(lambda:self.native.state == 'sleeping')
        release = await self.hold_snapshot_send()
        await self.case.turn('turn.created', 'assistant-1')
        await self.case.turn('turn.done', 'assistant-1')
        self.assertFalse(self.native.speech_completion_snapshot()['done_is_set'])
        release.set()
        await self.native._tool_task
        await self.case.turn('turn.done', 'assistant-1')
        self.assertFalse(self.native.speech_completion_snapshot()['done_is_set'])
        await self.case.turn('turn.created', 'current')
        await self.case.receive_media(.1, active=True)
        await self.case.turn('turn.done', 'current')
        await self.case.receive_media(.81)
        await self.case.until(lambda:self.native.state == 'sleeping')
        self.assertTrue(any(row['reason'] == 'previous_turn'
            for row in self.native.turn_lifecycle_snapshot()['events']))

    async def test_no_early_admission_before_snapshot_dispatch_and_after_cancel_or_close(self):
        entered, release = asyncio.Event(), asyncio.Event()
        prepare = self.native.context.prepare
        async def preparing(*args, **kwargs):
            entered.set()
            await release.wait()
            return await prepare(*args, **kwargs)
        self.native.context.prepare = preparing
        self.addCleanup(release.set)
        await self.case.utterance('小白小白，目前交付日？')
        await asyncio.wait_for(entered.wait(), 3)
        await self.case.turn('turn.created', 'unsolicited')
        release.set()
        await self.native._tool_task
        await self.case.turn('turn.done', 'unsolicited')
        self.assertFalse(self.native.speech_completion_snapshot()['done_is_set'])
        await self.native._cancel_request()
        release = await self.hold_snapshot_send()
        await self.case.turn('turn.created', 'cancelled-early')
        await self.native._cancel_request()
        await self.case.turn('turn.done', 'cancelled-early')
        await self.case.turn('turn.created', 'after-cancel')
        self.assertFalse(self.native.speech_completion_snapshot()['done_is_set'])
        self.assertFalse(self.native.output.active)
        await self.native.close()
        await self.case.turn('turn.done', 'cancelled-early')
        self.assertFalse(self.native.speech_completion_snapshot()['done_is_set'])

    async def test_partial_snapshot_failure_discards_early_turn_without_output_or_commit(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def send(event):
            entered.set()
            await release.wait()
            raise OSError('synthetic send failed')
        self.native.peer.send_event.side_effect = send
        self.addCleanup(release.set)
        await self.case.utterance('小白小白，目前交付日？')
        await asyncio.wait_for(entered.wait(), 3)
        await self.case.turn('turn.created', 'failed-input-answer')
        await self.case.turn('turn.done', 'failed-input-answer')
        release.set()
        await self.native._tool_task
        self.assertEqual(self.native.state, 'sleeping')
        self.assertFalse(self.native.output.active)
        self.assertFalse(self.native.status()['tool_deliveries'][-1]['marker_returned'])
        self.assertEqual(self.native.context.status()['committed_records'], 0)
        self.case.sink.set_muted.assert_awaited_once_with(True)  # Startup mute only.
        # A later fresh request cannot use the failed request's paired done.
        self.native.peer.send_event.side_effect = None
        await self.case.utterance('小白小白，現在交付日？')
        await self.case.until(lambda:self.native.state == 'answering')
        await self.case.turn('turn.done', 'failed-input-answer')
        await self.case.receive_media(.1, active=True)
        await self.case.receive_media(.81)
        self.assertFalse(self.native.speech_completion_snapshot()['done_is_set'])

    async def test_prior_nonfinal_turn_replay_cannot_complete_next_request(self):
        await self.case.utterance('小白小白，目前交付日？')
        await self.case.until(lambda:self.native.state == 'answering')
        for ident in ('old-prefix', 'old-answer'):
            await self.case.turn('turn.created', ident)
            await self.case.receive_media(.1, active=True)
            await self.case.turn('turn.done', ident)
        await self.case.receive_media(.81)
        await self.case.until(lambda:self.native.state == 'sleeping')
        release = await self.hold_snapshot_send()
        await self.case.turn('turn.created', 'old-prefix')
        await self.case.turn('turn.done', 'old-prefix')
        release.set()
        await self.native._tool_task
        self.assertFalse(self.native.speech_completion_snapshot()['done_is_set'])
        await self.case.receive_media(.1, active=True)
        await self.case.receive_media(.81)
        await asyncio.sleep(.06)
        self.assertEqual(self.native.state, 'answering')
        await self.case.turn('turn.created', 'fresh-answer')
        await self.case.receive_media(.1, active=True)
        await self.case.turn('turn.done', 'fresh-answer')
        await self.case.receive_media(.81)
        await self.case.until(lambda:self.native.state == 'sleeping')

    async def test_identity_history_capacity_fails_closed_without_evicting_retired_ids(self):
        from unittest.mock import patch
        release = await self.hold_snapshot_send()
        with patch('teams_transcription.b_native.TURN_ID_HISTORY_LIMIT', 2):
            for ident in ('first', 'second'):
                await self.case.turn('turn.created', ident)
                await self.case.turn('turn.done', ident)
            await self.case.turn('turn.created', 'first')  # Retired; no new allocation.
            self.assertIsNone(self.native.error)
            self.assertEqual(self.native.turn_lifecycle_snapshot()['turn_identity_history_count'], 2)
            await self.case.turn('turn.created', 'third')
            release.set()
            await self.native._tool_task
            self.assertEqual(self.native.state, 'failed')
            self.assertFalse(self.native.output.active)
            self.assertFalse(self.native.output.enabled)
            self.assertEqual(self.native.runtime_failure_snapshot()['code'],
                'response_turn_identity_capacity')
            trace = self.native.turn_lifecycle_snapshot()
            self.assertEqual(trace['turn_identity_history_count'], 2)
            self.assertEqual(trace['turn_identity_history_limit'], 2)
            self.assertTrue(any(row['reason'] == 'retired_turn' for row in trace['events']))
            self.assertTrue(any(row['reason'] == 'identity_capacity_exhausted' for row in trace['events']))
            self.assertFalse(self.native.status()['tool_deliveries'][-1]['marker_returned'])
