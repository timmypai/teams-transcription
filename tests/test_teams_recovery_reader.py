"""Recovery-reader compatibility with synthetic future policy rows; no model or Join."""
import json
import sqlite3
import unittest

from teams_transcription.teams_acceptance import POLICY_EVENT,policy_from_db,receipt,validate_policy


class RecoveryReaderTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');self.db.row_factory=sqlite3.Row
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE session_events(meeting_id TEXT,kind TEXT,payload_json TEXT)')
        self.policy=dict(version='teams-single-v1',request_sha256='a'*64,
            occurrence_revision=1,maximum_seconds=None)

    def insert(self,value):
        self.db.execute('INSERT INTO session_events VALUES(?,?,?)',('fixture',POLICY_EVENT,json.dumps(value)))

    def test_null_policy_can_be_read_without_rewriting_it_or_inventing_completion(self):
        self.insert(self.policy)
        before=list(self.db.iterdump())
        self.assertEqual(policy_from_db(self.db,'fixture'),self.policy)
        proof=receipt(self.db,'fixture',{})
        self.assertIsNone(proof['maximum_seconds'])
        self.assertEqual((proof['thread_attempts'],proof['native_attempts']),(0,0))
        self.assertFalse(proof['count_complete'] or proof['cleanup_verified'])
        self.assertEqual(list(self.db.iterdump()),before)

    def test_finite_historical_policy_and_admission_are_unchanged(self):
        legacy={**self.policy,'maximum_seconds':120}
        self.insert(legacy)
        self.assertEqual(policy_from_db(self.db,'fixture'),legacy)
        self.assertEqual(validate_policy(legacy),legacy)

    def test_null_history_does_not_relax_identity_schema_or_revision_validation(self):
        for change in ({'version':'foreign'},{'request_sha256':'bad'},
                {'occurrence_revision':True},{'occurrence_revision':0},
                {'private_field':'PRIVATE-SYNTHETIC'},{'maximum_seconds':'forever'}):
            with self.subTest(change=change):
                self.db.execute('DELETE FROM session_events');self.insert({**self.policy,**change})
                with self.assertRaisesRegex(ValueError,'teams_acceptance_policy_rejected'):
                    policy_from_db(self.db,'fixture')

    def test_duplicate_policy_binding_still_fails_closed(self):
        self.insert(self.policy);self.insert(self.policy)
        with self.assertRaisesRegex(ValueError,'teams_acceptance_binding_rejected'):
            policy_from_db(self.db,'fixture')
