#!/usr/bin/python3
"""Installed fixed QA bridge; never imports or executes a runner checkout.

Same-UID workflows retain that user's authority. Root-owned installation and the
exact job hook are trust controls, not a sandbox against a compromised runner UID.
"""
import base64
import contextlib
import copy
import fcntl
import hashlib
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import signal
import stat
import sys
import tempfile
import uuid

BIN=Path('/opt/teams-staging/bin')
BUNDLE=Path('/opt/teams-staging/qa-candidate')
ISOLATED_BUNDLE=Path('/opt/teams-staging/qa-isolated-candidate')
SEMANTIC_BUNDLE=Path('/opt/teams-staging/qa-semantic-v1-candidate')
SEMANTIC_MANIFEST='semantic-isolated-candidate-v1.json'
STARTUP_BUNDLE=Path('/opt/teams-staging/qa-semantic-startup-v1-candidate')
STARTUP_MANIFEST='semantic-startup-candidate-v1.json'
STARTUP_MODES=('semantic-startup-v1-offline','semantic-startup-v1-isolated')
SEMANTIC_EXPERIMENTS=('semantic-isolated','semantic-startup-isolated')
STATE=Path('/srv/teams-staging/state')
INTERLOCK='qa-cleanup-interlock.json'
MANIFEST='passive-audio-candidate-manifest-20261030.json'
CANDIDATE_FILES=('qa_audio_observation.py','qa_audio_tap.py','qa_audio_probe.py',MANIFEST,
    'check_qa_audio_candidate.py')
ISOLATED_MANIFEST='isolated-qa-candidate-manifest-20261030.json'


class Refused(Exception):pass


def fixed_file(path,*,owner,private=False,maximum=131072):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as source:
        info=os.fstat(source.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=owner
                or info.st_mode&(0o077 if private else 0o022)):
            raise Refused('qa_untrusted_file')
        value=source.read(maximum+1)
    if len(value)>maximum:raise Refused('qa_file_cap')
    return value


def directory(path,*,owner,private=False):
    if path.is_symlink() or not path.is_dir():raise Refused('qa_untrusted_directory')
    info=path.stat()
    if info.st_uid!=owner or info.st_mode&(0o077 if private else 0o022):
        raise Refused('qa_untrusted_directory')


@contextlib.contextmanager
def existing_lock(path):
    # No O_CREAT: unknown global authority is never silently initialized here.
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
            raise Refused('qa_private_lock_required')
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Refused('qa_operation_in_progress') from None
        yield
    finally:os.close(fd)


def exclusive(path,raw):
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'wb') as output:output.write(raw);output.flush();os.fsync(output.fileno())


def synced_directory(path):
    fd=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(fd)
    finally:os.close(fd)


def save_ledger(path,value,*,state):
    old=fixed_file(path,owner=os.getuid(),private=True,maximum=65536)
    history=state/'qa-ledger-history';history.mkdir(mode=0o700,exist_ok=True)
    directory(history,owner=os.getuid(),private=True)
    archive=history/(hashlib.sha256(old).hexdigest()+'.json')
    try:exclusive(archive,old)
    except FileExistsError:
        if fixed_file(archive,owner=os.getuid(),private=True,maximum=65536)!=old:
            raise Refused('qa_history_mismatch') from None
    synced_directory(history)
    raw=(json.dumps(value,sort_keys=True,allow_nan=False)+'\n').encode()
    if len(raw)>65536:raise Refused('qa_ledger_cap')
    temporary=path.with_name('.qa-ledger-'+uuid.uuid4().hex)
    try:
        exclusive(temporary,raw);os.replace(temporary,path);synced_directory(path.parent)
    finally:
        with contextlib.suppress(FileNotFoundError):temporary.unlink()


def reserve(ledger,*,run_id,attempt,starts,experiment,candidate_sha):
    """Shared adapter: passive needs1, separate isolated QA needs2 upfront.

    Each experiment has its own fixed container loader and result projection.
    """
    expected={'passive':('d5f729f6db228cc60b67674a031e99bc0545629b',1),
        'isolated':('836ecbe88d69b14a66326474d08a4c26d15c1e90',2),
        'semantic-isolated':('1849e60b98e5cc93a5aef7bda5ecaf432dede1fc',2),
        'semantic-startup-isolated':('e82bb30900e90c9559b9c3dcb71e532cad86b72e',2)}
    if (experiment not in expected or expected[experiment]!=(candidate_sha,starts)
            or type(starts) is not int or type(run_id) is not int or not 0<run_id<10**20
            or type(attempt) is not int or not 0<attempt<1000000):
        raise Refused('qa_reservation_rejected')
    if any(row.get('run_id')==run_id for row in ledger['entries']):
        raise Refused('qa_run_already_reserved')
    if ledger['consumed']+starts>50:raise Refused('qa_budget_exhausted')
    result=copy.deepcopy(ledger)
    for start_index in range(1,starts+1):
        result['consumed']+=1
        result['entries'].append({'slot':result['consumed'],'start_index':start_index,
            'run_id':run_id,'run_attempt':attempt,'status':'reserved',
            'experiment':experiment,'candidate_sha':candidate_sha})
    return result


def authority(state,config,*,owner=0):
    value=json.loads(fixed_file(config,owner=owner,maximum=4096))
    if value.get('legacy_writers_coordinated') is not True:
        raise Refused('qa_legacy_writers_not_reconciled')
    paths=[]
    for key in ('ledger_path','lock_path'):
        raw=value.get(key)
        if not isinstance(raw,str):raise Refused('qa_authority_unknown')
        path=Path(raw)
        # Only direct children of the already private state directory. Importing
        # an older different format/path is a separate reviewed handoff, not reset.
        if not path.is_absolute() or path.parent!=state or path.name in (INTERLOCK,'controller.lock'):
            raise Refused('qa_authority_path_rejected')
        paths.append(path)
    if paths[0]==paths[1]:raise Refused('qa_authority_path_rejected')
    return value,*paths


def operation_plan(mode,contract):
    if mode in STARTUP_MODES:
        return 'semantic-startup-isolated',mode=='semantic-startup-v1-isolated',2,contract.SEMANTIC_SHA,'semantic-startup-v1-offline',3
    if mode in ('offline','observe'):
        return 'passive',mode=='observe',1,contract.CANDIDATE_SHA,'offline',3
    if mode in ('isolated-offline','isolated'):
        return 'isolated',mode=='isolated',2,contract.ISOLATED_SHA,'isolated-offline',2
    if mode in ('semantic-v1-offline','semantic-v1-isolated'):
        return 'semantic-isolated',mode=='semantic-v1-isolated',2,contract.SEMANTIC_SHA,'semantic-v1-offline',3
    raise Refused('qa_operation_rejected')


def settle_attempt(ledger,safe,*,run_id,attempt,experiment,contract):
    """Settle this attempt's complete reservation together; never refund a slot."""
    if experiment not in ('passive','isolated',*SEMANTIC_EXPERIMENTS):raise Refused('qa_reservation_ownership_rejected')
    count=1 if experiment=='passive' else 2
    sha=contract.SEMANTIC_SHA if experiment in SEMANTIC_EXPERIMENTS else (contract.ISOLATED_SHA if experiment=='isolated' else contract.CANDIDATE_SHA)
    if experiment in SEMANTIC_EXPERIMENTS and (safe.get('criterion_version')!=contract.SEMANTIC_VERSION
            or safe.get('candidate_manifest_sha256')!=contract.SEMANTIC_MANIFEST_SHA256):
        raise Refused('qa_semantic_receipt_rejected')
    if safe.get('candidate_sha')!=sha or safe.get('owned_cleanup_verified') is not True:
        raise Refused('qa_cleanup_requires_reconciliation')
    attempts=1
    if experiment in ('isolated',*SEMANTIC_EXPERIMENTS):
        attempts=safe.get('model_start_attempts');sessions=safe.get('sessions')
        observed=safe.get('observed_native_start_attempts')
        if (safe.get('native_start_count_complete') is not True or not contract.integer(attempts,1,2)
                or not contract.integer(observed,0,attempts) or not isinstance(sessions,list)
                or len(sessions)!=attempts):
            raise Refused('qa_cleanup_requires_reconciliation')
        for index,row in enumerate(sessions,1):
            if (row.get('session_number')!=index or row.get('source_revision')!=index
                    or row.get('cleanup_verified') is not True or type(row.get('start_attempted')) is not bool):
                raise Refused('qa_cleanup_requires_reconciliation')
        if observed!=sum(row['start_attempted'] is True for row in sessions):
            raise Refused('qa_cleanup_requires_reconciliation')
    own=[i for i,row in enumerate(ledger['entries']) if row.get('run_id')==run_id and row.get('run_attempt')==attempt]
    if len(own)!=count or any(ledger['entries'][i].get('start_index')!=n
            or ledger['entries'][i].get('candidate_sha')!=sha or ledger['entries'][i].get('experiment')!=experiment
            or ledger['entries'][i].get('status')!='reserved' for n,i in enumerate(own,1)):
        raise Refused('qa_reservation_ownership_rejected')
    result=copy.deepcopy(ledger)
    receipt=hashlib.sha256(json.dumps(safe,sort_keys=True,allow_nan=False).encode()).hexdigest()
    for n,i in enumerate(own,1):
        started=(safe['sessions'][n-1]['start_attempted'] if experiment!='passive' else True) if n<=attempts else False
        result['entries'][i].update(status='cleaned' if n<=attempts else 'not_started',
            native_start_attempted=started,public_receipt_sha256=receipt,qa_accepted=safe.get('accepted') is True)
    return result


def perform(mode,*,backend,state,run_id,attempt,authorized=False,authority_path=None,contract=None,
        authority_loader=authority):
    """System seam: backend is the fixed Docker/controller boundary in production."""
    if mode in STARTUP_MODES and hasattr(contract,'startup_profile'):
        contract=contract.startup_profile()
    experiment,live,starts,candidate_sha,offline_mode,modules=operation_plan(mode,contract)
    if live and authorized is not True:raise Refused('qa_live_not_authorized')
    if mode in STARTUP_MODES:contract.require_release_ready()
    with existing_lock(state/'controller.lock'):
        runtime=backend.runtime()
        if mode in STARTUP_MODES:
            contract.validate_runtime(runtime)  # Missing release stops before any child or quota I/O.
        # Offline must also check fixed healthy paused/idle/maintenance, but must
        # not invent a ledger merely to call the ledger preflight helper.
        backend.validate_runtime(runtime)
        offline=backend.execute(offline_mode,run_id,attempt)
        if (offline.get('success') is not True or offline.get('candidate_verified') is not True
                or offline.get('base_verified') is not True or offline.get('model_started') is not False
                or offline.get('candidate_sha')!=candidate_sha
                or type(offline.get('modules')) is not int or offline['modules']!=modules):
            raise Refused('qa_offline_check_failed')
        if experiment in SEMANTIC_EXPERIMENTS and (offline.get('criterion_version')!=contract.SEMANTIC_VERSION
                or offline.get('candidate_manifest_sha256')!=contract.SEMANTIC_MANIFEST_SHA256):
            raise Refused('qa_semantic_offline_rejected')
        if not live:return {'success':True,'operation':mode,'candidate_verified':True,
            'base_verified':True,'model_started':False,'candidate_sha':candidate_sha,
            'base_source_sha':contract.BASE_SHA,'image':contract.IMAGE,
            'ledger_touched':False,'live_ready':False,**({'criterion_version':contract.SEMANTIC_VERSION,
                'candidate_manifest_sha256':contract.SEMANTIC_MANIFEST_SHA256,'candidate_identity_kind':
                    'git-source-and-manifest' if experiment=='semantic-startup-isolated' else 'git-commit-and-manifest'}
                if experiment in SEMANTIC_EXPERIMENTS else {})}
        config,ledger_path,lock_path=authority_loader(state,authority_path)
        with existing_lock(lock_path):
            marker=state/INTERLOCK
            if marker.exists() or marker.is_symlink():raise Refused('qa_cleanup_requires_reconciliation')
            ledger=json.loads(fixed_file(ledger_path,owner=os.getuid(),private=True,maximum=65536))
            # Re-read after acquiring both locks. No cached quiescence admission.
            runtime=backend.runtime()
            contract.preflight(runtime,ledger,config.get('checkpoint_sha256'),maximum_native_starts=starts)
            ledger=reserve(ledger,run_id=run_id,attempt=attempt,starts=starts,
                experiment=experiment,candidate_sha=candidate_sha)
            save_ledger(ledger_path,ledger,state=state)
            exclusive(marker,json.dumps({'run_id':run_id,'run_attempt':attempt,
                'candidate_sha':candidate_sha,'reserved':starts}).encode())
            synced_directory(state)
            payload=backend.execute(mode,run_id,attempt)
            raw=payload.get('result',{})
            if not isinstance(raw,dict) or raw.get('candidate_sha')!=candidate_sha:
                raise Refused('qa_child_result_rejected')
            if experiment in SEMANTIC_EXPERIMENTS:
                safe=contract.public_semantic_result(raw,run_id=run_id,run_attempt=attempt,projected=True)
            else:safe=(contract.public_isolated_result(raw,run_id=run_id,run_attempt=attempt,projected=True)
                if experiment=='isolated' else contract.public_result({**raw,'audio_observer':raw.get('observer')},
                    run_id=run_id,run_attempt=attempt))
            ledger=settle_attempt(ledger,safe,run_id=run_id,attempt=attempt,experiment=experiment,contract=contract)
            save_ledger(ledger_path,ledger,state=state)
            marker.unlink();synced_directory(state)
            return {'success':safe.get('accepted') is True,'operation':mode,'result':safe,
                'cleanup_verified':True,'consumed':ledger['consumed'],'remaining':50-ledger['consumed']}


def installed_module(name,path):
    fixed_file(path,owner=0)
    loader=importlib.machinery.SourceFileLoader(name,str(path))
    module=importlib.util.module_from_spec(importlib.util.spec_from_loader(name,loader))
    loader.exec_module(module)
    return module


class DockerBackend:
    def __init__(self,controller,staging,contract):
        self.controller,self.staging,self.contract=controller,staging,contract
        self.entry=fixed_file(BIN/'qa_container_entry.py',owner=0).decode('utf-8')
        self.ident=None
    def candidate_sources(self,mode):
        experiment,_,_,_,_,_=operation_plan(mode,self.contract)
        root=STARTUP_BUNDLE if experiment=='semantic-startup-isolated' else SEMANTIC_BUNDLE if experiment=='semantic-isolated' else (ISOLATED_BUNDLE if experiment=='isolated' else BUNDLE)
        directory(root,owner=0)
        manifest=STARTUP_MANIFEST if experiment=='semantic-startup-isolated' else SEMANTIC_MANIFEST if experiment=='semantic-isolated' else (ISOLATED_MANIFEST if experiment=='isolated' else MANIFEST)
        names=(*self.contract.SEMANTIC_FILES,manifest) if experiment in SEMANTIC_EXPERIMENTS else ((*self.contract.ISOLATED_FILES,manifest) if experiment=='isolated' else CANDIDATE_FILES)
        sources={name:fixed_file(root/name,owner=0) for name in names}
        sources['qa_cloud_contract.py']=fixed_file(BIN/'qa_cloud_contract.py',owner=0)
        expected_manifest=(self.contract.SEMANTIC_MANIFEST_SHA256 if experiment in SEMANTIC_EXPERIMENTS else self.contract.ISOLATED_MANIFEST_SHA256 if experiment=='isolated'
            else self.contract.MANIFEST_SHA256)
        if hashlib.sha256(sources[manifest]).hexdigest()!=expected_manifest:
            raise Refused('qa_candidate_manifest_mismatch')
        expected=json.loads(sources[manifest])['files']
        if experiment=='passive':
            expected['check_qa_audio_candidate.py']='5711a2b4a1c800a232ce58a524b08e43fb5c75fd07d84fb9c0151baeb2dbb319'
        for name,digest in expected.items():
            if name not in sources or hashlib.sha256(sources[name]).hexdigest()!=digest:
                raise Refused('qa_candidate_source_mismatch')
        return sources
    def runtime(self):
        state=self.staging.read_state()
        self.ident=self.controller.compose('ps','-a','-q','teams-transcription')
        import re
        if not re.fullmatch('[0-9a-f]{64}',self.ident):raise Refused('qa_container_identity_rejected')
        health=self.controller.health()
        from diagnostic_projection import database_flags,release_link
        return {'phase':state.get('phase'),'image':state.get('current'),'container':health,
            'database':database_flags(self.controller.probe(self.contract.IMAGE)),
            'release':release_link(state.get('release'),state.get('current'))}
    def validate_runtime(self,runtime):
        # Reuse actual contract checks without manufacturing any quota checkpoint.
        validate_runtime(runtime,self.contract)
        if self.controller.verify_image(self.contract.IMAGE)!=self.contract.BASE_SHA:
            raise Refused('qa_image_revision_rejected')
    def execute(self,mode,run_id,attempt):
        _,live,_,_,_,_=operation_plan(mode,self.contract)
        request={'operation':mode,'run_id':run_id,'run_attempt':attempt,'authorized':live,
            'files':{name:base64.b64encode(raw).decode() for name,raw in self.candidate_sources(mode).items()}}
        result=self.staging.capture_docker(('exec','-i','--user','10001:10001','--env','CODEX_HOME=/data/codex',
            self.ident,'/usr/local/bin/python','-c',self.entry),self.controller.env,
            input=json.dumps(request).encode(),timeout=170 if live else 30)
        if (result['timed_out'] or result['cleanup_uncertain'] or result['stdout_truncated']
                or result['returncode'] not in (0,1)):
            raise Refused('qa_child_outcome_unknown')
        value=json.loads(result['stdout'])
        if not isinstance(value,dict):raise Refused('qa_child_result_rejected')
        return value


def validate_runtime(runtime,contract):
    # Kept separate so offline mode never needs a ledger/checkpoint placeholder.
    contract.validate_runtime(runtime)


def main():
    os.umask(0o077)
    def cancelled(signum,frame):raise Refused('qa_bridge_cancelled')
    signal.signal(signal.SIGINT,cancelled);signal.signal(signal.SIGTERM,cancelled)
    try:
        if (len(sys.argv)!=2 or os.getuid()==0 or os.environ.get('GITHUB_REPOSITORY')!='paulpai0412/teams-transcription'
                or os.environ.get('GITHUB_EVENT_NAME')!='workflow_dispatch'
                or os.environ.get('GITHUB_REF')!='refs/heads/main'
                or os.environ.get('GITHUB_WORKFLOW_REF')!='paulpai0412/teams-transcription/.github/workflows/oracle-qa.yml@refs/heads/main'):
            raise Refused('qa_trusted_job_required')
        run,attempt=(os.environ.get(key,'') for key in ('GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT'))
        if not run.isascii() or not run.isdigit() or not 0<int(run)<10**20 \
                or not attempt.isascii() or not attempt.isdigit() or not 0<int(attempt)<1000000:
            raise Refused('qa_job_identity_rejected')
        for path in (Path('/opt'),BIN.parent,BIN,BUNDLE,Path('/srv'),STATE.parent):directory(path,owner=0)
        directory(STATE,owner=os.getuid(),private=True)
        staging=installed_module('qa_stagingctl',BIN/'stagingctl')
        contract=installed_module('qa_cloud_contract',BIN/'qa_cloud_contract.py')
        if sys.argv[1] in STARTUP_MODES:contract=contract.startup_profile()
        with staging.registry_config('diagnostics') as config:
            backend=DockerBackend(staging.Controller(config),staging,contract)
            result=perform(sys.argv[1],backend=backend,state=STATE,run_id=int(run),attempt=int(attempt),
                authorized=os.environ.get('STAGING_QA_AUTHORIZED')=='true',
                authority_path=BUNDLE/'qa-authority.json',contract=contract)
        print(json.dumps(result,allow_nan=False))
        return 0 if result['success'] else 1
    except BaseException as error:
        code=error.args[0] if type(error) is Refused else 'qa_bridge_failed'
        if type(error) is ValueError and error.args==('release_not_ready',):code='release_not_ready'
        print(json.dumps({'success':False,'error_code':code,'cleanup_verified':False,
            'model_request_possible':(STATE/INTERLOCK).exists()}))
        return 1


if __name__=='__main__':raise SystemExit(main())
