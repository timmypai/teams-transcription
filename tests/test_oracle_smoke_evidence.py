"""Current manual CLI smoke evidence, with injected Docker/GitHub only.

No Oracle run or user-safe access is manufactured by these disposable tests.
The immutable failed main can be loaded to prove missing evidence is assertion RED.
"""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy import target_deploy as candidate
ROOT=Path(__file__).resolve().parents[1]
td=candidate
if os.environ.get('ORACLE_SMOKE_BASE'):
    spec=importlib.util.spec_from_file_location('failed_smoke_adapter',os.environ['ORACLE_SMOKE_BASE'])
    td=importlib.util.module_from_spec(spec);spec.loader.exec_module(td)

SHA='a'*40
IMAGE='sha256:'+'b'*64
CID='c'*64
RUN=123
ATTEMPT=2
WORKFLOW=422680872
REPO_ID=1461049898


class SmokeCLITests(unittest.TestCase):
    def exercise(self, defect=None, *,mode='configured-https'):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);source=folder/'source';source.mkdir()
            controller=None
            container:dict=dict(Id=CID,Name='/'+td.APP,Image=IMAGE,
                Config=dict(User='10001:10001',Labels={td.OWNER:td.REPO}),
                HostConfig=dict(PortBindings={'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'18101'}]}),
                State=dict(StartedAt='synthetic-start',Running=True,Status='running',Health=dict(Status='healthy')))
            image=dict(Config=dict(Labels={'org.opencontainers.image.revision':SHA,
                'org.opencontainers.image.source':'https://github.com/'+td.REPO,td.OWNER:td.REPO}))
            safe=dict(approved_safe_entrypoint=True,anonymous_context=True,
                login_heading=True,password_field=True,login_button=True,
                anonymous_api_rejected=True,no_authenticated_data=True,origin_bound=True,
                access_mode=mode,no_secret_artifacts=True,same_deployment_session=True)
            if defect=='entrypoint':safe=dict(user_reachable_entrypoint='NOT VERIFIED')
            if defect=='missing':safe={}
            if defect=='sha':image['Config']['Labels']['org.opencontainers.image.revision']='d'*40
            if defect in {'wrong-port','nonloopback-bind'}:
                container['HostConfig']['PortBindings']['8080/tcp'][0].update(
                    {'HostPort':'18102'} if defect=='wrong-port' else {'HostIp':'0.0.0.0'})
            foreign=dict(Id='foreign',Image='foreign-image',Name='/foreign',Config=dict(Labels={}),
                State=dict(StartedAt='before',Status='running',Health=dict(Status='healthy')))
            def command(args,**kw):
                if 'inspect' in args:
                    return json.dumps([image if 'image' in args else container])
                raise AssertionError('unexpected command in disposable seam')
            original=td.Controller
            def factory(*args,**kw):
                nonlocal controller
                c=controller=original(*args,command=command,**kw)
                c.preflight=lambda state:td.snapshot([foreign])
                c.containers=lambda:[{**foreign,'State':{**foreign['State'],'StartedAt':'changed'}}] if defect=='foreign' else [foreign]
                c.build=lambda sha:IMAGE
                def switch(receipt,image,state):
                    c.write(c.root/'state.json',json.dumps(dict(phase='healthy',source_sha=SHA,
                        image=IMAGE,container_id=CID,release='synthetic')))
                    return dict(health=True,anonymous_login_page=True)
                c.switch=switch
                c.health=lambda image:(CID,dict(health=True,anonymous_login_page=True))
                def runtime(action,**kw):
                    if action=='login' and defect=='changed-container':container['Id']='e'*64
                    if action=='login' and defect=='changed-port':
                        container['HostConfig']['PortBindings']['8080/tcp'][0]['HostPort']='18102'
                    if action=='login' and defect=='changed-mode':
                        return {**safe,'access_mode':'configured-https'}
                    return copy.deepcopy(safe) if action in {'entrypoint','login'} else dict(health=True,anonymous_login_page=True)
                c.runtime=runtime
                return c
            class API:
                def request(self,path):
                    if path=='/repos/'+td.REPO:
                        return dict(id=REPO_ID,full_name=td.REPO,default_branch='main'),{}
                    if path.endswith('/actions/workflows/deploy.yml'):
                        return dict(id=WORKFLOW,path='.github/workflows/deploy.yml'),{}
                    if path.endswith('/actions/runs/123'):
                        return dict(id=RUN,run_attempt=ATTEMPT,workflow_id=WORKFLOW,
                            path='.github/workflows/deploy.yml',head_sha='d'*40 if defect=='run' else SHA,
                            head_branch='main',event='workflow_dispatch',status='in_progress',conclusion=None,
                            repository=dict(id=REPO_ID,full_name=td.REPO),head_repository=dict(id=REPO_ID,full_name=td.REPO)),{}
                    raise AssertionError('unexpected GitHub read')
            env=dict(GITHUB_REPOSITORY=td.REPO,GITHUB_EVENT_NAME='workflow_dispatch',GITHUB_REF='refs/heads/main',
                GITHUB_SHA=SHA,GITHUB_RUN_ID=str(RUN),GITHUB_RUN_ATTEMPT=str(ATTEMPT),
                GITHUB_WORKFLOW_REF=td.REPO+'/.github/workflows/deploy.yml@refs/heads/main',RUNNER_ENVIRONMENT='self-hosted')
            with patch.object(td,'ROOT',source),patch.object(td,'GitHub',return_value=API()),\
                patch.object(td,'manual_environment',return_value=SHA),\
                patch.object(td,'collect',return_value=dict(repository=td.REPO,source_sha=SHA)),\
                patch.object(td.Path,'home',return_value=folder),patch.object(td,'Controller',side_effect=factory),\
                patch.dict(os.environ,env),patch.object(sys,'argv',['target_deploy.py','deploy']),\
                contextlib.redirect_stdout(io.StringIO()):
                code=td.main()
            evidence=source/f'.devflow/evidence-deploy-{RUN}-{ATTEMPT}'
            self.assertTrue((evidence/'acceptance.json').is_file(),
                'formal deploy artifact must contain current scenario evidence, not receipt/audit only')
            report=json.loads((evidence/'acceptance.json').read_text())
            self.assertEqual(report['kind'],'deploy')
            self.assertEqual(len(report['scenarios']),3)
            spec=json.loads((ROOT/'.devflow/scenarios.json').read_text())
            for row in report['scenarios']:
                self.assertTrue(row['smoke'])
                self.assertEqual([st['title'] for st in row['steps']],
                    [spec[row['id']][phase] for phase in ('given','when','then')])
                self.assertTrue(row['evidence'])
            self.assertNotIn('S2',json.dumps(report))
            if code==0:
                login=next(row for row in report['scenarios'] if row['id'].endswith('AC-3.S3'))
                # Use the evidence references produced by the actual consumer,
                # not a separate report manufactured by the fixture.
                from deploy.target_smoke import acceptance
                given=evidence/'logs'/f"{acceptance.safe_id(login['id'])}.given.txt"
                assertions=json.loads(given.read_text())['assertions']
                self.assertEqual(assertions.get('access_mode'),mode)
                self.assertTrue(assertions.get('fixed_host_loopback_mapping_verified'))
                self.assertNotIn('approved_configured_https_entrypoint',assertions)
            return code,report,evidence.exists()

    def test_success_has_current_identity_image_and_nine_real_step_assertions(self):
        code,report,_=self.exercise()
        self.assertEqual(code,0)
        self.assertEqual(report['identity']['image_id'],IMAGE)
        self.assertEqual(report['identity']['head_sha'],SHA)
        self.assertEqual(report['identity']['run_id'],str(RUN))
        self.assertTrue(all(s['status']=='passed' for s in report['scenarios']))

    def negative(self,defect):
        code,report,_=self.exercise(defect)
        self.assertEqual(code,1)
        self.assertTrue(any(s['status']!='passed' for s in report['scenarios']))

    def test_missing_login_evidence_is_not_pass(self):self.negative('missing')
    def test_wrong_running_image_revision_is_not_pass(self):self.negative('sha')
    def test_wrong_run_identity_is_not_pass(self):self.negative('run')
    def test_foreign_changed_is_not_pass(self):self.negative('foreign')
    def test_missing_entrypoint_proof_is_not_verified_access(self):self.negative('entrypoint')


class PrivateSmokeCLITests(unittest.TestCase):
    def test_approved_private_mode_is_named_truthfully_in_formal_consumer(self):
        code,report,_=SmokeCLITests().exercise(mode='approved-private-ssh-tunnel')
        self.assertEqual(code,0)
        self.assertTrue(all(s['status']=='passed' for s in report['scenarios']))

    def test_private_mode_wrong_mapping_unknown_mode_and_identity_changes_refuse(self):
        for defect in ('wrong-port','nonloopback-bind','changed-container','changed-port','changed-mode'):
            with self.subTest(defect=defect):
                code,report,_=SmokeCLITests().exercise(defect,mode='approved-private-ssh-tunnel')
                self.assertEqual(code,1)
                self.assertNotEqual(next(s for s in report['scenarios'] if s['id'].endswith('AC-3.S3'))['status'],'passed')
        code,_,_=SmokeCLITests().exercise(mode='caller-approved-arbitrary-http')
        self.assertEqual(code,1)


class SmokeIdentityBoundaryTests(unittest.TestCase):
    def test_error_projection_keeps_only_fixed_codes(self):
        from deploy.target_smoke import step_error
        self.assertEqual(step_error(ValueError('approved_safe_entrypoint_unverified')),'approved_safe_entrypoint_unverified')
        self.assertEqual(step_error(ValueError('target_runtime_rejected:approved_safe_entrypoint_unverified')),
            'approved_safe_entrypoint_unverified')
        for error in (ValueError('SYNTHETIC-PRIVATE-cookie-token'),RuntimeError('approved_safe_entrypoint_unverified'),
            ValueError('target_runtime_rejected:SYNTHETIC-PRIVATE')):
            self.assertEqual(step_error(error),'current_live_assertion_unverified')

    def test_missing_wrong_attempt_repository_workflow_and_stale_run_refuse(self):
        from deploy.target_smoke import current_run
        valid=dict(id=RUN,run_attempt=ATTEMPT,workflow_id=WORKFLOW,path='.github/workflows/deploy.yml',
            head_sha=SHA,head_branch='main',event='workflow_dispatch',status='in_progress',conclusion=None,
            repository=dict(id=REPO_ID,full_name=td.REPO),head_repository=dict(id=REPO_ID,full_name=td.REPO))
        class API:
            run=valid
            def request(self,path):
                if path=='/repos/'+td.REPO:return dict(id=REPO_ID,full_name=td.REPO,default_branch='main'),{}
                if path.endswith('/deploy.yml'):return dict(id=WORKFLOW,path='.github/workflows/deploy.yml'),{}
                return self.run,{}
        api=API()
        with patch.dict(os.environ,dict(GITHUB_WORKFLOW_REF=td.REPO+'/.github/workflows/deploy.yml@refs/heads/main')):
            self.assertEqual(current_run(api,SHA,RUN,ATTEMPT)['repository_id'],REPO_ID)
            for changes in ({'run_attempt':1},{'workflow_id':1},{'id':999},{'event':'pull_request'},
                {'head_branch':'other'},{'status':'completed','conclusion':'success'},
                {'repository':dict(id=REPO_ID+1,full_name=td.REPO)},
                {'head_repository':dict(id=REPO_ID,full_name='foreign/target')},{'path':'readiness.yml'}):
                with self.subTest(changes=changes):
                    api.run={**valid,**changes}
                    with self.assertRaises(ValueError):current_run(api,SHA,RUN,ATTEMPT)
            api.run={}
            with self.assertRaises((ValueError,KeyError)):current_run(api,SHA,RUN,ATTEMPT)
