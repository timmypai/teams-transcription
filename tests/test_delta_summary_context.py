"""Explicit mock/local contexts preserve IDs; no formal GPT Live summary proof."""
import json
import tempfile
import unittest
from unittest.mock import patch
from teams_transcription import summary
from teams_transcription.incremental import text_chunks
from teams_transcription.storage import Session
from teams_transcription.meeting_store import MeetingStore

class SummaryContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_long_line_and_new_stream_preserve_source_ids_through_context_slices(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(dict(provider='mock',source='meeting'))
            legacy = 'context '*3750+'KEY_BUDGET_123'
            session.save_provisional(dict(provisional_id='provisional:'+'b'*32,revision=1,text=legacy,truncated=False))
            stream = '中文😀 '*6000+'STREAM_TAIL_456'
            parts = list(text_chunks(stream))
            session.record_increment(dict(capture_id='a'*32,receipt_sequence=1,part_index=0,
                text=parts[0],parts=parts,received_at='2026-10-10T12:00:00Z'))
            session.finalize_minutes('completed')
            originals = session.entries()
            source_ids = {item['id'] for item in originals}
            legacy_source = next(e for e in originals if e['source_state']=='legacy_provisional')
            # These are local context-preparation groups, not a model route.
            groups = list(summary.summary_groups(originals))
            observed = [entry for group in groups for entry in group]
            group_sizes = [len(json.dumps(group,ensure_ascii=False).encode()) for group in groups]
            with patch('teams_transcription.rpc.CodexRPC') as rpc:
                value=await summary.generate(session,session.path/'unused')
            self.assertTrue(group_sizes)
            self.assertTrue(all(n<=40000 for n in group_sizes))
            self.assertTrue(all(len(json.dumps(e,ensure_ascii=False).encode())<=20000 for e in observed))
            pieces=[e for e in observed if e['id']==legacy_source['id']]
            self.assertGreater(len(pieces),1)
            self.assertEqual(''.join(e['text'] for e in pieces),legacy)
            self.assertEqual([e['summary_slice']['start_char'] for e in pieces],[sum(len(p['text']) for p in pieces[:n]) for n in range(len(pieces))])
            self.assertTrue(all(e['source_state']=='legacy_provisional' and e['provider_final'] is False for e in pieces))
            self.assertTrue(all(e['source_timestamp'] is None and e['source_event_id'] is None for e in pieces))
            self.assertEqual({e['id'] for e in observed},source_ids)
            self.assertEqual(''.join(e['text'] for e in observed if e['source_state']=='application_segment'),stream)
            self.assertEqual(value['source_quality']['saved_source_count'],len(originals))
            self.assertEqual(value['source_quality']['provider_final_count'],0)
            self.assertEqual(value['source_quality']['quality_counts']['legacy_truncated'],0)
            self.assertEqual(value['source_quality']['source_clock_known_count'],0)
            self.assertNotIn('references',value['source_quality'])
            for item in value['重點']:
                self.assertTrue(set(item['sources'])<=source_ids)
            result=MeetingStore(root).read(session.ident)
            self.assertTrue(result['terminal'])
            self.assertEqual(result['provisionals'][0]['text'],legacy)
            self.assertEqual(''.join(e['text'] for e in result['segments']),stream)
            self.assertEqual(session.entries(),originals)
            rpc.assert_not_called()

    async def test_many_derived_items_preserve_complete_sqlite_citations(self):
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(dict(provider='mock',source='meeting'))
            session.record_increment(dict(capture_id='c'*32,receipt_sequence=1,part_index=0,text='嗯',parts=['嗯']*6000,received_at='2026-10-10T12:00:00Z'))
            session.finalize_minutes('completed')
            value=await summary.generate(session,session.path/'unused')
            self.assertEqual(len(value['重點']),6000)
            self.assertEqual(len(MeetingStore(root).read(session.ident)['summary']['重點']),6000)
            self.assertLess(len(json.dumps(value['source_quality']).encode()),4096)
            result=session.finalize_minutes('completed')
            self.assertTrue(result['terminal'])
            self.assertEqual(result['transcript_state']['application_segment_count'],6000)
