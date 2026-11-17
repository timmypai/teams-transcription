"""Exact old-reader sources with synthetic nullable rows; no old image or model."""
import asyncio
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

import httpx

from teams_transcription.admin_web import initialize_admin_credentials
from teams_transcription.teams_local_control import LocalTeamsControl
import test_teams_target_prepare as fixture


class ImmediateRecoveryTests(unittest.IsolatedAsyncioTestCase):
    event=fixture.TargetPrepareTests.event
    until=fixture.TargetPrepareTests.until
    call=fixture.TargetPrepareTests.call
    asyncSetUp=fixture.TargetPrepareTests.asyncSetUp

    def legacy(self):
        directory=tempfile.TemporaryDirectory();self.addCleanup(directory.cleanup)
        root=Path(directory.name)
        fixtures=Path(__file__).parent/'fixtures'
        manifest=json.loads((fixtures/'teams-recovery-1b8.json').read_text())
        raw=(fixtures/'teams-recovery-1b8.zip').read_bytes()
        self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest['archive_sha256'])
        with zipfile.ZipFile(fixtures/'teams-recovery-1b8.zip') as bundle:
            self.assertEqual(set(bundle.namelist()),set(manifest['modules']))
            self.assertLess(sum(i.file_size for i in bundle.infolist()),4*1024*1024)
            for name,digest in manifest['modules'].items():
                self.assertNotIn('/',name)
                content=bundle.read(name);self.assertLess(len(content),131072)
                self.assertEqual(hashlib.sha256(content).hexdigest(),digest)
                (root/name).write_bytes(content)
        prefix='teams_legacy_1b8_'+str(id(self))
        spec=importlib.util.spec_from_file_location(prefix,root/'__init__.py',submodule_search_locations=[str(root)])
        package=importlib.util.module_from_spec(spec)
        sys.modules[prefix]=package
        spec.loader.exec_module(package)
        def remove():
            for name in list(sys.modules):
                if name==prefix or name.startswith(prefix+'.'):sys.modules.pop(name,None)
        self.addCleanup(remove)
        return prefix

    async def test_null_times_project_and_old_app_reads_without_mutation_or_rejoin(self):
        async with LocalTeamsControl(self.daemon,self.settings):
            pending=await self.call('prepare',self.prep)
            detail=self.store.meeting_view(pending['occurrence_id'],'UTC')
            self.assertIsNone(detail['meeting']['start_at']);self.assertIsNone(detail['meeting']['end_at'])
            self.assertEqual(detail['meeting']['admission_mode'],'owner_immediate')
            self.assertFalse(detail['controls']['join'])  # machine acceptance only
            self.assertNotIn(pending['occurrence_id'],[r['id'] for r in self.store.due_occurrences(self.now)])
            self.root.joinpath('.deployment-maintenance').unlink()
            self.started.clear()
            joined=await self.call('join',dict(id=pending['occurrence_id'],acceptance_request='d'*64,
                acceptance_revision=1,acceptance_stop_policy='owner_or_meeting_end'))
            ident=joined['result']['session_id']
            await asyncio.wait_for(self.started.wait(),2)
            await self.call('stop',dict(id=ident,acceptance_request='d'*64))
            await self.until(lambda:self.daemon.status()['active_session_id'] is None)
            self.root.joinpath('.deployment-maintenance').touch()
            # A second approved case, kept pending, demonstrates no old auto-start.
            other={**self.prep,'request_sha256':'e'*64}
            pending=await self.call('prepare',other)
        await self.daemon.close()
        for calendar in self.store.list_calendars():self.store.save_calendar({**calendar,'enabled':False})
        initialize_admin_credentials(self.settings,'synthetic-recovery-password')
        prefix=self.legacy()
        old_store=importlib.import_module(prefix+'.assistant_store').AssistantStore(self.root)
        old_daemon_module=importlib.import_module(prefix+'.daemon')
        old_admin=importlib.import_module(prefix+'.admin_web')
        async def never_run(session):raise AssertionError('old reader must not start a meeting')
        old=old_daemon_module.AssistantDaemon(self.settings,store=old_store,calendar=self.daemon.calendar,
            runner=never_run,clock=lambda:self.now)
        def dump():
            with sqlite3.connect(self.root/'.meeting-store.sqlite3') as db:return list(db.iterdump())
        before=dump()
        app=old_admin.create_admin_app(old,self.settings)
        async with app.router.lifespan_context(app):
            self.assertIsNone(old_store.get_occurrence(pending['occurrence_id'])['end_at'])
            self.assertIsNone(old_store.get_run(ident)['occurrence']['start_at'])
            self.assertIsNone(old_store.get_run(ident)['acceptance_policy']['maximum_seconds'])
            detail=old_store.meeting_view(pending['occurrence_id'],'UTC')
            self.assertIsNone(detail['meeting']['start_at']);self.assertFalse(detail['controls']['join'])
            self.assertGreaterEqual(old_store.calendar_view('2000-01-01','2100-01-01','UTC')['undated_count'],2)
            self.assertNotIn(pending['occurrence_id'],[r['id'] for r in old_store.due_occurrences(self.now)])
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
                self.assertEqual((await client.get('/healthz')).status_code,200)
                self.assertEqual((await client.get('/api/status')).status_code,401)
            # Maintenance refuses first; after removing only this fixture marker,
            # the old scheduled admission must still refuse null time before RPC.
            self.root.joinpath('.deployment-maintenance').unlink()
            try:
                with self.assertRaisesRegex(ValueError,'occurrence_outside_join_window'):
                    await old.join(pending['occurrence_id'],acceptance=dict(version='teams-single-v1',
                        request_sha256='f'*64,maximum_seconds=None,occurrence_revision=1))
            finally:self.root.joinpath('.deployment-maintenance').touch()
        self.assertEqual(dump(),before)
