"""Case selection retains the real global ledger and cleanup ownership seam."""
import json
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy import teams_acceptance as host, qa_cloud_bridge as bridge, qa_cloud_contract as contract
import test_teams_preparation_host as preparation_fixture
import test_teams_acceptance_host as acceptance_fixture


class CaseHostTests(unittest.TestCase):
    def setup(self, root):
        preparation_fixture.HostPreparationTests.setup(self, root)

    def invoke(self, mode, case_id, run=123, confirmed_at=None):
        return host.perform(mode, backend=self.backend, bridge=bridge, contract=contract,
            state=self.state, qa_authority=self.qa, meeting_authority=self.authority,
            run=run, attempt=1, source=contract.BASE_SHA, image=contract.IMAGE,
            build_run=41022272702, build_attempt=1, clock=lambda:1000,
            monotonic=lambda:self.backend.elapsed, sleep=self.backend.sleep,
            authority_loader=lambda state,path:bridge.authority(state,path,owner=__import__('os').getuid()),
            confirmed_at=confirmed_at, case_id=case_id)

    def test_case_preparation_preserves_legacy_receipt_and_separates_request_binding(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root)
            legacy=self.state/host.PREPARED_NAME
            legacy.write_text('legacy-receipt-must-not-change');legacy.chmod(0o600)
            before=self.ledger.read_bytes()
            self.invoke('prepare','case-a')
            request_a=self.backend.preparation_request
            self.invoke('prepare','case-b',run=124)
            self.assertNotEqual(request_a,self.backend.preparation_request)
            self.assertEqual(legacy.read_text(),'legacy-receipt-must-not-change')
            self.assertEqual(self.ledger.read_bytes(),before)
            self.assertEqual(len(list(self.state.glob('teams-case-*.prepared.json'))),2)
            self.assertEqual(self.backend.maintenance_calls,[])

    def test_wrong_case_and_legacy_stop_leave_global_owned_run_unknown_and_untouched(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);self.invoke('prepare','case-a')
            self.invoke('run','case-a',run=124,confirmed_at='1970-01-01T00:16:30+00:00')
            marker=self.state/bridge.INTERLOCK
            before=marker.read_bytes();ledger=self.ledger.read_bytes()
            self.backend.actions.clear();self.backend.maintenance_calls.clear()
            for wrong in ('case-b',None):
                with self.subTest(wrong=wrong),self.assertRaisesRegex(host.Refused,'teams_owned_run_rejected'):
                    self.invoke('stop',wrong,run=125)
                self.assertEqual(marker.read_bytes(),before)
                self.assertEqual(self.ledger.read_bytes(),ledger)
                self.assertEqual(self.backend.actions,[])
                self.assertEqual(self.backend.maintenance_calls,[])
            result=self.invoke('stop','case-a',run=125)
            self.assertTrue(result['cleanup_verified'])
            self.assertFalse(marker.exists())

    def test_case_selector_cannot_escape_and_global_cleanup_and_quota_still_block(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root);before=self.ledger.read_bytes()
            for value in ('../other','/tmp/other','CaseA','',True,'a'*49,'a/b','a\n'):
                with self.subTest(value=value),self.assertRaisesRegex(host.Refused,'teams_case_rejected'):
                    self.invoke('prepare',value)
            marker=self.state/bridge.INTERLOCK;marker.write_text('original-unknown');marker.chmod(0o600)
            with self.assertRaisesRegex(host.Refused,'qa_cleanup_requires_reconciliation'):
                self.invoke('prepare','case-a')
            self.assertEqual(marker.read_text(),'original-unknown')
            self.assertEqual(self.ledger.read_bytes(),before)
            self.assertEqual(self.backend.actions,[])

    def test_case_and_legacy_share_both_global_locks(self):
        with tempfile.TemporaryDirectory() as root:
            self.setup(root)
            for name in ('controller.lock','existing-qa.lock'):
                with self.subTest(name=name),bridge.existing_lock(self.state/name):
                    with self.assertRaisesRegex(bridge.Refused,'qa_operation_in_progress'):
                        self.invoke('prepare','case-a')
            self.assertEqual(self.backend.actions,[])

    def test_scoped_cli_accepts_only_fixed_case_selector(self):
        self.assertEqual(host.operation_arguments(['prepare','--case','case-a']),('prepare','case-a'))
        self.assertEqual(host.operation_arguments(['stop']),('stop',None))
        for args in (['run','--case','../x'],['run','--authority','/tmp/x'],['run','--case','a','extra'],['bad']):
            with self.subTest(args=args),self.assertRaises(host.Refused):host.operation_arguments(args)

    def test_scheduled_pending_authority_uses_case_bound_correlation(self):
        requests=[]
        for case_id in ('case-a','case-b'):
            with tempfile.TemporaryDirectory() as root:
                acceptance_fixture.HostAcceptanceTests.setup(self,root)
                self.invoke('offline',case_id)
                self.invoke('run',case_id,confirmed_at='1970-01-01T00:16:30+00:00')
                requests.append(self.backend.request)
                self.assertTrue(self.invoke('stop',case_id,run=124)['cleanup_verified'])
        self.assertNotEqual(*requests)

    def test_case_authority_rejects_operator_owned_directory_and_symlinks(self):
        with tempfile.TemporaryDirectory() as root:
            from pathlib import Path
            directory=Path(root)/'cases';directory.mkdir(mode=0o700)
            with patch.object(host,'CASE_DIRECTORY',directory):
                with self.assertRaises(bridge.Refused):host.load_meeting_authority(bridge,'case-a')
            link=Path(root)/'linked';link.symlink_to(directory,target_is_directory=True)
            with patch.object(host,'CASE_DIRECTORY',link):
                with self.assertRaises(bridge.Refused):host.load_meeting_authority(bridge,'case-a')

    def test_scheduled_case_requires_actual_fresh_presence_before_reserving(self):
        with tempfile.TemporaryDirectory() as root:
            acceptance_fixture.HostAcceptanceTests.setup(self,root)
            before=self.ledger.read_bytes()
            for stamp in (None,'','1970-01-01T00:10:00+00:00','1970-01-01T00:17:00+00:00'):
                with self.subTest(stamp=stamp),self.assertRaisesRegex(host.Refused,'teams_fresh_presence_required'):
                    self.invoke('run','case-a',confirmed_at=stamp)
                self.assertEqual(self.ledger.read_bytes(),before)
                self.assertEqual(self.backend.actions,[])

    def test_fresh_presence_cannot_revive_expired_root_authority(self):
        with tempfile.TemporaryDirectory() as root:
            acceptance_fixture.HostAcceptanceTests.setup(self,root)
            self.authority['participant_confirmed_until']=999
            original=copy.deepcopy(self.authority)
            before=self.ledger.read_bytes()
            with self.assertRaisesRegex(host.Refused,'teams_acceptance_authority_rejected'):
                self.invoke('run','case-a',confirmed_at='1970-01-01T00:16:30+00:00')
            self.assertEqual(self.authority,original)
            self.assertEqual(self.ledger.read_bytes(),before)
            self.assertEqual(self.backend.actions,[])
            self.assertEqual(self.backend.maintenance_calls,[])

    def test_authority_deadline_is_independent_finite_and_strictly_unexpired(self):
        for deadline in (999,1000,1300.000001,float('nan'),float('inf'),True,None,'1100'):
            with self.subTest(deadline=deadline),tempfile.TemporaryDirectory() as root:
                acceptance_fixture.HostAcceptanceTests.setup(self,root)
                self.authority['participant_confirmed_until']=deadline
                before=self.ledger.read_bytes()
                with self.assertRaisesRegex(host.Refused,'teams_acceptance_authority_rejected'):
                    self.invoke('run','case-a',confirmed_at='1970-01-01T00:16:40+00:00')
                self.assertEqual(self.ledger.read_bytes(),before)
                self.assertEqual(self.backend.actions,[])
                self.assertEqual(self.backend.maintenance_calls,[])

    def test_valid_authority_cannot_replace_expired_future_or_invalid_presence(self):
        for presence in ('1970-01-01T00:11:40+00:00',
                '1970-01-01T00:16:40.000001+00:00','1970-01-01T00:16:40',
                'not-a-time',None,True,{},[],float('nan')):
            with self.subTest(presence=presence),tempfile.TemporaryDirectory() as root:
                acceptance_fixture.HostAcceptanceTests.setup(self,root)
                original=copy.deepcopy(self.authority)
                before=self.ledger.read_bytes()
                with self.assertRaisesRegex(host.Refused,'teams_fresh_presence_required'):
                    self.invoke('run','case-a',confirmed_at=presence)
                self.assertEqual(self.authority,original)
                self.assertEqual(self.ledger.read_bytes(),before)
                self.assertEqual(self.backend.actions,[])
                self.assertEqual(self.backend.maintenance_calls,[])

    def test_both_independent_boundaries_admit_without_changing_operator_input(self):
        for deadline in (1000.000001,1300):
            for presence in ('1970-01-01T00:11:40.000001+00:00','1970-01-01T00:16:40+00:00'):
                with self.subTest(deadline=deadline,presence=presence),tempfile.TemporaryDirectory() as root:
                    acceptance_fixture.HostAcceptanceTests.setup(self,root)
                    self.authority['participant_confirmed_until']=deadline
                    original=copy.deepcopy(self.authority)
                    value=self.invoke('run','case-a',confirmed_at=presence)
                    self.assertTrue(value['handoff_verified'])
                    self.assertEqual(self.authority,original)
                    self.assertEqual(sum(a=='join' for a,_ in self.backend.actions),1)
                    self.assertTrue(self.invoke('stop','case-a',run=124)['cleanup_verified'])

    def test_case_loader_requires_root_file_owner_and_rejects_leaf_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root)
            authority=directory/'case-a.json'
            authority.write_text('{}');authority.chmod(0o600)
            # This fixture supplies only the trusted directory seam. The real
            # fixed-file validator still checks owner=0 and O_NOFOLLOW.
            with patch.object(host,'CASE_DIRECTORY',directory),patch.object(bridge,'directory') as checked:
                with self.assertRaisesRegex(bridge.Refused,'qa_untrusted_file'):
                    host.load_meeting_authority(bridge,'case-a')
                checked.assert_called_once_with(directory,owner=0)
                authority.unlink();authority.symlink_to(directory/'not-an-authority')
                with self.assertRaises(OSError):host.load_meeting_authority(bridge,'case-a')

    def test_case_loader_selects_exact_file_and_keeps_real_permissions_checks(self):
        class FixturePrincipal:
            def directory(self,path,*,owner):
                if owner!=0:raise AssertionError('root owner must be requested')
                return bridge.directory(path,owner=os.getuid())
            def fixed_file(self,path,*,owner,maximum):
                if owner!=0:raise AssertionError('root owner must be requested')
                return bridge.fixed_file(path,owner=os.getuid(),maximum=maximum)
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root)
            (directory/'case-a.json').write_text('{"fixture":"a"}')
            other=directory/'case-b.json';other.write_text('{"fixture":"b"}')
            with patch.object(host,'CASE_DIRECTORY',directory):
                self.assertEqual(host.load_meeting_authority(FixturePrincipal(),'case-a'),{'fixture':'a'})
                self.assertEqual(host.load_meeting_authority(FixturePrincipal(),'case-b'),{'fixture':'b'})
                other.chmod(0o666)
                with self.assertRaisesRegex(bridge.Refused,'qa_untrusted_file'):
                    host.load_meeting_authority(FixturePrincipal(),'case-b')
                directory.chmod(0o777)
                with self.assertRaisesRegex(bridge.Refused,'qa_untrusted_directory'):
                    host.load_meeting_authority(FixturePrincipal(),'case-a')


if __name__=='__main__':unittest.main()
