"""Disk/restart/atomicity tests. No model, meeting, mic or saved audio."""
import json
import multiprocessing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from teams_transcription.meeting_store import MeetingStore, MeetingStoreError
from teams_transcription.live import TranscriptNormalizer


def event(ident="one", revision=0, text="本次結論", sequence=1):
    return {"event_id": ident, "revision": revision, "text": text,
            "timestamp": "2026-10-09T10:00:00Z", "sequence": sequence}


def append_in_process(root, index):
    MeetingStore(root).append_final("a", event(str(index), sequence=index))


class DurableAliasTests(unittest.TestCase):
    def test_alias_is_scoped_conflict_checked_and_survives_new_store(self):
        with tempfile.TemporaryDirectory() as root:
            store = MeetingStore(root)
            store.create('a')
            store.create('b')
            original = store.append_final('a', event('flat-source'))
            store.bind_final_alias('a', 'canonical-source', 'flat-source')
            store.bind_final_alias('a', 'canonical-source', 'flat-source')
            restarted = MeetingStore(root)
            alias = restarted.get_final('a', 'canonical-source')
            self.assertEqual(alias['event_id'], 'flat-source')
            self.assertEqual(alias['alias_event_id'], 'canonical-source')
            self.assertEqual(restarted.read('a')['version'], original['version'])
            self.assertIsNone(restarted.get_final('b', 'canonical-source'))
            with self.assertRaisesRegex(MeetingStoreError, 'final_alias_target_missing'):
                restarted.bind_final_alias('b', 'canonical-source', 'flat-source')
            restarted.append_final('a', event('second-source', text='真正第二次相同發言'))
            with self.assertRaisesRegex(MeetingStoreError, 'final_alias_conflict'):
                restarted.bind_final_alias('a', 'canonical-source', 'second-source')
            with self.assertRaisesRegex(MeetingStoreError, 'final_alias_conflict'):
                restarted.bind_final_alias('a', 'second-source', 'flat-source')


class MeetingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = MeetingStore(self.root)
        self.store.create("a",{"available":False,"note":"上次：完成原型。待辦：審查。"})
        self.store.create("b",{"available":False,"note":"另一場秘密"})

    def test_restart_preserves_scoped_prior_context_and_current_sources_privately(self):
        self.store.append_final("a",event())
        reloaded=MeetingStore(self.root).read("a")
        self.assertEqual(reloaded["prior_context"],{"available":False,"note":"上次：完成原型。待辦：審查。"})
        self.assertEqual([row["text"] for row in reloaded["events"]],["本次結論"])
        self.assertNotIn("另一場秘密",json.dumps(reloaded,ensure_ascii=False))
        self.assertEqual((self.root/".meeting-store.sqlite3").stat().st_mode&0o777,0o600)

    def test_resend_revision_out_of_order_and_conflict(self):
        first = self.store.append_final("a", event())
        self.assertEqual(self.store.append_final("a", event())["version"], first["version"])
        self.store.append_final("a",event(revision=2,text="更正結論"))
        self.store.append_final("a",event(revision=1,text="舊的更正"))
        result=self.store.read("a")
        self.assertEqual(result["events"][0]["text"],"更正結論")
        with self.assertRaisesRegex(MeetingStoreError, "revision_conflict"):
            self.store.append_final("a", event(revision=2, text="相同revision衝突"))
        self.store.append_final("a", event("later", sequence=3))
        self.store.append_final("a",event("earlier",sequence=0))
        result=self.store.read("a")
        self.assertEqual([e["event_id"] for e in result["events"]], ["earlier", "one", "later"])

    def test_normalizer_restart_replay_keeps_durable_timestamp_sequence_and_version(self):
        method = 'thread/realtime/item/completed'
        params = {'item': {'id':'speech-one', 'realtimeSessionId':'live-one',
                          'type':'transcriptSegment', 'role':'user', 'text':'相同來源 final'}}
        first_normalizer = TranscriptNormalizer()
        with patch('teams_transcription.live.stamp', return_value='2026-10-09T10:00:00Z'):
            first = first_normalizer.final_event(method, params, 1, 'thread-one')
        first['sequence'] = 12
        self.store.append_final('a',first)
        initial=self.store.read('a')
        restarted = TranscriptNormalizer()
        with patch('teams_transcription.live.stamp', return_value='2026-10-09T10:01:00Z'):
            replay = restarted.final_event(method, params, 2, 'thread-one')
        self.assertEqual(replay['event_id'], first['event_id'])
        self.assertNotEqual(replay['timestamp'], first['timestamp'])
        self.assertNotEqual(replay['sequence'], first['sequence'])
        MeetingStore(self.root).append_final('a',replay)
        result=self.store.read('a')
        self.assertEqual(result['version'], initial['version'])
        self.assertEqual(result['sha256'], initial['sha256'])
        self.assertEqual(result['events'][0]['sequence'], 12)
        self.assertEqual(result['events'][0]['timestamp'], '2026-10-09T10:00:00+00:00')
        with self.assertRaisesRegex(MeetingStoreError, 'revision_conflict'):
            self.store.append_final('a', {**replay, 'text':'同 revision 異文'})
        self.store.append_final('a',{**replay,'revision':1,'text':'明確新修訂'})
        corrected=self.store.read('a')
        self.assertEqual(corrected['events'][0]['sequence'], 12)
        self.assertEqual(corrected['events'][0]['timestamp'], result['events'][0]['timestamp'])



    def test_terminal_results_idempotency_and_no_source_replies(self):
        result = self.store.finalize("a", "failed", "rx_start_failed")
        self.assertEqual(self.store.finalize("a","failed","rx_start_failed")["version"],result["version"])
        with self.assertRaisesRegex(MeetingStoreError, "meeting_finalized"):
            self.store.append_final("a", event())
        with self.assertRaisesRegex(MeetingStoreError, "result_conflict"):
            self.store.finalize("a")
        with self.assertRaisesRegex(MeetingStoreError, "invalid_final_event"):
            self.store.append_final("b", {**event(), "source_role": "assistant"})
        with self.assertRaisesRegex(MeetingStoreError, "meeting_exists_conflict"):
            self.store.create("b",{"available":False,"note":"替換他場內容"})


    def test_existing_v1_database_upgrades_without_losing_notes(self):
        with tempfile.TemporaryDirectory() as root:
            db = sqlite3.connect(Path(root) / '.meeting-store.sqlite3')
            db.execute('CREATE TABLE meetings(meeting_id TEXT PRIMARY KEY,prior_minutes TEXT NOT NULL,status TEXT NOT NULL,version INTEGER NOT NULL,error_code TEXT)')
            db.execute("INSERT INTO meetings VALUES('legacy','既有歷史','active',7,NULL)")
            db.commit()
            db.close()
            store = MeetingStore(root)
            read = store.read('legacy')
            self.assertEqual(read['version'], 7)
            self.assertEqual(read['prior_context'],{'legacy_text':'既有歷史','quality':'historical_unverified'})
            self.assertNotIn('summary_state',read)
            result=store.finalize('legacy')
            self.assertEqual(result['version'], 8)
            self.assertTrue(result['terminal'])

    def test_invalid_paths_timestamps_revisions_and_limits(self):
        for ident in ("../b", "/tmp/b", ".", "a/b", "a\n"):
            with self.assertRaises(MeetingStoreError):
                self.store.read(ident)
        for changes in ({"timestamp": "yesterday"}, {"timestamp": "2026-10-09"},
                        {"revision": True}, {"sequence": -1}, {"text": ""}):
            with self.assertRaises(MeetingStoreError):
                self.store.append_final("a", {**event(), **changes})
        (self.root / "alias").symlink_to(self.root / "b", target_is_directory=True)
        with self.assertRaises(MeetingStoreError):
            self.store.create("alias")

    def test_process_writers_do_not_lose_finals(self):
        processes = [multiprocessing.Process(target=append_in_process, args=(str(self.root), i)) for i in range(8)]
        for process in processes:
            process.start()
        for process in processes:
            process.join(10)
            self.assertEqual(process.exitcode, 0)
        result = self.store.read("a")
        self.assertEqual(len(result["events"]), 8)
        self.assertEqual(result["version"], 9)
