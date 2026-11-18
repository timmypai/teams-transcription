"""Real private SQLite bootstrap/fence/backup. No Docker, accounts or network."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from deploy.target_runtime import initialize, database_state, maintenance
from teams_transcription.assistant_store import AssistantStore


class TargetRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.root.chmod(0o700)

    def test_init_is_private_paused_deny_all_and_never_resets_existing_secret(self):
        self.assertTrue(initialize(self.root,'disposable-test-password')['initialized'])
        password=self.root/'config/admin-password.json'
        before=password.read_bytes()
        self.assertFalse(initialize(self.root,'another-test-password')['initialized'])
        self.assertEqual(password.read_bytes(),before)
        self.assertEqual(password.stat().st_mode&0o777,0o600)
        settings=json.loads((self.root/'config/runtime.yml').read_text())
        self.assertEqual(settings['allowed_egress_hosts'],[])
        self.assertEqual(settings['codex_home'],str(self.root/'codex'))
        self.assertTrue(database_state(self.root)['quiescent'])

    def test_guard_refuses_unpaused_and_backup_does_not_restore_or_rewrite_live_data(self):
        initialize(self.root,'disposable-test-password')
        store=AssistantStore(self.root)
        store.update_settings({'scheduler_paused':False})
        with self.assertRaisesRegex(ValueError,'runtime_not_quiescent'):maintenance(self.root,'enable')
        self.assertFalse((self.root/'.deployment-maintenance').exists())
        store.update_settings({'scheduler_paused':True})
        self.assertTrue(maintenance(self.root,'enable')['maintenance'])
        before=(self.root/'.meeting-store.sqlite3').read_bytes()
        self.assertTrue(maintenance(self.root,'backup','a'*32)['backup_created'])
        self.assertEqual((self.root/'.meeting-store.sqlite3').read_bytes(),before)
        self.assertTrue((self.root/'deployment-backups'/('a'*32+'.sqlite3')).is_file())
        self.assertFalse(maintenance(self.root,'disable')['maintenance'])

    def test_repeat_enable_disable_converges_but_partial_init_and_symlinks_refuse(self):
        initialize(self.root,'disposable-test-password')
        for _ in range(2):self.assertTrue(maintenance(self.root,'enable')['maintenance'])
        for _ in range(2):self.assertFalse(maintenance(self.root,'disable')['maintenance'])
        path=self.root/'config/admin-password.json';path.unlink()
        with self.assertRaises(FileNotFoundError):initialize(self.root,'new-password-not-allowed')
        path.symlink_to(self.root/'config/runtime.yml')
        with self.assertRaises(ValueError):initialize(self.root,'new-password-not-allowed')

    def test_unknown_paused_policy_fails_closed(self):
        initialize(self.root,'disposable-test-password')
        store=AssistantStore(self.root)
        # Actual application claim/maintenance contract is also covered by original
        # store suites; explicitly keep unknown persisted state non-deployable.
        with store._locked() as db,db:
            db.execute("UPDATE assistant_settings SET value=? WHERE id=0",(json.dumps({'scheduler_paused':None}),))
        self.assertFalse(database_state(self.root)['quiescent'])
        with self.assertRaises(ValueError):maintenance(self.root,'enable')


from test_capture_cleanup_review import CleanupFixture
from unittest.mock import patch


class TargetCleanupParityTests(CleanupFixture,unittest.TestCase):
    def setUp(self):
        self.make_store()
        self.root=Path(self.tmp.name)
        (self.root/'config').mkdir(mode=0o700)
        runtime=self.root/'config/runtime.yml';runtime.write_text('{}');runtime.chmod(0o600)

    def test_real_active_capture_lease_prevents_maintenance_before_stop(self):
        self.capture(source=False)
        state=database_state(self.root)
        self.assertTrue(state['active_lease']);self.assertTrue(state['active_meeting'])
        with self.assertRaisesRegex(ValueError,'runtime_not_quiescent'):maintenance(self.root,'enable')
        self.assertFalse((self.root/'.deployment-maintenance').exists())

    def test_unknown_cleanup_valid_review_and_stale_review_match_application(self):
        from teams_transcription.storage import Session
        session,_=self.interrupted(source=False)
        for reviewed in (False,True):
            if reviewed:
                with patch('teams_transcription.assistant_store.os.kill',side_effect=ProcessLookupError):self.review(session)
            state=database_state(self.root)
            self.assertEqual(state['unreviewed_cleanup'],bool(self.store.capture_interlocks()))
            self.assertEqual(state['unreviewed_cleanup'],not reviewed)
        Session(self.tmp.name,session.ident).update(pid=987654320)
        self.assertTrue(database_state(self.root)['unreviewed_cleanup'])
        with self.assertRaises(ValueError):maintenance(self.root,'enable')

    def test_legacy_pending_summary_blocks_deployment_without_starting_worker(self):
        from summary_history_fixture import legacy_job
        session,_=self.interrupted(source=False)
        with patch('teams_transcription.assistant_store.os.kill',side_effect=ProcessLookupError):self.review(session)
        legacy_job(self.store,session.ident)
        self.assertTrue(database_state(self.root)['pending_summary'])
        with self.assertRaises(ValueError):maintenance(self.root,'enable')
