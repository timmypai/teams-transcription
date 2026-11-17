"""Separate fixed B cloud path: same persistent budget, two reserved serial slots."""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from deploy import qa_cloud_bridge as bridge, qa_cloud_contract as contract
from test_cloud_qa_bridge import CloudQABridgeTests, DockerFixture
from test_cloud_qa_contract import CloudQAContractTests

ROOT=Path(__file__).resolve().parents[1]
B_SHA='836ecbe88d69b14a66326474d08a4c26d15c1e90'


def clean_owned():
    return {'status':'cleaned','stop_status':'stopped','stop_ack_received':True,
        'stop_closed_received':True,'native_cleanup_status':'cleaned','reaped':True,
        'pending_operation':False,'live_processes':0,'pending_close_tasks':0}


def isolated_raw(*,count=2,accepted=True,second_unknown=False):
    rows=[]
    for number in range(1,count+1):
        owned=clean_owned()
        if second_unknown and number==2:owned['status']='unknown'
        rows.append({'session_number':number,'source_revision':number,
            'identity':{'thread_sha256':str(number)*64,'start_attempted':True,
                'startup_completed':True,'realtime_start_issued':True},
            'owned_cleanup':owned,'launch_elapsed_seconds':number,
            'completed_elapsed_seconds':number+1,'work_budget_seconds':120-number,
            'question_pcm_sha256':'a'*64,'context_deliveries':[]})
    return {'kind':'isolated_synthetic_native_qa','accepted':accepted,
        'failure':None if accepted else {'phase':'session_1','error_code':'session_probe_failed',
            'child_failure':{'message':'PRIVATE'}},'secondary_failure':None,
        'session_isolation_verified':count==2 and not second_unknown,
        'model_start_attempts':count,'model_start_limit':2,'native_start_count_complete':True,
        'observed_native_start_attempts':count,'sessions':rows,
        'owned_cleanup_verified':not second_unknown,'checks':[]}


class IsolatedDockerFixture(DockerFixture):
    def __init__(self,raw=None):
        super().__init__();self.raw=raw if raw is not None else isolated_raw()
    def execute(self,mode,run,attempt):
        self.calls.append(mode)
        if mode=='isolated-offline':return {'success':True,'candidate_verified':True,
            'base_verified':True,'model_started':False,'modules':2,'candidate_sha':B_SHA}
        if mode!='isolated':raise AssertionError('B must not enter A')
        ledger=json.loads(self.ledger.read_text())
        if ledger['consumed']!=9 or [r['status'] for r in ledger['entries'][-2:]]!=['reserved','reserved']:
            raise AssertionError('B started before both durable reservations')
        marker=json.loads((self.ledger.parent/bridge.INTERLOCK).read_text())
        if marker['reserved']!=2 or marker['candidate_sha']!=B_SHA:
            raise AssertionError('B missing own two-slot interlock')
        return {'success':self.raw['accepted'],'operation':'isolated',
            'result':contract.public_isolated_result(self.raw,run_id=run,run_attempt=attempt)}


class CloudQAIsolatedTests(unittest.TestCase):
    def invoke(self,mode,backend,state,config,*,attempt=1,authorized=True):
        return bridge.perform(mode,backend=backend,state=state,run_id=123,attempt=attempt,
            authority_path=config,contract=contract,authorized=authorized,
            authority_loader=lambda state,path:bridge.authority(state,path,owner=os.getuid()))

    def test_b_offline_needs_no_ledger_and_live_without_confirmation_starts_nothing(self):
        with tempfile.TemporaryDirectory() as root:
            state=Path(root);(state/'controller.lock').touch(mode=0o600)
            backend=IsolatedDockerFixture()
            result=self.invoke('isolated-offline',backend,state,None,authorized=False)
            self.assertFalse(result['model_started']);self.assertFalse(result['ledger_touched'])
            self.assertEqual(result['candidate_sha'],B_SHA)
            with self.assertRaisesRegex(bridge.Refused,'qa_live_not_authorized'):
                self.invoke('isolated',backend,state,None,authorized=False)
            self.assertEqual(backend.calls,['isolated-offline'])
            self.assertEqual([p.name for p in state.iterdir()],['controller.lock'])

    def test_b_success_settles_both_same_attempt_reservations_and_preserves_identity(self):
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=CloudQABridgeTests().setup_state(root)
            backend=IsolatedDockerFixture();backend.ledger=ledger
            result=self.invoke('isolated',backend,state,config)
            self.assertEqual(backend.calls,['isolated-offline','isolated'])
            self.assertEqual(result['result']['candidate_sha'],B_SHA)
            self.assertNotIn('observer',result['result'])
            self.assertTrue(result['success']);self.assertEqual(result['consumed'],9)
            saved=json.loads(ledger.read_text())
            self.assertEqual([r['status'] for r in saved['entries']],['cleaned','cleaned'])
            self.assertEqual([r['start_index'] for r in saved['entries']],[1,2])
            self.assertFalse((state/bridge.INTERLOCK).exists())
            with self.assertRaisesRegex(bridge.Refused,'qa_run_already_reserved'):
                self.invoke('isolated',backend,state,config,attempt=2)
            self.assertEqual(backend.calls.count('isolated'),1)

    def test_first_failure_is_preserved_unused_second_slot_settles_without_refund(self):
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=CloudQABridgeTests().setup_state(root)
            backend=IsolatedDockerFixture(isolated_raw(count=1,accepted=False));backend.ledger=ledger
            result=self.invoke('isolated',backend,state,config)
            self.assertFalse(result['success']);self.assertFalse(result['result']['accepted'])
            self.assertEqual(result['result']['failure']['error_code'],'session_probe_failed')
            self.assertEqual(result['result']['failure']['session_number'],1)
            saved=json.loads(ledger.read_text())
            self.assertEqual(saved['consumed'],9);self.assertEqual(result['remaining'],41)
            self.assertEqual([r['status'] for r in saved['entries']],['cleaned','not_started'])
            self.assertIs(saved['entries'][1]['native_start_attempted'],False)
            self.assertEqual(contract.preflight(backend.value,saved,'a'*64)['remaining'],41)
            self.assertNotIn('PRIVATE',json.dumps(result))

    def test_second_unknown_retains_both_reservations_and_blocks_reentry(self):
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=CloudQABridgeTests().setup_state(root)
            backend=IsolatedDockerFixture(isolated_raw(accepted=False,second_unknown=True));backend.ledger=ledger
            with self.assertRaisesRegex(bridge.Refused,'qa_cleanup_requires_reconciliation'):
                self.invoke('isolated',backend,state,config)
            self.assertEqual([r['status'] for r in json.loads(ledger.read_text())['entries']],['reserved','reserved'])
            self.assertTrue((state/bridge.INTERLOCK).exists())
            with self.assertRaisesRegex(bridge.Refused,'qa_cleanup_requires_reconciliation'):
                self.invoke('isolated',backend,state,config)
            self.assertEqual(backend.calls.count('isolated'),1)

    def test_aggregate_cleanup_flag_cannot_hide_missing_or_unclean_second_session(self):
        for mutate in (lambda r:r['sessions'][1]['owned_cleanup'].update(status='unknown'),
                lambda r:r.update(sessions=r['sessions'][:1]),
                lambda r:r.update(native_start_count_complete=False),
                lambda r:r.update(observed_native_start_attempts=0)):
            with self.subTest(mutate=mutate),tempfile.TemporaryDirectory() as root:
                state,ledger,config=CloudQABridgeTests().setup_state(root)
                raw=isolated_raw();mutate(raw)
                backend=IsolatedDockerFixture(raw);backend.ledger=ledger
                with self.assertRaisesRegex(bridge.Refused,'qa_cleanup_requires_reconciliation'):
                    self.invoke('isolated',backend,state,config)
                self.assertEqual([r['status'] for r in json.loads(ledger.read_text())['entries']],['reserved','reserved'])

    def test_b_public_projection_keeps_first_failure_but_drops_private_and_hostile_fields(self):
        raw=isolated_raw(count=1,accepted=False)
        raw['failure']['phase']={'token':'PRIVATE'}
        raw['sessions'][0]['identity']['refresh_token']='PRIVATE'
        raw['sessions'][0]['context_deliveries']=[{'snapshot_sha256':'a'*64,'payload_sha256':'b'*64,
            'source_version':3,'text':'PRIVATE','sdp':'PRIVATE'}]*100
        raw['secondary_failure']={'phase':'private_receipt','error_code':'private_evidence_write_failed',
            'exception_type':'PRIVATE'}
        result=contract.public_isolated_result(raw,run_id=123,run_attempt=1)
        self.assertEqual(result['candidate_sha'],B_SHA);self.assertNotIn('observer',result)
        self.assertEqual(result['failure']['error_code'],'session_probe_failed')
        self.assertEqual(result['secondary_failure']['error_code'],'private_evidence_write_failed')
        self.assertIsNone(result['failure']['session_number'])
        self.assertEqual(len(result['sessions'][0]['context_deliveries']),8)
        self.assertNotIn('PRIVATE',json.dumps(result));self.assertFalse(result['accepted'])
        self.assertLess(len(json.dumps(result).encode()),16384)

    def test_one_remaining_global_slot_refuses_b_before_any_session(self):
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=CloudQABridgeTests().setup_state(root)
            value=CloudQAContractTests().ledger();value['consumed']=49
            value['entries']=[{'slot':n,'run_id':n,'run_attempt':1,'status':'cleaned'} for n in range(8,50)]
            ledger.write_text(json.dumps(value));original=ledger.read_bytes()
            backend=IsolatedDockerFixture();backend.ledger=ledger
            with self.assertRaisesRegex(ValueError,'ledger_exhausted'):
                self.invoke('isolated',backend,state,config)
            self.assertEqual(backend.calls,['isolated-offline'])
            self.assertEqual(ledger.read_bytes(),original)

    def request(self,operation='isolated-offline'):
        files={}
        for name in ('qa_probe.py','qa_session_isolation.py'):
            files[name]=base64.b64encode((ROOT/'deploy/qa_isolated_candidate'/name).read_bytes()).decode()
        for name,path in [('isolated-qa-candidate-manifest-20261030.json',
                ROOT/'evidence/isolated-qa-candidate-manifest-20261030.json'),
                ('qa_cloud_contract.py',ROOT/'deploy/qa_cloud_contract.py')]:
            files[name]=base64.b64encode(path.read_bytes()).decode()
        return {'operation':operation,'run_id':123,'run_attempt':1,'authorized':True,'files':files}

    def test_fixed_b_offline_loader_and_live_route_never_import_a_observation(self):
        # Separate process: execute fixed B sources but replace asyncio.run at the
        # external model-session boundary; inspect then close its unstarted coroutine.
        script='''
import asyncio,importlib.abc,json,sys
from pathlib import Path
class NoA(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith('teams_transcription.qa_audio'): raise AssertionError('A import')
sys.meta_path.insert(0,NoA())
from deploy.qa_container_entry import run
request=json.load(sys.stdin)
if request['operation']=='isolated':
 def model_boundary(coroutine):
  args=coroutine.cr_frame.f_locals['args']
  assert args.session_mode=='isolated' and args.qa_start_stage=='question'
  assert coroutine.cr_code.co_name=='run_probe'
  coroutine.close()
  return {'accepted':False,'owned_cleanup_verified':False,'checks':[],'sessions':[],
   'model_start_attempts':0,'model_start_limit':2,'native_start_count_complete':False}
 asyncio.run=model_boundary
result=run(request,data_root=Path(sys.argv[1]))
assert not any(n.startswith('teams_transcription.qa_audio') for n in sys.modules)
print(json.dumps(result))
'''
        for mode in ('isolated-offline','isolated'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                from qa_c409_fixture import run_c409
                process=run_c409(script,args=(root,),input=json.dumps(self.request(mode)),timeout=10)
                self.assertEqual(process.returncode,0,process.stderr)
                result=json.loads(process.stdout)
                if mode=='isolated-offline':
                    self.assertFalse(result['model_started']);self.assertEqual(result['modules'],2)
                    self.assertEqual(result['candidate_sha'],B_SHA)
                else:self.assertEqual(result['result']['candidate_sha'],B_SHA)

    def test_wrong_second_file_hash_rejects_b_before_any_candidate_exec(self):
        from deploy.qa_container_entry import run
        request=self.request();request['files']['qa_session_isolation.py']=base64.b64encode(b'PRIVATE').decode()
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaisesRegex(ValueError,'qa_entry_hash_rejected'):run(request,data_root=Path(root))
            self.assertEqual(list(Path(root).iterdir()),[])


if __name__=='__main__':unittest.main()
