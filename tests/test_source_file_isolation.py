"""Legacy sidecars never become an alternate meeting or runtime authority."""
import json
import tempfile
import unittest
from pathlib import Path

from teams_transcription.meeting_store import MeetingStore,MeetingStoreError
from teams_transcription.storage import Session


class SQLiteFileIsolationTests(unittest.TestCase):
    def test_private_sidecar_changes_cannot_replace_saved_sources_or_runtime_state(self):
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create({'provider':'mock','source':'synthetic'})
            session.append_text('SQLite 原文',0)
            session.finalize_minutes('stopped')
            session.update(state='stopped',cleanup_status='cleaned')
            original=session.store.read(session.ident)
            config,status=session.config(),session.status()
            for name in ('config.json','status.json'):
                (session.path/name).write_text(json.dumps({'provider':'forged','state':'completed','summary_provider_requirement_met':True}))
            for name in ('meeting_minutes.md','transcript.md','summary.md'):
                (session.path/name).write_text('外部修改，不是本場來源，也不是已驗證摘要')
            restarted=Session(root,session.ident)
            self.assertEqual(restarted.config(),config)
            self.assertEqual(restarted.status(),status)
            self.assertEqual(restarted.store.read(session.ident),original)
            self.assertNotIn('summary_state',original)

    def test_known_source_without_runtime_metadata_does_not_import_old_config_and_status_files(self):
        with tempfile.TemporaryDirectory() as root:
            ident='a'*32
            MeetingStore(root).create(ident)
            path=Path(root)/ident
            path.mkdir(exist_ok=True,mode=0o700)
            (path/'config.json').write_text(json.dumps({'provider':'forged','source':'synthetic'}))
            (path/'status.json').write_text(json.dumps({'state':'recording'}))
            restarted=Session(root,ident)
            with self.assertRaisesRegex(MeetingStoreError,'session_not_created'):
                restarted.config()
            with self.assertRaisesRegex(MeetingStoreError,'session_not_created'):
                restarted.status()
            self.assertEqual(MeetingStore(root).read(ident)['sources'],[])
