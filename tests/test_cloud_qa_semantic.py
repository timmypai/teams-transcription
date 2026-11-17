import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy import qa_cloud_bridge as bridge, qa_cloud_contract as contract
from test_cloud_qa_isolated import isolated_raw, IsolatedDockerFixture
from test_cloud_qa_bridge import CloudQABridgeTests
from qa_c409_fixture import run_c409

ROOT=Path(__file__).resolve().parents[1]


def semantic_raw(count=2,**kw):
    value=isolated_raw(count=count,**kw)
    value.update(schema_version=2,criterion_version='synthetic_current_fact_semantic_v1',qa_receipt_schema_version=3)
    value['checks']=[{key:True for key in ('scoped_tool_called','expected_fact','no_stale_fact','output_muted','audio_rx_verified')}
        for _ in range(count)]
    for index,row in enumerate(value['sessions'],1):
        row['context_deliveries']=[{'question_number':1,'source_version':index+1,
            'snapshot_sha256':'b'*64,'payload_sha256':'c'*64,'payload_bytes':100,
            'sent_bytes':115,'marker_returned':True}]
    for index,row in enumerate(value['checks'],1):
        row.update(source_revision=index,source_version=index+1,exact_source_quote=False,semantic_content_verified=True,semantic_content={
            'criterion_version':'synthetic_current_fact_semantic_v1','status':'pass',
            'reason':'complete_current_fact_equivalent','fact_type':'budget'})
    return value


class SemanticDockerFixture(IsolatedDockerFixture):
    def execute(self,mode,run,attempt):
        self.calls.append(mode)
        if mode=='semantic-v1-offline':
            return {'success':True,'candidate_verified':True,'base_verified':True,
                'model_started':False,'modules':3,'candidate_sha':contract.SEMANTIC_SHA,
                'criterion_version':contract.SEMANTIC_VERSION,'candidate_manifest_sha256':contract.SEMANTIC_MANIFEST_SHA256}
        if mode!='semantic-v1-isolated':raise AssertionError('semantic must not enter old A/B')
        ledger=json.loads(self.ledger.read_text())
        assert len(ledger['entries'])>=2 and all(row['status']=='reserved' for row in ledger['entries'][-2:])
        assert json.loads((self.ledger.parent/bridge.INTERLOCK).read_text())['candidate_sha']==contract.SEMANTIC_SHA
        return {'result':contract.public_semantic_result(self.raw,run_id=run,run_attempt=attempt)}


class SemanticRuntimeTests(unittest.TestCase):
    def request(self,operation='semantic-v1-offline'):
        files={name:base64.b64encode((ROOT/'deploy/qa_semantic_candidate'/name).read_bytes()).decode()
            for name in contract.SEMANTIC_FILES}
        files['semantic-isolated-candidate-v1.json']=base64.b64encode((ROOT/'evidence/semantic-isolated-candidate-v1.json').read_bytes()).decode()
        files['qa_cloud_contract.py']=base64.b64encode((ROOT/'deploy/qa_cloud_contract.py').read_bytes()).decode()
        return {'operation':operation,'run_id':123,'run_attempt':1,'authorized':True,'files':files}

    def invoke(self,backend,state,config,*,mode='semantic-v1-isolated',attempt=1,authorized=True):
        import os
        return bridge.perform(mode,backend=backend,state=state,authority_path=config,contract=contract,
            run_id=123,attempt=attempt,authorized=authorized,
            authority_loader=lambda state,path:bridge.authority(state,path,owner=os.getuid()))

    def test_projection_keeps_semantic_evidence_in_both_stages_and_rejects_wrong_version(self):
        raw=semantic_raw()
        result=contract.public_semantic_result(raw,run_id=123,run_attempt=1)
        self.assertTrue(result['accepted'])
        self.assertFalse(result['checks'][0]['exact_source_quote'])
        self.assertEqual(result,contract.public_semantic_result(result,run_id=123,run_attempt=1,projected=True))
        for key,value in [('criterion_version','old'),('qa_receipt_schema_version',2),('schema_version',1)]:
            with self.subTest(key=key),self.assertRaises(ValueError):
                contract.public_semantic_result({**raw,key:value},run_id=123,run_attempt=1)

    def test_unknown_or_hostile_semantics_cannot_accept_or_leak(self):
        for status in ('unknown','fail',{'token':'PRIVATE'}):
            raw=semantic_raw();raw['checks'][0]['semantic_content']['status']=status
            raw['checks'][0]['semantic_content']['raw_text']='PRIVATE'
            result=contract.public_semantic_result(raw,run_id=123,run_attempt=1)
            self.assertFalse(result['accepted']);self.assertNotIn('PRIVATE',json.dumps(result))

    def test_two_slots_settle_with_old_checkpoint_and_history_intact_no_rerun(self):
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=CloudQABridgeTests().setup_state(root)
            value=json.loads(ledger.read_text())
            value['entries']=[{'slot':slot,'run_id':900+slot,'run_attempt':1,'status':'cleaned'} for slot in (8,9,10)]
            value['consumed']=10;ledger.write_text(json.dumps(value));old=copy.deepcopy(value)
            backend=SemanticDockerFixture(semantic_raw());backend.ledger=ledger
            result=self.invoke(backend,state,config)
            self.assertTrue(result['success']);self.assertEqual(result['consumed'],12)
            saved=json.loads(ledger.read_text());self.assertEqual(saved['checkpoint'],old['checkpoint'])
            self.assertEqual(saved['entries'][:3],old['entries'])
            self.assertEqual([r['status'] for r in saved['entries'][-2:]],['cleaned','cleaned'])
            self.assertFalse((state/bridge.INTERLOCK).exists())
            with self.assertRaisesRegex(bridge.Refused,'qa_run_already_reserved'):
                self.invoke(backend,state,config,attempt=2)
            self.assertEqual(backend.calls.count('semantic-v1-isolated'),1)

    def test_first_failure_consumes_two_without_refund_and_unknown_cleanup_blocks(self):
        for count,unknown in ((1,False),(2,True)):
            with self.subTest(count=count),tempfile.TemporaryDirectory() as root:
                state,ledger,config=CloudQABridgeTests().setup_state(root)
                backend=SemanticDockerFixture(semantic_raw(count=count,accepted=False,second_unknown=unknown));backend.ledger=ledger
                if unknown:
                    with self.assertRaisesRegex(bridge.Refused,'qa_cleanup_requires_reconciliation'):
                        self.invoke(backend,state,config)
                    self.assertTrue((state/bridge.INTERLOCK).exists())
                    self.assertEqual([r['status'] for r in json.loads(ledger.read_text())['entries']],['reserved','reserved'])
                else:
                    result=self.invoke(backend,state,config);self.assertFalse(result['success'])
                    self.assertEqual([r['status'] for r in json.loads(ledger.read_text())['entries']],['cleaned','not_started'])
                self.assertEqual(json.loads(ledger.read_text())['consumed'],9)

    def test_offline_and_authorization_gates_never_touch_ledger_or_start(self):
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=CloudQABridgeTests().setup_state(root);old=ledger.read_bytes()
            backend=SemanticDockerFixture(semantic_raw());backend.ledger=ledger
            result=self.invoke(backend,state,config,mode='semantic-v1-offline',authorized=False)
            self.assertFalse(result['model_started']);self.assertFalse(result['ledger_touched'])
            with self.assertRaisesRegex(bridge.Refused,'qa_live_not_authorized'):
                self.invoke(backend,state,config,authorized=False)
            self.assertEqual(ledger.read_bytes(),old)

    def test_actual_loader_checks_three_files_and_never_imports_a(self):
        script='''import importlib.abc,json,sys
from pathlib import Path
class NoA(importlib.abc.MetaPathFinder):
 def find_spec(self,fullname,path=None,target=None):
  if fullname.startswith('teams_transcription.qa_audio'):raise AssertionError('A import')
sys.meta_path.insert(0,NoA())
from deploy.qa_container_entry import run
print(json.dumps(run(json.load(sys.stdin),data_root=Path(sys.argv[1]))))'''
        with tempfile.TemporaryDirectory() as root:
            result=run_c409(script,args=(root,),input=json.dumps(self.request()))
            self.assertEqual(result.returncode,0,result.stderr)
            value=json.loads(result.stdout);self.assertFalse(value['model_started']);self.assertEqual(value['modules'],3)
            self.assertEqual(value['candidate_sha'],contract.SEMANTIC_SHA)

    def test_loader_refuses_modified_helper_and_cross_version_bundle_before_execution(self):
        from deploy.qa_container_entry import run
        for change in ('helper','manifest'):
            request=self.request()
            name='qa_content.py' if change=='helper' else 'semantic-isolated-candidate-v1.json'
            request['files'][name]=base64.b64encode(b'raise AssertionError("must not execute")').decode()
            with tempfile.TemporaryDirectory() as root:
                with self.assertRaisesRegex(ValueError,'qa_entry_hash_rejected'):
                    run(request,data_root=Path(root))
                self.assertEqual(list(Path(root).iterdir()),[])

    def test_insufficient_two_slots_and_wrong_offline_version_never_start(self):
        with tempfile.TemporaryDirectory() as root:
            state,ledger,config=CloudQABridgeTests().setup_state(root)
            value=json.loads(ledger.read_text());value.update(consumed=49,
                entries=[{'slot':n,'run_id':n,'run_attempt':1,'status':'cleaned'} for n in range(8,50)])
            ledger.write_text(json.dumps(value));original=ledger.read_bytes()
            backend=SemanticDockerFixture(semantic_raw());backend.ledger=ledger
            with self.assertRaisesRegex(ValueError,'ledger_exhausted'):
                self.invoke(backend,state,config)
            self.assertEqual(backend.calls,['semantic-v1-offline']);self.assertEqual(ledger.read_bytes(),original)
            original_execute=backend.execute
            def old_version(*args):
                result=original_execute(*args);result['criterion_version']='old';return result
            with patch.object(backend,'execute',side_effect=old_version):
                with self.assertRaisesRegex(bridge.Refused,'qa_semantic_offline_rejected'):
                    self.invoke(backend,state,config,mode='semantic-v1-offline')

    def test_projection_does_not_truncate_extra_checks_into_a_pass(self):
        raw=semantic_raw();raw['checks'].append(copy.deepcopy(raw['checks'][0]))
        self.assertFalse(contract.public_semantic_result(raw,run_id=123,run_attempt=1)['accepted'])

    def test_check_binds_to_same_session_current_complete_delivery(self):
        for change in ('version','marker','bytes','hash','missing'):
            raw=semantic_raw();delivery=raw['sessions'][1]['context_deliveries'][0]
            if change=='version':raw['checks'][1]['source_version']=999
            elif change=='marker':delivery['marker_returned']=False
            elif change=='bytes':delivery['sent_bytes']=0
            elif change=='hash':delivery['snapshot_sha256']=None
            else:raw['sessions'][1]['context_deliveries']=[]
            with self.subTest(change=change):
                self.assertFalse(contract.public_semantic_result(raw,run_id=123,run_attempt=1)['accepted'])

    def test_runtime_helper_keeps_unsupported_unknown_and_rejects_wrong_stale_or_new_claim(self):
        from deploy.qa_semantic_candidate.qa_content import assess_content
        self.assertEqual((ROOT/'deploy/qa_semantic_candidate/qa_content.py').read_bytes(),
            (ROOT/'teams_transcription/qa_content.py').read_bytes())
        for answer in ('The budget is120000.','預算十萬元。','預算十二萬元，另有十萬元。','預算十二萬元。支出已完成。'):
            result=assess_content([answer],'預算十二萬元。',expected_version=3,delivered_version=3)
            self.assertNotEqual(result['status'],'pass')
        self.assertEqual(assess_content(['Hello'],'預算十二萬元。',expected_version=3,delivered_version=3)['status'],'unknown')

    def test_loaded_coordinator_is_serial_and_refuses_wrong_child_version_or_cleanup(self):
        script='''import asyncio,json,sys,argparse
from pathlib import Path
from deploy.qa_container_entry import run
request=json.load(sys.stdin);run(request,data_root=Path(sys.argv[1]))
from teams_transcription import qa_probe,qa_session_isolation
mode=sys.argv[2];calls=[]
qa_probe.synthetic_pcm=lambda text:b'fixed-synthetic'
async def child(args,*,revision,**kwargs):
 calls.append(revision)
 owned={'status':'cleaned','stop_status':'stopped','stop_ack_received':True,'stop_closed_received':True,
  'native_cleanup_status':'cleaned','reaped':True,'pending_operation':False,'live_processes':0,'pending_close_tasks':0}
 if mode=='unknown_cleanup':owned['status']='unknown'
 args.private_result.write_text(json.dumps({'request':{'model':'gpt-live-1-codex','version':'v3',
  'includeStartupContext':False,'initialItems':[]},'context_deliveries':[]}))
 return {'schema_version':2 if mode=='wrong_version' else 3,'content_criterion_version':'synthetic_current_fact_semantic_v1',
  'accepted':True,'failure':None,'checks':[{'source_revision':revision}], 'owned_cleanup':owned,
  'session':{'thread_sha256':str(revision)*64,'start_attempted':True,'startup_completed':True,'realtime_start_issued':True}}
qa_probe._run_probe_session=child
args=argparse.Namespace(authorize_live=True,private_result=Path(sys.argv[1])/'coordinator.json',
 qa_start_stage='question',peer_media_profile='legacy')
result=asyncio.run(qa_session_isolation.run_isolated_probe(args))
print(json.dumps({'calls':calls,'accepted':result['accepted'],'failure':result['failure'],
 'clean':result['owned_cleanup_verified'],'version':result['criterion_version']}))'''
        for mode in ('normal','wrong_version','unknown_cleanup'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                result=run_c409(script,args=(root,mode),input=json.dumps(self.request()))
                self.assertEqual(result.returncode,0,result.stderr)
                value=json.loads(result.stdout)
                self.assertEqual(value['calls'],[1,2] if mode=='normal' else [1])
                self.assertEqual(value['accepted'],mode=='normal')
                if mode=='wrong_version':self.assertEqual(value['failure']['error_code'],'session_criterion_unverified')

    def test_actual_loaded_probe_uses_semantic_criteria_and_keeps_rx_and_cleanup_gates(self):
        script='''import json,sys,unittest
from pathlib import Path
from deploy.qa_container_entry import run
run(json.load(sys.stdin),data_root=Path(sys.argv[1]))
from teams_transcription import qa_probe
assert 'qa-cloud' in qa_probe.__file__
names=['test_correct_budget_with_neutral_prefix_passes_versioned_semantic_criterion',
 'test_audio_and_budget_terms_do_not_accept_translation_wrong_value_or_extra_text',
 'test_correct_last_utterance_cannot_hide_earlier_prefix_wrong_value_or_duplicate',
 'test_text_and_outbound_packets_alone_cannot_pass_audio_rx']
suite=unittest.defaultTestLoader.loadTestsFromNames(['test_qa_probe.ProbeTests.'+n for n in names])
result=unittest.TextTestRunner(stream=sys.stderr).run(suite)
print(json.dumps({'tests':result.testsRun,'success':result.wasSuccessful()}))
sys.exit(0 if result.wasSuccessful() else 1)'''
        with tempfile.TemporaryDirectory() as root:
            result=run_c409(script,args=(root,),input=json.dumps(self.request()),timeout=15)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),{'tests':4,'success':True})
