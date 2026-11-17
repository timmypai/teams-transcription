"""Authoritative structured source exchange, without meeting file projections."""
import tempfile
import unittest
from pathlib import Path

from teams_transcription.meeting_store import MeetingStore


class SQLiteOnlyStoreTests(unittest.TestCase):
    def test_received_sources_are_readable_without_markdown_or_jsonl_files(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('one')
            store.record_increment('one', {'capture_id':'a'*32,'receipt_sequence':1,
                'part_index':0,'text':'系統導入需要核對控制參數。',
                'received_at':'2026-10-15T10:00:00+00:00'})
            store.append_final('one', {'event_id':'provider-one','revision':0,
                'text':'下週進行控制參數核對。','timestamp':'2026-10-15T10:00:01+00:00'})
            self.assertEqual(list(Path(root).rglob('*.md')), [])
            self.assertEqual(list(Path(root).rglob('*.jsonl')), [])
            result = store.read('one')
            self.assertEqual([entry['text'] for entry in result['sources']],
                ['下週進行控制參數核對。','系統導入需要核對控制參數。'])
            self.assertTrue(result['sources'][0]['provider_final'])
            self.assertFalse(result['sources'][1]['provider_final'])
            self.assertEqual(result['transcript_state']['unfinalized_segment_count'], 1)
            self.assertNotIn('markdown', result)
