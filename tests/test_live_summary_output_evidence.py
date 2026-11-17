"""Opt-in synthetic assistant evidence with controlled RPC/peer, never a model."""
import asyncio
import json
import unittest
from unittest.mock import patch

from teams_transcription.live_summary_output_evidence import (
    ASSEMBLY_HEAD_BYTES, ASSEMBLY_TAIL_BYTES, EVENT_LIMIT, TEXT_BYTES,
    LiveSummaryOutputEvidence,
)
from teams_transcription.live_summary_probe import LiveSummaryProbe as ActualProbe
from teams_transcription.live_summary_stream import LiveSummaryStream, LiveSummaryStreamError
from tests import test_live_summary_probe


class LiveSummaryOutputEvidenceTests(unittest.IsolatedAsyncioTestCase):
    def make_probe(self, *, capture_synthetic_output=True, voice_pcm=b'\1\0'*20,
                   input_mode='initial_items', **options):
        def construct(*args, **kwargs):
            return ActualProbe(*args, **kwargs, capture_synthetic_output=capture_synthetic_output,
                               voice_pcm=voice_pcm, input_mode=input_mode)
        with patch.object(test_live_summary_probe, 'LiveSummaryProbe', side_effect=construct):
            return test_live_summary_probe.LiveSummaryProbeTests.make_probe(self, **options)

    def value(self):
        return {'重點':[{'text':'預算十萬元。', 'sources':['s1'], 'owner':'', 'date':''}],
                '決策':[], '待辦':[], '未決問題':[]}

    def candidate(self, probe, value=None):
        return json.dumps(self.value() if value is None else value, ensure_ascii=False) + '\n' + probe.stream.marker

    def stream(self, capture=True):
        stream = LiveSummaryStream('meeting-one', 'probe-thread',
            [{'id':'s1', 'text':'預算十萬元。'}], nonce='a'*32)
        if capture:
            stream.enable_synthetic_text_evidence()
        return stream

    def feed(self, stream, text, method='done'):
        return stream.feed('thread/realtime/transcript/' + method,
            {'threadId':'probe-thread', 'role':'assistant', 'text' if method == 'done' else 'delta':text})

    def assert_cleaned_draft_scope(self, probe, result):
        self.assertTrue(result['draft_only'])
        self.assertFalse(result['service_contract_verified'])
        self.assertFalse(result['provider_final'])
        self.assertEqual(result['stop']['cleanup_state'], 'cleaned')
        self.assertIsNone(probe.receiver)
        self.assertIsNone(probe._voice_pcm)
        probe.peer.close.assert_awaited_once()
        probe.rpc.close.assert_awaited_once()

    def test_utf8_total_event_text_cap_is_shared_across_all_four_lanes(self):
        evidence = LiveSummaryOutputEvidence()
        evidence.record('rpc_delta', '🙂'*8191, 0)
        evidence.record('data_delta', '界A', 1)
        evidence.record('rpc_done', 'must not get a new budget', 2)
        evidence.record('data_done', '也不能', 3)
        result = evidence.snapshot({})
        self.assertEqual(result['retained_text_bytes'], TEXT_BYTES)
        self.assertEqual([row['bytes'] for row in result['events']], [32764, 4, 0, 0])
        self.assertEqual(result['events'][1]['text'], '界A')
        self.assertEqual([row['lane'] for row in result['events']],
                         ['rpc_delta', 'data_delta', 'rpc_done', 'data_done'])
        self.assertTrue(result['events'][2]['truncated'])
        self.assertEqual(sum(len(row['text'].encode('utf-8')) for row in result['events']), TEXT_BYTES)
        self.assertFalse(result['channels_merged'])
        self.assertFalse(result['service_contract_verified'])

    def test_partial_multibyte_characters_are_not_retained_and_event_count_is_bounded(self):
        evidence = LiveSummaryOutputEvidence()
        evidence.record('rpc_delta', 'x'*(TEXT_BYTES-2), 0)
        evidence.record('data_done', '🙂', 1)
        self.assertEqual(evidence.events[-1]['text'], '')
        self.assertTrue(evidence.events[-1]['truncated'])
        for index in range(EVENT_LIMIT+5):
            evidence.record('rpc_done', '', index)
        result = evidence.snapshot({})
        self.assertEqual(len(result['events']), EVENT_LIMIT)
        self.assertEqual(result['observed_events'], EVENT_LIMIT+7)
        self.assertEqual(result['dropped_events'], 7)
        self.assertEqual(result['events'][-1]['sequence'], EVENT_LIMIT)
        self.assertLess(len(json.dumps(result)), 65536)

    def test_invalid_text_timestamps_and_snapshot_mutations_do_not_leak_into_retained_state(self):
        evidence = LiveSummaryOutputEvidence()
        for value, timestamp in (('\ud800PRIVATE_INVALID_SURROGATE', True), (None, -1),
                                 ({'private':'PRIVATE_INVALID_OBJECT'}, 'PRIVATE_TIME')):
            evidence.record('rpc_done', value, timestamp)
        evidence.record('unknown-private-lane', 'PRIVATE_UNKNOWN_LANE', 0)
        assembly = {'assembly_prefix':{'text':'controlled'}, 'nested':[{'state':'original'}]}
        snapshot = evidence.snapshot(assembly)
        self.assertEqual(snapshot['observed_events'], 3)
        for row in snapshot['events']:
            self.assertTrue(row['invalid_text'])
            self.assertEqual(row['text'], '')
            self.assertIsNone(row['observed_ms'])
        self.assertNotIn('PRIVATE', json.dumps(snapshot))
        snapshot['events'][0]['text'] = 'changed'
        snapshot['assembly']['nested'][0]['state'] = 'changed'
        self.assertEqual(assembly['nested'][0]['state'], 'original')
        assembly['assembly_prefix']['text'] = 'external mutation'
        fresh = evidence.snapshot({'nested':[{'state':'original'}]})
        self.assertEqual(fresh['events'][0]['text'], '')
        self.assertEqual(fresh['assembly']['nested'][0]['state'], 'original')

    def test_stream_head_and_tail_preserve_unicode_end_marker_with_combined_budget(self):
        stream = self.stream()
        value = self.value()
        value['重點'][0]['text'] = '界🙂'*12000
        text = json.dumps(value, ensure_ascii=False) + '\n' + stream.marker
        self.assertTrue(self.feed(stream, text))
        snapshot = stream.diagnostic_snapshot()
        head, tail = snapshot['assembly_prefix'], snapshot['assembly_suffix']
        self.assertEqual(snapshot['assembly_bytes'], len(text.encode('utf-8')))
        self.assertEqual(head['bytes'], len(head['text'].encode('utf-8')))
        self.assertEqual(tail['bytes'], len(tail['text'].encode('utf-8')))
        self.assertLessEqual(head['bytes'], ASSEMBLY_HEAD_BYTES)
        self.assertLessEqual(tail['bytes'], ASSEMBLY_TAIL_BYTES)
        self.assertLessEqual(head['bytes']+tail['bytes'], TEXT_BYTES)
        self.assertTrue(text.startswith(head['text']))
        self.assertTrue(text.endswith(tail['text']))
        self.assertTrue(tail['text'].endswith(stream.marker))
        self.assertTrue(head['truncated'])
        self.assertTrue(tail['truncated'])
        snapshot['assembly_suffix']['text'] = 'mutated'
        self.assertTrue(stream.diagnostic_snapshot()['assembly_suffix']['text'].endswith(stream.marker))
        self.assertEqual(snapshot['assembly_snapshot_scope'], 'last_evaluated_candidate_before_clear')

    def test_default_stream_has_no_text_storage_fields_and_capture_cannot_be_enabled_late(self):
        stream = self.stream(capture=False)
        self.feed(stream, '{', 'delta')
        snapshot = stream.diagnostic_snapshot()
        for field in ('expected_marker', 'assembly_prefix', 'assembly_suffix'):
            self.assertNotIn(field, snapshot)
        with self.assertRaises(ValueError):
            stream.enable_synthetic_text_evidence()

    def test_later_overflow_retains_prior_evaluated_candidate_but_cannot_claim_success(self):
        stream = self.stream()
        text = json.dumps(self.value(), ensure_ascii=False) + '\n' + stream.marker
        self.assertTrue(self.feed(stream, text))
        with self.assertRaises(LiveSummaryStreamError) as failure:
            self.feed(stream, 'x'*(stream.max_bytes+1), 'delta')
        self.assertEqual(failure.exception.code, 'summary_stream_overflow')
        snapshot = stream.diagnostic_snapshot()
        self.assertEqual(snapshot['candidate_state'], 'application_complete')
        self.assertEqual(snapshot['schema_state'], 'valid')
        self.assertEqual(snapshot['assembly_prefix']['text'], text)
        self.assertEqual(snapshot['stream_state'], 'failed')
        self.assertEqual(snapshot['stream_error'], 'summary_stream_overflow')
        self.assertFalse(stream.ready)
        with self.assertRaises(LiveSummaryStreamError):
            stream.result()

    def test_invalid_capture_flags_and_unqualified_modes_fail_before_rpc_construction(self):
        with patch('teams_transcription.b_live.CodexRPC') as rpc:
            for flag in (None, 0, 1, 'true', {}, []):
                with self.subTest(flag=repr(flag)), self.assertRaises(ValueError):
                    self.make_probe(capture_synthetic_output=flag)
            for options in ({'voice_pcm':None}, {'input_mode':'append_text'}):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    self.make_probe(**options)
            rpc.assert_not_called()

    async def test_default_probe_returns_no_captured_output(self):
        probe, _, _ = self.make_probe(capture_synthetic_output=False, voice_pcm=None,
                                      input_mode='append_text')
        result = await probe.run()
        self.assertEqual(result['state'], 'draft_ready')
        self.assertIsNone(result['assistant_output_evidence'])
        for field in ('expected_marker', 'assembly_prefix', 'assembly_suffix'):
            self.assertNotIn(field, probe.stream.diagnostic_snapshot())
        self.assert_cleaned_draft_scope(probe, result)

    async def test_probe_filters_users_foreign_threads_and_keeps_rpc_data_lanes_separate(self):
        probe, calls, notify = self.make_probe(mode='silent')
        async def push(piece):
            notify('thread/realtime/transcript/done', role='user', text='PRIVATE_OWN_USER')
            notify('thread/realtime/transcript/done', thread='foreign', role='assistant', text='PRIVATE_FOREIGN')
            notify('thread/realtime/unknown', role='assistant', text='PRIVATE_UNKNOWN_METHOD')
            await probe._data_event({'type':'input_transcript.added', 'item':{'text':'PRIVATE_DATA_USER'}})
            await probe._data_event({'type':'turn.done', 'turn':{'role':'user', 'transcript':'PRIVATE_DATA_TURN_USER'}})
            await probe._data_event({'type':'output_transcript.added', 'item':{'text':'synthetic data delta'}})
            await probe._data_event({'type':'turn.done', 'turn':{'role':'assistant', 'transcript':'synthetic data done'}})
            text = self.candidate(probe)
            notify('thread/realtime/transcript/delta', role='assistant', delta=text[:20])
            notify('thread/realtime/transcript/done', role='assistant', text=text)
            await probe._finished.wait()
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertEqual(result['state'], 'draft_ready')
        evidence = result['assistant_output_evidence']
        self.assertEqual([row['lane'] for row in evidence['events']],
                         ['data_delta', 'data_done', 'rpc_delta', 'rpc_done'])
        self.assertEqual([row['text'] for row in evidence['events'][:2]],
                         ['synthetic data delta', 'synthetic data done'])
        self.assertEqual(evidence['assembly']['assembly_prefix']['text'], self.candidate(probe))
        self.assertEqual(evidence['assembly']['assistant_events'], 2)
        self.assertEqual(evidence['assembly']['candidate_state'], 'application_complete')
        self.assertNotIn('PRIVATE_', json.dumps(result))
        self.assertFalse(evidence['channels_merged'])
        self.assertFalse(any(method in {'turn/start', 'thread/realtime/appendText'} for method, _ in calls))
        self.assert_cleaned_draft_scope(probe, result)

    async def test_rpc_invalid_json_is_captured_before_failure_and_clear(self):
        probe, _, notify = self.make_probe(mode='silent')
        text_holder = []
        async def push(piece):
            text = 'CONTROLLED_INVALID_JSON\n' + probe.stream.marker
            text_holder.append(text)
            notify('thread/realtime/transcript/done', role='assistant', text=text)
            await probe._finished.wait()
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_stream_failed')
        self.assertIsNone(result['summary'])
        evidence = result['assistant_output_evidence']
        self.assertEqual(evidence['events'][0]['text'], text_holder[0])
        assembly = evidence['assembly']
        self.assertEqual(assembly['assembly_prefix']['text'], text_holder[0])
        self.assertEqual(assembly['candidate_state'], 'invalid_json_with_unquoted_marker')
        self.assertEqual(assembly['stream_error'], 'summary_stream_invalid_json')
        self.assertFalse(assembly['json_decoded'])
        self.assertEqual(probe.stream._segments, [])
        self.assertEqual(probe.stream._current, '')
        self.assert_cleaned_draft_scope(probe, result)

    async def test_rpc_invalid_surrogate_is_observed_without_retaining_raw_text(self):
        probe, _, notify = self.make_probe(mode='silent')
        async def push(piece):
            notify('thread/realtime/transcript/done', role='assistant', text='\ud800PRIVATE_SURROGATE')
            await probe._finished.wait()
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_stream_failed')
        row = result['assistant_output_evidence']['events'][0]
        self.assertTrue(row['invalid_text'])
        self.assertEqual(row['text'], '')
        self.assertEqual(result['assistant_output_evidence']['assembly']['stream_error'],
                         'summary_stream_invalid_transcript')
        self.assertNotIn('PRIVATE_SURROGATE', json.dumps(result))
        self.assert_cleaned_draft_scope(probe, result)

    async def test_missing_marker_times_out_with_observable_candidate_without_success(self):
        probe, _, notify = self.make_probe(mode='silent', timeout=.04)
        text = json.dumps(self.value(), ensure_ascii=False)
        async def push(piece):
            notify('thread/realtime/transcript/done', role='assistant', text=text)
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_probe_timeout')
        self.assertEqual(result['diagnostics']['timeout_scope'], 'overall_budget')
        self.assertIsNone(result['summary'])
        evidence = result['assistant_output_evidence']
        self.assertEqual(evidence['events'][0]['text'], text)
        self.assertEqual(evidence['assembly']['candidate_state'], 'json_decoded_missing_marker')
        self.assertTrue(evidence['assembly']['json_decoded'])
        self.assertEqual(evidence['assembly']['assembly_prefix']['text'], text)
        self.assertFalse(evidence['assembly']['provider_response_done'])
        self.assert_cleaned_draft_scope(probe, result)

    async def test_data_only_candidate_is_observable_but_cannot_complete_rpc_assembly(self):
        probe, _, _ = self.make_probe(mode='silent', timeout=.04)
        async def push(piece):
            await probe._data_event({'type':'turn.done', 'turn':{'role':'assistant',
                'transcript':self.candidate(probe)}})
        probe.peer.push.side_effect = push
        result = await probe.run()
        self.assertEqual(result['failure_code'], 'live_summary_probe_timeout')
        self.assertIsNone(result['summary'])
        evidence = result['assistant_output_evidence']
        self.assertEqual(evidence['events'][0]['lane'], 'data_done')
        self.assertEqual(evidence['events'][0]['text'], self.candidate(probe))
        self.assertEqual(evidence['assembly']['candidate_state'], 'no_assistant_text')
        self.assertEqual(evidence['assembly']['assistant_events'], 0)
        self.assertEqual(evidence['assembly']['assembly_prefix']['text'], '')
        self.assertEqual(evidence['assembly']['assembly_suffix']['text'], '')
        self.assertFalse(evidence['channels_merged'])
        self.assert_cleaned_draft_scope(probe, result)

    async def test_strict_schema_or_marker_failures_preserve_the_evaluated_candidate(self):
        for kind in ('schema', 'nonce', 'trailing'):
            with self.subTest(kind=kind):
                probe, _, notify = self.make_probe(mode='silent')
                sent = []
                async def push(piece):
                    value = self.value()
                    if kind == 'schema':
                        value['重點'][0]['extra'] = 'controlled disallowed key'
                    text = self.candidate(probe, value)
                    if kind == 'nonce':
                        text = text.replace(probe.stream.marker, '[[SUMMARY_END:'+'b'*32+']]')
                    if kind == 'trailing':
                        text += '\ncontrolled trailing text'
                    sent.append(text)
                    notify('thread/realtime/transcript/done', role='assistant', text=text)
                    await probe._finished.wait()
                probe.peer.push.side_effect = push
                result = await probe.run()
                self.assertEqual(result['failure_code'], 'live_summary_stream_failed')
                self.assertIsNone(result['summary'])
                assembly = result['assistant_output_evidence']['assembly']
                self.assertEqual(assembly['assembly_prefix']['text'], sent[0])
                self.assertEqual(assembly['expected_marker'], probe.stream.marker)
                self.assertTrue(assembly['json_decoded'])
                self.assertEqual(assembly['candidate_state'],
                    {'schema':'invalid_summary', 'nonce':'nonce_mismatch', 'trailing':'unexpected_trailing_text'}[kind])
                self.assertEqual(assembly['schema_state'], 'invalid' if kind == 'schema' else 'not_checked')
                self.assert_cleaned_draft_scope(probe, result)

    async def test_transport_error_bodies_are_excluded_even_when_candidate_evidence_exists(self):
        for channel in ('rpc', 'data'):
            with self.subTest(channel=channel):
                probe, _, notify = self.make_probe(mode='silent')
                processed = asyncio.Event()
                original_handler = probe.handle_notification
                async def handle(method, params):
                    await original_handler(method, params)
                    if method == 'thread/realtime/transcript/delta':
                        processed.set()
                probe.handle_notification = handle
                async def push(piece):
                    notify('thread/realtime/transcript/delta', role='assistant', delta='{')
                    if channel == 'rpc':
                        notify('thread/realtime/error', error={'message':'PRIVATE_RPC_ERROR'}, text='PRIVATE_RPC_ERROR_TEXT')
                        await probe._finished.wait()
                    else:
                        await processed.wait()
                        await probe._data_event({'type':'error', 'message':'PRIVATE_DATA_ERROR',
                            'turn':{'role':'assistant', 'transcript':'PRIVATE_DATA_ERROR_TRANSCRIPT'}})
                probe.peer.push.side_effect = push
                result = await probe.run()
                self.assertEqual(result['failure_code'], 'live_summary_transport_failed')
                self.assertEqual([row['lane'] for row in result['assistant_output_evidence']['events']], ['rpc_delta'])
                self.assertEqual(result['assistant_output_evidence']['events'][0]['text'], '{')
                self.assertNotIn('PRIVATE_', json.dumps(result))
                self.assert_cleaned_draft_scope(probe, result)


if __name__ == '__main__':
    unittest.main()
