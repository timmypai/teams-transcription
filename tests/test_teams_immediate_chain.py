"""Real Unix/daemon/SQLite/canonical-ledger chain; Docker/Teams are synthetic."""
import asyncio
import json
import os
import tempfile
import unittest

from deploy import teams_acceptance as host, qa_cloud_bridge as bridge, qa_cloud_contract as contract
from teams_transcription.teams_local_control import LocalTeamsControl,request
from teams_transcription.teams_acceptance import FINISHED_EVENT
from test_assistant_daemon import iso
import test_teams_target_prepare as app_fixture
import test_teams_acceptance_host as ledger_fixture
import test_cloud_qa_contract as runtime_fixture


class ImmediateChainTests(unittest.IsolatedAsyncioTestCase):
    event=app_fixture.TargetPrepareTests.event
    until=app_fixture.TargetPrepareTests.until
    call=app_fixture.TargetPrepareTests.call
    asyncSetUp=app_fixture.TargetPrepareTests.asyncSetUp

    async def test_prepare_offline_run_owned_stop_without_login_or_invented_deadline(self):
        async def controlled_runner(session):
            session.update(state='recording');self.started.set()
            while not session.stopped():await asyncio.sleep(.01)
            session.update(state='stopped',cleanup_status='cleaned')
            session.event(FINISHED_EVENT,{})  # zero real thread/native attempts
            return 0
        self.daemon.runner=controlled_runner;self.started.clear()
        loop=asyncio.get_running_loop();owner=self
        class Backend:
            def runtime(self):
                value=runtime_fixture.CloudQAContractTests().runtime()
                value['database']['maintenance']=owner.root.joinpath('.deployment-maintenance').exists()
                value['database']['quiescent']=owner.daemon.status()['active_session_id'] is None
                return value
            def invoke(self,operation,fields):
                return asyncio.run_coroutine_threadsafe(request(owner.root,dict(operation=operation,fields=fields)),loop).result(20)
            def view(self,ident):return self.invoke('view',dict(id=ident))
            def control(self,operation,fields):return self.invoke(operation,fields)
            def maintenance(self,enabled):
                marker=owner.root/'.deployment-maintenance'
                if enabled:marker.touch()
                else:marker.unlink()
                return dict(maintenance=marker.exists(),scheduler_paused=owner.store.settings()['scheduler_paused'])
        async with LocalTeamsControl(self.daemon,self.settings):
            source=await self.call('view',dict(id=self.original['id']))
            authority=dict(version=host.PREPARE_VERSION,source_occurrence_id=self.original['id'],source_revision=3,
                mode='owner_immediate',maximum_seconds=None,egress_policy_sha256=source['egress_policy_sha256'])
            with tempfile.TemporaryDirectory() as directory:
                fixture=ledger_fixture.HostAcceptanceTests();fixture.setup(directory)
                original=fixture.ledger.read_bytes()
                def run(operation,number,confirmed_at=None):
                    return host.perform(operation,backend=Backend(),bridge=bridge,contract=contract,
                        state=fixture.state,qa_authority=fixture.qa,meeting_authority=authority,run=number,attempt=1,
                        source=contract.BASE_SHA,image=contract.IMAGE,build_run=41022272702,build_attempt=1,
                        clock=lambda:self.now,confirmed_at=confirmed_at,
                        authority_loader=lambda state,path:bridge.authority(state,path,owner=os.getuid()))
                prepared=await asyncio.to_thread(run,'prepare',123)
                self.assertTrue(prepared['prepared']);self.assertEqual(fixture.ledger.read_bytes(),original)
                offline=await asyncio.to_thread(run,'offline',124)
                self.assertFalse(offline['model_started'] or offline['ledger_touched'])
                self.assertEqual(fixture.ledger.read_bytes(),original)
                self.now+=7*86400  # no prepare-time admission/meeting expiration
                with self.assertRaisesRegex(host.Refused,'teams_fresh_presence_required'):
                    await asyncio.to_thread(run,'run',125)
                self.assertEqual(fixture.ledger.read_bytes(),original)
                started=await asyncio.to_thread(run,'run',125,iso(self.now-1))
                self.assertTrue(started['handoff_verified']);self.assertEqual(started['consumed'],14)
                await asyncio.wait_for(self.started.wait(),2)
                stopped=await asyncio.to_thread(run,'stop',126)
                self.assertTrue(stopped['cleanup_verified'] and stopped['maintenance_restored'])
                self.assertFalse(stopped['success'] or stopped['transport_checks_passed'])
                self.assertEqual(stopped['model_start_attempts'],0)
                saved=json.loads(fixture.ledger.read_text())
                self.assertEqual(saved['consumed'],14)
                self.assertEqual([row['status'] for row in saved['entries'][-2:]],['not_started','not_started'])
                self.assertEqual(saved['entries'][:-2],json.loads(original)['entries'])
                self.assertFalse((fixture.state/bridge.INTERLOCK).exists())
                self.assertTrue(self.root.joinpath('.deployment-maintenance').exists())
                self.assertIsNone(self.daemon.status()['active_session_id'])
