"""Long source lines remain complete and query metadata preserves provenance."""
import tempfile
import unittest
from unittest.mock import AsyncMock

from teams_transcription.storage import Session
from teams_transcription.response_runtime import _AsyncStore
from teams_transcription.b_response import MeetingMinutesTool, MeetingStoreMinutesReader


class DeltaLongContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = Session(self.tmp.name).create({"provider": "mock", "source": "wav"})
        self.reader = MeetingStoreMinutesReader(_AsyncStore(self.tmp.name))

    async def lookup(self, query):
        tool = MeetingMinutesTool(self.session.ident, self.reader)
        return await tool.dispatch(tool.name, {"query": query})

    async def test_actual_legacy_long_line_head_and_tail_survive_with_source_provenance(self):
        text = "UNRELATED_HEAD " + "context " * 3750 + "KEY_BUDGET_123"
        ident = "provisional:" + "b" * 32
        self.session.save_provisional({"provisional_id": ident, "revision": 1,
                                       "text": text, "truncated": False})
        result = await self.lookup("KEY_BUDGET_123")
        slices = [row for row in result["context"] if "KEY_BUDGET_123" in row["text"]]
        self.assertTrue(slices)
        self.assertEqual([row['text'] for row in result['context']],[text])
        self.assertFalse(result["truncated"])
        self.assertEqual(result["coverage"], "complete")
        self.assertGreater(result["query_hit_count"], 0)
        self.assertEqual(result["transcript_state"]["provider_final_count"], 0)
        self.assertEqual(result["completeness"], "partial")
        records=(await self.reader(self.session.ident))['records']
        for row in slices:
            original=records[row['record_index']-1]
            self.assertEqual(row['text'],original['text'])
            locator,=[value for value in result['source_locators'] if value['record_index']==row['record_index']]
            self.assertEqual(locator['source_id'],original['source_id'])
            self.assertFalse(locator['provider_final'])
            self.assertEqual(locator['revision'],1)
        # Query specificity changes metadata, not which saved facts survive.
        contextual = await self.lookup("context KEY_BUDGET_123")
        self.assertTrue(any("KEY_BUDGET_123" in row["text"] for row in contextual["context"]))

    async def test_actual_increment_hits_at_part_end_and_next_part_begin_keep_distinct_ids(self):
        parts = ["LEFT_UNRELATED " + "l" * 7900 + "END_TARGET_123",
                 "BEGIN_TARGET_456 " + "r" * 7900 + " RIGHT_UNRELATED"]
        self.session.record_increment({"capture_id": "c" * 32, "receipt_sequence": 1,
                                       "part_index": 0, "text": parts[0], "parts": parts,
                                       "received_at": "2026-10-10T15:00:00Z"})
        seen = []
        for query in ("END_TARGET_123", "BEGIN_TARGET_456"):
            result = await self.lookup(query)
            rows = [row for row in result["context"] if query in row["text"]]
            self.assertTrue(rows)
            row = rows[0]
            locator, = [value for value in result["source_locators"] if value["record_index"] == row["record_index"]]
            seen.append(locator["source_id"])
            self.assertEqual(locator['source_state'],'application_segment')
            self.assertFalse(locator['provider_final'])
            self.assertEqual([value['text'] for value in result['context']],parts)
            self.assertFalse(result["truncated"])
            self.assertEqual(result["coverage"], "complete")
        self.assertNotEqual(*seen)

    async def test_natural_language_identifier_and_quoted_needle_prefer_specific_tail(self):
        text = ("KEY BUDGET 123 context " * 1400) + "KEY_BUDGET_123"
        ident = "provisional:" + "b" * 32
        self.session.save_provisional({"provisional_id": ident, "revision": 1,
                                       "text": text, "truncated": False})
        for query in ("請確認 KEY_BUDGET_123", '請確認 "KEY_BUDGET_123"',
                      "請確認「KEY_BUDGET_123」"):
            result = await self.lookup(query)
            row, = [row for row in result["context"] if "KEY_BUDGET_123" in row["text"]]
            self.assertEqual(row["text"],text)
            self.assertGreater(result["literal_hit_count"], 0)
            self.assertEqual(result["coverage"], "complete")
            locator, = [item for item in result["source_locators"] if item["record_index"] == row["record_index"]]
            original=(await self.reader(self.session.ident))['records'][row['record_index']-1]
            self.assertEqual(locator['source_id'],original['source_id'])
        # A quoted phrase is a specific needle even without an ASCII identifier.
        phrase = ("budget approved context " * 1400) + "budget approved today"
        self.session.save_provisional({"provisional_id": ident, "revision": 2,
                                       "text": phrase, "truncated": False})
        result = await self.lookup('請確認 "budget approved today"')
        self.assertTrue(any("budget approved today" in row["text"] for row in result["context"]))
        self.assertGreater(result["literal_hit_count"], 0)

    async def test_literal_cross_chunk_term_matches_known_receipt_parts(self):
        parts = ["x" * 7900 + "ABCD", "EFGH" + "y" * 7900]
        self.session.record_increment({"capture_id": "c" * 32, "receipt_sequence": 1,
                                       "part_index": 0, "text": parts[0], "parts": parts,
                                       "received_at": "2026-10-10T15:00:00Z"})
        result = await self.lookup("ABCDEFGH")
        self.assertGreater(result["query_hit_count"], 0)
        self.assertEqual(result['boundary_hit_count'], 1)
        match, = result['boundary_matches']
        self.assertEqual(match['needle'], 'abcdefgh')
        self.assertEqual(match['ordering'], 'receipt_order')
        self.assertFalse(match['provider_final'])
        self.assertEqual([e['part_index'] for e in match['sources']], [0, 1])
        originals = self.session.entries()
        self.assertEqual(''.join(e['text'][ref['char_start']:ref['char_end']]
            for ref in match['sources'] for e in originals if e['event_id'] == ref['source_id']), 'ABCDEFGH')
        self.assertEqual(result["coverage"], "complete")
        self.assertFalse(result["truncated"])
        self.assertEqual([row['text'] for row in result['context']],parts)
        self.assertTrue(match['context_complete'])

    async def test_missing_match_remains_partial_and_foreign_reader_is_rejected(self):
        self.session.save_provisional({"provisional_id": "provisional:" + "b" * 32,
                                       "revision": 1, "text": "context " * 3750, "truncated": False})
        result = await self.lookup("missingneedle")
        self.assertEqual(result["query_hit_count"], 0)
        self.assertEqual(result["coverage"], "partial")
        reader=AsyncMock(return_value={'meeting_id':'foreign','records':[]})
        tool = MeetingMinutesTool(self.session.ident, reader)
        with self.assertRaises(ValueError):
            await tool.dispatch(tool.name, {"query": "PRIVATE_TAIL"})

    async def test_unicode_casefold_expansion_keeps_full_original_source(self):
        text = "ß" * 17000 + "SPECIAL_TARGET_987"
        self.session.save_provisional({"provisional_id": "provisional:" + "b" * 32,
                                       "revision": 1, "text": text, "truncated": False})
        result = await self.lookup("SPECIAL_TARGET_987")
        row, = [value for value in result["context"] if "SPECIAL_TARGET_987" in value["text"]]
        original=(await self.reader(self.session.ident))['records'][row['record_index']-1]['text']
        self.assertEqual(row['text'],original)
