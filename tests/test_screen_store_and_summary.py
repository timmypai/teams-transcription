"""SQLite screen_observations storage, lease fencing, and summary provenance seam."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from teams_transcription.meeting_store import MeetingStore, MeetingStoreError, screen_source_id
from teams_transcription.storage import Session
from teams_transcription import summary


def _sample_observation(seq=1, *, title='2026 Q4 預算審查', summary_text='雲端基礎設施 120,000 美元已核准。',
                        screen_kind='slide', frame_seed=b'frame-1'):
    frame_sha = hashlib.sha256(frame_seed).hexdigest()
    return {
        'observation_id': f'screen:{seq:04d}:{frame_sha[:8]}',
        'capture_seq': seq,
        'frame_sha256': frame_sha,
        'dhash': f'{seq:016x}',
        'capture_method': 'webrtc_video_frame',
        'width': 1280,
        'height': 720,
        'captured_at': '2026-11-24T13:50:00+00:00',
        'analyzed_at': '2026-11-24T13:50:06+00:00',
        'vision_model': 'gpt-5.6-sol',
        'screen_kind': screen_kind,
        'title': title,
        'summary_text': summary_text,
        'structured': {
            'screen_kind': screen_kind,
            'title': title,
            'summary_text': summary_text,
            'key_items': ['雲端基礎設施：120,000 USD（已核准）'],
            'metrics_or_numbers': ['120,000 USD'],
            'visible_text_excerpt': '2026 Q4 Budget Review | Cloud Infra | 120,000 | Approved',
            'uncertain_areas': [],
        },
    }


class ScreenStoreAndSummaryTests(unittest.TestCase):
    def test_record_screen_observation_preserves_speech_provenance_and_increments_version(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            created = store.create('m-screen-1')
            v0 = created['version']
            obs1 = _sample_observation(1, frame_seed=b'page-1')
            obs2 = _sample_observation(2, title='風險清單', summary_text='API 限流待評估。',
                                       screen_kind='diagram', frame_seed=b'page-2')

            r1 = store.record_screen_observation('m-screen-1', obs1)
            self.assertEqual(r1['version'], v0 + 1)
            r2 = store.record_screen_observation('m-screen-1', obs2)
            self.assertEqual(r2['version'], v0 + 2)

            rows = store.list_screen_observations('m-screen-1')
            self.assertEqual([r['capture_seq'] for r in rows], [1, 2])
            self.assertEqual(rows[0]['id'], screen_source_id(obs1['observation_id']))
            self.assertTrue(rows[0]['id'].startswith('V'))
            self.assertEqual(rows[0]['title'], '2026 Q4 預算審查')
            self.assertEqual(rows[0]['structured']['metrics_or_numbers'], ['120,000 USD'])

            # Speech transcript ledger remains untouched
            snap = store.read('m-screen-1')
            self.assertEqual(snap['events'], [])
            self.assertEqual(snap['segments'], [])
            self.assertFalse(snap['transcript_state']['saved'])
            self.assertEqual(len(snap['screen_observations']), 2)

    def test_idempotent_duplicate_and_conflict_rejection(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('m-screen-dup')
            obs = _sample_observation(1)
            first = store.record_screen_observation('m-screen-dup', obs)
            second = store.record_screen_observation('m-screen-dup', obs)
            self.assertEqual(second['version'], first['version'])

            altered = {**obs, 'summary_text': '竄改後的摘要文字'}
            with self.assertRaises(MeetingStoreError) as raised:
                store.record_screen_observation('m-screen-dup', altered)
            self.assertEqual(raised.exception.code, 'screen_observation_conflict')

    def test_rejects_terminal_meeting_invalid_fields_and_stale_lease(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('m-screen-fence')
            with self.assertRaises(MeetingStoreError):
                store.record_screen_observation('m-screen-fence', {**_sample_observation(1), 'screen_kind': 'invalid_kind'})
            with self.assertRaises(MeetingStoreError):
                store.record_screen_observation('m-screen-fence', {**_sample_observation(1), 'frame_sha256': 'not-hex'})

            # Stale lease fails closed inside _locked transaction
            fenced = MeetingStore(root, lease={'id': 'missing', 'owner': 'o', 'generation': 1,
                'token': 't', 'session_id': 'm-screen-fence', 'boot_id': 'b'})
            with self.assertRaises(MeetingStoreError) as raised:
                fenced.record_screen_observation('m-screen-fence', _sample_observation(1))
            self.assertEqual(raised.exception.code, 'stale_execution_lease')

            store.finalize('m-screen-fence', 'completed')
            with self.assertRaises(MeetingStoreError) as raised:
                store.record_screen_observation('m-screen-fence', _sample_observation(1))
            self.assertEqual(raised.exception.code, 'meeting_finalized')

    def test_summary_includes_screen_observations_with_distinct_provenance_and_citations(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root)
            session.create({'source': 'meeting', 'provider': 'codex',
                'screen_include_in_summary': True})
            session.append_final({'event_id': 'live:1', 'revision': 0,
                'text': '我們今天先看第四季預算。', 'timestamp': '2026-11-24T13:50:00+00:00', 'sequence': 1})
            obs = _sample_observation(1)
            session.record_screen_observation(obs)
            session.finalize_minutes('completed')

            src = session.summary_source()
            self.assertTrue(src['terminal'])
            entries = src['entries']
            speech_ids = [e['id'] for e in entries if e['source_state'] == 'provider_final']
            screen_ids = [e['id'] for e in entries if e['source_state'] == 'screen_observation']
            self.assertEqual(len(speech_ids), 1)
            self.assertEqual(len(screen_ids), 1)
            self.assertTrue(screen_ids[0].startswith('V'))

            screen_entry = next(e for e in entries if e['source_state'] == 'screen_observation')
            self.assertFalse(screen_entry['provider_final'])
            self.assertEqual(screen_entry['quality'], 'derived_visual')
            self.assertIn('2026 Q4 預算審查', screen_entry['text'])

            qual = summary.source_quality(entries)
            self.assertEqual(qual['provider_final_count'], 1)
            self.assertEqual(qual['source_state_counts']['screen_observation'], 1)
            self.assertEqual(qual['quality_counts']['derived_visual'], 1)
            self.assertFalse(qual['all_sources_provider_final'])

            candidate = {section: [] for section in summary.SECTIONS}
            candidate['重點'] = [
                {'text': '第四季雲端基礎設施預算為 120,000 美元且已核准。',
                 'sources': [speech_ids[0], screen_ids[0]], 'owner': '', 'date': ''},
            ]
            candidate['model'] = 'gpt-5.6-sol'
            candidate['source_quality'] = qual
            published = session.publish_summary(candidate, expected_version=src['version'],
                expected_source_sha256=src['source_sha256'], generation_mode='codex')
            self.assertEqual(published['summary_state']['revision'], 1)

            # Opt-out flag omits screen observations from summary source
            session_optout = Session(root)
            session_optout.create({'source': 'meeting', 'provider': 'codex',
                'screen_include_in_summary': False})
            session_optout.append_final({'event_id': 'live:1', 'revision': 0,
                'text': '僅語音摘要。', 'timestamp': '2026-11-24T13:50:00+00:00', 'sequence': 1})
            session_optout.record_screen_observation(obs)
            session_optout.finalize_minutes('completed')
            optout_entries = session_optout.summary_source()['entries']
            self.assertFalse(any(e.get('source_state') == 'screen_observation' for e in optout_entries))

    def test_manual_delete_occurrence_removes_all_attempt_screen_observations_and_preserves_other_meetings(self):
        import sqlite3
        from teams_transcription.assistant_store import AssistantStore

        with tempfile.TemporaryDirectory() as root:
            astore = AssistantStore(root)
            mstore = MeetingStore(root)
            occ_del = astore.create_manual_occurrence({
                'title': '待刪除手動會議',
                'join_url': 'https://teams.microsoft.com/l/meetup-join/del-1',
                'start_at': '2026-11-24T13:00:00+08:00',
                'end_at': '2026-11-24T14:00:00+08:00',
                'recording_authorized': True,
                'speaking_authorized': True,
            })
            occ_keep = astore.create_manual_occurrence({
                'title': '保留手動會議',
                'join_url': 'https://teams.microsoft.com/l/meetup-join/keep-1',
                'start_at': '2026-11-24T15:00:00+08:00',
                'end_at': '2026-11-24T16:00:00+08:00',
                'recording_authorized': True,
                'speaking_authorized': True,
            })

            # Attempt 1 & Attempt 2 (rejoin) on occ_del, both with lease-fenced screen observations
            lease_del_1 = astore.claim_run(occ_del['id'], 'worker-1', 60)
            MeetingStore(root, lease=lease_del_1).record_screen_observation(
                lease_del_1['session_id'],
                _sample_observation(1, title='機密頁 1', frame_seed=b'del-1'))
            MeetingStore(root, lease=lease_del_1).finalize(lease_del_1['session_id'], 'completed')
            astore.finish_run(lease_del_1, 'completed', 'meeting_ended')

            lease_del_2 = astore.claim_run(occ_del['id'], 'worker-1', 60, allow_rejoin=True)
            MeetingStore(root, lease=lease_del_2).record_screen_observation(
                lease_del_2['session_id'],
                _sample_observation(1, title='機密頁 2', frame_seed=b'del-2'))
            MeetingStore(root, lease=lease_del_2).finalize(lease_del_2['session_id'], 'completed')
            astore.finish_run(lease_del_2, 'completed', 'meeting_ended')

            # Separate meeting that MUST survive deletion of occ_del
            lease_keep = astore.claim_run(occ_keep['id'], 'worker-1', 60)
            MeetingStore(root, lease=lease_keep).record_screen_observation(
                lease_keep['session_id'],
                _sample_observation(1, title='其他會議畫面', frame_seed=b'keep-1'))
            MeetingStore(root, lease=lease_keep).finalize(lease_keep['session_id'], 'completed')
            astore.finish_run(lease_keep, 'completed', 'meeting_ended')

            res = astore.delete_occurrence(occ_del['id'])
            self.assertTrue(res['deleted'])
            self.assertFalse(res['tombstoned'])

            with astore._locked() as db:
                del_ids = (lease_del_1['session_id'], lease_del_2['session_id'])
                del_meetings = db.execute(
                    'SELECT COUNT(*) FROM meetings WHERE meeting_id IN (?, ?)', del_ids).fetchone()[0]
                del_screens = db.execute(
                    'SELECT COUNT(*) FROM screen_observations WHERE meeting_id IN (?, ?)', del_ids).fetchone()[0]
                keep_screens = db.execute(
                    'SELECT COUNT(*) FROM screen_observations WHERE meeting_id = ?',
                    (lease_keep['session_id'],)).fetchone()[0]
            self.assertEqual(del_meetings, 0)
            self.assertEqual(del_screens, 0)
            self.assertEqual(keep_screens, 1)


if __name__ == '__main__':
    unittest.main()
