"""Synthetic Google payload → SQLite identity/scheduling acceptance fixtures.

No real Calendar, model, Teams admission or notification is exercised here.
"""
import tempfile
import unittest
from datetime import datetime

from teams_transcription.assistant_store import AssistantStore, AssistantStoreError
from teams_transcription.calendar_client import normalize_event


START = '2026-11-02T15:00:00Z'
END = '2026-11-02T17:00:00Z'
ORIGINAL = '2026-11-02T15:00:00Z'
LINK = 'https://teams.microsoft.com/meet/12345'


def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def google_instance(ident='test-instance-one', **changes):
    """Invented payload; IDs, text and URL are not live test resources."""
    return {
        'id': ident,
        'summary': 'Synthetic recurring integration',
        'recurringEventId': 'test-recurring-parent',
        'originalStartTime': {'dateTime': ORIGINAL},
        'start': {'dateTime': START},
        'end': {'dateTime': END},
        'description': LINK,
        'updated': '2026-11-02T12:00:00Z',
        'etag': '"synthetic-revision-one"',
        **changes,
    }


class PostmeetingCalendarIdentityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = AssistantStore(temporary.name)
        self.calendar = self.calendar_row()

    def calendar_row(self, account='dot-test-account', calendar='dot-test-calendar@example.invalid'):
        return self.store.save_calendar(dict(
            account_id=account, calendar_id=calendar, name='Synthetic dot-only test',
            enabled=True, recording_authorized=True, speaking_authorized=False,
            join_early_seconds=300, max_lateness_seconds=300,
        ))

    def sync(self, payload, calendar=None):
        return self.store.sync_occurrences(
            (calendar or self.calendar)['id'], [normalize_event(payload)])[0]

    def test_formal_account_calendar_parent_scope_isolated_despite_shared_url_and_title(self):
        first = self.sync(google_instance())
        next_instance = self.sync(google_instance(
            'test-instance-two', originalStartTime={'dateTime': '2026-11-09T15:00:00Z'},
            start={'dateTime': '2026-11-09T15:00:00Z'}, end={'dateTime': '2026-11-09T17:00:00Z'}))
        other_account = self.sync(google_instance(), self.calendar_row(account='other-test-account'))
        other_calendar = self.sync(google_instance(), self.calendar_row(calendar='other-calendar@example.invalid'))
        other_parent = self.sync(google_instance('another-instance', recurringEventId='other-parent'))
        self.assertEqual(first['series_id'], next_instance['series_id'])
        self.assertNotEqual(first['id'], next_instance['id'])
        self.assertEqual(len({first['series_id'], other_account['series_id'],
                              other_calendar['series_id'], other_parent['series_id']}), 4)

    def test_moved_instance_keeps_original_identity_and_uses_latest_start_for_t_minus_five(self):
        first = self.sync(google_instance())
        self.assertEqual(self.store.due_occurrences(instant('2026-11-02T14:54:59Z')), [])
        self.assertEqual([value['id'] for value in self.store.due_occurrences(
            instant('2026-11-02T14:55:00Z'))], [first['id']])

        moved = self.sync(google_instance(
            originalStartTime={'dateTime': '2026-11-02T10:00:00-05:00'},
            start={'dateTime': '2026-11-02T16:00:00Z'},
            end={'dateTime': '2026-11-02T18:00:00Z'},
            description='https://teams.microsoft.com/meet/67890',
            updated='2026-11-02T12:01:00Z', etag='"synthetic-revision-two"'))
        self.assertEqual((moved['id'], moved['series_id']), (first['id'], first['series_id']))
        self.assertEqual(moved['original_start'], '2026-11-02T15:00:00+00:00')
        self.assertGreater(moved['revision'], first['revision'])
        self.assertEqual(self.store.due_occurrences(instant('2026-11-02T14:55:00Z')), [])
        self.assertEqual(self.store.due_occurrences(instant('2026-11-02T15:54:59Z')), [])
        self.assertEqual([value['id'] for value in self.store.due_occurrences(
            instant('2026-11-02T15:55:00Z'))], [first['id']])

    def test_minimal_cancelled_response_prevents_due_or_claim_without_rebuilding_occurrence(self):
        first = self.sync(google_instance())
        cancelled = self.sync({'id': 'test-instance-one', 'status': 'cancelled'})
        self.assertEqual((cancelled['id'], cancelled['series_id']), (first['id'], first['series_id']))
        self.assertEqual(cancelled['state'], 'cancelled')
        self.assertEqual(self.store.due_occurrences(instant('2026-11-02T14:55:00Z')), [])
        with self.assertRaisesRegex(AssistantStoreError, 'occurrence_not_ready'):
            self.store.claim_run(first['id'], 'synthetic-owner')
        self.assertEqual(self.store.list_runs(), [])

    def test_duplicate_sync_and_restart_cannot_recreate_a_completed_occurrence_run(self):
        first = self.sync(google_instance())
        duplicate = self.sync(google_instance())
        self.assertEqual((duplicate['id'], duplicate['revision']), (first['id'], first['revision']))
        lease = self.store.claim_run(first['id'], 'synthetic-owner')
        with self.assertRaisesRegex(AssistantStoreError, 'occurrence_not_ready'):
            self.store.claim_run(first['id'], 'another-owner')
        self.store.finish_run(lease, 'stopped', 'operator_stop')
        restarted = AssistantStore(self.store.root)
        restarted.sync_occurrences(self.calendar['id'], [normalize_event(google_instance())])
        self.assertEqual(restarted.due_occurrences(instant('2026-11-02T15:00:00Z')), [])
        self.assertEqual([value['session_id'] for value in restarted.list_runs()], [lease['session_id']])
        with self.assertRaisesRegex(AssistantStoreError, 'occurrence_not_ready'):
            restarted.claim_run(first['id'], 'restarted-owner')

    def test_stale_google_revision_cannot_move_current_occurrence_back_to_old_window(self):
        first = self.sync(google_instance())
        moved = self.sync(google_instance(start={'dateTime': '2026-11-02T16:00:00Z'},
            end={'dateTime': '2026-11-02T18:00:00Z'}, updated='2026-11-02T12:02:00Z'))
        self.store.sync_occurrences(self.calendar['id'], [normalize_event(google_instance())])
        stored = self.store.get_occurrence(first['id'])
        self.assertEqual((stored['revision'], stored['start_at']), (moved['revision'], moved['start_at']))
        self.assertEqual(self.store.due_occurrences(instant('2026-11-02T14:55:00Z')), [])


if __name__ == '__main__':
    unittest.main()

class FormalCalendarBindingTests(unittest.TestCase):
    def test_calendar_identity_cannot_be_rebound_after_save(self):
        from tempfile import TemporaryDirectory
        from teams_transcription.assistant_store import AssistantStore
        with TemporaryDirectory() as root:
            store = AssistantStore(root)
            calendar = store.save_calendar(dict(account_id='dot-test',calendar_id='one@example.invalid',name='One'))
            self.assertEqual(calendar['join_early_seconds'],300)
            with self.assertRaisesRegex(ValueError,'calendar_identity_immutable'):
                store.save_calendar(dict(id=calendar['id'],revision=calendar['revision'],
                    calendar_id='other@example.invalid'))

    def test_legacy_local_calendar_keys_migrate_without_changing_public_ids(self):
        import sqlite3
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        with TemporaryDirectory() as root:
            store = AssistantStore(root)
            calendar = store.save_calendar(dict(account_id='dot-test',calendar_id='formal@example.invalid',name='One'))
            event = normalize_event(google_instance())
            first = store.sync_occurrences(calendar['id'],[event])[0]
            # Schema fixture from pre-migration main, not a publication shortcut.
            with sqlite3.connect(Path(root)/'.meeting-store.sqlite3') as db:
                db.execute('DELETE FROM calendar_identity_migrations')
                db.execute('UPDATE meeting_series SET series_key=? WHERE id=?',
                    (json.dumps(['dot-test',calendar['id'],'test-recurring-parent'],separators=(',',':')),first['series_id']))
                db.execute('UPDATE calendar_occurrences SET identity_key=? WHERE id=?',
                    (json.dumps(['dot-test',calendar['id'],'test-recurring-parent',event['original_start']],separators=(',',':')),first['id']))
            restarted = AssistantStore(root)
            moved = restarted.sync_occurrences(calendar['id'],[
                normalize_event(google_instance(start={'dateTime':'2026-11-02T16:00:00Z'}))])[0]
            self.assertEqual((first['id'],first['series_id']),(moved['id'],moved['series_id']))
            self.assertEqual(json.loads(restarted.get_series(first['series_id'])['series_key']),
                ['dot-test','formal@example.invalid','test-recurring-parent'])
