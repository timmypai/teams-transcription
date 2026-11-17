#!/usr/bin/python3
"""One fixed authenticated Teams acceptance run; installation is not authorized here.

No arbitrary URL/command/model input, password login or ledger initialization
or retry. Shares the existing rootless controller and canonical QA ledger/locks.
Release pins intentionally await a built image containing the runtime RPC caps.
"""
import copy
import hashlib
import importlib.machinery
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import time
from datetime import datetime

VERSION='teams-single-v1'
PREPARE_VERSION='teams-source-v2'
IMMEDIATE_MODE='owner_immediate'
STATE=Path('/srv/teams-staging/state')
BIN=Path('/opt/teams-staging/bin')
QA_AUTHORITY=Path('/opt/teams-staging/qa-candidate/qa-authority.json')
AUTHORITY=Path('/opt/teams-staging/qa-candidate/teams-acceptance-authority.json')
PREPARED_NAME='teams-prepared.json'
CASE_DIRECTORY=AUTHORITY.parent/'teams-cases'
CASE_VERSION='teams-case-v1'
RELEASE_SOURCE_SHA='6e53b1f2a0cee5badd6a7abdcc2a79586b5158b3'
IMAGE='ghcr.io/paulpai0412/teams-transcription@sha256:497b9556aaeabab36d1f0f0a8068b3203b1f2abbaf0c99ab2d44bcd8cb37183a'
BUILD_RUN_ID=41668506996
BUILD_RUN_ATTEMPT=1


class Refused(Exception):
    def __init__(self,code,*,cleanup_code=None):
        super().__init__(code)
        self.cleanup_code=cleanup_code


def error_report(error):
    safe=str(error) if isinstance(error,Refused) and re.fullmatch('[a-z0-9_]{1,80}',str(error)) else 'teams_acceptance_failed'
    result=dict(success=False,error_code=safe)
    if isinstance(error,Refused) and error.cleanup_code=='teams_maintenance_restore_unverified':
        result['cleanup_error_code']=error.cleanup_code
    return result


def restore_maintenance(backend,primary=None):
    try:
        after=backend.maintenance(True)
        if after.get('maintenance') is not True or after.get('scheduler_paused') is not True:
            raise Refused('teams_maintenance_restore_unverified')
    except BaseException:
        if primary is None:raise Refused('teams_maintenance_restore_unverified') from None
        raise Refused(error_report(primary)['error_code'],cleanup_code='teams_maintenance_restore_unverified') from None


def require_release():
    if (not isinstance(RELEASE_SOURCE_SHA,str) or not re.fullmatch('[0-9a-f]{40}',RELEASE_SOURCE_SHA)
            or not isinstance(IMAGE,str) or not re.fullmatch(r'ghcr\.io/paulpai0412/teams-transcription@sha256:[0-9a-f]{64}',IMAGE)
            or type(BUILD_RUN_ID) is not int or BUILD_RUN_ID<=0 or type(BUILD_RUN_ATTEMPT) is not int or BUILD_RUN_ATTEMPT<=0):
        raise Refused('teams_acceptance_release_not_ready')


def validate_authority(value,view,now,*,presence=True,immediate=False):
    required={'version','occurrence_id','occurrence_revision','maximum_seconds','participant_confirmed_until','egress_policy_sha256'}
    if (not isinstance(value,dict) or set(value)!=required or value['version']!=VERSION
            or not isinstance(value['occurrence_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['occurrence_id'])
            or not isinstance(value['egress_policy_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['egress_policy_sha256'])
            or type(value['occurrence_revision']) is not int or value['occurrence_revision']<1
            or value['maximum_seconds'] is not None
            or presence and (type(value['participant_confirmed_until']) not in (int,float)
                or not now<value['participant_confirmed_until']<=now+300)):
        raise Refused('teams_acceptance_authority_rejected')
    if (view.get('protocol')!=VERSION or view.get('occurrence_id')!=value['occurrence_id']
            or view.get('revision')!=value['occurrence_revision'] or view.get('occurrence_state')!='pending'
            or view.get('session_id') is not None or view.get('active_lease') is not False
            or view.get('scheduler_paused') is not True or view.get('capture_interlock') is not False
            or view.get('recording_authorized') is not True or view.get('speaking_authorized') is not True
            or view.get('egress_allows_target_host') is not True
            or view.get('egress_policy_sha256')!=value['egress_policy_sha256']
            or view.get('join_host') not in ('teams.microsoft.com','teams.cloud.microsoft','teams.live.com')):
        raise Refused('teams_acceptance_target_rejected')
    if immediate:
        if (view.get('admission_mode')!=IMMEDIATE_MODE
                or any(k not in view or view[k] is not None for k in ('start_at','end_at','max_lateness_seconds'))):
            raise Refused('teams_immediate_contract_rejected')
        return
    try:
        start=datetime.fromisoformat(view['start_at']).timestamp();end=datetime.fromisoformat(view['end_at']).timestamp()
    except (ValueError,KeyError,TypeError):raise Refused('teams_acceptance_target_rejected') from None
    lateness=view.get('max_lateness_seconds')
    if (type(lateness) is not int or lateness<0 or now>start+lateness
            or end<=start or presence and not start<=now<end):
        raise Refused('teams_acceptance_window_rejected')


def preparation_authority(value,view):
    keys={'version','source_occurrence_id','source_revision','mode','maximum_seconds','egress_policy_sha256'}
    if (not isinstance(value,dict) or set(value)!=keys or value['version']!=PREPARE_VERSION
            or not isinstance(value['source_occurrence_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['source_occurrence_id'])
            or type(value['source_revision']) is not int or value['source_revision']<1 or value['maximum_seconds'] is not None
            or value['mode']!=IMMEDIATE_MODE
            or not isinstance(value['egress_policy_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['egress_policy_sha256'])):
        raise Refused('teams_preparation_authority_rejected')
    if (view.get('protocol')!=VERSION or view.get('join_host') not in {'teams.microsoft.com','teams.cloud.microsoft','teams.live.com'}
            or view.get('occurrence_id')!=value['source_occurrence_id'] or view.get('revision')!=value['source_revision']
            or view.get('occurrence_state') not in {'failed','completed'}
            or view.get('active_lease') is not False or view.get('capture_interlock') is not False
            or view.get('scheduler_paused') is not True or view.get('recording_authorized') is not True
            or view.get('speaking_authorized') is not True or view.get('egress_allows_target_host') is not True
            or view.get('egress_policy_sha256')!=value['egress_policy_sha256']):
        raise Refused('teams_preparation_source_rejected')
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def confirmed_until(confirmed_at,now):
    try:
        confirmed=datetime.fromisoformat(confirmed_at)
        if confirmed.tzinfo is None:raise ValueError()
        stamp=confirmed.timestamp()
        if not math.isfinite(stamp) or not 0<=now-stamp<300:raise ValueError()
    except (ValueError,TypeError,OverflowError):raise Refused('teams_fresh_presence_required') from None
    return stamp+300  # actual supplied confirmation, never synthesized from execution time


def validate_case(case_id):
    if case_id is not None and (not isinstance(case_id,str) or not re.fullmatch('[a-z0-9][a-z0-9-]{0,47}',case_id)):
        raise Refused('teams_case_rejected')
    return case_id


def case_request(authority,case_id):
    if case_id is None:return authority
    return dict(contract=CASE_VERSION,case_id=case_id,authority=authority)


def action_request(run,attempt,occurrence,case_id):
    value=f'{VERSION}:{run}:{attempt}:{occurrence}'
    if case_id is not None:value=f'{CASE_VERSION}:{case_id}:'+value
    return hashlib.sha256(value.encode()).hexdigest()


def prepared_target(mode,backend,bridge,state,authority,*,confirmed_at,now,case_id=None):
    source=backend.view(authority.get('source_occurrence_id'))
    request=preparation_authority(authority,source)
    if case_id is not None:
        request=hashlib.sha256(json.dumps(case_request(authority,case_id),sort_keys=True,separators=(',',':')).encode()).hexdigest()
    path=state/(PREPARED_NAME if case_id is None else 'teams-case-'+case_id+'.prepared.json')
    exists=path.exists() or path.is_symlink()
    if exists:
        previous=json.loads(bridge.fixed_file(path,owner=os.getuid(),private=True,maximum=4096))
        if not isinstance(previous,dict) or previous.get('request_sha256')!=request or previous.get('version')!=PREPARE_VERSION:
            raise Refused('teams_preparation_receipt_conflict')
    if mode=='prepare' and not exists:
        value=backend.control('prepare',dict(source_id=authority['source_occurrence_id'],
            source_revision=authority['source_revision'],request_sha256=request,
            mode=authority['mode']))
        if (value.get('prepared') is not True or value.get('request_sha256')!=request or type(value.get('revision')) is not int or value['revision']!=1
                or not isinstance(value.get('occurrence_id'),str) or not re.fullmatch('[0-9a-f]{32}',value['occurrence_id'])):
            raise Refused('teams_preparation_response_unknown')
        record=dict(version=PREPARE_VERSION,request_sha256=request,occurrence_id=value['occurrence_id'],revision=1)
        bridge.exclusive(path,json.dumps(record).encode());bridge.synced_directory(state)
    record=json.loads(bridge.fixed_file(path,owner=os.getuid(),private=True,maximum=4096))
    if (set(record)!={'version','request_sha256','occurrence_id','revision'} or record['version']!=PREPARE_VERSION
            or record['request_sha256']!=request or record['revision']!=1 or type(record['revision']) is not int
            or not isinstance(record['occurrence_id'],str) or not re.fullmatch('[0-9a-f]{32}',record['occurrence_id'])):
        raise Refused('teams_preparation_receipt_rejected')
    view=backend.view(record['occurrence_id'])
    if view.get('preparation_request_sha256')!=request:
        raise Refused('teams_preparation_binding_rejected')
    effective=dict(version=VERSION,occurrence_id=record['occurrence_id'],occurrence_revision=1,maximum_seconds=None,
        egress_policy_sha256=authority['egress_policy_sha256'],
        participant_confirmed_until=confirmed_until(confirmed_at,now) if mode=='run' else None)
    validate_authority(effective,view,now,presence=mode=='run',immediate=True)
    return effective,view


def reserve(ledger,run,attempt,source):
    if (type(run) is not int or not 0<run<10**20 or type(attempt) is not int or not 0<attempt<1000000
            or not isinstance(source,str) or not re.fullmatch('[0-9a-f]{40}',source)):
        raise Refused('qa_reservation_rejected')
    if any(row.get('run_id')==run for row in ledger['entries']):raise Refused('qa_run_already_reserved')
    if ledger['consumed']+2>50:raise Refused('qa_budget_exhausted')
    result=copy.deepcopy(ledger)
    for index in (1,2):
        result['consumed']+=1
        result['entries'].append(dict(slot=result['consumed'],start_index=index,run_id=run,run_attempt=attempt,
            status='reserved',experiment=VERSION,candidate_sha=source))
    return result


def settle(ledger,proof,run,attempt,source):
    if (proof.get('cleanup_verified') is not True or proof.get('count_complete') is not True
            or proof.get('summary_disabled') is not True
            or type(proof.get('thread_attempts')) is not int or type(proof.get('native_attempts')) is not int
            or not 0<=proof['native_attempts']<=proof['thread_attempts']<=2):
        raise Refused('qa_cleanup_requires_reconciliation')
    indices=[i for i,r in enumerate(ledger['entries']) if r.get('run_id')==run and r.get('run_attempt')==attempt]
    rows=[ledger['entries'][i] for i in indices]
    if len(rows)!=2 or any(r.get('status')!='reserved' or r.get('experiment')!=VERSION
            or r.get('candidate_sha')!=source or r.get('start_index')!=index for index,r in enumerate(rows,1)):
        raise Refused('qa_reservation_ownership_rejected')
    for role in ('A','B'):
        if type(proof.get(role+'_thread_attempted')) is not bool or type(proof.get(role+'_native_attempted')) is not bool \
                or proof[role+'_native_attempted'] and not proof[role+'_thread_attempted']:
            raise Refused('qa_cleanup_requires_reconciliation')
    if (sum(proof[r+'_thread_attempted'] for r in ('A','B'))!=proof['thread_attempts']
            or sum(proof[r+'_native_attempted'] for r in ('A','B'))!=proof['native_attempts']):
        raise Refused('qa_cleanup_requires_reconciliation')
    result=copy.deepcopy(ledger);digest=hashlib.sha256(json.dumps(proof,sort_keys=True).encode()).hexdigest()
    for index,role in zip(indices,('A','B')):
        row=result['entries'][index]
        row.update(status='cleaned' if proof[role+'_thread_attempted'] else 'not_started',
            native_start_attempted=proof[role+'_thread_attempted'],public_receipt_sha256=digest)
    return result


class LocalControl:
    """Fixed installed CLI inside the release-verified container; no web session."""
    def __init__(self,staging,env):
        self.staging,self.env=staging,env
        self.ident=None

    def request(self,operation,fields):
        if not isinstance(self.ident,str) or not re.fullmatch('[0-9a-f]{64}',self.ident):
            raise Refused('teams_local_control_container_rejected')
        if operation not in ('view','prepare','join','output','stop'):
            raise Refused('teams_control_rejected')
        raw=json.dumps(dict(operation=operation,fields=fields),allow_nan=False).encode()+b'\n'
        if len(raw)>4096:raise Refused('teams_control_rejected')
        result=self.staging.capture_docker(('exec','-i','--user','10001:10001',self.ident,
            '/usr/local/bin/teams-transcription','local-teams-control','--config','/data/config/runtime.yml'),
            self.env,input=raw,timeout=20)
        if (result['timed_out'] or result['cleanup_uncertain'] or result['stdout_truncated']
                or result['stderr_truncated'] or result['returncode']!=0):
            raise Refused('teams_local_control_request_rejected')
        try:
            value=json.loads(result['stdout'])
        except (ValueError,TypeError):raise Refused('teams_local_control_request_rejected') from None
        if not isinstance(value,dict) or 'error' in value:
            raise Refused('teams_local_control_request_rejected')
        return value

    def view(self,occurrence):return self.request('view',dict(id=occurrence))
    def control(self,action,fields):return self.request(action,fields)


def bound_view(value,request,authority):
    candidate=value.get('acceptance') or {}
    if (candidate.get('request_sha256')!=request or candidate.get('version')!=VERSION
            or candidate.get('occurrence_revision')!=authority['occurrence_revision']
            or 'maximum_seconds' not in candidate or candidate['maximum_seconds']!=authority['maximum_seconds']
            or not isinstance(value.get('session_id'),str)
            or not re.fullmatch('[0-9a-f]{32}',value['session_id'])):
        raise Refused('teams_join_requires_reconciliation')
    return value['session_id'],candidate


def final_report(proof,ledger,failure):
    def metric(value):
        return value if type(value) in (int,float) and math.isfinite(value) and 0<=value<10**15 else None
    transport=(proof.get('joined') is True and proof.get('native_attempts')==2
        and all(metric(proof.get(k)) is not None and metric(proof[k])>0
            for k in ('source_rx_packets','B_tx_packets','B_tx_frames','B_tx_energy')))
    return dict(success=failure is None and transport,transport_checks_passed=transport,
        operation=VERSION,failure_code=failure,
        consumed=ledger['consumed'],remaining=50-ledger['consumed'],cleanup_verified=True,
        maintenance_restored=True,model_start_attempts=proof['thread_attempts'],
        native_start_attempts=proof['native_attempts'],joined=proof.get('joined') is True,
        source_rx_packets=metric(proof.get('source_rx_packets')),B_tx_packets=metric(proof.get('B_tx_packets')),
        B_tx_frames=metric(proof.get('B_tx_frames')),B_tx_energy=metric(proof.get('B_tx_energy')),
        remote_audibility_verified=False,audio_continuity_verified=False,summary_requested=False)


def finish_owner_run(*,backend,bridge,state,ledger_path,source,image,build_run,build_attempt,monotonic,sleep,case_id=None):
    """Stop/finalize only the original durable claim; never create a Join or reserve again."""
    marker=state/bridge.INTERLOCK
    owned=json.loads(bridge.fixed_file(marker,owner=os.getuid(),private=True,maximum=4096))
    keys={'run_id','run_attempt','reserved','experiment','occurrence_id','occurrence_revision',
        'request_sha256','source_sha','image','build_run_id','build_run_attempt','reserved_sha256','stop_policy'}
    if case_id is not None:keys.add('case_id')
    if (set(owned)!=keys or owned['reserved']!=2 or owned['experiment']!=VERSION
            or owned.get('case_id')!=case_id
            or owned['stop_policy']!='owner_or_meeting_end' or owned['source_sha']!=source or owned['image']!=image
            or type(owned['build_run_id']) is not int or owned['build_run_id']!=build_run
            or type(owned['build_run_attempt']) is not int or owned['build_run_attempt']!=build_attempt
            or type(owned['run_id']) is not int or not 0<owned['run_id']<10**20
            or type(owned['run_attempt']) is not int or not 0<owned['run_attempt']<1000000
            or type(owned['occurrence_revision']) is not int or owned['occurrence_revision']<1
            or not isinstance(owned['occurrence_id'],str) or not re.fullmatch('[0-9a-f]{32}',owned['occurrence_id'])
            or not isinstance(owned['reserved_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',owned['reserved_sha256'])
            or owned['request_sha256']!=action_request(owned['run_id'],owned['run_attempt'],owned['occurrence_id'],case_id)):
        raise Refused('teams_owned_run_rejected')
    runtime=backend.runtime();release=runtime.get('release') or {}
    if (runtime.get('image')!=image or (runtime.get('container') or {}).get('image')!=image
            or release.get('verified_release_record') is not True or release.get('source_sha')!=source
            or release.get('build_run_id')!=build_run or release.get('build_run_attempt')!=build_attempt):
        raise Refused('teams_owned_release_rejected')
    authority=dict(occurrence_revision=owned['occurrence_revision'],maximum_seconds=None)
    failure=None;primary=None
    try:
        session,proof=bound_view(backend.view(owned['occurrence_id']),owned['request_sha256'],authority)
        if proof.get('cleanup_verified') is not True or proof.get('count_complete') is not True:
            try:backend.control('output',dict(id=session,acceptance_request=owned['request_sha256'],enabled='false'))
            except Exception:failure='teams_output_disable_uncertain'
            try:backend.control('stop',dict(id=session,acceptance_request=owned['request_sha256']))
            except Exception:failure=failure or 'teams_stop_response_uncertain'
            cleanup_deadline=monotonic()+35
            while monotonic()<cleanup_deadline:
                ident,proof=bound_view(backend.view(owned['occurrence_id']),owned['request_sha256'],authority)
                if ident!=session:raise Refused('teams_acceptance_binding_rejected')
                if proof.get('cleanup_verified') is True and proof.get('count_complete') is True:break
                sleep(.5)
    except BaseException as error:
        primary=error
        raise
    finally:
        restore_maintenance(backend,primary if primary is not None else Refused(failure) if failure else None)
    raw=bridge.fixed_file(ledger_path,owner=os.getuid(),private=True,maximum=65536)
    if hashlib.sha256(raw).hexdigest()!=owned['reserved_sha256']:
        raise Refused('qa_ledger_changed_requires_reconciliation')
    ledger=settle(json.loads(raw),proof,owned['run_id'],owned['run_attempt'],source)
    bridge.save_ledger(ledger_path,ledger,state=state)
    marker.unlink();bridge.synced_directory(state)
    return final_report(proof,ledger,failure)


def perform(mode,*,backend,bridge,contract,state,qa_authority,meeting_authority,run,attempt,
        source,image,build_run,build_attempt,clock=time.time,monotonic=time.monotonic,sleep=time.sleep,
        authority_loader=None,confirmed_at=None,case_id=None):
    validate_case(case_id)
    if mode not in ('prepare','offline','run','stop') or type(run) is not int or not 0<run<10**20 \
            or type(attempt) is not int or not 0<attempt<1000000:
        raise Refused('teams_operation_rejected')
    with bridge.existing_lock(state/'controller.lock'):
        config,ledger_path,lock_path=(authority_loader or bridge.authority)(state,qa_authority)
        with bridge.existing_lock(lock_path):
            if mode=='stop':
                return finish_owner_run(backend=backend,bridge=bridge,state=state,ledger_path=ledger_path,
                    source=source,image=image,build_run=build_run,build_attempt=build_attempt,
                    monotonic=monotonic,sleep=sleep,case_id=case_id)
            marker=state/bridge.INTERLOCK
            if marker.exists() or marker.is_symlink():raise Refused('qa_cleanup_requires_reconciliation')
            runtime=backend.runtime()
            contract._validate_runtime(runtime,image=image,source_sha=source,build_run_id=build_run,build_run_attempt=build_attempt)
            ledger=json.loads(bridge.fixed_file(ledger_path,owner=os.getuid(),private=True,maximum=65536))
            contract._ledger_preflight(ledger,config.get('checkpoint_sha256'),maximum_native_starts=2,
                base_sha=source,candidate_sha=source,
                manifest_sha=hashlib.sha256(json.dumps(case_request(meeting_authority,case_id),sort_keys=True).encode()).hexdigest())
            if meeting_authority.get('version')==PREPARE_VERSION:
                meeting_authority,view=prepared_target(mode,backend,bridge,state,meeting_authority,
                    confirmed_at=confirmed_at,now=clock(),case_id=case_id)
            else:
                if mode=='prepare':raise Refused('teams_preparation_authority_required')
                view=backend.view(meeting_authority['occurrence_id'])
                now=clock()
                validate_authority(meeting_authority,view,now,presence=mode=='run')
                if case_id is not None and mode=='run':
                    # Operator authority and current participant presence are
                    # separate gates. New presence cannot extend root authority.
                    confirmed_until(confirmed_at,now)
            if mode=='prepare':return dict(success=True,prepared=True,model_started=False,ledger_touched=False,live_ready=False)
            if mode=='offline':return dict(success=True,model_started=False,ledger_touched=False,protocol=VERSION,live_ready=False)
            ledger=reserve(ledger,run,attempt,source);bridge.save_ledger(ledger_path,ledger,state=state)
            reserved_bytes=bridge.fixed_file(ledger_path,owner=os.getuid(),private=True,maximum=65536)
            request=action_request(run,attempt,meeting_authority['occurrence_id'],case_id)
            ownership=dict(run_id=run,run_attempt=attempt,reserved=2,experiment=VERSION)
            ownership.update(occurrence_id=meeting_authority['occurrence_id'],
                occurrence_revision=meeting_authority['occurrence_revision'],request_sha256=request,
                source_sha=source,image=image,build_run_id=build_run,build_run_attempt=build_attempt,
                reserved_sha256=hashlib.sha256(reserved_bytes).hexdigest(),stop_policy='owner_or_meeting_end')
            if case_id is not None:ownership['case_id']=case_id
            bridge.exclusive(marker,json.dumps(ownership).encode())
            bridge.synced_directory(state)
            session=None;proof=None;failure=None;maintenance_restored=False
            handed_off=False
            def bound(value):
                return bound_view(value,request,meeting_authority)
            try:
                backend.maintenance(False)
                try:
                    fields=dict(id=meeting_authority['occurrence_id'],acceptance_request=request,
                        acceptance_revision=meeting_authority['occurrence_revision'])
                    fields['acceptance_stop_policy']='owner_or_meeting_end'
                    joined=backend.control('join',fields)
                    result=joined.get('result',{})
                    if (result.get('occurrence_id')!=meeting_authority['occurrence_id'] or result.get('acceptance_request')!=request
                            or not isinstance(result.get('session_id'),str) or not re.fullmatch('[0-9a-f]{32}',result['session_id'])):
                        raise Refused('teams_acceptance_binding_rejected')
                    session=result['session_id']
                except Exception:
                    failure='teams_join_response_uncertain'  # Reconcile once; never dispatch Join again.
                session,proof=bound(backend.view(meeting_authority['occurrence_id']))
                if failure is not None:raise Refused(failure)
                handed_off=True
                return dict(success=True,operation=VERSION,lifecycle='handed_off',handoff_verified=True,
                    stop_policy='owner_or_meeting_end',transport_checks_passed=False,
                    consumed=ledger['consumed'],remaining=50-ledger['consumed'],ledger_settled=False,
                    cleanup_verified=False,maintenance_restored=False,joined=proof.get('joined') is True,
                    model_start_attempts=proof.get('thread_attempts'),native_start_attempts=proof.get('native_attempts'),
                    remote_audibility_verified=False,audio_continuity_verified=False,summary_requested=False)
            except BaseException as error:
                failure=failure or error_report(error)['error_code']
            finally:
                if not handed_off:
                    try:
                        if session is not None and not (proof or {}).get('cleanup_verified'):
                            # Mute and stop exact owned run. A failed/ambiguous call is never repeated.
                            try:backend.control('output',dict(id=session,acceptance_request=request,enabled='false'))
                            except Exception:failure=failure or 'teams_output_disable_uncertain'
                            try:backend.control('stop',dict(id=session,acceptance_request=request))
                            except Exception:failure=failure or 'teams_stop_response_uncertain'
                            cleanup_deadline=monotonic()+35
                            while monotonic()<cleanup_deadline:
                                ident,proof=bound(backend.view(meeting_authority['occurrence_id']))
                                if ident!=session:raise Refused('teams_acceptance_binding_rejected')
                                if proof.get('cleanup_verified') is True and proof.get('count_complete') is True:break
                                sleep(.5)
                    finally:
                        restore_maintenance(backend,Refused(failure) if failure else None)
                        maintenance_restored=True
            if not maintenance_restored:raise Refused('teams_maintenance_restore_unverified')
            if proof is None:raise Refused('qa_cleanup_requires_reconciliation')
            if bridge.fixed_file(ledger_path,owner=os.getuid(),private=True,maximum=65536)!=reserved_bytes:
                raise Refused('qa_ledger_changed_requires_reconciliation')
            ledger=settle(ledger,proof,run,attempt,source)
            bridge.save_ledger(ledger_path,ledger,state=state)
            marker.unlink();bridge.synced_directory(state)
            return final_report(proof,ledger,failure)


def installed(bridge,name,path):
    bridge.fixed_file(path,owner=0)
    loader=importlib.machinery.SourceFileLoader(name,str(path))
    module=importlib.util.module_from_spec(importlib.util.spec_from_loader(name,loader));loader.exec_module(module)
    return module


def operation_arguments(args):
    if len(args) not in (1,3) or args[0] not in ('prepare','offline','run','stop'):
        raise Refused('teams_operation_rejected')
    if len(args)==1:return args[0],None
    if args[1]!='--case':raise Refused('teams_case_rejected')
    return args[0],validate_case(args[2])


def load_meeting_authority(bridge,case_id):
    validate_case(case_id)
    path=AUTHORITY
    if case_id is not None:
        bridge.directory(CASE_DIRECTORY,owner=0)
        path=CASE_DIRECTORY/(case_id+'.json')
    return json.loads(bridge.fixed_file(path,owner=0,maximum=4096))


def main():
    os.umask(0o077)
    def cancelled(signum,frame):raise Refused('teams_acceptance_cancelled')
    signal.signal(signal.SIGINT,cancelled);signal.signal(signal.SIGTERM,cancelled)
    try:
        require_release()  # Source preparation cannot execute against the previous uncapped image.
        operation,case_id=operation_arguments(sys.argv[1:])
        if (os.getuid()==0
                or os.environ.get('GITHUB_REPOSITORY')!='paulpai0412/teams-transcription'
                or os.environ.get('GITHUB_EVENT_NAME')!='workflow_dispatch'
                or os.environ.get('GITHUB_REF')!='refs/heads/main'
                or os.environ.get('GITHUB_WORKFLOW_REF')!='paulpai0412/teams-transcription/.github/workflows/oracle-teams.yml@refs/heads/main'):
            raise Refused('teams_job_identity_rejected')
        if operation in ('run','stop') and os.environ.get('STAGING_TEAMS_AUTHORIZED')!='true':
            raise Refused('teams_meeting_not_authorized')
        # Loading the reviewed bridge uses the same root-owned install boundary.
        path=BIN/'qa-cloud-bridge'
        if any(p.is_symlink() or not p.is_dir() or p.stat().st_uid!=0 or p.stat().st_mode&0o022
                for p in (Path('/opt'),BIN.parent,BIN)):
            raise Refused('teams_installation_untrusted')
        info=path.lstat()
        import stat
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=0 or info.st_mode&0o022:
            raise Refused('teams_installation_untrusted')
        loader=importlib.machinery.SourceFileLoader('qa_bridge',str(path))
        bridge=importlib.util.module_from_spec(importlib.util.spec_from_loader('qa_bridge',loader));loader.exec_module(bridge)
        for path in (Path('/srv'),STATE.parent,QA_AUTHORITY.parent):bridge.directory(path,owner=0)
        bridge.directory(STATE,owner=os.getuid(),private=True)
        contract=installed(bridge,'qa_contract',BIN/'qa_cloud_contract.py')
        staging=installed(bridge,'stagingctl',BIN/'stagingctl')
        authority=load_meeting_authority(bridge,case_id)
        class Backend:
            def __init__(self,config):
                self.controller=staging.Controller(config)
                self.local=LocalControl(staging,self.controller.env)
            def view(self,occurrence):return self.local.view(occurrence)
            def control(self,action,fields):return self.local.control(action,fields)
            def runtime(self):
                # Existing adapter binds image metadata and private DB flags without a second daemon.
                if self.controller.verify_image(IMAGE)!=RELEASE_SOURCE_SHA:
                    raise Refused('teams_image_revision_rejected')
                profile=type('Profile',(),dict(IMAGE=IMAGE))()
                backend=bridge.DockerBackend(self.controller,staging,profile)
                result=backend.runtime()
                self.local.ident=backend.ident
                return result
            def maintenance(self,enabled):
                return self.controller.probe(IMAGE,maintenance='enable' if enabled else 'disable')
        with staging.registry_config('diagnostics') as config:
            result=perform(operation,backend=Backend(config),bridge=bridge,contract=contract,state=STATE,
                qa_authority=QA_AUTHORITY,meeting_authority=authority,run=int(os.environ['GITHUB_RUN_ID']),
                attempt=int(os.environ['GITHUB_RUN_ATTEMPT']),source=RELEASE_SOURCE_SHA,image=IMAGE,
                build_run=BUILD_RUN_ID,build_attempt=BUILD_RUN_ATTEMPT,
                confirmed_at=os.environ.get('STAGING_TEAMS_CONFIRMED_AT'),case_id=case_id)
        print(json.dumps(result,allow_nan=False));return 0 if result['success'] else 1
    except BaseException as error:
        print(json.dumps(error_report(error)));return 1


if __name__=='__main__':raise SystemExit(main())
