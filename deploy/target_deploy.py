"""This target's fixed manual devflow deployment/control adapter.

Adaptation of devflow build/up/health/smoke/exact-release rollback. No registry,
old staging controller, arbitrary shell/path input, prune, volume delete or data
restore. Shared rootless UID/daemon is the accepted trust boundary, NOT isolation.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tools.target_admission import collect, require, REPO
from tools.pr_policy_github import GitHub
from deploy.target_diagnostics import Failures, public_command
from deploy.private_control import ACTIONS as PRIVATE_CONTROL_ACTIONS, dispatch_identity, run_control

PROJECT='devflow-teams-transcription'
APP=PROJECT+'-app'
VOLUME=PROJECT+'-data'
OWNER='io.devflow.owner'
IMAGE=re.compile('sha256:[0-9a-f]{64}\\Z')
RUNTIME='/opt/meeting/devflow_runtime.py'


def execute(args,*,input=None,timeout=120,public_phase=None):
    if public_phase is not None:
        require(input is None,'public_stdin_rejected')
        return public_command(args,phase=public_phase,timeout=timeout,
            env={k:v for k,v in os.environ.items() if k not in {'DEVFLOW_APP_ENV','DEVFLOW_OPS_ENV','TEAMS_GOOGLE_CLIENT_JSON','GH_TOKEN','GITHUB_TOKEN'}})
    result=subprocess.run(args,input=input,text=True,capture_output=True,timeout=timeout,
        env={k:v for k,v in os.environ.items() if k not in {'DEVFLOW_APP_ENV','DEVFLOW_OPS_ENV','TEAMS_GOOGLE_CLIENT_JSON','GH_TOKEN','GITHUB_TOKEN'}})
    if result.returncode and RUNTIME in args:
        from deploy.target_runtime import SAFE_FAILURE_CODES
        try:value=json.loads(result.stdout)
        except (ValueError,TypeError):value={}
        code=value.get('code') if isinstance(value,dict) else None
        if isinstance(value,dict) and value.get('error')=='target_runtime_rejected' and code in SAFE_FAILURE_CODES:
            raise ValueError('target_runtime_rejected:'+code)
    require(result.returncode==0,'target_command_failed')
    return result.stdout


def snapshot(containers):
    """No env/logs/mount contents. Exact identities/start/status for foreign services."""
    return sorted([dict(id=c['Id'],image=c['Image'],started=c['State']['StartedAt'],
        status=c['State']['Status'],health=c['State'].get('Health',{}).get('Status'))
        for c in containers if c.get('Config',{}).get('Labels',{}).get(OWNER)!=REPO],key=lambda c:c['id'])


def collision(kind,name,inspect,prior):
    require(inspect.get('Labels',inspect.get('Config',{}).get('Labels',{})).get(OWNER)==REPO,
        'resource_owner_collision')
    require(prior is not None,'untracked_resource_collision')
    if kind=='container':require(inspect['Id']==prior,'container_identity_collision')
    else:
        require(inspect['Name']==name,'resource_name_collision')
        require((inspect['CreatedAt']==prior.get('volume_created_at') if kind=='volume'
            else inspect['Id']==prior.get('network_id')),'resource_identity_collision')


class Audit:
    def __init__(self,allowlist):
        self.allowlist=frozenset(allowlist);self.events=[]

    def apply(self,plan,fn):
        require(plan and all(tuple(row) in self.allowlist for row in plan),'foreign_plan_rejected')
        rows=[dict(action=a,kind=k,target=t,outcome='attempted') for a,k,t in plan]
        self.events.extend(rows)
        try:value=fn()
        except BaseException:
            for row in rows:row['outcome']='failed'
            raise
        for row in rows:row['outcome']='succeeded'
        return value

    def report(self,before,after):
        return dict(repository=REPO,scope='fixed-adapter-operations-and-docker-service-snapshot',
            outside_mutations=0,foreign_unchanged=before==after,foreign_before=before,foreign_after=after,
            allowlist=[list(row) for row in sorted(self.allowlist)],operations=self.events,
            limitation='Not a syscall/whole-host filesystem audit; shared UID is trusted. No foreign mount/database/file operations are issued.')


class Controller:
    def __init__(self,state_root,run_id,attempt,*,command=execute):
        self.root=Path(state_root);self.run_id=run_id;self.attempt=attempt;self.command=command
        self.helper=f'{PROJECT}-helper-{run_id}-{attempt}'
        self.builder=f'{PROJECT}-build-{run_id}-{attempt}'
        self.release=None
        self.failures=Failures()
        self.docker_prefix=['docker','--host=unix://'+f'/run/user/{os.getuid()}/docker.sock']
        self.audit=Audit([(a,k,t) for k,t,actions in [
            ('directory',str(self.root),['write']),('container',APP,['up','stop','exec']),
            ('container',self.helper,['run','remove']),('container',self.builder,['run','remove']),
            ('volume',VOLUME,['create','initialize','backup','fence']),
            ('network',PROJECT,['create']),('image',REPO,['build']),
        ] for a in actions])

    def docker(self,*args,**kw):return self.command(self.docker_prefix+list(args),**kw)

    def mutate(self,action,kind,target,fn):return self.audit.apply([(action,kind,target)],fn)

    def write(self,path,body):
        require(path.is_relative_to(self.root),'state_path_rejected')
        def save():
            path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
            temporary=path.with_name(path.name+'.new')
            require(not temporary.is_symlink() and not path.is_symlink(),'state_symlink_rejected')
            with temporary.open('w') as out:out.write(body)
            temporary.chmod(0o600);os.replace(temporary,path)
        self.mutate('write','directory',str(self.root),save)

    def read(self,path):
        info=path.lstat()
        require(path.absolute()==path.resolve() and stat.S_ISREG(info.st_mode)
            and info.st_uid==os.getuid() and not info.st_mode&0o077,'unsafe_state_file')
        return json.loads(path.read_text())

    def containers(self):
        ids=self.docker('ps','-aq').split()
        return json.loads(self.docker('inspect',*ids)) if ids else []

    def probe(self,kind,name):
        # List first: failure to inspect is not permission to create/adopt.
        names=self.docker(kind,'ls','--format','{{.Name}}' if kind!='container' else '{{.Names}}').splitlines()
        return json.loads(self.docker(kind,'inspect',name))[0] if name in names else None

    def compose(self,release,*args):
        info=self.read(release/'release.json')
        env=dict(DEVFLOW_IMAGE=info['image'],DEVFLOW_SOURCE_SHA=info['source_sha'],DEVFLOW_RELEASE_DIR=str(release))
        # No inherited COMPOSE_FILE/PROJECT/DOCKER_HOST or app secrets become config.
        prefix=['env','-i','PATH='+os.environ['PATH'],'HOME='+os.environ['HOME']]
        prefix.extend(k+'='+v for k,v in env.items())
        return self.command(prefix+self.docker_prefix+['compose','-p',PROJECT,'-f',str(release/'compose.yml'),
            '--env-file',str(release/'config.env'),*args],timeout=180)

    def runtime(self,action,*,image=None,ident=None,input=None):
        args=[action]
        if ident:args+=['--id',ident]

        if image:
            release=self.release
            require(release is not None and IMAGE.fullmatch(image),'helper_release_required')
            command=['run','--rm','--name',self.helper,'--label',OWNER+'='+REPO,
                '--network','none','--read-only','--user','10001:10001','--cap-drop','ALL',
                '--security-opt','no-new-privileges','--security-opt','seccomp='+str(release/'chromium-seccomp.json'),
                '--tmpfs','/tmp:size=512m,mode=1777','--shm-size','1g',
                '--memory','4g','--cpus','2','--pids-limit','512','--entrypoint','python']
            if action!='sandbox':command+=['--mount',f'type=volume,src={VOLUME},dst=/data']
            if input is not None:command+=['-i']
            raw=self.mutate('run','container',self.helper,lambda:self.docker(*command,image,RUNTIME,*args,input=input))
        else:
            raw=self.mutate('exec','container',APP,lambda:self.docker('exec',*(['-i'] if input is not None else []),
                APP,'python',RUNTIME,*args,input=input))
        value=json.loads(raw)
        require(isinstance(value,dict) and 'error' not in value,'runtime_operation_rejected')
        return value

    def install_google_client(self,state,raw):
        """Validate persistent target identity BEFORE passing private stdin."""
        require(isinstance(raw,str) and bool(raw.strip()) and len(raw.encode())<=65536,'google_client_input_required')
        installed=json.loads(self.docker('inspect',APP))[0]
        require(installed['Id']==state['container_id'] and installed['Image']==state['image']
            and installed['Config']['User']=='10001:10001' and any(
                m.get('Type')=='volume' and m.get('Name')==VOLUME and m.get('Destination')=='/data'
                and m.get('RW') is True for m in installed.get('Mounts',[])),'deployed_persistence_unverified')
        self.health(state['image'])
        value=self.runtime('install-google-client',input=raw)
        require(isinstance(value,dict) and set(value)=={'installed','existing','client_private',
            'redirect_registered','oauth_verified','google_read_verified'} and
            all(type(v) is bool for v in value.values()) and
            all(value[k] for k in ('installed','client_private','redirect_registered')) and
            not value['oauth_verified'] and not value['google_read_verified'],'google_client_receipt_invalid')
        after=json.loads(self.docker('inspect',APP))[0]
        require(after['Id']==installed['Id'] and after['State']['StartedAt']==installed['State']['StartedAt'],
            'deployed_container_changed')
        return value

    def preflight(self,state):
        info=json.loads(self.docker('info','--format','{{json .}}'))
        require(any('rootless' in x for x in info['SecurityOptions']),'rootless_required')
        return self.resources(state)

    def resources(self,state):
        """Ownership/port seam, also exercised on disposable hosted Docker."""
        all_containers=self.containers()
        owned=[c for c in all_containers if c.get('Config',{}).get('Labels',{}).get(OWNER)==REPO]
        require(not any(c['Name'].lstrip('/')!=APP for c in owned),'stale_helper_or_unknown_resource')
        app=next((c for c in all_containers if c['Name'].lstrip('/')==APP),None)
        if app:
            collision('container',APP,app,state.get('container_id') if state else None)
            require(app['Image']==state['image'],'running_image_mismatch')
            require(app['HostConfig']['PortBindings']=={'8080/tcp':[{'HostIp':'127.0.0.1','HostPort':'18101'}]},'port_owner_mismatch')
        else:
            require(state is None,'recorded_container_missing')
            with socket.socket() as listener:
                listener.bind(('127.0.0.1',18101))
        for kind,name in [('volume',VOLUME),('network',PROJECT)]:
            resource=self.probe(kind,name)
            if resource:collision(kind,name,resource,state)
            elif state:raise ValueError('recorded_resource_missing')
        return snapshot(all_containers)

    def build(self,sha):
        contexts=self.root/f'contexts-{self.run_id}-{self.attempt}'
        require(not contexts.exists(),'build_context_collision')
        def create_public_context():
            contexts.mkdir(mode=0o700)
            # Public-only data under a private parent. Rootful hosted UID0 has
            # no DAC override with cap_drop ALL; rootless UID0 maps differently.
            # Do not solve this by adding capabilities or mounting private data.
            contexts.chmod(0o777)
        self.mutate('write','directory',str(self.root),create_public_context)
        base=next(line.split('=',1)[1] for line in (ROOT/'Dockerfile').read_text().splitlines() if line.startswith('ARG PYTHON_BASE='))
        self.failures.cleanup='unknown'
        with self.failures.stage('prepare_contexts'):
            self.mutate('run','container',self.builder,lambda:self.docker('run','--rm','--name',self.builder,
                '--label',OWNER+'='+REPO,'--cap-drop','ALL','--security-opt','no-new-privileges',
                '--mount',f'type=bind,src={ROOT},dst=/source,readonly',
                '--mount',f'type=bind,src={contexts},dst=/workspace','--entrypoint','sh',base,
                '/source/.devflow/prepare-contexts.sh',timeout=900,public_phase='prepare_contexts'))
        iid=contexts/'image-id'
        with self.failures.stage('image_build'):
            self.mutate('build','image',REPO,lambda:self.docker('buildx','build','--load','--provenance=false',
                '--build-context',f'codex_npm={contexts}/codex_npm','--build-context',f'python_wheels={contexts}/python_wheels',
                '--label','org.opencontainers.image.source=https://github.com/'+REPO,
                '--label','org.opencontainers.image.revision='+sha,'--label',OWNER+'='+REPO,
                '--iidfile',str(iid),str(ROOT),timeout=1800,public_phase='image_build'))
            image=iid.read_text().strip();require(IMAGE.fullmatch(image),'immutable_image_required')
            inspect=json.loads(self.docker('image','inspect',image))[0]
            require(inspect['Config']['Labels']['org.opencontainers.image.revision']==sha,'built_revision_mismatch')
        self.failures.cleanup='not_required'
        return image

    def create_release(self,receipt,image):
        release=self.root/'releases'/f"{receipt['source_sha']}-{self.run_id}-{self.attempt}-{uuid.uuid4().hex}"
        require(not release.exists(),'release_collision')
        for name,source in [('compose.yml',ROOT/'deploy/compose.yml'),
                            ('chromium-seccomp.json',ROOT/'docker/chromium-seccomp.json'),
                            ('config.env',ROOT/'.devflow/config.env')]:
            self.write(release/name,source.read_text())
        info={**receipt,'image':image,'deploy_run_id':self.run_id,'deploy_run_attempt':self.attempt,
            'files':{n:hashlib.sha256((release/n).read_bytes()).hexdigest() for n in ['compose.yml','config.env','chromium-seccomp.json']}}
        self.write(release/'release.json',json.dumps(info))
        self.release=release
        return release

    def verify_release(self,release):
        require(release.parent==self.root/'releases' and release.resolve()==release,'release_path_rejected')
        info=self.read(release/'release.json')
        require(IMAGE.fullmatch(info['image']),'release_image_invalid')
        for name,digest in info['files'].items():
            require(name in {'compose.yml','config.env','chromium-seccomp.json'}
                and hashlib.sha256((release/name).read_bytes()).hexdigest()==digest,'release_config_tampered')
        require(set(info['files'])=={'compose.yml','config.env','chromium-seccomp.json'},'release_incomplete')
        return info

    def health(self,image):
        c=json.loads(self.docker('inspect',APP))[0]
        require(c['Image']==image and c['State']['Running']
            and c['State'].get('Health',{}).get('Status')=='healthy','candidate_unhealthy')
        host=c['HostConfig']
        require(c['Config']['User']=='10001:10001' and host['ReadonlyRootfs']
            and host['CapDrop']==['ALL'] and any('no-new-privileges' in x for x in host['SecurityOpt'])
            and any('seccomp=' in x or 'seccomp:' in x for x in host['SecurityOpt'])
            and not host['Privileged'] and host['Memory']==4294967296 and host['NanoCpus']==2000000000
            and host['PidsLimit']==512
            and len([m for m in c['Mounts'] if m['Type']=='volume' and m['Name']==VOLUME and m['Destination']=='/data'])==1
            and all(m['Type']=='volume' and m['Name']==VOLUME and m['Destination']=='/data'
                or m['Type']=='tmpfs' and m['Destination'] in {'/tmp','/dev/shm'} for m in c['Mounts']),
            'runtime_security_mismatch')
        smoke=self.runtime('smoke')
        require(smoke['health'] and smoke['anonymous_login_page'],'smoke_failed')
        return c['Id'],smoke

    def capture(self,phase):
        rows=[c for c in self.containers() if c['Name'].lstrip('/')==APP]
        value=[dict(id=c['Id'],image=c['Image'],running=c['State']['Running'],
            exit_code=c['State']['ExitCode'],oom=c['State']['OOMKilled'],
            health=c['State'].get('Health',{}).get('Status')) for c in rows]
        self.write(self.root/f'diagnostics-{self.run_id}-{self.attempt}-{phase}.json',json.dumps(value))

    def switch(self,receipt,image,state):
        self.failures.cleanup='unknown'
        release=self.create_release(receipt,image)
        self.runtime('sandbox',image=image)  # no data mount, credentials or network
        if state:
            old=self.root/'releases'/state['release'];previous=self.verify_release(old)
            before=self.runtime('enable');require(before['maintenance'],'maintenance_failed')
            schema=before['schema'];runtime_config=before['config_sha256']
            self.write(self.root/'state.json',json.dumps({**state,'phase':'switching'}))
            self.mutate('stop','container',APP,lambda:self.compose(old,'stop','teams-transcription'))
            backup=self.runtime('backup',image=state['image'],ident=uuid.uuid4().hex)
            require(backup['schema']==schema,'post_stop_schema_changed')
        else:
            schema=None;old=None;previous=None
            for kind,name in [('volume',VOLUME),('network',PROJECT)]:
                if not self.probe(kind,name):
                    self.mutate('create',kind,name,lambda k=kind,n=name:self.docker(k,'create','--label',OWNER+'='+REPO,n))
            self.runtime('init',image=image,input=os.environ.get('DEVFLOW_APP_ENV',''))
            before=self.runtime('enable',image=image);schema=before['schema'];runtime_config=before['config_sha256']
            self.write(self.root/'state.json',json.dumps(dict(phase='switching',source_sha=receipt['source_sha'])))
        try:
            self.mutate('up','container',APP,lambda:self.compose(release,'up','-d','--no-build','--pull','never',
                '--wait','--wait-timeout','90','teams-transcription'))
            ident,smoke=self.health(image)
            after=self.runtime('state');require(after['quiescent'],'post_start_not_quiescent')
        except BaseException as primary:
            self.failures.record(primary)
            try:self.capture('candidate')
            except Exception as capture_error:self.failures.record(capture_error,'audit','snapshot_failed')
            # Stop only the owned candidate. Failure to confirm stop never becomes
            # success; do not start an old writer or restore SQLite in uncertainty.
            try:
                self.mutate('stop','container',APP,lambda:self.compose(release,'stop','teams-transcription'))
                if previous:
                    current=self.runtime('state',image=image)
                    require(current['schema']==schema and current['config_sha256']==runtime_config
                        and current['quiescent'],'rollback_schema_or_cleanup_interlock')
                    self.release=old
                    self.mutate('up','container',APP,lambda:self.compose(old,'up','-d','--no-build','--pull','never',
                        '--wait','--wait-timeout','90','teams-transcription'))
                    restored,_=self.health(state['image'])
                    self.runtime('disable')
                    self.write(self.root/'state.json',json.dumps({**state,'phase':'healthy','container_id':restored}))
                    self.failures.cleanup='rollback_verified'
                    self.capture('rollback')
            except Exception as cleanup_error:
                self.failures.cleanup='unknown'
                self.failures.record(cleanup_error,'cleanup','cleanup_failed')
            raise
        # Releasing maintenance is last. If unknown, retain switching; never
        # blindly rollback/stop after claims might have resumed.
        self.runtime('disable')
        self.write(self.root/'state.json',json.dumps(dict(phase='healthy',source_sha=receipt['source_sha'],
            image=image,container_id=ident,release=release.name,previous_release=state['release'] if state else None,
            volume_created_at=self.probe('volume',VOLUME)['CreatedAt'],network_id=self.probe('network',PROJECT)['Id'])))
        self.failures.cleanup='not_required'
        return smoke


def manual_environment():
    require(os.environ.get('GITHUB_REPOSITORY')==REPO and os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch'
        and os.environ.get('GITHUB_REF')=='refs/heads/main' and os.environ.get('RUNNER_ENVIRONMENT')=='self-hosted',
        'manual_self_hosted_main_required')
    sha=os.environ.get('GITHUB_SHA','')
    require(re.fullmatch('[0-9a-f]{40}',sha),'source_sha_invalid')
    require(execute(['git','rev-parse','HEAD']).strip()==sha,'checkout_mismatch')
    require(not execute(['git','status','--porcelain','--untracked-files=no']).strip(),'tracked_source_dirty')
    return sha


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['deploy','prepare-home','verify','install-google-client',*PRIVATE_CONTROL_ACTIONS])
    args=p.parse_args()
    failures=Failures();sha=None;run=None;attempt=None;evidence=None
    try:
        os.umask(0o077)
        run=int(os.environ['GITHUB_RUN_ID']);attempt=int(os.environ['GITHUB_RUN_ATTEMPT'])
        require(0<run<2**63 and 0<attempt<2**31,'run_identity_invalid')
        sha=manual_environment()
        api=GitHub(os.environ.get('GH_TOKEN'))
        receipt=collect(sha,api)  # readonly; before private state/secret writes

        root=Path.home()/'.devflow-live'/'timmypai-teams-transcription'
        root.parent.mkdir(mode=0o700,exist_ok=True)
        created=not root.exists()
        if created:root.mkdir(mode=0o700)
        require(root.absolute()==root.resolve() and root.stat().st_uid==os.getuid()
            and not root.stat().st_mode&0o077,'state_root_unsafe')
        c=Controller(root,run,attempt)
        c.failures=failures
        oracle_smoke=None
        if args.action=='deploy':
            from deploy.target_smoke import OracleSmoke
            evidence=ROOT/f'.devflow/evidence-deploy-{run}-{attempt}'
            oracle_smoke=OracleSmoke(evidence,sha,run,attempt,api)
        if created:c.write(root/'owner.json',json.dumps(dict(repository=REPO,uid=os.getuid())))
        require(c.read(root/'owner.json')==dict(repository=REPO,uid=os.getuid()),'state_owner_collision')
        # Fixed private state, lock held across deploy/control (same workflow group).
        lock=os.open(root/'operation.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(lock,'r+b') as fd:
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            state=c.read(root/'state.json') if (root/'state.json').exists() else None
            require(state is None or state.get('phase')=='healthy','interrupted_switch_requires_review')
            before=c.preflight(state)
            try:
                if args.action=='deploy':
                    if state and state['source_sha']==sha:
                        _,result=c.health(state['image'])  # idempotent success, no rebuild/restart
                    else:
                        image=c.build(sha)
                        # Fresh latest-tip/review/run admission again after long build.
                        receipt=collect(sha,GitHub(os.environ.get('GH_TOKEN')))
                        result=c.switch(receipt,image,state)
                    # These assertions run against this installed service while
                    # the original host lock/admission still hold. Never run the
                    # disposable verifier or consume prior readiness receipts.
                    oracle_smoke.health(c)
                    oracle_smoke.login(c)
                else:
                    require(state and state['source_sha']==sha,'deployed_revision_required')
                    c.release=root/'releases'/state['release'];c.verify_release(c.release)
                    if args.action=='install-google-client':
                        result=c.install_google_client(state,os.environ.pop('TEAMS_GOOGLE_CLIENT_JSON',''))
                    elif args.action=='verify':
                        info=json.loads(c.docker('inspect',APP))[0]
                        image_info=json.loads(c.docker('image','inspect',state['image']))[0]
                        labels=image_info['Config']['Labels']
                        require(labels['org.opencontainers.image.revision']==sha
                            and labels['org.opencontainers.image.source']=='https://github.com/'+REPO
                            and labels[OWNER]==REPO,'deployed_source_mismatch')
                        ident,_=c.health(state['image'])
                        require(ident==state['container_id'],'deployed_container_mismatch')
                        result=c.runtime('verify',input=os.environ.get('DEVFLOW_APP_ENV',''))
                        result['runtime_metadata']=c.runtime('metadata')
                        result['runtime_metadata']['data_volume_rw']=any(
                            m.get('Type')=='volume' and m.get('Name')==VOLUME and m.get('Destination')=='/data'
                            and m.get('RW') is True for m in info['Mounts'])
                        after=json.loads(c.docker('inspect',APP))[0]
                        require(after['Id']==ident and after['State']['StartedAt']==info['State']['StartedAt'],
                            'deployed_container_changed')
                        c.health(state['image'])
                        result.update(container_id=ident,image=state['image'],uid=10001,source_sha=sha)
                    elif args.action in PRIVATE_CONTROL_ACTIONS:
                        with failures.stage('control_'+args.action):
                            actor=dispatch_identity(api,sha,run,attempt)
                            # resources() already verifies exact container identity.
                            # Stop/mute must still work when the app is unhealthy.
                            if args.action in {'preview','join-confirmed-present-and-media'}:c.health(state['image'])
                            result=run_control(c,args.action,sha,state['container_id'],actor,
                                os.environ.pop('DEVFLOW_OPS_ENV',''))
                    else:
                        c.health(state['image'])
                        with failures.stage('control_'+args.action):
                            result=c.runtime(args.action)
                        if args.action=='prepare-home':
                            installed=json.loads(c.docker('inspect',APP))[0]
                            writable=any(m.get('Type')=='volume' and m.get('Name')==VOLUME
                                and m.get('Destination')=='/data' and m.get('RW') is True for m in installed['Mounts'])
                            require(writable and installed['Id']==state['container_id'] and installed['Image']==state['image'],
                                'deployed_persistence_unverified')
                            result.update(data_volume_rw=True,container_id=installed['Id'],image=state['image'],source_sha=sha)
            except Exception as exc:
                failures.record(exc)
                raise
            finally:
                # A failed snapshot/audit must not hide the build/operation
                # failure, nor manufacture an audit or successful receipt.
                try:
                    report=c.audit.report(before,snapshot(c.containers()))
                    c.write(root/f'audit-{run}-{attempt}.json',json.dumps(report))
                    evidence=ROOT/f'.devflow/evidence-deploy-{run}-{attempt}'
                    evidence.mkdir(parents=True,exist_ok=True)
                    if args.action not in PRIVATE_CONTROL_ACTIONS:
                        (evidence/'audit.json').write_text(json.dumps(report,indent=2)+'\n')
                        for phase in ('candidate','rollback'):
                            diagnostic=root/f'diagnostics-{run}-{attempt}-{phase}.json'
                            if diagnostic.exists():
                                (evidence/(phase+'.json')).write_text(diagnostic.read_text())
                except Exception as exc:
                    failures.record(exc,'audit','audit_failed')
                    raise
            if oracle_smoke is not None:
                oracle_smoke.isolation(report,c.audit.allowlist)
                verified=oracle_smoke.finish()
                # Missing operator access must fail the run, but does not turn
                # an already healthy installed service into an unsafe rollback.
                require(verified,'oracle_smoke_unverified')
            require(report['foreign_unchanged'],'foreign_snapshot_changed')
            public_receipt = dict(repository=REPO,source_sha=sha) if args.action in PRIVATE_CONTROL_ACTIONS else receipt
            (evidence/'receipt.json').write_text(json.dumps({**public_receipt,'operation':args.action,'result':result,
                'deploy_run_url':f'https://github.com/{REPO}/actions/runs/{run}',
                'scope':'oracle-manual' if args.action=='deploy' else 'same-deployment-local-control',
                'audit':dict(foreign_unchanged=True) if args.action in PRIVATE_CONTROL_ACTIONS else report},indent=2)+'\n')
        return 0
    except Exception as exc:
        failures.record(exc)
        # Public evidence only. Invalid/unavailable identities are null, never
        # copied verbatim from untrusted environment or exception messages.
        value=failures.document(REPO,sha if isinstance(sha,str) and re.fullmatch('[0-9a-f]{40}',sha) else None,
            run if type(run) is int and 0<run<2**63 else None,
            attempt if type(attempt) is int and 0<attempt<2**31 else None,args.action)
        try:
            if value['run_id'] is not None and value['run_attempt'] is not None:
                evidence=ROOT/f'.devflow/evidence-deploy-{run}-{attempt}'
                evidence.mkdir(parents=True,exist_ok=True)
                (evidence/'failure.json').write_text(json.dumps(value,indent=2)+'\n')
        except Exception as write_error:
            failures.record(write_error,'audit','evidence_write_failed')
            value['secondary_failures']=failures.secondary
        print(json.dumps(value))
        return 1


if __name__=='__main__':raise SystemExit(main())
