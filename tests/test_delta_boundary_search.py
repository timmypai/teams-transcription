"""Cross-part matches use only known adjacent durable source ranges."""
import json
import tempfile
import unittest

from teams_transcription.b_response import MeetingMinutesTool, MeetingStoreMinutesReader
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.response_runtime import _AsyncStore
from teams_transcription.storage import Session
from test_rx_contract import config


CAPTURE = 'b'*32
RECEIVED = '2026-10-10T18:00:00+00:00'


class DeltaBoundarySearchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Session(self.tmp.name).create(config())
        self.reader = MeetingStoreMinutesReader(_AsyncStore(self.tmp.name))

    def save(self, text, *, part=0, receipt=1, capture=CAPTURE, **identity):
        return self.session.record_increment(dict(capture_id=capture, receipt_sequence=receipt,
            part_index=part, text=text, received_at=RECEIVED, **identity))

    def save_parts(self, parts, **identity):
        return self.session.record_increment(dict(capture_id=CAPTURE, receipt_sequence=1,
            part_index=0, text=parts[0], parts=parts, received_at=RECEIVED, **identity))

    async def search(self, query):
        return await MeetingMinutesTool(self.session.ident, self.reader).dispatch(
            'read_meeting_context',{'query':query})

    def assert_original_context(self, result):
        document = [source['text'] for source in MeetingStore(self.tmp.name).read(self.session.ident)['sources']]
        self.assertEqual([item['text'] for item in result['context']],document)
        self.assertEqual([item['record_index'] for item in result['context']],list(range(1,len(document)+1)))

    def assert_original_match(self, match, expected_needle):
        snapshot = MeetingStore(self.tmp.name).read(self.session.ident)
        originals = {segment['event_id']:segment for segment in snapshot['segments']}
        lines = [source['text'] for source in snapshot['sources']]
        self.assertEqual(match['needle'].casefold(), expected_needle.casefold())
        self.assertEqual(match['capture_id'], CAPTURE)
        self.assertEqual(match['receipt_sequence'], 1)
        self.assertEqual(match['source_state'], 'application_segment')
        self.assertIs(match['provider_final'], False)
        self.assertEqual(match['ordering'], 'receipt_order')
        pieces, indices = [], []
        for source in match['sources']:
            original = originals[source['source_id']]
            self.assertEqual(source['part_index'], original['part_index'])
            self.assertEqual(source['source_chars'], len(original['text']))
            self.assertGreaterEqual(source['char_start'], 0)
            self.assertLessEqual(source['char_end'], len(original['text']))
            self.assertGreater(source['char_end'], source['char_start'])
            piece = original['text'][source['char_start']:source['char_end']]
            pieces.append(piece)
            indices.append(source['part_index'])
            rendered = []
            for locator in source['record_ranges']:
                line = lines[locator['record_index']-1]
                self.assertGreaterEqual(locator['char_start'], 0)
                self.assertLessEqual(locator['char_end'], len(line))
                rendered.append(line[locator['char_start']:locator['char_end']])
            self.assertEqual(''.join(rendered),piece)
        self.assertEqual(indices, list(range(indices[0], indices[-1]+1)))
        self.assertEqual(''.join(pieces).casefold(), expected_needle.casefold())

    async def test_natural_query_finds_original_identifier_split_across_adjacent_parts(self):
        self.save_parts(['會議識別碼 ABCD', 'EFGH 已保存。'])
        result = await self.search('請找 ABCDEFGH 的原文')
        self.assertGreater(result['boundary_hit_count'], 0)
        self.assertEqual(len(result['boundary_matches']), 1)
        match = result['boundary_matches'][0]
        self.assert_original_match(match, 'ABCDEFGH')
        self.assertEqual(len(match['sources']), 2)
        self.assertTrue(match['context_complete'])
        self.assertEqual(result['transcript_state']['provider_final_count'], 0)
        self.assertEqual(result['transcript_state']['quality'], 'includes_unfinalized')
        self.assert_original_context(result)

    async def test_query_beyond_two_thousand_characters_retains_all_adjacent_parts(self):
        needle = 'abcd'*599 + 'wxyz'
        parts = [needle[offset:offset+50] for offset in range(0, len(needle), 50)]
        self.save_parts(parts)
        result = await self.search(needle)
        self.assertEqual(result['boundary_hit_count'],1)
        match, = result['boundary_matches']
        self.assert_original_match(match,needle)
        self.assertEqual(len(match['sources']),48)
        self.assertTrue(match['context_complete'])
        self.assertFalse(result['truncated'])
        self.assertEqual(result['coverage'],'complete')
        self.assert_original_context(result)

    async def test_unicode_and_newline_coordinates_refer_to_original_source_and_markdown(self):
        parts = ['前文🙂\nΩABCD', 'EFGH😀\n後文']
        self.save_parts(parts)
        result = await self.search('請核對「ABCDEFGH」')
        match = result['boundary_matches'][0]
        self.assert_original_match(match, 'ABCDEFGH')
        self.assertEqual(match['sources'][0]['char_start'], parts[0].index('ABCD'))
        self.assertEqual(match['sources'][0]['char_end'], len(parts[0]))
        self.assertEqual(match['sources'][1]['char_start'], 0)
        self.assertEqual(match['sources'][1]['char_end'], 4)
        self.assert_original_context(result)

    async def test_literal_newline_inside_cross_part_match_preserves_original_ranges(self):
        self.save_parts(['prefix ABC\nD', 'EFGH tail'])
        result = await self.search('ABC\nDEFGH')
        self.assert_original_match(result['boundary_matches'][0], 'ABC\nDEFGH')
        self.assert_original_context(result)

    async def test_casefold_expansion_and_crlf_preserve_original_coordinates(self):
        for parts, query in ((['prefix ßABCD', 'EFGH tail'], 'ssABCDEFGH'),
                             (['prefix ABC\r\nD', 'EFGH tail'], 'DEFGH')):
            with self.subTest(query=query):
                session = Session(self.tmp.name).create(config())
                session.record_increment(dict(capture_id=CAPTURE, receipt_sequence=1, part_index=0,
                    text=parts[0], parts=parts, received_at=RECEIVED))
                result = await MeetingMinutesTool(session.ident, self.reader).dispatch(
                    'read_meeting_context',{'query':query})
                match = result['boundary_matches'][0]
                source_refs = match['sources']
                self.assertEqual(''.join(parts[ref['part_index']][ref['char_start']:ref['char_end']]
                    for ref in source_refs).casefold(), query.casefold())
                raw_lines = [source['text'] for source in MeetingStore(self.tmp.name).read(session.ident)['sources']]
                for row in result['context']:
                    line = raw_lines[row['record_index']-1]
                    self.assertEqual(row['text'], line[row.get('char_start', 0):row.get('char_end', len(line))])

    async def test_different_capture_receipt_and_gap_never_create_boundary_match(self):
        cases = (dict(capture='c'*32, receipt=1, part=1),
                 dict(capture=CAPTURE, receipt=2, part=1),
                 dict(capture=CAPTURE, receipt=1, part=2))
        for second in cases:
            with self.subTest(second=second):
                session = Session(self.tmp.name).create(config())
                session.record_increment(dict(capture_id=CAPTURE, receipt_sequence=1,
                    part_index=0, text='ABCD', received_at=RECEIVED))
                session.record_increment(dict(capture_id=second['capture'], receipt_sequence=second['receipt'],
                    part_index=second['part'], text='EFGH', received_at=RECEIVED))
                result = await MeetingMinutesTool(session.ident, self.reader).dispatch(
                    'read_meeting_context',{'query':'ABCDEFGH'})
                self.assertEqual(result['boundary_hit_count'], 0)
                self.assertEqual(result['boundary_matches'], [])

    async def test_provider_final_legacy_and_superseded_revision_are_not_spliced(self):
        # A provider final is an independent source; its text cannot complete
        # an application's delta prefix or legacy provisional suffix.
        self.save('ABCD')
        self.session.append_final(dict(event_id='known-final', revision=0, text='EFGH', timestamp=RECEIVED))
        self.session.save_provisional(dict(provisional_id='provisional:'+'d'*32,
            revision=12, text='EFGH', truncated=False))
        result = await self.search('ABCDEFGH')
        self.assertEqual(result['boundary_hit_count'], 0)
        self.assertEqual(result['boundary_matches'], [])
        revised = Session(self.tmp.name).create(config())
        revised.record_increment(dict(capture_id=CAPTURE, receipt_sequence=1, part_index=0,
            text='ABCD', received_at=RECEIVED, source_event_id='known-source', source_revision=1))
        revised.record_increment(dict(capture_id=CAPTURE, receipt_sequence=1, part_index=1,
            text='EFGH', received_at=RECEIVED, source_event_id='known-source', source_revision=2))
        result = await MeetingMinutesTool(revised.ident, self.reader).dispatch(
            'read_meeting_context',{'query':'ABCDEFGH'})
        self.assertEqual(result['boundary_hit_count'], 0)
        self.assertEqual(result['boundary_matches'], [])
        history = MeetingStore(self.tmp.name).read(revised.ident)
        self.assertEqual([part['text'] for part in history['increments']], ['ABCD', 'EFGH'])
        self.assertEqual([part['text'] for part in history['segments']], ['EFGH'])

    async def test_early_source_remains_in_complete_question_snapshot(self):
        from teams_transcription.b_context import MeetingQuestionContext
        self.save('EARLY_FACT_123 預算十萬元。')
        for receipt in range(2, 40):
            self.save('好。', receipt=receipt)
        context = MeetingQuestionContext(MeetingMinutesTool(self.session.ident, self.reader))
        context.bind_session('early-fact-thread')
        plan = await context.prepare('請找 EARLY_FACT_123 的原文', session_id='early-fact-thread')
        readings = json.loads(plan.payload)['selected_source_reading']
        self.assertEqual([row['text'] for row in readings],
            ['EARLY_FACT_123 預算十萬元。' + '好。'*38])
        self.assertEqual([row['record_index'] for row in plan.result['selected_context_refs']],
            list(range(1, 40)))
        projection = readings[0]
        self.assertEqual(projection['record_index_range_inclusive'], [1, 39])
        self.assertIs(projection['provider_final'], False)
        self.assertIs(projection['speech_continuity_verified'], False)

    async def test_foreign_meeting_sources_never_enter_bound_tool(self):
        foreign = Session(self.tmp.name).create(config())
        foreign.record_increment(dict(capture_id=CAPTURE, receipt_sequence=1, part_index=0,
            text='ABCD', parts=['ABCD','EFGH'], received_at=RECEIVED))
        result = await self.search('ABCDEFGH')
        self.assertEqual(result['boundary_hit_count'], 0)
        self.assertEqual(result['boundary_matches'], [])
        self.assertNotIn('ABCD', ''.join(row['text'] for row in result['context']))
        async def unscoped_reader(meeting_id):
            return await self.reader(foreign.ident)
        with self.assertRaisesRegex(ValueError, 'different meeting'):
            await MeetingMinutesTool(self.session.ident, unscoped_reader).dispatch(
                'read_meeting_context',{'query':'ABCDEFGH'})
