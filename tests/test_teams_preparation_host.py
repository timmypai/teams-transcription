import json
import tempfile
import unittest

from deploy import teams_acceptance as host, qa_cloud_bridge as bridge
import test_teams_acceptance_host as fixture


class PreparedBackend(fixture.Backend):
    def __init__(self):
        super().__init__();self.prepared=False;self.preparation_request=None
    def view(self,occurrence):
        value=super().view(occurrence)
        if occurrence=='a'*32:
            value.update(revision=3,occurrence_state='failed',session_id='e'*32)
        elif occurrence=='d'*32:
            value.update(start_at=None,end_at=None,max_lateness_seconds=None,admission_mode='owner_immediate',
                preparation_request_sha256=self.preparation_request)
        return value
    def control(self,action,fields):
        if action=='prepare':
            self.actions.append((action,fields.copy()));self.prepared=True
            self.preparation_request=fields['request_sha256']
            return dict(prepared=True,occurrence_id='d'*32,revision=1,request_sha256=self.preparation_request)
        return super().control(action,fields)


class HostPreparationTests(unittest.TestCase):
    invoke=fixture.HostAcceptanceTests.invoke
    def setup(self,root):
        fixture.HostAcceptanceTests.setup(self,root)
        self.backend=PreparedBackend()
        self.authority=dict(version=host.PREPARE_VERSION,source_occurrence_id='a'*32,source_revision=3,
            mode='owner_immediate',
            maximum_seconds=None,egress_policy_sha256='a'*64)

    def test_prepare_and_offline_need_no_presence_and_touch_no_ledger_or_maintenance(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);before=self.ledger.read_bytes()
            result=self.invoke('prepare')
            self.assertTrue(result['prepared']);self.assertFalse(result['model_started'] or result['ledger_touched'])
            result=self.invoke('offline',run=124)
            self.assertFalse(result['live_ready'] or result['model_started'] or result['ledger_touched'])
            self.assertEqual(self.ledger.read_bytes(),before)
            self.assertEqual(self.backend.maintenance_calls,[])
            self.assertEqual([a for a,_ in self.backend.actions],['prepare'])

    def test_only_actual_fresh_confirmation_can_start_then_owned_stop_settles_two(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.invoke('prepare')
            before=self.ledger.read_bytes()
            for value in (None,'','1970-01-01T00:10:00+00:00','1970-01-01T00:17:00+00:00'):
                with self.subTest(value=value),self.assertRaisesRegex(host.Refused,'teams_fresh_presence_required'):
                    self.invoke('run',run=124,confirmed_at=value)
                self.assertEqual(self.ledger.read_bytes(),before)
            value=self.invoke('run',run=124,confirmed_at='1970-01-01T00:16:30+00:00')
            self.assertTrue(value['handoff_verified']);self.assertEqual(value['consumed'],14)
            self.assertEqual(sum(a=='join' for a,_ in self.backend.actions),1)
            value=self.invoke('stop',run=125)
            self.assertTrue(value['cleanup_verified']);self.assertEqual(value['consumed'],14)

    def test_authority_and_prepared_receipt_drift_cannot_change_target(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.invoke('prepare')
            before=self.ledger.read_bytes()
            path=self.state/host.PREPARED_NAME
            record=json.loads(path.read_text());record['occurrence_id']='f'*32;path.write_text(json.dumps(record))
            with self.assertRaisesRegex(host.Refused,'teams_preparation_binding_rejected'):self.invoke('offline')
            self.assertEqual(self.ledger.read_bytes(),before)
            self.assertFalse((self.state/bridge.INTERLOCK).exists())

    def test_changed_authority_is_refused_before_any_new_preparation(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.invoke('prepare')
            self.backend.actions.clear()
            self.authority['source_revision']=4
            original=self.backend.view
            def view(ident):
                value=original(ident)
                if ident=='a'*32:value['revision']=4
                return value
            self.backend.view=view
            with self.assertRaisesRegex(host.Refused,'teams_preparation_receipt_conflict'):self.invoke('prepare')
            self.assertEqual(self.backend.actions,[])
