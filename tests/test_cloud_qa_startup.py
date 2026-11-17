"""New runtime source boundary; no browser, model, host or release fabrication."""
import base64
import copy
import contextlib
import hashlib
import io
import json
import os
import re
from pathlib import Path
import subprocess
import shutil
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from deploy import qa_cloud_contract as contract
from deploy import qa_cloud_bridge as bridge
from test_cloud_qa_semantic import semantic_raw
from qa_c409_fixture import run_c409

ROOT=Path(__file__).resolve().parents[1]


def request(mode='semantic-startup-v1-offline', *, pending=False):
    files={name:base64.b64encode((ROOT/'deploy/qa_semantic_candidate'/name).read_bytes()).decode()
        for name in contract.SEMANTIC_FILES}
    files['semantic-startup-candidate-v1.json']=base64.b64encode(
        (ROOT/'evidence/semantic-startup-candidate-v1.json').read_bytes()).decode()
    source=(ROOT/'deploy/qa_cloud_contract.py').read_text()
    if pending:
        source=re.sub(r'^STARTUP_RELEASE_SOURCE_SHA=.*$', 'STARTUP_RELEASE_SOURCE_SHA=None',source,flags=re.M)
    files['qa_cloud_contract.py']=base64.b64encode(source.encode()).decode()
    return dict(operation=mode,run_id=123,run_attempt=1,authorized=False,files=files)


@contextlib.contextmanager
def pending_release():
    with patch.multiple(contract.StartupProfile,BASE_SHA=None,IMAGE=None), \
            patch.multiple(contract,STARTUP_BUILD_RUN_ID=None,STARTUP_BUILD_RUN_ATTEMPT=None):
        yield


class StartupRuntimeTests(unittest.TestCase):
    def test_fixed_release_bridge_offline_and_two_slot_lifecycle_without_external_io(self):
        from test_cloud_qa_bridge import CloudQABridgeTests
        from test_cloud_qa_contract import CloudQAContractTests
        startup=contract.startup_profile()
        class Backend:
            calls=[]
            def runtime(self):
                value=CloudQAContractTests().runtime()
                value['image']=value['container']['image']=startup.IMAGE
                value['release'].update(source_sha=startup.BASE_SHA,
                    build_run_id=41159965976,build_run_attempt=1)
                return value
            def validate_runtime(self,value):startup.validate_runtime(value)
            def execute(self,mode,run,attempt):
                self.calls.append(mode)
                if mode=='semantic-startup-v1-offline':
                    return dict(success=True,candidate_verified=True,base_verified=True,model_started=False,
                        modules=3,candidate_sha=startup.SEMANTIC_SHA,
                        criterion_version=startup.SEMANTIC_VERSION,candidate_manifest_sha256=startup.SEMANTIC_MANIFEST_SHA256)
                self.assert_reserved()
                return {'result':startup.public_semantic_result(semantic_raw(),run_id=run,run_attempt=attempt)}
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=CloudQABridgeTests().setup_state(root)
            original=ledger.read_bytes();backend=Backend()
            def assert_reserved():
                value=json.loads(ledger.read_text())
                self.assertEqual(value['consumed'],9)
                self.assertEqual([row['status'] for row in value['entries']],['reserved','reserved'])
                self.assertEqual(json.loads((state/bridge.INTERLOCK).read_text())['reserved'],2)
            backend.assert_reserved=assert_reserved
            def invoke(mode):
                return bridge.perform(mode,backend=backend,state=state,run_id=123,attempt=1,
                    authorized=True,contract=contract,authority_path=config,
                    authority_loader=lambda state,path:bridge.authority(state,path,owner=os.getuid()))
            offline=invoke('semantic-startup-v1-offline')
            self.assertFalse(offline['ledger_touched'] or offline['model_started'] or offline['live_ready'])
            self.assertEqual(ledger.read_bytes(),original)
            live=invoke('semantic-startup-v1-isolated')
            self.assertTrue(live['success'] and live['cleanup_verified'])
            self.assertEqual(live['consumed'],9)
            saved=json.loads(ledger.read_text())
            self.assertEqual(saved['checkpoint'],json.loads(original)['checkpoint'])
            self.assertEqual([row['status'] for row in saved['entries']],['cleaned','cleaned'])
            self.assertFalse((state/bridge.INTERLOCK).exists())
            with self.assertRaisesRegex(bridge.Refused,'qa_run_already_reserved'):
                invoke('semantic-startup-v1-isolated')
            self.assertEqual(backend.calls.count('semantic-startup-v1-isolated'),1)

    def test_verified_release_admits_only_exact_runtime_without_starting_model(self):
        from test_cloud_qa_contract import CloudQAContractTests
        startup=contract.startup_profile()
        runtime=CloudQAContractTests().runtime()
        runtime.update(image='ghcr.io/paulpai0412/teams-transcription@sha256:57a433bdc059e462100f139466251e4b9670a46770899f6d3b1c9caf9597f657')
        runtime['container']['image']=runtime['image']
        runtime['release'].update(source_sha='c9d54b7f72be1c94318386ce7dab7a61df4396e3',
            build_run_id=41159965976,build_run_attempt=1)
        result=startup.preflight(runtime,CloudQAContractTests().ledger(),'a'*64)
        self.assertTrue(startup.release_ready())
        self.assertFalse(result['live_ready'] or result['model_started'])
        self.assertEqual(result['remaining'],43)
        for group,key,value in (('', 'image',contract.IMAGE),('container','image',contract.IMAGE),
                ('release','source_sha',contract.BASE_SHA),('release','build_run_id',41159965977),
                ('release','build_run_attempt',2),('release','verified_release_record',False)):
            wrong=copy.deepcopy(runtime);(wrong[group] if group else wrong)[key]=value
            with self.subTest(group=group,key=key),self.assertRaisesRegex(ValueError,'runtime_contract_rejected'):
                startup.validate_runtime(wrong)
        projected=startup.public_semantic_result(semantic_raw(),run_id=123,run_attempt=1)
        self.assertTrue(projected['accepted'] and projected['release_ready'])
        self.assertEqual(projected['base_source_sha'],runtime['release']['source_sha'])
        self.assertEqual(projected['image'],runtime['image'])

    @pending_release()
    def test_unknown_release_stays_closed_even_with_arbitrary_runtime(self):
        startup = contract.startup_profile()
        self.assertIsNone(startup.IMAGE)
        self.assertIsNone(startup.BASE_SHA)
        with self.assertRaisesRegex(ValueError, 'release_not_ready'):
            startup.validate_runtime({'image': 'attacker-selected-image'})

    def test_real_offline_loader_verifies_new_bytes_without_a_or_model(self):
        script='''import importlib.abc,json,sys
from pathlib import Path
class NoA(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith('teams_transcription.qa_audio'):raise AssertionError('A import')
sys.meta_path.insert(0,NoA())
from deploy.qa_container_entry import run
print(json.dumps(run(json.load(sys.stdin),data_root=Path(sys.argv[1]))))'''
        with tempfile.TemporaryDirectory() as root:
            result=run_c409(script,args=(root,),input=json.dumps(request()),timeout=15,startup=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertTrue(value['candidate_verified'] and value['base_verified'])
        self.assertFalse(value['model_started'])
        self.assertEqual(value['candidate_sha'],'e82bb30900e90c9559b9c3dcb71e532cad86b72e')
        self.assertEqual(value['modules'],3)
        self.assertFalse(value['release_ready'])

    @pending_release()
    def test_new_route_keeps_two_slot_identity_and_unknown_release_never_touches_ledger(self):
        startup=contract.startup_profile()
        plan=bridge.operation_plan('semantic-startup-v1-isolated',startup)
        self.assertEqual(plan,('semantic-startup-isolated',True,2,
            'e82bb30900e90c9559b9c3dcb71e532cad86b72e','semantic-startup-v1-offline',3))
        from test_cloud_qa_bridge import CloudQABridgeTests
        from test_cloud_qa_semantic import SemanticDockerFixture
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=CloudQABridgeTests().setup_state(root)
            old=ledger.read_bytes();backend=SemanticDockerFixture();backend.ledger=ledger
            with patch.object(backend,'runtime',side_effect=AssertionError('runtime must not be probed')) as runtime, \
                    self.assertRaisesRegex(ValueError,'release_not_ready'):
                bridge.perform('semantic-startup-v1-isolated',backend=backend,state=state,
                    run_id=123,attempt=1,authorized=True,contract=contract,authority_path=config)
            runtime.assert_not_called()
            self.assertEqual(ledger.read_bytes(),old)
            self.assertFalse((state/bridge.INTERLOCK).exists())
            self.assertEqual(backend.calls,[])

    def test_old_route_still_rejects_new_physical_base(self):
        from test_cloud_qa_semantic import SemanticRuntimeTests
        script='''import json,sys
from pathlib import Path
from deploy.qa_container_entry import run
run(json.load(sys.stdin),data_root=Path(sys.argv[1]))'''
        with tempfile.TemporaryDirectory() as root:
            result=subprocess.run([sys.executable,'-c',script,root],cwd=ROOT,
                input=json.dumps(SemanticRuntimeTests().request()),capture_output=True,text=True,timeout=15)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('qa_entry_base_rejected',result.stderr)
            self.assertEqual(list(Path(root).iterdir()),[])

    def test_modified_manifest_or_module_and_cross_version_files_are_rejected_before_load(self):
        from deploy.qa_container_entry import run
        for name in ('semantic-startup-candidate-v1.json','qa_content.py'):
            value=request();value['files'][name]=base64.b64encode(b'raise AssertionError("no execution")').decode()
            with self.subTest(name=name),tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(ValueError,'qa_entry_hash_rejected'):
                    run(value,data_root=Path(root))
                self.assertEqual(list(Path(root).iterdir()),[])
        value=request();value['files']['semantic-isolated-candidate-v1.json']=value['files'].pop('semantic-startup-candidate-v1.json')
        with tempfile.TemporaryDirectory() as root,self.assertRaisesRegex(ValueError,'qa_entry_files_rejected'):
            run(value,data_root=Path(root))

    def test_actual_private_physical_mix_and_live_before_release_are_rejected(self):
        script='''import json,sys
from pathlib import Path
from deploy.qa_container_entry import run
run(json.load(sys.stdin),data_root=Path(sys.argv[1]))'''
        with tempfile.TemporaryDirectory() as root:
            private=Path(root);shutil.copytree(ROOT/'teams_transcription',private/'teams_transcription',
                ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            shutil.copytree(ROOT/'deploy',private/'deploy',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
            (private/'teams_transcription/b_live.py').write_bytes((ROOT/'tests/fixtures/qa-c4090ed/b_live.py.txt').read_bytes())
            target=private/'teams_transcription/b_browser_tx.py';target.write_bytes(target.read_bytes()+b'\n# mixed base\n')
            data=private/'data';data.mkdir()
            result=subprocess.run([sys.executable,'-c',script,str(data)],cwd=private,
                input=json.dumps(request()),capture_output=True,text=True,timeout=15)
            self.assertIn('qa_entry_base_rejected',result.stderr)
            self.assertNotEqual(result.returncode,0);self.assertEqual(list(data.iterdir()),[])
        value=request('semantic-startup-v1-isolated',pending=True);value['authorized']=True
        with tempfile.TemporaryDirectory() as root:
            result=run_c409(script,args=(root,),input=json.dumps(value),startup=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('release_not_ready',result.stderr)
            self.assertEqual(list(Path(root).iterdir()),[])

    def test_old_startup_candidate_rejects_new_native_cap_runtime_before_execution(self):
        from deploy.qa_container_entry import run
        with tempfile.TemporaryDirectory() as root,self.assertRaisesRegex(ValueError,'qa_entry_base_rejected'):
            run(request(),data_root=Path(root))

    @pending_release()
    def test_versioned_projection_is_two_stage_whitelisted_and_does_not_admit_pending_release(self):
        startup=contract.startup_profile();raw=semantic_raw()
        raw['secret']='PRIVATE';raw['checks'][0]['private_text']='PRIVATE'
        safe=startup.public_semantic_result(raw,run_id=123,run_attempt=1)
        self.assertFalse(safe['accepted']);self.assertFalse(safe['release_ready'])
        self.assertTrue(all(r['semantic_content_verified'] and r['context_delivery_verified'] for r in safe['checks']))
        self.assertNotIn('PRIVATE',json.dumps(safe))
        self.assertEqual(safe,startup.public_semantic_result(safe,run_id=123,run_attempt=1,projected=True))
        old=contract.public_semantic_result(raw,run_id=123,run_attempt=1)
        with self.assertRaisesRegex(ValueError,'startup_receipt_version_rejected'):
            startup.public_semantic_result(old,run_id=123,run_attempt=1,projected=True)
        with self.assertRaisesRegex(ValueError,'semantic_receipt_version_rejected'):
            contract.public_semantic_result(safe,run_id=123,run_attempt=1,projected=True)
        for key,value in (('runtime_source_sha','0'*40),('base_source_sha','0'*40),('image','arbitrary')):
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'startup_receipt_version_rejected'):
                startup.public_semantic_result({**safe,key:value},run_id=123,run_attempt=1,projected=True)
        raw['checks'][1]['source_version']=999
        rejected=startup.public_semantic_result(raw,run_id=123,run_attempt=1)
        self.assertFalse(rejected['checks'][1]['context_delivery_verified'])

    def test_two_slots_settle_together_without_refund_rerun_or_unknown_cleanup(self):
        startup=contract.startup_profile()
        ledger={'schema_version':1,'limit':50,'checkpoint':{'used':7,'through_case':7},
            'consumed':10,'entries':[{'slot':s,'run_id':s,'run_attempt':1,'status':'cleaned'} for s in (8,9,10)]}
        before=copy.deepcopy(ledger)
        reserved=bridge.reserve(ledger,run_id=123,attempt=1,starts=2,
            experiment='semantic-startup-isolated',candidate_sha=startup.SEMANTIC_SHA)
        self.assertEqual(ledger,before);self.assertEqual(reserved['consumed'],12)
        self.assertEqual(reserved['checkpoint'],before['checkpoint'])
        self.assertEqual(reserved['entries'][:3],before['entries'])
        safe=startup.public_semantic_result(semantic_raw(count=1,accepted=False),run_id=123,run_attempt=1)
        settled=bridge.settle_attempt(reserved,safe,run_id=123,attempt=1,
            experiment='semantic-startup-isolated',contract=startup)
        self.assertEqual(settled['consumed'],12)
        self.assertEqual([r['status'] for r in settled['entries'][-2:]],['cleaned','not_started'])
        with self.assertRaisesRegex(bridge.Refused,'qa_run_already_reserved'):
            bridge.reserve(settled,run_id=123,attempt=2,starts=2,
                experiment='semantic-startup-isolated',candidate_sha=startup.SEMANTIC_SHA)
        unknown=startup.public_semantic_result(semantic_raw(second_unknown=True),run_id=123,run_attempt=1)
        with self.assertRaisesRegex(bridge.Refused,'qa_cleanup_requires_reconciliation'):
            bridge.settle_attempt(reserved,unknown,run_id=123,attempt=1,
                experiment='semantic-startup-isolated',contract=startup)
        self.assertEqual([r['status'] for r in reserved['entries'][-2:]],['reserved','reserved'])
        with self.assertRaisesRegex(bridge.Refused,'qa_budget_exhausted'):
            bridge.reserve({**ledger,'consumed':49},run_id=456,attempt=1,starts=2,
                experiment='semantic-startup-isolated',candidate_sha=startup.SEMANTIC_SHA)

    def test_manifest_pins_source_preparation_and_same_three_semantic_files(self):
        manifest=json.loads((ROOT/'evidence/semantic-startup-candidate-v1.json').read_text())
        prepared=(ROOT/'evidence/no-plc-startup-runtime-base-v1.json').read_bytes()
        self.assertEqual(hashlib.sha256(prepared).hexdigest(),manifest['runtime_base_manifest_sha256'])
        self.assertEqual(manifest['physical_runtime_files'],json.loads(prepared)['physical_runtime_files'])
        self.assertEqual(manifest['files'],json.loads(prepared)['semantic_modules'])
        for name,want in manifest['files'].items():
            self.assertEqual(hashlib.sha256((ROOT/'deploy/qa_semantic_candidate'/name).read_bytes()).hexdigest(),want)

    def test_cli_preserves_only_fixed_pending_release_reason(self):
        env={'GITHUB_REPOSITORY':'paulpai0412/teams-transcription','GITHUB_EVENT_NAME':'workflow_dispatch',
            'GITHUB_REF':'refs/heads/main','GITHUB_WORKFLOW_REF':
            'paulpai0412/teams-transcription/.github/workflows/oracle-qa.yml@refs/heads/main',
            'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1'}
        staging=SimpleNamespace(registry_config=lambda _:contextlib.nullcontext({}),Controller=lambda _:None)
        for message,expected in (('release_not_ready','release_not_ready'),('PRIVATE','qa_bridge_failed')):
            with self.subTest(message=message),tempfile.TemporaryDirectory() as root, \
                    patch.object(bridge,'STATE',Path(root)),patch.object(bridge,'directory'), \
                    patch.object(bridge.os,'getuid',return_value=1002),patch.object(bridge.os,'umask'), \
                    patch.object(bridge.signal,'signal'),patch.dict(bridge.os.environ,env,clear=True), \
                    patch.object(sys,'argv',['qa-cloud-bridge','semantic-startup-v1-isolated']), \
                    patch.object(bridge,'installed_module',side_effect=[staging,contract]), \
                    patch.object(bridge,'DockerBackend'), \
                    patch.object(bridge,'perform',side_effect=ValueError(message)):
                output=io.StringIO()
                with contextlib.redirect_stdout(output):self.assertEqual(bridge.main(),1)
                value=json.loads(output.getvalue())
                self.assertEqual(value['error_code'],expected)
                self.assertNotIn('PRIVATE',output.getvalue())


if __name__ == '__main__':
    unittest.main()
