"""Source receipt order must not masquerade as a provider sequence."""
import tempfile
import unittest

from teams_transcription.meeting_store import MeetingStore


class SourceOrderingTests(unittest.TestCase):
    def test_locally_allocated_final_sequence_is_not_a_provider_sequence(self):
        with tempfile.TemporaryDirectory() as root:
            store=MeetingStore(root)
            store.create('idless')
            store.append_final('idless',dict(event_id='local',revision=0,text='無來源序號的明確 done',
                timestamp='2026-10-10T15:00:00Z'))
            first=store.read('idless')
            self.assertIsNotNone(first['events'][0]['sequence'])
            self.assertIsNone(first['events'][0]['source_sequence'])
            self.assertEqual(first['events'][0]['ordering'],'receipt_order')
            restarted=MeetingStore(root).read('idless')
            self.assertEqual(restarted['transcript_state']['ordering'],'receipt_order')
            self.assertIsNone(restarted['events'][0]['source_sequence'])
            store.append_final('idless',dict(event_id='explicit',revision=0,text='有 provider sequence',
                timestamp='2026-10-10T15:00:00Z',sequence=2))
            self.assertEqual(store.read('idless')['transcript_state']['ordering'],'grouped_source_streams')
