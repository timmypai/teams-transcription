"""Fixed cloud bridge through real private files/locks and a controlled Docker seam."""
import base64
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy import qa_cloud_bridge as bridge, qa_cloud_contract as contract, qa_container_entry as entry
from test_cloud_qa_contract import CloudQAContractTests
from qa_c409_fixture import run_c409

ROOT=Path(__file__).resolve().parents[1]


class DockerFixture:
    """External Docker boundary; no provider/browser/process is started."""
    def __init__(self):
        self.calls=[];self.value=CloudQAContractTests().runtime()
        self.result={'success':False,'result':{'candidate_sha':contract.CANDIDATE_SHA,'accepted':False,'owned_cleanup_verified':True,
            'checks':[{'audio_rx_verified':True}],'observer':{}}}
        self.failure=None;self.ledger=None
    def runtime(self):return copy.deepcopy(self.value)
    def validate_runtime(self,value):contract.validate_runtime(value)
    def execute(self,mode,run,attempt):
        self.calls.append(mode)
        if mode=='offline':return {'success':True,'candidate_verified':True,'base_verified':True,
            'model_started':False,'modules':3,'candidate_sha':contract.CANDIDATE_SHA}
        # Verify admission is already durable before any possible model start.
        ledger=json.loads(self.ledger.read_text())
        if ledger['consumed']!=8 or ledger['entries'][-1]['status']!='reserved':
            raise AssertionError('start happened without durable reservation')
        if not (self.ledger.parent/bridge.INTERLOCK).exists():
            raise AssertionError('start happened without persistent interlock')
        if self.failure:raise self.failure
        return self.result


class CloudQABridgeTests(unittest.TestCase):
    def setup_state(self,root):
        state=Path(root);(state/'controller.lock').touch(mode=0o600)
        ledger=state/'existing-ledger.json';ledger.write_text(json.dumps(CloudQAContractTests().ledger()));ledger.chmod(0o600)
        (state/'existing-qa.lock').touch(mode=0o600)
        config=state/'operator-authority.json';config.write_text(json.dumps({
            'ledger_path':str(ledger),'lock_path':str(state/'existing-qa.lock'),
            'checkpoint_sha256':'a'*64,'legacy_writers_coordinated':True}))
        return state,ledger,config
    def invoke(self,mode,backend,state,config,**kw):
        return bridge.perform(mode,backend=backend,state=state,run_id=123,attempt=1,
            authority_path=config,contract=contract,
            authority_loader=lambda state,path:bridge.authority(state,path,owner=os.getuid()),**kw)

    def test_offline_checks_fixed_candidate_without_requiring_or_touching_quota_authority(self):
        with tempfile.TemporaryDirectory() as root:
            state=Path(root);(state/'controller.lock').touch(mode=0o600)
            backend=DockerFixture();result=self.invoke('offline',backend,state,None)
            self.assertEqual(backend.calls,['offline'])
            self.assertFalse(result['model_started']);self.assertFalse(result['ledger_touched'])
            self.assertEqual([p.name for p in state.iterdir()],['controller.lock'])

    def test_observe_reserves_before_start_keeps_failed_qa_false_and_commits_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=self.setup_state(root);backend=DockerFixture();backend.ledger=ledger
            original=ledger.read_bytes();result=self.invoke('observe',backend,state,config,authorized=True)
            self.assertEqual(backend.calls,['offline','observe']);self.assertFalse(result['success'])
            self.assertFalse(result['result']['accepted']);self.assertTrue(result['cleanup_verified'])
            saved=json.loads(ledger.read_text());self.assertEqual(saved['consumed'],8)
            self.assertEqual(saved['entries'][-1]['status'],'cleaned')
            self.assertFalse((state/bridge.INTERLOCK).exists())
            self.assertEqual((state/'qa-ledger-history'/(hashlib.sha256(original).hexdigest()+'.json')).read_bytes(),original)
            # A rerun never consumes/starts again, even after clean completion.
            with self.assertRaisesRegex(bridge.Refused,'qa_run_already_reserved'):
                self.invoke('observe',backend,state,config,authorized=True)
            self.assertEqual(backend.calls.count('observe'),1)

    def test_uncertain_child_retains_consumed_slot_interlock_and_blocks_another_start(self):
        for outcome in ('raise','unknown_cleanup'):
            with self.subTest(outcome=outcome),tempfile.TemporaryDirectory() as root:
                state,ledger,config=self.setup_state(root);backend=DockerFixture();backend.ledger=ledger
                if outcome=='raise':backend.failure=bridge.Refused('qa_child_outcome_unknown')
                else:backend.result={'result':{'candidate_sha':contract.CANDIDATE_SHA,'owned_cleanup_verified':False}}
                with self.assertRaises(bridge.Refused):self.invoke('observe',backend,state,config,authorized=True)
                self.assertEqual(json.loads(ledger.read_text())['consumed'],8)
                self.assertEqual(json.loads(ledger.read_text())['entries'][-1]['status'],'reserved')
                self.assertTrue((state/bridge.INTERLOCK).exists())
                with self.assertRaisesRegex(bridge.Refused,'qa_cleanup_requires_reconciliation'):
                    self.invoke('observe',backend,state,config,authorized=True)
                self.assertEqual(backend.calls.count('observe'),1)

    def test_missing_authority_and_unapproved_or_busy_host_never_start_or_reset_ledger(self):
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=self.setup_state(root);backend=DockerFixture();backend.ledger=ledger
            original=ledger.read_bytes()
            with self.assertRaisesRegex(bridge.Refused,'qa_live_not_authorized'):
                self.invoke('observe',backend,state,config)
            with bridge.existing_lock(state/'controller.lock'):
                with self.assertRaisesRegex(bridge.Refused,'qa_operation_in_progress'):
                    self.invoke('offline',backend,state,config)
            with bridge.existing_lock(state/'existing-qa.lock'):
                with self.assertRaisesRegex(bridge.Refused,'qa_operation_in_progress'):
                    self.invoke('observe',backend,state,config,authorized=True)
            config.unlink()
            with self.assertRaises(FileNotFoundError):self.invoke('observe',backend,state,config,authorized=True)
            backend.value['database']['maintenance']=False
            with self.assertRaisesRegex(ValueError,'runtime_contract_rejected'):
                self.invoke('offline',backend,state,None)
            self.assertEqual(ledger.read_bytes(),original);self.assertNotIn('observe',backend.calls)

    def test_two_session_candidate_reserves_two_global_slots_without_mixing_experiments(self):
        ledger=CloudQAContractTests().ledger()
        reserved=bridge.reserve(ledger,run_id=99,attempt=1,starts=2,experiment='isolated',
            candidate_sha='836ecbe88d69b14a66326474d08a4c26d15c1e90')
        self.assertEqual(reserved['consumed'],9);self.assertEqual(ledger['consumed'],7)
        self.assertEqual([r['slot'] for r in reserved['entries']],[8,9])
        self.assertEqual([r['start_index'] for r in reserved['entries']],[1,2])
        with self.assertRaisesRegex(ValueError,'ledger_unresolved_attempt'):
            contract.preflight(CloudQAContractTests().runtime(),reserved,'a'*64,maximum_native_starts=2)
        for row in reserved['entries']:row['status']='cleaned'
        self.assertEqual(contract.preflight(CloudQAContractTests().runtime(),reserved,'a'*64,
            maximum_native_starts=2)['remaining'],41)
        with self.assertRaisesRegex(bridge.Refused,'qa_reservation_rejected'):
            bridge.reserve(ledger,run_id=99,attempt=1,starts=1,experiment='isolated',candidate_sha=contract.CANDIDATE_SHA)

    def test_symlink_or_fifo_or_public_private_files_are_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)/'file';path.write_bytes(b'PRIVATE');path.chmod(0o644)
            with self.assertRaises(bridge.Refused):bridge.fixed_file(path,owner=os.getuid(),private=True)
            path.unlink();os.mkfifo(path,0o600)
            with self.assertRaises(bridge.Refused):bridge.fixed_file(path,owner=os.getuid(),private=True)
            path.unlink();path.symlink_to('/dev/null')
            with self.assertRaises(OSError):bridge.fixed_file(path,owner=os.getuid(),private=True)

    def test_controller_mutations_are_blocked_by_qa_unknown_cleanup_even_after_cli_exit(self):
        spec=importlib.util.spec_from_file_location('qa_controller_test',ROOT/'deploy/stagingctl.py')
        staging=importlib.util.module_from_spec(spec);spec.loader.exec_module(staging)
        with tempfile.TemporaryDirectory() as root,patch.object(staging,'STATE',Path(root)):
            (Path(root)/bridge.INTERLOCK).write_text('{}')
            for operation in ('deploy','rollback','recover-first-deploy','diagnose-create'):
                with self.assertRaisesRegex(staging.Refused,'qa_cleanup_requires_reconciliation'):
                    staging.operate(object(),operation,'',{})

    def test_real_candidate_offline_container_protocol_checks_hashes_and_never_runs_live(self):
        request={'operation':'offline','run_id':123,'run_attempt':1,'files':{}}
        for name in entry.FILES:
            path=ROOT/('teams_transcription' if name.startswith('qa_audio_') else
                'evidence' if name==bridge.MANIFEST else 'tools' if name=='check_qa_audio_candidate.py' else 'deploy')/name
            request['files'][name]=base64.b64encode(path.read_bytes()).decode()
        with tempfile.TemporaryDirectory() as root:
            command=[sys.executable,'-c',
                'from pathlib import Path; import json,sys; from deploy.qa_container_entry import run; '
                'print(json.dumps(run(json.load(sys.stdin),data_root=Path(sys.argv[1]))))',root]
            process=run_c409(command[2],args=command[3:],input=json.dumps(request),timeout=10)
            self.assertEqual(process.returncode,0,process.stderr)
            result=json.loads(process.stdout)
            self.assertTrue(result['candidate_verified']);self.assertFalse(result['model_started'])
            self.assertFalse((Path(root)/'qa-cloud/123-1-offline/private-qa.json').exists())
            repeated=run_c409(command[2],args=command[3:],input=json.dumps(request),timeout=10)
            self.assertNotEqual(repeated.returncode,0)
        request['files']['qa_audio_tap.py']=base64.b64encode(b'raise AssertionError("must not execute")').decode()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError,'qa_entry_hash_rejected'):entry.run(request,data_root=Path(root))
            self.assertEqual(list(Path(root).iterdir()),[])

    def test_container_cli_rejects_private_invalid_input_without_any_raw_stdout_or_stderr(self):
        for raw in ('PRIVATE refresh_token 王小明', 'PRIVATE'*160000):
            process=subprocess.run([sys.executable,str(ROOT/'deploy/qa_container_entry.py')],
                input=raw,text=True,capture_output=True,timeout=5)
            self.assertEqual(process.returncode,1)
            self.assertEqual(json.loads(process.stdout),{'success':False,
                'error_code':'qa_entry_failed','cleanup_verified':False})
            self.assertEqual(process.stderr,'')
            self.assertNotIn('PRIVATE',process.stdout)


if __name__=='__main__':unittest.main()
