"""Live fixed-target smoke assertions, not a receipt-to-PASS converter.

Only constructed by the admitted manual deploy CLI. Callbacks observe this
invocation's installed service. S2/disposable harnesses never run here.
"""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil

from tools.target_admission import REPO, require

ROOT=Path(__file__).resolve().parents[1]
SPEC=json.loads((ROOT/'.devflow/scenarios.json').read_text())
spec=importlib.util.spec_from_file_location('target_acceptance_format',ROOT/'.devflow/acceptance.py')
assert spec is not None and spec.loader is not None
acceptance=importlib.util.module_from_spec(spec);spec.loader.exec_module(acceptance)
SHA=re.compile('[0-9a-f]{40}\\Z')
IMAGE=re.compile('sha256:[0-9a-f]{64}\\Z')
SAFE_STEP_ERRORS=frozenset({'smoke_run_identity_unverified','smoke_deployed_revision_mismatch',
    'smoke_installed_identity_mismatch','smoke_installed_identity_unverified','smoke_health_failed',
    'smoke_installed_service_changed','approved_safe_entrypoint_unverified','anonymous_login_not_observed',
    'anonymous_login_controls_unverified','smoke_audit_missing','smoke_allowlist_changed',
    'smoke_foreign_or_operation_changed'})


def step_error(error):
    if type(error) is ValueError:
        value=str(error)
        if value in SAFE_STEP_ERRORS:return value
        prefix='target_runtime_rejected:'
        if value.startswith(prefix):
            from deploy.target_runtime import SAFE_FAILURE_CODES
            code=value[len(prefix):]
            if code in SAFE_FAILURE_CODES:return code
    return 'current_live_assertion_unverified'


def current_run(api,sha,run,attempt):
    """GitHub identity is independently read, never trusted from an old receipt."""
    require(isinstance(sha,str) and SHA.fullmatch(sha)
        and type(run) is int and 0<run<2**63 and type(attempt) is int and 0<attempt<2**31,
        'smoke_request_identity_invalid')
    prefix='/repos/'+REPO
    observed=api.request(prefix+f'/actions/runs/{run}')[0]
    workflow=api.request(prefix+'/actions/workflows/deploy.yml')[0]
    repository=api.request(prefix)[0]
    require(repository['full_name']==REPO and repository['default_branch']=='main'
        and type(repository['id']) is int and repository['id']>0,'smoke_repository_mismatch')
    require(observed['id']==run and type(observed['id']) is int
        and observed['run_attempt']==attempt and type(observed['run_attempt']) is int
        and observed['head_sha']==sha and observed['head_branch']=='main'
        and observed['event']=='workflow_dispatch' and observed['status']=='in_progress'
        and observed['conclusion'] is None and observed['workflow_id']==workflow['id']
        and type(workflow['id']) is int and workflow['id']>0
        and observed['path']==workflow['path']=='.github/workflows/deploy.yml'
        and observed['repository']['full_name']==observed['head_repository']['full_name']==REPO
        and type(observed['repository']['id']) is int and observed['repository']['id']>0
        and observed['repository']['id']==observed['head_repository']['id']==repository['id'], 'smoke_run_identity_mismatch')
    require(os.environ.get('GITHUB_WORKFLOW_REF')==REPO+'/.github/workflows/deploy.yml@refs/heads/main',
        'smoke_workflow_ref_mismatch')
    return dict(repository_id=observed['repository']['id'],workflow_id=workflow['id'],
        run_id=run,run_attempt=attempt,source_sha=sha,repository=REPO,
        workflow='.github/workflows/deploy.yml',event='workflow_dispatch',branch='main')


class OracleSmoke:
    def __init__(self,evidence,sha,run,attempt,api):
        self.evidence=Path(evidence);self.evidence.mkdir(parents=True,exist_ok=True)
        require(not (self.evidence/'scenario-events.jsonl').exists(),'smoke_evidence_collision')
        self.sha=sha;self.run=run;self.attempt=attempt;self.events=[];self.image='';self.details={}
        self.context=None;self.api=api
        try:self.context=current_run(api,sha,run,attempt)
        except Exception:pass  # Unknown identity is failed Given, never PASS.

    def scenario(self,ac,steps):
        sid=f't_a3f4f673/AC-{ac}.S3';blocked=False
        for phase,operation in zip(acceptance.PHASES,steps):
            value={};status='not-run';error=''
            if not blocked:
                try:
                    require(self.context is not None,'smoke_run_identity_unverified')
                    value=operation()
                    status='passed'
                except Exception as exc:
                    # Only fixed allowlisted failure codes, never exception
                    # bodies, private config, DOM or HTTP content, may escape.
                    status='failed';error=step_error(exc);blocked=True
            event=dict(id=sid,runner='shell',smoke=True,phase=phase,title=SPEC[sid][phase],status=status,error=error)
            self.events.append(event)
            path=self.evidence/'logs'/f'{acceptance.safe_id(sid)}.{phase}.txt'
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(dict(status=status,assertions=value,error=error),sort_keys=True)+'\n')

    def health(self,c):
        observed={}
        def installed():
            state=c.read(c.root/'state.json')
            require(state['phase']=='healthy' and state['source_sha']==self.sha,'smoke_deployed_revision_mismatch')
            info=json.loads(c.docker('inspect','devflow-teams-transcription-app'))[0]
            labels=json.loads(c.docker('image','inspect',state['image']))[0]['Config']['Labels']
            require(IMAGE.fullmatch(state['image']) and info['Image']==state['image']
                and info['Id']==state['container_id'] and re.fullmatch('[0-9a-f]{64}',info['Id'])
                and info['Config']['Labels'].get('io.devflow.owner')==REPO
                and labels.get('org.opencontainers.image.revision')==self.sha
                and labels.get('org.opencontainers.image.source')=='https://github.com/'+REPO
                and labels.get('io.devflow.owner')==REPO
                and info['State']['Running'] is True,'smoke_installed_identity_mismatch')
            return dict(image_id=info['Image'],container_id=info['Id'],started=info['State']['StartedAt'])
        def given():
            observed.update(installed());self.image=observed['image_id']
            return dict(deployed_sha_verified=True,installed_image_verified=True)
        def when():
            ident,result=c.health(observed['image_id'])
            require(ident==observed['container_id'] and result.get('health') is True,'smoke_health_failed')
            return dict(readonly_health=True)
        def then():
            require(installed()==observed,'smoke_installed_service_changed')
            self.details['installed']=dict(source_sha=self.sha,**observed)
            return dict(health_passed=True,actual_sha_matches_this_run=True,container_unchanged=True)
        self.scenario(1,(given,when,then))

    def login(self,c):
        from deploy.target_runtime import PRIVATE_ACCESS,HTTPS_ACCESS
        value={};entry={}
        def installed_binding():
            observed=self.details['installed']
            info=json.loads(c.docker('inspect','devflow-teams-transcription-app'))[0]
            require(info['Id']==observed['container_id'] and info['Image']==observed['image_id']
                and info['State']['StartedAt']==observed['started'] and info['State']['Running'] is True
                and info['Config']['Labels'].get('io.devflow.owner')==REPO
                and info['HostConfig']['PortBindings']=={'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'18101'}]},
                'smoke_installed_service_changed')
        def given():
            require(bool(self.image) and 'installed' in self.details,'smoke_installed_identity_unverified')
            installed_binding()
            entry.update(c.runtime('entrypoint'))
            require(entry.get('approved_safe_entrypoint') is True and entry.get('origin_bound') is True
                and entry.get('access_mode') in {PRIVATE_ACCESS,HTTPS_ACCESS},
                'approved_safe_entrypoint_unverified')
            return dict(approved_configured_entrypoint=True,access_mode=entry['access_mode'],
                fixed_host_loopback_mapping_verified=True,anonymous_browser_required=True)
        def when():
            value.update(c.runtime('login'))
            require(value.get('anonymous_context') is True and value.get('origin_bound') is True
                and value.get('access_mode')==entry['access_mode'],
                'anonymous_login_not_observed')
            return dict(fresh_anonymous_browser_opened_login=True)
        def then():
            keys=('approved_safe_entrypoint','login_heading','password_field','login_button',
                'anonymous_api_rejected','no_authenticated_data','no_secret_artifacts','same_deployment_session')
            require(all(value.get(k) is True for k in keys),'anonymous_login_controls_unverified')
            installed_binding()
            self.details['login']=dict(**{k:True for k in keys},access_mode=entry['access_mode'])
            return self.details['login']
        self.scenario(3,(given,when,then))

    def isolation(self,report,expected):
        def given():
            require(report.get('repository')==REPO
                and report.get('scope')=='fixed-adapter-operations-and-docker-service-snapshot'
                and all(k in report for k in ('foreign_before','foreign_after','operations','allowlist')),
                'smoke_audit_missing')
            return dict(current_audit_and_snapshots_saved=True)
        def when():
            require({tuple(row) for row in report['allowlist']}==set(expected),'smoke_allowlist_changed')
            return dict(readonly_exact_allowlist_comparison=True)
        def then():
            require(type(report.get('outside_mutations')) is int and report['outside_mutations']==0
                and report.get('foreign_unchanged') is True and report['foreign_before']==report['foreign_after']
                and all((row['action'],row['kind'],row['target']) in expected and row['outcome']=='succeeded'
                    for row in report['operations']),'smoke_foreign_or_operation_changed')
            return dict(outside_mutations=0,all_operations_allowlisted=True,foreign_unchanged=True)
        self.scenario(2,(given,when,then))

    def finish(self):
        if self.context is not None:
            try:
                require(current_run(self.api,self.sha,self.run,self.attempt)==self.context,'smoke_identity_changed')
            except Exception:self.context=None
        events=self.evidence/'scenario-events.jsonl'
        with events.open('x') as out:
            for event in self.events:out.write(json.dumps(event,ensure_ascii=False)+'\n')
        scenarios=[acceptance.assemble(sid,[e for e in self.events if e['id']==sid],[],self.evidence)
            for sid in sorted({e['id'] for e in self.events})]
        identity=dict(repo=REPO,head_sha=self.sha,run_id=str(self.run),run_attempt=str(self.attempt),
            workflow_ref=REPO+'/.github/workflows/deploy.yml@refs/heads/main',image_id=self.image)
        report=dict(version=1,kind='deploy',identity=identity,
            health='passed' if any(s['id'].endswith('AC-1.S3') and s['status']=='passed' for s in scenarios) else 'failed',
            scenarios=scenarios,problems=[] if self.context is not None else ['current_run_identity_unverified'])
        (self.evidence/'acceptance.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
        (self.evidence/'acceptance-matrix.md').write_text(acceptance.matrix(report))
        (self.evidence/'smoke-identity.json').write_text(json.dumps(dict(context=self.context,
            installed=self.details.get('installed'),scope='current-oracle-fixed-adapter-smoke-not-whole-host'),indent=2)+'\n')
        sources=self.evidence/'sources';sources.mkdir(exist_ok=True)
        for path in ('deploy/target_smoke.py','deploy/target_deploy.py','deploy/target_runtime.py','.devflow/scenarios.json'):
            shutil.copyfile(ROOT/path,sources/Path(path).name)
        return len(scenarios)==3 and not report['problems'] and all(s['status']=='passed' for s in scenarios)
