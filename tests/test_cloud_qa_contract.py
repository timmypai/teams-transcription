"""Offline cloud handoff never grants model authority or exports private evidence."""
import copy
import json
from pathlib import Path
import subprocess
import unittest

import yaml

from deploy import qa_cloud_contract as contract


class CloudQAContractTests(unittest.TestCase):
    def test_manual_cloud_jobs_and_exact_previously_approved_qa_hook(self):
        root=Path(__file__).resolve().parents[1]
        for filename in ('oracle-diagnostics.yml','oracle-staging.yml','oracle-qa.yml'):
            workflow=yaml.load((root/'.github/workflows'/filename).read_text(),Loader=yaml.BaseLoader)
            self.assertEqual(set(workflow['on']),{'workflow_dispatch'})
            self.assertEqual(workflow['concurrency'],{'group':'oracle-staging','cancel-in-progress':'false'})
            for job in workflow['jobs'].values():
                self.assertEqual(job['runs-on'],['self-hosted','Linux','ARM64','teams-transcription-staging'])
                for step in job['steps']:
                    self.assertNotIn('actions/checkout',step.get('uses',''))
                    self.assertNotRegex(step.get('run',''),r'(?im)(?:^|\s)(?:ssh|scp|adb|tailscale)\s')
        # Source now matches the separately approved/installed QA workflow case.
        env={'GITHUB_REPOSITORY':'paulpai0412/teams-transcription',
            'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/main',
            'GITHUB_WORKFLOW_REF':'paulpai0412/teams-transcription/.github/workflows/oracle-qa.yml@refs/heads/main'}
        result=subprocess.run(['/bin/bash',str(root/'deploy/runner-job-started.sh')],
            env=env,capture_output=True,timeout=2)
        self.assertEqual(result.returncode,0)
        for changes in ({'GITHUB_REF':'refs/heads/feature'},{'GITHUB_EVENT_NAME':'pull_request'},
                {'GITHUB_WORKFLOW_REF':'paulpai0412/teams-transcription/.github/workflows/new-qa.yml@refs/heads/main'}):
            denied=subprocess.run(['/bin/bash',str(root/'deploy/runner-job-started.sh')],
                env={**env,**changes},capture_output=True,timeout=2)
            self.assertEqual(denied.returncode,1)
        workflow=yaml.load((root/'.github/workflows/oracle-qa.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(workflow['permissions'],{})
        self.assertEqual(set(workflow['on']),{'workflow_dispatch'})
        self.assertEqual(workflow['concurrency'],{'group':'oracle-staging','cancel-in-progress':'false'})
        self.assertEqual(workflow['on']['workflow_dispatch']['inputs']['operation']['default'],'offline')

    def runtime(self):
        return {'phase':'healthy','image':contract.IMAGE,
            'container':{'status':'running','health':'healthy','image':contract.IMAGE},
            'database':{'database_present':True,'scheduler_paused':True,
                'quiescent':True,'maintenance':True,'active_meeting':False,
                'active_lease':False,'pending_summary':False,'unreviewed_cleanup':False},
            'release':{'verified_release_record':True,'source_sha':contract.BASE_SHA,
                'build_run_id':41022272702,'build_run_attempt':1}}

    def ledger(self):
        return {'schema_version':1,'limit':50,'checkpoint':{
            'used':7,'through_case':7,'source_receipt_sha256':'a'*64},
            'consumed':7,'entries':[]}

    def test_preflight_accepts_exact_fixture_without_authorizing_or_starting_live(self):
        ledger=self.ledger();before=copy.deepcopy(ledger)
        result=contract.preflight(self.runtime(),ledger,'a'*64)
        self.assertTrue(result['offline_contract_passed'])
        self.assertFalse(result['live_ready'])
        self.assertFalse(result['model_started'])
        self.assertEqual(result['remaining'],43)
        self.assertEqual(ledger,before)
        self.assertIn('host_bridge_and_global_lock_not_verified',result['integration_gaps'])

    def test_no_missing_checkpoint_quota_reset_or_counter_rewrite(self):
        for mutate in (
                lambda x:x.clear(),lambda x:x.update(consumed=0),
                lambda x:x.update(limit=500),lambda x:x['checkpoint'].update(used=0),
                lambda x:x['checkpoint'].update(through_case=6),
                lambda x:x['checkpoint'].update(source_receipt_sha256='b'*64),
                lambda x:x.update(consumed=True)):
            ledger=self.ledger();mutate(ledger)
            with self.subTest(ledger=ledger),self.assertRaisesRegex(ValueError,'ledger_contract_rejected'):
                contract.preflight(self.runtime(),ledger,'a'*64)

    def test_attempted_slots_are_never_refunded_and_uncertain_cleanup_blocks(self):
        ledger=self.ledger();ledger['consumed']=8
        row={'slot':8,'run_id':123,'run_attempt':1,'status':'cleaned'}
        ledger['entries']=[row]
        self.assertEqual(contract.preflight(self.runtime(),ledger,'a'*64)['remaining'],42)
        for status in ('reserved','unknown','failed','PRIVATE'):
            row['status']=status
            with self.subTest(status=status),self.assertRaisesRegex(ValueError,'ledger_unresolved_attempt'):
                contract.preflight(self.runtime(),ledger,'a'*64)
        row['status']='cleaned'
        ledger['entries'].append(dict(row));ledger['consumed']=9
        with self.assertRaisesRegex(ValueError,'ledger_contract_rejected'):
            contract.preflight(self.runtime(),ledger,'a'*64)

    def test_exhausted_allowance_does_not_admit_a_start(self):
        ledger=self.ledger();ledger['consumed']=50
        ledger['entries']=[{'slot':slot,'run_id':slot,'run_attempt':1,'status':'cleaned'}
            for slot in range(8,51)]
        with self.assertRaisesRegex(ValueError,'ledger_exhausted'):
            contract.preflight(self.runtime(),ledger,'a'*64)

    def test_runtime_requires_healthy_fixed_image_and_maintenance_paused_idle(self):
        for group,key,value in (
                (None,'image','ghcr.io/elsewhere/app:latest'),(None,'phase','switching'),
                ('container','image','PRIVATE'),('container','health','unhealthy'),
                ('database','maintenance',False),('database','scheduler_paused',False),
                ('database','active_lease',True),('database','quiescent',1),
                ('release','source_sha','b'*40),('release','build_run_id',True)):
            runtime=self.runtime();(runtime[group] if group else runtime)[key]=value
            with self.subTest(group=group,key=key),self.assertRaisesRegex(ValueError,'runtime_contract_rejected'):
                contract.preflight(runtime,self.ledger(),'a'*64)

    def test_safe_artifact_drops_secrets_text_paths_ids_and_unknown_fields_at_all_levels(self):
        private='PRIVATE Bearer refresh_token 王小明 https://private.invalid'
        result={'accepted':False,'owned_cleanup_verified':True,'failure':{'message':private},
            'checks':[{'expected_fact':False,'audio_rx_verified':True,'assistant_final':private,
                'audio_rx':{'inbound_delta':{'packets_received':3,'bytes_received':90,'secret':private},
                    'source_delta':{'packets':3,'frames':2880,'duration':.06,'energy':.001}}}]*100,
            'audio_observer':{'audio_bytes':{'source':4,'render':8,'secret':private},
                'findings':{'render_exact_zero_frames':10,'secret':private},
                'limit_reasons':[private],'files':{'source.f32le':{'bytes':4,'sha256':'a'*64,
                    'path':private},private:{'bytes':5,'sha256':'b'*64}},'raw_pcm':private},
            'turn_lifecycle':private,'private_evidence':{'path':private},'secret':private}
        safe=contract.public_result(result,run_id=123,run_attempt=1)
        rendered=json.dumps(safe,ensure_ascii=False,allow_nan=False)
        for forbidden in (private,'Bearer','refresh_token','王小明','assistant_final','raw_pcm','private_evidence'):
            self.assertNotIn(forbidden,rendered)
        self.assertEqual(len(safe['checks']),2)
        self.assertEqual(safe['checks'][0]['audio_rx']['inbound_delta']['packets_received'],3)
        self.assertFalse(safe['accepted'])
        self.assertFalse(safe['audio_quality_verified'])
        self.assertLess(len(rendered.encode()),16384)

    def test_safe_artifact_never_coerces_numeric_secrets_nonfinite_or_boolean_counts(self):
        result={'accepted':'true','audio_observer':{'polls':True,'poll_wall_ms':float('nan'),
            'reported_loss_delta':-2,'reported_jitter_max_seconds':'SECRET',
            'audio_bytes':{'source':float('inf'),'render':'123'},'frames':{'source':10**1000}}}
        safe=contract.public_result(result,run_id=123,run_attempt=1)
        self.assertIsNone(safe['accepted'])
        self.assertIsNone(safe['observer']['polls'])
        self.assertIsNone(safe['observer']['poll_wall_ms'])
        self.assertEqual(safe['observer']['reported_loss_delta'],-2)
        self.assertIsNone(safe['observer']['frames']['source'])
        json.dumps(safe,allow_nan=False)
        with self.assertRaisesRegex(ValueError,'actions_identity_rejected'):
            contract.public_result({},run_id='PRIVATE',run_attempt=1)


if __name__=='__main__':unittest.main()
