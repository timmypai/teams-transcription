"""Offline preflight contracts and strict artifact projection; no runner or model I/O.

Supplied reports are not fresh host observations. Passing this module grants no
execution authority and does not reserve quota, acquire locks or install a bridge.
"""
import math
import re

BASE_SHA='c4090edb0e8e5db50d8950515522dba733618c51'
CANDIDATE_SHA='d5f729f6db228cc60b67674a031e99bc0545629b'
IMAGE='ghcr.io/paulpai0412/teams-transcription@sha256:7742843eb5810bb041a2e10214e61efc0da6790dba9863c2eaa53c42fb1b1828'
MANIFEST_SHA256='f496f4d9f4df36f8f09129f1f921283ff1b2f6c6c6cab70776b99a6294eaf4ad'
ISOLATED_SHA='836ecbe88d69b14a66326474d08a4c26d15c1e90'
ISOLATED_MANIFEST_SHA256='7102a643472e81407ceefdc693cdc24b58a783886b9cce7046857c204bafce99'
ISOLATED_FILES={'qa_probe.py':'6616f75dbf175acc2991cb2b933098d3230adc5e9720c4a64f0ab00cbbf39a29',
    'qa_session_isolation.py':'5d0befd7fa5af94d33250ba80434e8659071409de78e3a74b02e6f2fd62467fd'}
SEMANTIC_VERSION='synthetic_current_fact_semantic_v1'
SEMANTIC_MANIFEST_SHA256='9432a42aff31b1b75d2e86ede18f921cb1a12496aaa204a9a9dd861254000aa5'
# Exact runtime bundle commit plus separate manifest pin; this is not the
# earlier two-file source-preparation commit or its manifest.
SEMANTIC_SHA='1849e60b98e5cc93a5aef7bda5ecaf432dede1fc'
SEMANTIC_FILES=('qa_content.py','qa_probe.py','qa_session_isolation.py')
STARTUP_SOURCE_SHA='e82bb30900e90c9559b9c3dcb71e532cad86b72e'
STARTUP_MANIFEST_SHA256='0e1996c09d73144485b26ff178a9b0b99fe54d3ba5b636698dbc460eb3109292'
# Fixed main release verified against real run/artifact metadata and release_gate.
# Neither workflow inputs nor supplied runtime metadata can replace these pins.
STARTUP_RELEASE_SOURCE_SHA='c9d54b7f72be1c94318386ce7dab7a61df4396e3'
STARTUP_IMAGE='ghcr.io/paulpai0412/teams-transcription@sha256:57a433bdc059e462100f139466251e4b9670a46770899f6d3b1c9caf9597f657'
STARTUP_BUILD_RUN_ID=41159965976
STARTUP_BUILD_RUN_ATTEMPT=1


def integer(value,minimum=0,maximum=10**15):
    return type(value) is int and minimum<=value<=maximum


def numeric(value,*,negative=False):
    return value if type(value) in (int,float) and \
        (-10**15 if negative else 0)<=value<=10**15 and math.isfinite(value) else None


def mapping(value):
    return value if isinstance(value,dict) else {}


def validate_runtime(runtime):
    """Validate supplied metadata only; caller must obtain fresh trusted evidence."""
    _validate_runtime(runtime,image=IMAGE,source_sha=BASE_SHA,
        build_run_id=41022272702,build_run_attempt=1)


def _validate_runtime(runtime,*,image,source_sha,build_run_id,build_run_attempt):
    runtime=mapping(runtime);database=mapping(runtime.get('database'))
    container=mapping(runtime.get('container'));release=mapping(runtime.get('release'))
    if (runtime.get('phase')!='healthy' or runtime.get('image')!=image
            or container.get('status')!='running' or container.get('health')!='healthy'
            or container.get('image')!=image
            or any(database.get(key) is not True for key in
                ('database_present','scheduler_paused','quiescent','maintenance'))
            or any(database.get(key) is not False for key in
                ('active_meeting','active_lease','pending_summary','unreviewed_cleanup'))
            or release.get('verified_release_record') is not True
            or release.get('source_sha')!=source_sha
            or type(release.get('build_run_id')) is not int or release['build_run_id']!=build_run_id
            or type(release.get('build_run_attempt')) is not int or release['build_run_attempt']!=build_run_attempt):
        raise ValueError('runtime_contract_rejected')


def preflight(runtime,ledger,checkpoint_sha256,*,maximum_native_starts=1):
    """Validate supplied offline handoff data. Never default missing usage to zero."""
    validate_runtime(runtime)
    return _ledger_preflight(ledger,checkpoint_sha256,maximum_native_starts=maximum_native_starts,
        base_sha=BASE_SHA,candidate_sha=ISOLATED_SHA if maximum_native_starts==2 else CANDIDATE_SHA,
        manifest_sha=ISOLATED_MANIFEST_SHA256 if maximum_native_starts==2 else MANIFEST_SHA256)


def _ledger_preflight(ledger,checkpoint_sha256,*,maximum_native_starts,base_sha,candidate_sha,manifest_sha):
    ledger=mapping(ledger);checkpoint=mapping(ledger.get('checkpoint'));entries=ledger.get('entries')
    if (type(ledger.get('schema_version')) is not int or ledger['schema_version']!=1
            or type(ledger.get('limit')) is not int or ledger['limit']!=50
            or type(checkpoint.get('used')) is not int or checkpoint['used']!=7
            or type(checkpoint.get('through_case')) is not int or checkpoint['through_case']!=7
            or not isinstance(checkpoint_sha256,str) or not re.fullmatch('[0-9a-f]{64}',checkpoint_sha256)
            or checkpoint.get('source_receipt_sha256')!=checkpoint_sha256
            or not isinstance(entries,list) or len(entries)>43
            or not integer(ledger.get('consumed'),7,50) or ledger['consumed']!=7+len(entries)):
        raise ValueError('ledger_contract_rejected')
    if type(maximum_native_starts) is not int or maximum_native_starts not in (1,2):
        raise ValueError('ledger_contract_rejected')
    seen=set()
    for slot,row in enumerate(entries,8):
        row=mapping(row)
        if (type(row.get('slot')) is not int or row['slot']!=slot
                or not integer(row.get('run_id'),1,10**20-1)
                or not integer(row.get('run_attempt'),1,999999)):
            raise ValueError('ledger_contract_rejected')
        start_index=row.get('start_index',1)
        if not integer(start_index,1,2):raise ValueError('ledger_contract_rejected')
        identity=(row['run_id'],row['run_attempt'],start_index)
        if identity in seen:raise ValueError('ledger_contract_rejected')
        seen.add(identity)
        if row.get('status')=='not_started':
            if row.get('native_start_attempted') is not False or digest(row.get('public_receipt_sha256')) is None:
                raise ValueError('ledger_unresolved_attempt')
        elif row.get('status')!='cleaned':raise ValueError('ledger_unresolved_attempt')
    remaining=50-ledger['consumed']
    if remaining<maximum_native_starts:raise ValueError('ledger_exhausted')
    return {'offline_contract_passed':True,'live_ready':False,'model_started':False,
        'base_source_sha':base_sha,'candidate_sha':candidate_sha,'manifest_sha256':manifest_sha,
        'consumed':ledger['consumed'],'remaining':remaining,
        'maximum_native_starts_per_job':maximum_native_starts,
        'integration_gaps':['fresh_runner_host_and_candidate_checks_not_performed',
            'host_bridge_and_global_lock_not_verified','global_ledger_import_not_verified',
            'trusted_manual_workflow_not_installed']}


def boolean(value):return value if type(value) is bool else None


def digest(value):
    return value if isinstance(value,str) and re.fullmatch('[0-9a-f]{64}',value) else None


def _public_core(value,*,run_id,run_attempt,candidate_sha):
    if not integer(run_id,1,10**20-1) or not integer(run_attempt,1,999999):
        raise ValueError('actions_identity_rejected')
    value=mapping(value)
    result={'schema_version':1,'candidate_sha':candidate_sha,'base_source_sha':BASE_SHA,
        'image':IMAGE,'run_id':run_id,'run_attempt':run_attempt,
        'accepted':boolean(value.get('accepted')),
        'owned_cleanup_verified':boolean(value.get('owned_cleanup_verified')),
        'audio_quality_verified':False,'teams_verified':False,'physical_tx_verified':False,
        'observer_noninterference_verified':False,'checks':[]}
    checks=value.get('checks')
    for row in checks[:2] if isinstance(checks,list) else []:
        row=mapping(row)
        safe={key:boolean(row.get(key)) for key in ('scoped_tool_called','expected_fact',
            'exact_source_quote','no_stale_fact','output_muted','audio_rx_verified')}
        rx=mapping(row.get('audio_rx'));safe['audio_rx']={}
        for name,fields in {'inbound_delta':('packets_received','bytes_received'),
                'source_delta':('packets','frames','duration','energy'),
                'render_delta':('frames','duration','energy')}.items():
            raw=mapping(rx.get(name));safe['audio_rx'][name]={key:numeric(raw.get(key)) for key in fields}
        result['checks'].append(safe)
    return result


def public_result(value,*,run_id,run_attempt):
    """Fixed A projection. Never upload input, private QA JSON, PCM or raw logs."""
    value=mapping(value)
    result=_public_core(value,run_id=run_id,run_attempt=run_attempt,candidate_sha=CANDIDATE_SHA)
    observer=mapping(value.get('audio_observer'))
    safe={key:numeric(observer.get(key),negative=key=='reported_loss_delta') for key in
        ('polls','poll_wall_ms','host_ingest_ms','finalize_before_manifest_ms',
            'reported_loss_delta','reported_jitter_max_seconds')}
    safe['truncated']=boolean(observer.get('truncated'))
    for group,fields in {'audio_bytes':('source','render'),'frames':('source','render'),
            'findings':('decoded_silent_frames','render_exact_zero_frames',
                'render_zero_after_source_stall_candidate_frames','rtp_timestamp_gap_frames',
                'rtp_nonforward_events','render_missing_input_frames')}.items():
        raw=mapping(observer.get(group));safe[group]={key:numeric(raw.get(key)) for key in fields}
    safe['files']={}
    files=mapping(observer.get('files'))
    for name in ('source.f32le','render.f32le','index.jsonl','events.jsonl'):
        row=mapping(files.get(name));digest=row.get('sha256')
        safe['files'][name]={'bytes':numeric(row.get('bytes')),
            'sha256':digest if isinstance(digest,str) and re.fullmatch('[0-9a-f]{64}',digest) else None}
    result['observer']=safe
    return result


def cleanup_verified(value):
    value=mapping(value)
    return (value.get('status')=='cleaned' and value.get('stop_status')=='stopped'
        and value.get('stop_ack_received') is True and value.get('stop_closed_received') is True
        and value.get('native_cleanup_status')=='cleaned' and value.get('reaped') is True
        and value.get('pending_operation') is False
        and all(type(value.get(key)) is int and value[key]==0 for key in ('live_processes','pending_close_tasks')))


def _isolated_failure(value,*,projected):
    if value is None:return None
    value=mapping(value)
    codes={'session_probe_failed','session_cleanup_unverified','session_identity_unverified',
        'session_revision_unverified','work_deadline_exceeded','isolated_probe_cancelled',
        'isolated_probe_failed','private_evidence_write_failed'}
    code=value.get('error_code')
    phase=value.get('phase')
    number=value.get('session_number') if projected else (
        {'session_1':1,'session_2':2}.get(phase) if isinstance(phase,str) else None)
    return {'error_code':code if isinstance(code,str) and code in codes else 'unavailable',
        'session_number':number if integer(number,1,2) else None}


def public_isolated_result(value,*,run_id,run_attempt,projected=False):
    """Fixed B projection. projected=True only reprojects the trusted entry protocol."""
    value=mapping(value)
    result=_public_core(value,run_id=run_id,run_attempt=run_attempt,candidate_sha=ISOLATED_SHA)
    result.update(kind='isolated_synthetic_native_qa',experiment='isolated',
        session_isolation_verified=boolean(value.get('session_isolation_verified')),
        native_start_count_complete=boolean(value.get('native_start_count_complete')),
        model_consumption_verified=False,model_start_limit=2,
        failure=_isolated_failure(value.get('failure'),projected=projected),
        secondary_failure=_isolated_failure(value.get('secondary_failure'),projected=projected),sessions=[])
    for key in ('model_start_attempts','observed_native_start_attempts'):
        raw=value.get(key);result[key]=raw if integer(raw,0,2) else None
    rows=value.get('sessions')
    # Do not silently truncate an oversized lifecycle list into a complete count.
    if not isinstance(rows,list) or len(rows)>2:
        result['native_start_count_complete']=False;rows=[]
    for row in rows:
        row=mapping(row);identity=row if projected else mapping(row.get('identity'))
        safe={key:numeric(row.get(key)) for key in ('launch_elapsed_seconds',
            'completed_elapsed_seconds','work_budget_seconds')}
        for key in ('session_number','source_revision'):
            raw=row.get(key);safe[key]=raw if integer(raw,1,2) else None
        safe.update(thread_sha256=digest(identity.get('thread_sha256')),
            question_pcm_sha256=digest(row.get('question_pcm_sha256')),
            cleanup_verified=boolean(row.get('cleanup_verified')) if projected else cleanup_verified(row.get('owned_cleanup')))
        for key in ('start_attempted','startup_completed','realtime_start_issued'):
            safe[key]=boolean(identity.get(key))
        deliveries=row.get('context_deliveries');safe['context_deliveries']=[]
        for item in deliveries[:8] if isinstance(deliveries,list) else []:
            item=mapping(item)
            receipt={key:digest(item.get(key)) for key in ('snapshot_sha256','payload_sha256')}
            receipt.update({key:numeric(item.get(key)) for key in
                ('question_number','source_version','payload_bytes','sent_bytes')})
            receipt.update(marker_returned=boolean(item.get('marker_returned')),model_consumption_verified=False)
            safe['context_deliveries'].append(receipt)
        result['sessions'].append(safe)
    return result


def public_semantic_result(value,*,run_id,run_attempt,projected=False):
    return _public_semantic_result(value,run_id=run_id,run_attempt=run_attempt,projected=projected,
        candidate_sha=SEMANTIC_SHA,manifest_sha=SEMANTIC_MANIFEST_SHA256,
        candidate_id='semantic-isolated-v1',base_sha=BASE_SHA,image=IMAGE,
        experiment='semantic-isolated')


def _public_semantic_result(value,*,run_id,run_attempt,projected,candidate_sha,
        manifest_sha,candidate_id,base_sha,image,experiment):
    value=mapping(value)
    if (type(value.get('schema_version')) is not int or value['schema_version']!=2
            or value.get('criterion_version')!=SEMANTIC_VERSION
            or type(value.get('qa_receipt_schema_version')) is not int or value['qa_receipt_schema_version']!=3
            or projected and (value.get('candidate_sha')!=candidate_sha
                or value.get('candidate_manifest_sha256')!=manifest_sha)):
        raise ValueError('semantic_receipt_version_rejected')
    result=public_isolated_result(value,run_id=run_id,run_attempt=run_attempt,projected=projected)
    result.update(schema_version=2,candidate_sha=candidate_sha,candidate_id=candidate_id,
        candidate_identity_kind='git-commit-and-manifest',candidate_manifest_sha256=manifest_sha,
        base_source_sha=base_sha,image=image,
        criterion_version=SEMANTIC_VERSION,qa_receipt_schema_version=3,experiment=experiment)
    for name in ('failure','secondary_failure'):
        raw=mapping(value.get(name))
        if raw.get('error_code')=='session_criterion_unverified':
            phase=raw.get('phase');number=raw.get('session_number') if projected else (
                {'session_1':1,'session_2':2}.get(phase) if isinstance(phase,str) else None)
            result[name]={'error_code':'session_criterion_unverified',
                'session_number':number if integer(number,1,2) else None}
    reasons={'unsupported_answer_form','source_version_mismatch','unsupported_evidence',
        'unsupported_source_form','multiple_fact_values','wrong_fact_value','complete_current_fact_equivalent'}
    rows=value.get('checks');complete_checks=isinstance(rows,list) and len(rows)==2
    rows=rows[:2] if isinstance(rows,list) else []
    for target,raw in zip(result['checks'],rows):
        raw=mapping(raw);semantic=mapping(raw.get('semantic_content'))
        target['source_revision']=raw.get('source_revision') if integer(raw.get('source_revision'),1,2) else None
        target['source_version']=raw.get('source_version') if integer(raw.get('source_version'),1) else None
        status=semantic.get('status');reason=semantic.get('reason');fact=semantic.get('fact_type')
        status=status if isinstance(status,str) and status in ('pass','fail','unknown') else 'unknown'
        target['semantic_content']={'criterion_version':SEMANTIC_VERSION if semantic.get('criterion_version')==SEMANTIC_VERSION else None,
            'status':status,'reason':reason if isinstance(reason,str) and reason in reasons else 'unavailable',
            'fact_type':fact if isinstance(fact,str) and fact in ('budget','time') else None}
        target['semantic_content_verified']=(raw.get('semantic_content_verified') is True
            and target['semantic_content']['criterion_version']==SEMANTIC_VERSION and status=='pass'
            and reason=='complete_current_fact_equivalent' and target['semantic_content']['fact_type'] is not None)
    required=('scoped_tool_called','expected_fact','no_stale_fact','output_muted','audio_rx_verified','semantic_content_verified')
    for index,row in enumerate(result['checks']):
        sessions=result['sessions'];deliveries=sessions[index]['context_deliveries'] if index<len(sessions) else []
        delivery=deliveries[0] if len(deliveries)==1 else {}
        row['context_delivery_verified']=(row['source_version'] is not None
            and integer(delivery.get('question_number'),1,1)
            and integer(delivery.get('source_version'),1) and delivery['source_version']==row['source_version']
            and delivery.get('marker_returned') is True
            and digest(delivery.get('snapshot_sha256')) is not None and digest(delivery.get('payload_sha256')) is not None
            and integer(delivery.get('payload_bytes'),1) and integer(delivery.get('sent_bytes'),1)
            and delivery['sent_bytes']>=delivery['payload_bytes'])
    result['accepted']=(value.get('accepted') is True and result['failure'] is None and result['secondary_failure'] is None
        and result['owned_cleanup_verified'] is True and result['session_isolation_verified'] is True
        and result['native_start_count_complete'] is True and result['model_start_attempts']==2
        and result['observed_native_start_attempts']==2 and len(result['sessions'])==2
        and len({row['thread_sha256'] for row in result['sessions']})==2
        and len({row['question_pcm_sha256'] for row in result['sessions']})==1
        and all(row['cleanup_verified'] is True and row['start_attempted'] is True
            and row['startup_completed'] is True and row['realtime_start_issued'] is True
            and row['thread_sha256'] is not None and row['question_pcm_sha256'] is not None
            and row['session_number']==index and row['source_revision']==index
            for index,row in enumerate(result['sessions'],1))
        and complete_checks and len(result['checks'])==2 and all(all(row[key] is True for key in required)
            and row['source_revision']==index and row['context_delivery_verified'] is True
            for index,row in enumerate(result['checks'],1)))
    return result


class StartupProfile:
    """One fixed version, selected by operation enum, never by supplied runtime."""
    BASE_SHA=STARTUP_RELEASE_SOURCE_SHA
    IMAGE=STARTUP_IMAGE
    SEMANTIC_SHA=STARTUP_SOURCE_SHA
    SEMANTIC_MANIFEST_SHA256=STARTUP_MANIFEST_SHA256
    SEMANTIC_VERSION=SEMANTIC_VERSION
    SEMANTIC_FILES=SEMANTIC_FILES
    integer=staticmethod(integer)

    def release_ready(self):
        return bool(isinstance(self.BASE_SHA,str) and re.fullmatch('[0-9a-f]{40}',self.BASE_SHA)
                and isinstance(self.IMAGE,str) and re.fullmatch(
                    r'ghcr\.io/paulpai0412/teams-transcription@sha256:[0-9a-f]{64}',self.IMAGE)
                and integer(STARTUP_BUILD_RUN_ID,1) and integer(STARTUP_BUILD_RUN_ATTEMPT,1))

    def require_release_ready(self):
        if not self.release_ready():
            raise ValueError('release_not_ready')

    def validate_runtime(self,runtime):
        self.require_release_ready()
        _validate_runtime(runtime,image=self.IMAGE,source_sha=self.BASE_SHA,
            build_run_id=STARTUP_BUILD_RUN_ID,build_run_attempt=STARTUP_BUILD_RUN_ATTEMPT)

    def preflight(self,runtime,ledger,checkpoint_sha256,*,maximum_native_starts=2):
        self.validate_runtime(runtime)
        if maximum_native_starts!=2:raise ValueError('ledger_contract_rejected')
        return _ledger_preflight(ledger,checkpoint_sha256,maximum_native_starts=2,
            base_sha=self.BASE_SHA,candidate_sha=self.SEMANTIC_SHA,manifest_sha=self.SEMANTIC_MANIFEST_SHA256)

    def public_semantic_result(self,value,*,run_id,run_attempt,projected=False):
        value=mapping(value)
        if projected and (value.get('candidate_id')!='semantic-startup-v1'
                or value.get('runtime_source_sha')!=STARTUP_SOURCE_SHA
                or value.get('base_source_sha')!=self.BASE_SHA or value.get('image')!=self.IMAGE):
            raise ValueError('startup_receipt_version_rejected')
        result=_public_semantic_result(value,run_id=run_id,run_attempt=run_attempt,projected=projected,
            candidate_sha=self.SEMANTIC_SHA,manifest_sha=self.SEMANTIC_MANIFEST_SHA256,
            candidate_id='semantic-startup-v1',base_sha=self.BASE_SHA,image=self.IMAGE,
            experiment='semantic-startup-isolated')
        result.update(candidate_identity_kind='git-source-and-manifest',runtime_source_sha=STARTUP_SOURCE_SHA)
        result['release_ready']=self.release_ready()
        result['accepted']=result['accepted'] and result['release_ready']
        return result


def startup_profile():
    return StartupProfile()
