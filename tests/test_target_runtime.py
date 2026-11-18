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

    def test_active_lease_pending_summary_and_unknown_cleanup_fail_closed(self):
        initialize(self.root,'disposable-test-password')
        store=AssistantStore(self.root)
        # Actual application claim/maintenance contract is also covered by original
        # store suites; explicitly keep unknown persisted state non-deployable.
        with store._locked() as db,db:
            db.execute("UPDATE assistant_settings SET value=? WHERE id=0",(json.dumps({'scheduler_paused':None}),))
        self.assertFalse(database_state(self.root)['quiescent'])
        with self.assertRaises(ValueError):maintenance(self.root,'enable')
