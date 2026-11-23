"""Regression tests for Taipei timezone, auto-summary, paragraph transcript, rejoin, delete, auto-join, and auto-join status."""
import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.assistant_store import AssistantStore, AssistantStoreError
from teams_transcription.calendar_view import group_transcript_paragraphs
from teams_transcription.daemon import AssistantDaemon
from teams_transcription.storage import Session
from teams_transcription.summary import SECTIONS, source_quality


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


class UXFeaturesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.store = AssistantStore(self.root)
        self.calendar = self.store.save_calendar(dict(
            account_id='acct', calendar_id='primary', name='Main',
            enabled=True, recording_authorized=True, speaking_authorized=True))

    def test_default_timezone_is_asia_taipei(self):
        settings = AssistantSettings(
            data_root=str(self.root),
            admin_password_file=str(self.root / 'password.json'),
            admin_session_secret_file=str(self.root / 'secret.key'),
            google_credentials_file=str(self.root / 'google.json'),
        )
        self.assertEqual(settings.timezone, 'Asia/Taipei')

    def test_paragraph_grouping_merges_continuous_fragments_and_splits_on_pauses_or_provenance(self):
        transcript = [
            dict(id='S1', text='要', timestamp='2026-11-23T14:31:33+00:00', source_state='application_segment', quality='unfinalized'),
            dict(id='S2', text='做', timestamp='2026-11-23T14:31:34+00:00', source_state='application_segment', quality='unfinalized'),
            dict(id='S3', text='入會測試', timestamp='2026-11-23T14:31:35+00:00', source_state='application_segment', quality='unfinalized'),
            dict(id='S4', text='Timmy', timestamp='2026-11-23T14:31:36+00:00', source_state='application_segment', quality='unfinalized'),
            dict(id='S5', text='owner', timestamp='2026-11-23T14:31:37+00:00', source_state='application_segment', quality='unfinalized'),
            # 12s pause -> new paragraph
            dict(id='S6', text='小白小白', timestamp='2026-11-23T14:31:49+00:00', source_state='application_segment', quality='unfinalized'),
            dict(id='S7', text='，明天星期幾', timestamp='2026-11-23T14:31:50+00:00', source_state='application_segment', quality='unfinalized'),
            # Different provenance class -> new paragraph even without pause
            dict(id='F1', text='正式定稿句子。', timestamp='2026-11-23T14:31:51+00:00', source_state='provider_final', quality='provider_final'),
        ]
        paragraphs = group_transcript_paragraphs(transcript)
        self.assertEqual(len(paragraphs), 3)
        self.assertEqual(paragraphs[0]['text'], '要做入會測試Timmy owner')
        self.assertEqual(paragraphs[0]['source_ids'], ['S1', 'S2', 'S3', 'S4', 'S5'])
        self.assertEqual(paragraphs[0]['segment_count'], 5)
        self.assertEqual(paragraphs[1]['text'], '小白小白，明天星期幾')
        self.assertEqual(paragraphs[1]['source_ids'], ['S6', 'S7'])
        self.assertEqual(paragraphs[2]['text'], '正式定稿句子。')
        self.assertEqual(paragraphs[2]['source_state'], 'provider_final')

    def test_legacy_unique_assistant_runs_migrates_for_rejoin_without_losing_data(self):
        with tempfile.TemporaryDirectory() as other_root:
            db_path = Path(other_root) / '.meeting-store.sqlite3'
            store = AssistantStore(other_root)
            occ = store.create_manual_occurrence(dict(
                title='Migration fixture', join_url='https://teams.microsoft.com/meet/12345',
                start_at='2026-11-23T01:00:00+00:00', end_at='2026-11-23T02:00:00+00:00',
                recording_authorized=True, speaking_authorized=False))
            lease = store.claim_run(occ['id'], 'owner-1')
            Session(other_root, lease['session_id'], lease=lease).create(dict(
                provider='codex', source='meeting', prior_context=store.get_run(lease['session_id'])['prior_context']))
            store.finish_run(lease, 'stopped', 'operator_stop')
            # Simulate legacy schema with UNIQUE(occurrence_id)
            with sqlite3.connect(db_path) as db:
                db.execute('''CREATE TABLE assistant_runs_old(session_id TEXT PRIMARY KEY,occurrence_id TEXT NOT NULL UNIQUE,
                    started_at TEXT NOT NULL,ended_at TEXT,state TEXT NOT NULL,end_reason TEXT,error_code TEXT,
                    prior_json TEXT NOT NULL,glossary_json TEXT NOT NULL)''')
                db.execute('INSERT INTO assistant_runs_old SELECT * FROM assistant_runs')
                db.execute('DROP TABLE assistant_runs')
                db.execute('ALTER TABLE assistant_runs_old RENAME TO assistant_runs')
            migrated = AssistantStore(other_root)
            # Ordinary claim_run still fails closed on executed occurrence
            with self.assertRaisesRegex(AssistantStoreError, 'occurrence_not_ready'):
                migrated.claim_run(occ['id'], 'owner-2')
            # Explicit allow_rejoin creates a second run while keeping the first sealed
            lease2 = migrated.claim_run(occ['id'], 'owner-2', allow_rejoin=True)
            self.assertNotEqual(lease2['session_id'], lease['session_id'])
            migrated.finish_run(lease2, 'completed', 'capture_complete')
            detail = migrated.meeting_view(occ['id'], 'Asia/Taipei')
            self.assertEqual(detail['session_id'], lease2['session_id'])
            self.assertEqual({r['session_id'] for r in detail['runs']}, {lease['session_id'], lease2['session_id']})

    async def test_daemon_rejoin_supports_overtime_and_preserves_prior_run(self):
        now = time.time()
        settings = AssistantSettings(
            data_root=str(self.root),
            admin_password_file=str(self.root / 'password.json'),
            admin_session_secret_file=str(self.root / 'secret.key'),
            google_credentials_file=str(self.root / 'google.json'),
        )
        runs_started = []
        release = asyncio.Event()
        async def runner(session):
            runs_started.append(session.ident)
            session.update(state='recording')
            session.append_text(f'Run {len(runs_started)} transcript.', 0)
            await release.wait()
            session.update(state='stopped', cleanup_status='cleaned')
            return 0
        async def fake_summary(publisher, runtime, codex_bin, model):
            src = publisher.summary_source()
            val = {k: [] for k in SECTIONS}
            val['重點'] = [dict(text=src['entries'][0]['text'], owner='', date='', sources=[src['entries'][0]['id']])]
            val.update(model='controlled-test-codex', source_quality=source_quality(src['entries']))
            publisher.publish_summary(val, expected_version=src['version'],
                expected_source_sha256=src['source_sha256'], generation_mode='codex')
            return val
        clock_now = [now]
        daemon = AssistantDaemon(settings, store=self.store, runner=runner, summarizer=fake_summary, clock=lambda: clock_now[0])
        await daemon.start()
        self.addAsyncCleanup(daemon.close)
        occ = self.store.create_manual_occurrence(dict(
            title='Rejoin test', join_url='https://teams.microsoft.com/meet/99999',
            start_at=_iso(now - 60), end_at=_iso(now + 60),
            recording_authorized=True, speaking_authorized=True))
        release.set()
        first = await daemon.join(occ['id'])
        async with asyncio.timeout(5):
            while self.store.get_run(first['session_id'])['state'] == 'running':
                await asyncio.sleep(0.02)
        # Advance clock past scheduled end_at to verify overtime rejoin works without instant stop
        clock_now[0] = now + 120
        release.clear()
        second = await daemon.rejoin(occ['id'])
        self.assertNotEqual(second['session_id'], first['session_id'])
        await asyncio.sleep(0.08)
        self.assertEqual(self.store.get_run(second['session_id'])['state'], 'running')
        release.set()
        async with asyncio.timeout(5):
            while self.store.get_run(second['session_id'])['state'] == 'running':
                await asyncio.sleep(0.02)
        self.assertEqual([r['text'] for r in self.store.read(first['session_id'])['sources']], ['Run 1 transcript.'])
        self.assertEqual([r['text'] for r in self.store.read(second['session_id'])['sources']], ['Run 2 transcript.'])

    def test_delete_manual_removes_records_and_delete_synced_tombstones_against_resync(self):
        manual = self.store.create_manual_occurrence(dict(
            title='Manual delete', join_url='https://teams.microsoft.com/meet/12345',
            start_at='2026-11-23T01:00:00+00:00', end_at='2026-11-23T02:00:00+00:00',
            recording_authorized=True, speaking_authorized=False))
        lease = self.store.claim_run(manual['id'], 'owner-del')
        session = Session(self.root, lease['session_id'], lease=lease).create(dict(
            provider='codex', source='meeting', prior_context=self.store.get_run(lease['session_id'])['prior_context']))
        session.append_text('Should be deleted.', 0)
        # Cannot delete while running
        with self.assertRaisesRegex(AssistantStoreError, 'meeting_running_cannot_delete'):
            self.store.delete_occurrence(manual['id'])
        self.store.finish_run(lease, 'stopped', 'operator_stop')
        result = self.store.delete_occurrence(manual['id'])
        self.assertEqual(result, {'deleted': True, 'id': manual['id'], 'tombstoned': False})
        with self.assertRaisesRegex(AssistantStoreError, 'record_not_found'):
            self.store.meeting_view(manual['id'])
        self.assertEqual(self.store.list_runs(), [])

        # Google Calendar synced occurrence -> tombstone survives subsequent sync_occurrences
        event = dict(event_id='gcal-ev-1', title='Synced meeting',
            start_at='2026-11-23T03:00:00+00:00', end_at='2026-11-23T04:00:00+00:00',
            status='confirmed', link_status='valid', all_day=False,
            join_url='https://teams.microsoft.com/meet/54321')
        synced = self.store.sync_occurrences(self.calendar['id'], [event])[0]
        res = self.store.delete_occurrence(synced['id'])
        self.assertEqual(res, {'deleted': True, 'id': synced['id'], 'tombstoned': True})
        self.store.sync_occurrences(self.calendar['id'], [event])
        self.assertEqual(self.store.list_occurrences(), [])
        self.assertEqual(self.store.calendar_view('2026-11-23', '2026-11-24', 'Asia/Taipei')['meetings'], [])

    def test_auto_join_status_reflects_scheduler_and_calendar_policy(self):
        event = dict(event_id='gcal-auto-1', title='Auto join test',
            start_at='2026-11-23T05:00:00+00:00', end_at='2026-11-23T06:00:00+00:00',
            status='confirmed', link_status='valid', all_day=False,
            join_url='https://teams.microsoft.com/meet/77777')
        occ = self.store.sync_occurrences(self.calendar['id'], [event])[0]
        # Default scheduler_paused=True
        paused_view = self.store.meeting_view(occ['id'], 'Asia/Taipei')
        self.assertTrue(paused_view['auto_join']['enabled'])
        self.assertFalse(paused_view['auto_join']['ready'])
        self.assertEqual(paused_view['auto_join']['reason'], 'scheduler_paused')
        # Unpause scheduler -> ready!
        self.store.update_settings({'scheduler_paused': False})
        ready_view = self.store.meeting_view(occ['id'], 'Asia/Taipei')
        self.assertTrue(ready_view['auto_join']['ready'])
        self.assertEqual(ready_view['auto_join']['reason'], 'ready')

    def test_delete_refuses_interlocked_capture_until_reviewed(self):
        occ = self.store.create_manual_occurrence(dict(
            title='Interlocked delete', join_url='https://teams.microsoft.com/meet/12345',
            start_at='2026-11-23T01:00:00+00:00', end_at='2026-11-23T02:00:00+00:00',
            recording_authorized=True, speaking_authorized=False))
        lease = self.store.claim_run(occ['id'], 'owner-interlock')
        Session(self.root, lease['session_id'], lease=lease).create(dict(
            provider='codex', source='meeting', prior_context=self.store.get_run(lease['session_id'])['prior_context']))
        self.store.recover_expired(now_boottime=lease['expires_boottime'] + 1)
        with self.assertRaisesRegex(AssistantStoreError, 'capture_cleanup_review_required'):
            self.store.delete_occurrence(occ['id'])
        self.assertFalse(self.store.meeting_view(occ['id'], 'Asia/Taipei')['controls']['delete'])


class DeleteConfirmationWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import test_admin_web as fixture
        await fixture.AdminWebTests.asyncSetUp(self)

    async def login(self):
        import test_admin_web as fixture
        return await fixture.AdminWebTests.login(self)

    async def test_schedule_delete_requires_confirmation_page_and_explicit_checkbox(self):
        from test_admin_web import Forms, ORIGIN
        token = await self.login()
        store = self.daemon.store
        occ = store.create_manual_occurrence(dict(
            title='Confirm delete manual', join_url='https://teams.microsoft.com/meet/12345',
            start_at='2026-11-23T01:00:00+00:00', end_at='2026-11-23T02:00:00+00:00',
            recording_authorized=True, speaking_authorized=False))
        lease = store.claim_run(occ['id'], 'confirm-owner')
        session = Session(self.settings.data_root, lease['session_id'], lease=lease).create(dict(
            provider='mock', source='synthetic', prior_context=store.get_run(lease['session_id'])['prior_context']))
        session.append_text('Sealed manual source.', 0)
        store.finish_run(lease, 'stopped', 'operator_stop')

        # /schedule has NO direct POST /actions/delete form — only a safe link to confirmation page
        sched = await self.client.get('/schedule')
        self.assertEqual(sched.status_code, 200)
        direct_delete_forms = [f for f in Forms(sched.text).forms if f['attrs'].get('action') == '/actions/delete']
        self.assertEqual(direct_delete_forms, [])
        self.assertIn(f'/schedule/{occ["id"]}/delete', sched.text)

        # GET confirmation page warns about irreversible manual deletion and renders unchecked checkbox without deleting
        confirm_page = await self.client.get(f'/schedule/{occ["id"]}/delete')
        self.assertEqual(confirm_page.status_code, 200)
        self.assertIn('永久移除', confirm_page.text)
        confirm_forms = [f for f in Forms(confirm_page.text).forms if f['attrs'].get('action') == '/actions/delete']
        self.assertEqual(len(confirm_forms), 1)
        inputs = {item['name']: item for item in confirm_forms[0]['inputs']}
        self.assertEqual(inputs['confirm_delete']['type'], 'checkbox')
        self.assertNotIn('checked', inputs['confirm_delete'])
        self.assertEqual(store.get_occurrence(occ['id'])['id'], occ['id'])

        # Submitting without confirm_delete='on' fails closed (422) and preserves data
        unconfirmed = await self.client.post('/actions/delete',
            data={'csrf': token, 'id': occ['id']}, headers={'Origin': ORIGIN})
        self.assertEqual(unconfirmed.status_code, 422)
        self.assertEqual(store.read(session.ident)['sources'][0]['text'], 'Sealed manual source.')

        # Explicit confirm_delete='on' deletes the manual occurrence and sealed source
        confirmed = await self.client.post('/actions/delete',
            data={'csrf': token, 'id': occ['id'], 'confirm_delete': 'on'}, headers={'Origin': ORIGIN})
        self.assertEqual(confirmed.status_code, 303)
        self.assertEqual(confirmed.headers['location'], '/schedule')
        with self.assertRaisesRegex(AssistantStoreError, 'record_not_found'):
            store.get_occurrence(occ['id'])

    async def test_workspace_json_delete_requires_confirmed_true_and_tombstones_google_event(self):
        from test_admin_web import ORIGIN
        token = await self.login()
        store = self.daemon.store
        cal = store.save_calendar(dict(account_id='acct', calendar_id='primary', name='Main',
            enabled=True, recording_authorized=True, speaking_authorized=True))
        occ = store.sync_occurrences(cal['id'], [dict(
            event_id='gcal-confirm-1', title='Confirm Google Hide',
            start_at='2026-11-23T03:00:00+00:00', end_at='2026-11-23T04:00:00+00:00',
            status='confirmed', link_status='valid', all_day=False,
            join_url='https://teams.microsoft.com/meet/54321')])[0]

        # Confirmation page clearly states Google local hiding
        confirm_page = await self.client.get(f'/schedule/{occ["id"]}/delete')
        self.assertEqual(confirm_page.status_code, 200)
        self.assertIn('不會修改遠端 Google 行事曆', confirm_page.text)

        # JSON delete without confirmed='true' fails closed (422)
        rejected = await self.client.post('/actions/delete',
            data={'csrf': token, 'id': occ['id']},
            headers={'Origin': ORIGIN, 'Accept': 'application/json'})
        self.assertEqual(rejected.status_code, 422)
        self.assertEqual(rejected.json(), {'error': 'delete_confirmation_required'})

        # JSON delete with confirmed='true' tombstones locally
        accepted = await self.client.post('/actions/delete',
            data={'csrf': token, 'id': occ['id'], 'confirmed': 'true'},
            headers={'Origin': ORIGIN, 'Accept': 'application/json'})
        self.assertEqual(accepted.status_code, 200)
        self.assertTrue(accepted.json()['ok'])
        self.assertTrue(accepted.json()['result']['tombstoned'])


if __name__ == '__main__':
    unittest.main()
