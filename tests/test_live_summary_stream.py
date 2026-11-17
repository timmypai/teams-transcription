"""Application sentinel assembly only; no service/model/audio validation."""
import json
import unittest

from teams_transcription.live_summary_stream import LiveSummaryStream, LiveSummaryStreamError
from teams_transcription.summary import SECTIONS


SOURCE = [{'id': 'S1', 'text': '原始來源，未定稿。', 'provider_final': False}]
NONCE = 'a' * 32


def value(text='來源支持的合成文字', sources=None):
    result = {section: [] for section in SECTIONS}
    result['重點'] = [{'text': text, 'sources': ['S1'] if sources is None else sources,
                     'owner': '', 'date': ''}]
    return result


class LiveSummaryStreamTests(unittest.TestCase):
    def make(self, **kwargs):
        return LiveSummaryStream('meeting', 'own', SOURCE, nonce=NONCE, **kwargs)

    def delta(self, stream, text, **params):
        return stream.feed('thread/realtime/transcript/delta',
                           {'threadId': 'own', 'role': 'assistant', 'delta': text, **params}, meeting_id='meeting')

    def done(self, stream, text):
        return stream.feed('thread/realtime/transcript/done',
                           {'threadId': 'own', 'role': 'assistant', 'text': text}, meeting_id='meeting')

    def assert_failure(self, stream, code, action):
        with self.assertRaises(LiveSummaryStreamError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        self.assertFalse(stream.ready)
        with self.assertRaises(LiveSummaryStreamError) as caught:
            stream.result()
        self.assertEqual(caught.exception.code, code)

    def test_fragmented_json_and_marker_complete_by_application_criterion_only(self):
        stream = self.make()
        payload = json.dumps(value(), ensure_ascii=False)
        for character in payload:
            self.assertFalse(self.delta(stream, character))
        with self.assertRaisesRegex(LiveSummaryStreamError, 'summary_stream_incomplete'):
            stream.result()
        for character in stream.marker[:-1]:
            self.assertFalse(self.delta(stream, character))
        self.assertTrue(self.delta(stream, stream.marker[-1]))
        result = stream.result()
        self.assertEqual(result['summary'], value())
        provenance = result['provenance']
        self.assertEqual(provenance['provider'], 'gpt_live')
        self.assertEqual(provenance['criterion'], 'application_sentinel')
        self.assertFalse(provenance['provider_response_done'])
        self.assertFalse(provenance['provider_final'])
        self.assertFalse(provenance['service_contract_verified'])
        self.assertTrue(self.delta(stream, ' \n'))

    def test_snapshot_replaces_current_delta_and_duplicate_done_replays_once(self):
        stream = self.make()
        self.delta(stream, 'unfinished incorrect draft')
        payload = json.dumps(value(), ensure_ascii=False) + stream.marker
        self.assertTrue(self.done(stream, payload))
        self.assertTrue(self.done(stream, payload))
        self.assertEqual(stream.status()['snapshot_replays'], 1)
        self.assertEqual(stream.result()['summary'], value())

    def test_distinct_done_segments_are_retained_and_later_current_correction_is_validated(self):
        stream = self.make()
        payload = json.dumps(value(), ensure_ascii=False)
        split = payload.index(',') + 1
        self.done(stream, payload[:split])
        self.delta(stream, 'incorrect current segment')
        self.assertTrue(self.done(stream, payload[split:] + stream.marker))
        self.assertEqual(stream.result()['summary'], value())

    def test_foreign_thread_meeting_and_user_text_cannot_enter_candidate(self):
        stream = self.make()
        self.delta(stream, 'FOREIGN', threadId='foreign')
        stream.feed('thread/realtime/transcript/delta',
                    {'threadId': 'own', 'role': 'assistant', 'delta': 'FOREIGN'}, meeting_id='other')
        self.delta(stream, 'USER', role='user')
        self.assertEqual(stream.status()['assistant_events'], 0)
        self.delta(stream, json.dumps(value()) + stream.marker)
        self.assertEqual(stream.result()['summary'], value())

    def test_binding_nonce_timeout_and_size_limits_are_explicit_failures(self):
        stream = self.make()
        self.assert_failure(stream, 'summary_stream_invalid_binding', lambda:
                            stream.feed('thread/realtime/transcript/delta', {'role': 'assistant', 'delta': 'x'}))
        stream = self.make()
        self.assert_failure(stream, 'summary_stream_nonce_mismatch', lambda:
                            self.delta(stream, json.dumps(value()) + '[[SUMMARY_END:' + 'b' * 32 + ']]'))
        now = [0.]
        stream = self.make(clock=lambda: now[0], timeout=1)
        now[0] = 1.
        self.assert_failure(stream, 'summary_stream_timeout', stream.result)
        stream = self.make(max_bytes=512)
        self.assert_failure(stream, 'summary_stream_overflow', lambda: self.delta(stream, '字' * 200))
        stream = self.make(max_chars=256)
        self.assert_failure(stream, 'summary_stream_overflow', lambda: self.delta(stream, 'x' * 257))

    def test_closed_error_handoff_and_ordinary_turn_rejected_even_after_candidate(self):
        cases = [('thread/realtime/closed', {}, 'summary_stream_provider_closed'),
                 ('thread/realtime/error', {}, 'summary_stream_provider_error'),
                 ('thread/realtime/itemAdded', {'item': {'type': 'handoff_request'}}, 'summary_stream_handoff_rejected'),
                 ('turn/completed', {'turn': {'status': 'completed'}}, 'summary_stream_regular_turn_rejected'),
                 ('item/completed', {'item': {'type': 'agentMessage'}}, 'summary_stream_regular_turn_rejected')]
        for method, extra, code in cases:
            stream = self.make()
            self.delta(stream, json.dumps(value()) + stream.marker)
            self.assert_failure(stream, code, lambda:
                                stream.feed(method, {'threadId': 'own', **extra}))

    def test_no_provider_done_event_alias_and_done_without_marker_is_not_complete(self):
        stream = self.make()
        self.done(stream, json.dumps(value()))
        self.assertFalse(stream.ready)
        stream.feed('response.done', {'threadId': 'own', 'response': {'status': 'completed'}})
        self.assertFalse(stream.ready)

    def test_strict_json_schema_citations_and_trailing_text(self):
        valid = json.dumps(value())
        bad = ['```json\n' + valid + '\n```', '\ufeff' + valid,
               'prefix ' + valid, '{"wrapper":' + valid + '}',
               '{"重點":[],"重點":[],"決策":[],"待辦":[],"未決問題":[]}',
               json.dumps(value(sources=['FOREIGN']))]
        extra = value()
        extra['重點'][0]['provider_final'] = True
        bad.append(json.dumps(extra))
        for payload in bad:
            stream = self.make()
            with self.assertRaises(LiveSummaryStreamError):
                self.delta(stream, payload + stream.marker)
            self.assertFalse(stream.ready)
        stream = self.make()
        self.delta(stream, valid + stream.marker)
        self.assert_failure(stream, 'summary_stream_unexpected_trailing_text', lambda: self.delta(stream, 'more'))

    def test_marker_inside_quoted_json_is_data_until_real_marker_and_result_is_a_copy(self):
        stream = self.make()
        payload = json.dumps(value(text=stream.marker + ' source data'))
        split = payload.index(' source data')
        self.delta(stream, payload[:split])
        self.assertFalse(stream.ready)
        self.delta(stream, payload[split:] + stream.marker)
        result = stream.result()
        result['summary']['重點'][0]['text'] = 'mutated caller data'
        self.assertNotEqual(stream.result()['summary']['重點'][0]['text'], 'mutated caller data')

    def test_correcting_done_cannot_keep_cached_candidate_on_invalid_new_source(self):
        stream = self.make()
        self.delta(stream, json.dumps(value()) + stream.marker)
        self.assertTrue(stream.ready)
        invalid = json.dumps(value(sources=['FOREIGN'])) + stream.marker
        self.assert_failure(stream, 'summary_stream_invalid_summary', lambda: self.done(stream, invalid))

    def test_sources_are_immutable_bounded_snapshots_and_duplicate_ids_rejected(self):
        mutable = [{'id': 'S1', 'text': 'source text'}]
        stream = LiveSummaryStream('meeting', 'own', mutable, nonce=NONCE)
        mutable[0]['id'] = 'FOREIGN'
        with self.assertRaises(TypeError):
            stream.entries[0]['id'] = 'FOREIGN'
        self.delta(stream, json.dumps(value()) + stream.marker)
        self.assertEqual(stream.result()['summary'], value())
        with self.assertRaisesRegex(ValueError, 'invalid_live_summary_sources'):
            LiveSummaryStream('meeting', 'own', SOURCE + SOURCE, nonce=NONCE)
        with self.assertRaisesRegex(ValueError, 'live_summary_sources_overflow'):
            LiveSummaryStream('meeting', 'own', [{'id': 'S1', 'text': '字' * 30000}], nonce=NONCE)

    def test_accumulated_size_is_checked_before_retaining_an_overflowing_fragment(self):
        stream = self.make(max_bytes=512)
        self.delta(stream, ' ' * 400)
        self.assert_failure(stream, 'summary_stream_overflow', lambda: self.delta(stream, ' ' * 200))
        self.assertEqual(stream._current, '')
        self.assertEqual(stream._segments, [])
