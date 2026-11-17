"""Real private canonical ledger/locks with synthetic HTTP/controller boundaries."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,MagicMock

from deploy import teams_acceptance as host, qa_cloud_bridge as bridge, qa_cloud_contract as contract
import test_cloud_qa_bridge as bridge_fixture
import test_cloud_qa_contract as contract_fixture


class Backend:
    def __init__(self):
        self.actions=[];self.maintenance_calls=[];self.joined=False;self.stopped=False
        self.request=None;self.unknown=False;self.uncertain_join=False;self.elapsed=0
        self.maximum_seconds=None
    def runtime(self):return contract_fixture.CloudQAContractTests().runtime()
    def view(self,occurrence):
        value=dict(protocol=host.VERSION,occurrence_id=occurrence,revision=1,occurrence_state='pending',
            start_at='1970-01-01T00:00:00+00:00',end_at='2100-01-01T00:00:00+00:00',
            join_host='teams.microsoft.com',recording_authorized=True,speaking_authorized=True,
            egress_policy_sha256='a'*64,egress_allows_target_host=True,
            scheduler_paused=True,capture_interlock=False,active_lease=False,session_id=None)
        value['max_lateness_seconds']=1800
        if self.joined:
            value.update(session_id='b'*32,run_state='stopped' if self.stopped else 'running',response_ready=True,
                acceptance=dict(request_sha256=self.request,version=host.VERSION,occurrence_revision=1,maximum_seconds=self.maximum_seconds,
                    cleanup_verified=self.stopped and not self.unknown,
                    count_complete=self.stopped,summary_disabled=True,thread_attempts=2,native_attempts=2,
                    A_thread_attempted=True,A_native_attempted=True,B_thread_attempted=True,B_native_attempted=True,
                    joined=True,source_rx_packets=100,B_tx_packets=90,B_tx_frames=86400,B_tx_energy=.01,
                    private_text='PRIVATE-SYNTHETIC'))
        return value
    def maintenance(self,enabled):
        self.maintenance_calls.append(enabled)
        return dict(maintenance=enabled,scheduler_paused=True)
    def control(self,action,fields):
        self.actions.append((action,fields.copy()))
        if action=='join':
            self.joined=True;self.request=fields['acceptance_request']
            if self.uncertain_join:raise TimeoutError('PRIVATE-SYNTHETIC')
            return dict(ok=True,result=dict(session_id='b'*32,occurrence_id=fields['id'],acceptance_request=self.request))
        if action=='stop':self.stopped=True
        return dict(ok=True)
    def sleep(self,seconds):self.elapsed+=seconds


class HostAcceptanceTests(unittest.TestCase):
    def setup(self,root):
        state,ledger,qa=bridge_fixture.CloudQABridgeTests().setup_state(root)
        value=json.loads(ledger.read_text());value['consumed']=12
        value['entries']=[dict(slot=n,run_id=n,run_attempt=1,status='cleaned') for n in range(8,13)]
        ledger.write_text(json.dumps(value));self.original=copy.deepcopy(value)
        self.state,self.ledger,self.qa=state,ledger,qa;self.backend=Backend()
        self.authority=dict(version=host.VERSION,occurrence_id='a'*32,occurrence_revision=1,
            maximum_seconds=None,participant_confirmed_until=1100,egress_policy_sha256='a'*64)
    def invoke(self,mode='run',run=123,attempt=1,confirmed_at=None):
        return host.perform(mode,backend=self.backend,bridge=bridge,contract=contract,state=self.state,
            qa_authority=self.qa,meeting_authority=self.authority,run=run,attempt=attempt,
            source=contract.BASE_SHA,image=contract.IMAGE,build_run=41022272702,build_attempt=1,
            clock=lambda:1000,monotonic=lambda:self.backend.elapsed,sleep=self.backend.sleep,
            authority_loader=lambda state,path:bridge.authority(state,path,owner=os.getuid()),confirmed_at=confirmed_at)

    def owner_mode(self):
        self.authority['maximum_seconds']=None;self.backend.maximum_seconds=None

    def test_owner_start_returns_without_stopping_and_later_explicit_stop_settles_original_two_slots(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.owner_mode()
            started=self.invoke()
            self.assertTrue(started['handoff_verified'])
            self.assertFalse(started['transport_checks_passed'] or started['cleanup_verified'] or started['ledger_settled'])
            self.assertEqual(self.backend.maintenance_calls,[False])
            self.assertFalse(self.backend.stopped);self.assertEqual(self.backend.elapsed,0)
            self.assertTrue((self.state/bridge.INTERLOCK).exists())
            self.assertEqual([r['status'] for r in json.loads(self.ledger.read_text())['entries'][-2:]],['reserved','reserved'])
            lock_path=Path(json.loads(self.qa.read_text())['lock_path'])
            with bridge.existing_lock(self.state/'controller.lock'),bridge.existing_lock(lock_path):pass
            self.backend.elapsed=3600
            self.authority['participant_confirmed_until']=999
            stopped=self.invoke('stop',run=124)
            self.assertTrue(stopped['cleanup_verified'] and stopped['maintenance_restored'])
            self.assertEqual(stopped['consumed'],14)
            self.assertEqual(sum(a=='join' for a,_ in self.backend.actions),1)
            self.assertEqual(sum(a=='stop' for a,_ in self.backend.actions),1)
            self.assertFalse((self.state/bridge.INTERLOCK).exists())
            saved=json.loads(self.ledger.read_text())
            self.assertEqual(saved['entries'][:5],self.original['entries'])
            self.assertEqual([r['run_id'] for r in saved['entries'][-2:]],[123,123])
            self.assertEqual([r['status'] for r in saved['entries'][-2:]],['cleaned','cleaned'])

    def test_natural_meeting_end_can_finalize_without_another_stop_or_join(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.owner_mode();self.invoke();self.backend.stopped=True
            final=self.invoke('stop',run=124)
            self.assertTrue(final['cleanup_verified'])
            self.assertEqual([a for a,_ in self.backend.actions],['join'])
            self.assertEqual(json.loads(self.ledger.read_text())['consumed'],14)

    def test_owner_unknown_cleanup_keeps_original_reservation_and_interlock(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.owner_mode();self.invoke();before=self.ledger.read_bytes()
            self.backend.unknown=True
            with self.assertRaisesRegex(host.Refused,'qa_cleanup_requires_reconciliation'):
                self.invoke('stop',run=124)
            self.assertEqual(self.ledger.read_bytes(),before)
            self.assertTrue((self.state/bridge.INTERLOCK).exists())
            self.assertEqual(self.backend.maintenance_calls,[False,True])
            self.assertEqual(sum(a=='join' for a,_ in self.backend.actions),1)

    def test_owner_stop_rejects_changed_binding_and_changed_ledger_without_clearing_interlock(self):
        for mutation in ('binding','ledger'):
            with self.subTest(mutation=mutation),tempfile.TemporaryDirectory() as root:
                self.setup(root);self.owner_mode();self.invoke()
                if mutation=='binding':self.backend.request='b'*64
                else:
                    ledger=json.loads(self.ledger.read_text());ledger['entries'][0]['run_id']=999
                    self.ledger.write_text(json.dumps(ledger))
                with self.assertRaises(host.Refused):self.invoke('stop',run=124)
                self.assertTrue((self.state/bridge.INTERLOCK).exists())
                self.assertEqual(json.loads(self.ledger.read_text())['consumed'],14)
                self.assertEqual(sum(a=='join' for a,_ in self.backend.actions),1)

    def test_primary_binding_failure_survives_maintenance_cleanup_failure_without_private_text(self):
        for outcome in ('unverified','exception'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as root:
                self.setup(root);self.invoke();self.backend.request='b'*64
                def maintenance(enabled):
                    if outcome=='exception':raise OSError('PRIVATE-SYNTHETIC-SECRET')
                    return dict(maintenance=False,scheduler_paused=True)
                self.backend.maintenance=maintenance
                with self.assertRaises(host.Refused) as raised:self.invoke('stop',run=124)
                self.assertEqual(str(raised.exception),'teams_join_requires_reconciliation')
                report=host.error_report(raised.exception)
                self.assertEqual(report,dict(success=False,error_code='teams_join_requires_reconciliation',
                    cleanup_error_code='teams_maintenance_restore_unverified'))
                self.assertNotIn('PRIVATE',json.dumps(report))
                self.assertTrue((self.state/bridge.INTERLOCK).exists())
                self.assertEqual(json.loads(self.ledger.read_text())['consumed'],14)

    def test_second_owner_start_is_denied_and_other_interlocks_are_not_stoppable(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.owner_mode();self.invoke()
            with self.assertRaises(host.Refused):self.invoke(run=125)
            marker=self.state/bridge.INTERLOCK
            data=json.loads(marker.read_text());data['experiment']='other-candidate'
            marker.write_text(json.dumps(data))
            with self.assertRaisesRegex(host.Refused,'teams_owned_run_rejected'):self.invoke('stop',run=124)
            self.assertTrue(marker.exists());self.assertFalse(self.backend.stopped)
            self.assertEqual(sum(a=='join' for a,_ in self.backend.actions),1)

    def test_old_fixed_duration_authority_is_rejected_before_reservation(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.authority['maximum_seconds']=120
            before=self.ledger.read_bytes()
            with self.assertRaisesRegex(host.Refused,'teams_acceptance_authority_rejected'):self.invoke()
            self.assertEqual(self.ledger.read_bytes(),before)
            self.assertEqual(self.backend.actions,[])

    def test_owner_uncertain_join_stops_and_settles_without_retry_or_refund(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.owner_mode();self.backend.uncertain_join=True
            result=self.invoke()
            self.assertFalse(result['success']);self.assertTrue(result['cleanup_verified'])
            self.assertEqual(result['consumed'],14)
            self.assertEqual(sum(a=='join' for a,_ in self.backend.actions),1)
            self.assertEqual(sum(a=='stop' for a,_ in self.backend.actions),1)
    def test_offline_has_no_reservation_or_maintenance_or_control_side_effect(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);before=self.ledger.read_bytes();v=self.invoke('offline')
            self.assertFalse(v['ledger_touched'] or v['model_started'] or v['live_ready'])
            self.assertEqual(self.ledger.read_bytes(),before);self.assertEqual(self.backend.actions,[])
            self.assertEqual(self.backend.maintenance_calls,[])
    def test_existing12_reserve_both_before_join_then_settle_preserving_history(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);original_control=self.backend.control
            def control(action,fields):
                value=json.loads(self.ledger.read_text())
                self.assertEqual(value['consumed'],14)
                self.assertEqual([r['status'] for r in value['entries'][-2:]],['reserved','reserved'])
                self.assertTrue((self.state/bridge.INTERLOCK).exists())
                return original_control(action,fields)
            self.backend.control=control
            self.invoke();value=self.invoke('stop',run=124)
            self.assertTrue(value['success'] and value['cleanup_verified'])
            self.assertNotIn('PRIVATE',json.dumps(value));self.assertFalse(value['remote_audibility_verified'])
            saved=json.loads(self.ledger.read_text());self.assertEqual(saved['consumed'],14)
            self.assertEqual(saved['entries'][:5],self.original['entries']);self.assertEqual(saved['checkpoint'],self.original['checkpoint'])
            self.assertEqual(self.backend.maintenance_calls,[False,True]);self.assertFalse((self.state/bridge.INTERLOCK).exists())
            with self.assertRaises(RefusedErrors):self.invoke(attempt=2)
            self.assertEqual(sum(a=='join' for a,_ in self.backend.actions),1)
    def test_uncertain_join_is_reconciled_but_never_retried_or_refunded(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.backend.uncertain_join=True
            value=self.invoke();self.assertFalse(value['success']);self.assertEqual(value['consumed'],14)
            self.assertEqual(sum(a=='join' for a,_ in self.backend.actions),1)
            self.assertEqual(sum(a=='stop' for a,_ in self.backend.actions),1)
    def test_unknown_cleanup_keeps_two_reservations_and_interlock_after_maintenance(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.backend.unknown=True
            self.invoke()
            with self.assertRaisesRegex(host.Refused,'qa_cleanup_requires_reconciliation'):self.invoke('stop',run=124)
            self.assertEqual(self.backend.maintenance_calls[-1],True)
            saved=json.loads(self.ledger.read_text());self.assertEqual(saved['consumed'],14)
            self.assertEqual([r['status'] for r in saved['entries'][-2:]],['reserved','reserved'])
            self.assertTrue((self.state/bridge.INTERLOCK).exists())
    def test_no_participant_confirmation_or_insufficient_budget_never_joins(self):
        for condition in ('expired','budget'):
            with self.subTest(condition=condition),tempfile.TemporaryDirectory() as root:
                self.setup(root)
                if condition=='expired':self.authority['participant_confirmed_until']=999
                else:
                    value=json.loads(self.ledger.read_text());value['consumed']=49
                    value['entries']=[dict(slot=n,run_id=n,run_attempt=1,status='cleaned') for n in range(8,50)]
                    self.ledger.write_text(json.dumps(value))
                with self.assertRaises(RefusedErrors):self.invoke()
                self.assertFalse(self.backend.joined);self.assertEqual(self.backend.maintenance_calls,[])
    def test_installed_release_pins_are_required_and_unset_source_template_is_refused(self):
        # The installed reviewed release is pinned; the repository template was not.
        host.require_release()
        with patch.object(host,'RELEASE_SOURCE_SHA',None):
            with self.assertRaisesRegex(host.Refused,'teams_acceptance_release_not_ready'):host.require_release()

    def test_empty_or_changed_egress_and_stale_binding_stop_before_reservation(self):
        for field,value in (('egress_allows_target_host',False),('egress_policy_sha256','b'*64),
                ('revision',2),('recording_authorized',False),('speaking_authorized',False)):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as root:
                self.setup(root);view=self.backend.view(self.authority['occurrence_id']);view[field]=value
                self.backend.view=lambda _:view
                with self.assertRaises(host.Refused):self.invoke()
                self.assertEqual(json.loads(self.ledger.read_text())['consumed'],12)
                self.assertEqual(self.backend.actions,[])

    def test_runtime_late_join_policy_is_checked_before_reserving_two_slots(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);before=self.ledger.read_bytes();view=self.backend.view(self.authority['occurrence_id'])
            view['max_lateness_seconds']=999  # fixture now1000, actual start0
            self.backend.view=lambda _:view
            with self.assertRaisesRegex(host.Refused,'teams_acceptance_window_rejected'):self.invoke()
            self.assertEqual(self.ledger.read_bytes(),before)
            self.assertEqual(self.backend.actions,[])

    def test_local_client_uses_fixed_same_uid_exec_not_admin_credentials(self):
        staging=MagicMock()
        staging.capture_docker.return_value=dict(returncode=0,stdout=b'{"protocol":"teams-single-v1"}',
            stdout_truncated=False,stderr_truncated=False,timed_out=False,cleanup_uncertain=False)
        client=host.LocalControl(staging,{'FIXTURE':'env'})
        client.ident='b'*64
        self.assertEqual(client.view('a'*32),{'protocol':'teams-single-v1'})
        args,env=staging.capture_docker.call_args.args
        self.assertEqual(args,('exec','-i','--user','10001:10001','b'*64,
            '/usr/local/bin/teams-transcription','local-teams-control','--config','/data/config/runtime.yml'))
        self.assertEqual(json.loads(staging.capture_docker.call_args.kwargs['input']),
            dict(operation='view',fields=dict(id='a'*32)))
        self.assertEqual(env,{'FIXTURE':'env'})

    def test_local_client_unknown_outcome_is_not_retried_or_exposed(self):
        for change in (dict(returncode=1),dict(timed_out=True),dict(cleanup_uncertain=True),
                dict(stdout_truncated=True),dict(stdout=b'PRIVATE-SYNTHETIC'),dict(stdout=b'{"error":"PRIVATE"}')):
            with self.subTest(change=change):
                staging=MagicMock()
                staging.capture_docker.return_value=dict(returncode=0,stdout=b'{}',stdout_truncated=False,
                    stderr_truncated=False,timed_out=False,cleanup_uncertain=False)
                staging.capture_docker.return_value.update(change)
                client=host.LocalControl(staging,{})
                client.ident='b'*64
                with self.assertRaisesRegex(host.Refused,'teams_local_control_request_rejected'):
                    client.control('join',dict(id='a'*32))
                self.assertEqual(staging.capture_docker.call_count,1)

    def test_local_client_requires_validated_container_before_request(self):
        staging=MagicMock();client=host.LocalControl(staging,{})
        with self.assertRaisesRegex(host.Refused,'teams_local_control_container_rejected'):client.view('a'*32)
        staging.capture_docker.assert_not_called()

    def test_missing_RX_TX_is_not_hidden_by_successful_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);old_view=self.backend.view
            def view(occurrence):
                value=old_view(occurrence)
                if value.get('acceptance'):value['acceptance']['B_tx_energy']=None
                return value
            self.backend.view=view
            self.invoke();value=self.invoke('stop',run=124)
            self.assertFalse(value['success'] or value['transport_checks_passed'])
            self.assertTrue(value['cleanup_verified']);self.assertEqual(value['consumed'],14)


RefusedErrors=(host.Refused,bridge.Refused,ValueError)
