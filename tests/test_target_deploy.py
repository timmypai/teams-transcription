"""Fixed deployment adapter seams; no host daemon or credentials used."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

from deploy.target_deploy import Audit, Controller, collision, manual_environment, snapshot, REPO, APP, OWNER

ROOT=Path(__file__).resolve().parents[1]


class TargetDeployTests(unittest.TestCase):
    def test_secret_is_stdin_only_and_only_known_runtime_codes_escape(self):
        from deploy.target_deploy import execute,RUNTIME
        from types import SimpleNamespace
        secret='synthetic-stdin-only-private-marker'
        for payload,expected in ((dict(error='target_runtime_rejected',code='daemon_identity_mismatch'),
                                   'target_runtime_rejected:daemon_identity_mismatch'),
                                  (dict(error=secret,code=secret),'target_command_failed'),
                                  ([], 'target_command_failed')):
            def command(args,**kw):
                self.assertNotIn(secret,str(args))
                self.assertEqual(kw['input'],secret)
                self.assertNotIn('DEVFLOW_APP_ENV',kw['env'])
                return SimpleNamespace(returncode=1,stdout=json.dumps(payload),stderr=secret)
            with patch('deploy.target_deploy.subprocess.run',side_effect=command):
                with self.assertRaisesRegex(ValueError,'^'+expected+'$'):
                    execute(['docker','exec','-i',APP,'python',RUNTIME,'verify'],input=secret)

    def test_foreign_sentinel_overrides_inherited_image_owner(self):
        import importlib.util
        spec=importlib.util.spec_from_file_location('target_probe',ROOT/'.devflow/target_adapter_probe.py')
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        command=module.sentinel_command('sha256:'+'a'*64,'synthetic-foreign')
        self.assertIn(OWNER+'=fixture-foreign',command)
        self.assertNotIn(OWNER+'='+REPO,command)
        foreign=dict(Id='foreign-id',Image='image',Config=dict(Labels={OWNER:'fixture-foreign'}),
            State=dict(StartedAt='original',Status='running'))
        self.assertEqual(snapshot([foreign])[0]['id'],'foreign-id')

    def test_public_build_context_bind_is_writable_without_dac_override(self):
        with tempfile.TemporaryDirectory() as folder:
            c=None
            def command(args,**kwargs):
                # Stop at the real builder boundary; no daemon in this unit test.
                context=Path(folder)/'contexts-1-1'
                self.assertEqual(context.stat().st_mode&0o777,0o777,
                    'public-only bind must be writable by rootful container UID0 with cap_drop ALL as well as rootless UID0')
                self.assertEqual(Path(folder).stat().st_mode&0o777,0o700,
                    'host parent remains private; no credentials enter the context')
                raise RuntimeError('public-boundary-checked')
            c=Controller(folder,1,1,command=command)
            with self.assertRaisesRegex(RuntimeError,'public-boundary-checked'):c.build('a'*40)

    def test_whole_foreign_plan_rejected_before_first_effect_and_failures_recorded(self):
        audit=Audit([('stop','container',APP)])
        effects=[]
        with self.assertRaisesRegex(ValueError,'foreign_plan_rejected'):
            audit.apply([('stop','container',APP),('stop','container','foreign')],lambda:effects.append(True))
        self.assertEqual(effects,[]);self.assertEqual(audit.events,[])
        with self.assertRaisesRegex(ValueError,'synthetic'):
            audit.apply([('stop','container',APP)],lambda:(_ for _ in ()).throw(ValueError('synthetic')))
        self.assertEqual(audit.events[0]['outcome'],'failed')
        self.assertFalse(audit.report([],['changed'])['foreign_unchanged'])

    def test_name_label_and_recorded_identity_are_all_required(self):
        resource=dict(Id='container-a',Config=dict(Labels={OWNER:REPO}))
        collision('container',APP,resource,'container-a')
        for prior,data in [(None,resource),('other',resource),('container-a',{**resource,'Config':{'Labels':{}}})]:
            with self.assertRaises(ValueError):collision('container',APP,data,prior)

    def test_pr_branch_and_non_actions_execution_fail_before_any_command(self):
        for env in ({},{'GITHUB_REPOSITORY':REPO,'GITHUB_EVENT_NAME':'pull_request','GITHUB_REF':'refs/heads/main'},
                    {'GITHUB_REPOSITORY':REPO,'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/wt/unsafe'}):
            with patch.dict(os.environ,env,clear=True),patch('deploy.target_deploy.execute',side_effect=AssertionError('must not execute')):
                with self.assertRaisesRegex(ValueError,'manual_self_hosted_main_required'):manual_environment()

    def test_exact_release_keeps_image_compose_seccomp_config_and_refuses_tampering(self):
        with tempfile.TemporaryDirectory() as folder:
            c=Controller(folder,1,1,command=lambda *a,**k:(_ for _ in ()).throw(AssertionError('no host command')))
            release=c.create_release(dict(source_sha='a'*40),'sha256:'+'b'*64)
            info=c.verify_release(release)
            self.assertEqual(info['image'],'sha256:'+'b'*64)
            self.assertEqual(set(info['files']),{'compose.yml','chromium-seccomp.json','config.env'})
            (release/'compose.yml').write_text('modified')
            with self.assertRaisesRegex(ValueError,'release_config_tampered'):c.verify_release(release)
            second=c.create_release(dict(source_sha='a'*40),'sha256:'+'b'*64)
            self.assertNotEqual(second,release)
            self.assertEqual((release/'compose.yml').read_text(),'modified')

    def test_manual_workflows_are_readonly_main_latest_and_separate_from_ci(self):
        deploy_wf=yaml.load((ROOT/'.github/workflows/deploy.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(deploy_wf['on']),{'push','workflow_dispatch'})
        self.assertEqual(deploy_wf['on']['push'],{'branches':['main','master']})
        self.assertEqual(deploy_wf['concurrency']['group'],'deploy-${{ github.repository }}')
        djob=deploy_wf['jobs']['deploy']
        self.assertEqual(djob['runs-on'],['self-hosted','devflow'])
        self.assertTrue(any('default_branch' in str(s.get('env',{})) and 'git ls-remote' in s.get('run','') for s in djob['steps']))
        self.assertTrue(any('bash .devflow/deploy.sh' in s.get('run','') for s in djob['steps']))

        workflow=yaml.load((ROOT/'.github/workflows/control.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']),{'workflow_dispatch'})
        self.assertEqual(set(workflow['permissions'].values()),{'read'})
        self.assertEqual(workflow['concurrency']['group'],'manual-target-teams-transcription')
        job=workflow['jobs']['control']
        self.assertEqual(job['runs-on'],['self-hosted','devflow'])
        self.assertIn("github.ref == 'refs/heads/main'",job['if'])
        checkout=job['steps'][0]
        self.assertEqual(checkout['with']['ref'],'${{ github.sha }}')
        self.assertEqual(checkout['with']['persist-credentials'],'false')
        self.assertIn('target_admission.py',job['steps'][1]['run'])
        # Only the new private browser branch receives the existing operations
        # secret, and never before the fixed default-tip admission step.
        control=(ROOT/'.github/workflows/control.yml').read_text()
        self.assertEqual(control.count('secrets.'),1)
        steps=workflow['jobs']['control']['steps']
        self.assertNotIn('DEVFLOW_OPS_ENV',steps[1]['env'])
        self.assertEqual(steps[2]['env']['DEVFLOW_OPS_ENV'],
            "${{ (inputs.operation == 'preview' || inputs.operation == 'join-confirmed-present-and-media') && secrets.DEVFLOW_OPS_ENV || '' }}")
        self.assertEqual(set(workflow['on']['workflow_dispatch']['inputs']),{'operation'})

    def test_product_compose_has_fixed_scope_no_auth_or_host_socket_mount(self):
        compose=yaml.load((ROOT/'deploy/compose.yml').read_text(),Loader=yaml.BaseLoader)
        service=compose['services']['teams-transcription']
        self.assertEqual(service['user'],'10001:10001')
        self.assertEqual(service['read_only'],'true')
        self.assertEqual(service['cap_drop'],['ALL'])
        self.assertEqual(service['ports'],['127.0.0.1:18101:8080'])
        self.assertEqual(service['volumes'],[dict(type='volume',source='data',target='/data')])
        self.assertNotIn('privileged',service)
        self.assertNotIn('network_mode',service)
        self.assertEqual(compose['volumes']['data']['name'],'devflow-teams-transcription-data')
        self.assertEqual(service['command'][-1],'--local-teams-control')
