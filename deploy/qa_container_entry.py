"""Fixed stdin protocol for a new private QA process in the existing app container.

Invoked only by the installed host bridge. Suppresses inherited log streams; the
original private QA and capped PCM stay under /data. No configurable command/path.
"""
import argparse
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import sys
import types

MANIFEST_SHA='f496f4d9f4df36f8f09129f1f921283ff1b2f6c6c6cab70776b99a6294eaf4ad'
CHECKER_SHA='5711a2b4a1c800a232ce58a524b08e43fb5c75fd07d84fb9c0151baeb2dbb319'
FILES=('qa_audio_observation.py','qa_audio_tap.py','qa_audio_probe.py',
    'passive-audio-candidate-manifest-20261030.json','check_qa_audio_candidate.py','qa_cloud_contract.py')
ISOLATED_SHA='836ecbe88d69b14a66326474d08a4c26d15c1e90'
ISOLATED_MANIFEST_SHA='7102a643472e81407ceefdc693cdc24b58a783886b9cce7046857c204bafce99'
ISOLATED_FILES=('qa_probe.py','qa_session_isolation.py','isolated-qa-candidate-manifest-20261030.json',
    'qa_cloud_contract.py')
SEMANTIC_MANIFEST_SHA='9432a42aff31b1b75d2e86ede18f921cb1a12496aaa204a9a9dd861254000aa5'
SEMANTIC_SHA='1849e60b98e5cc93a5aef7bda5ecaf432dede1fc'
SEMANTIC_VERSION='synthetic_current_fact_semantic_v1'
SEMANTIC_FILES=('qa_content.py','qa_probe.py','qa_session_isolation.py','semantic-isolated-candidate-v1.json','qa_cloud_contract.py')
STARTUP_SHA='e82bb30900e90c9559b9c3dcb71e532cad86b72e'
STARTUP_MANIFEST_SHA='0e1996c09d73144485b26ff178a9b0b99fe54d3ba5b636698dbc460eb3109292'
STARTUP_FILES=(*SEMANTIC_FILES[:3],'semantic-startup-candidate-v1.json','qa_cloud_contract.py')
BASE_HASHES={'qa_probe.py':'c718c7e08600b383a9916be3684393dcd531fb86007be8811b60a2431be39197',
    'b_no_plc.py':'5fb5e4322c27d36f74a31fe8cbc740fd232088d58db5e74ba417fff224525384',
    'b_live.py':'1415852671c4377860511f93df284c12750f88165cbcef8021349c4090304547',
    'b_media_profile.py':'1dfac1d3515b40874fe52408b1540660a2c8ae661620e095feb566f207257882'}


def run(request,*,data_root=Path('/data')):
    """data_root is an offline-test seam, never exposed through CLI or protocol."""
    mode=request.get('operation');run_id=request.get('run_id');attempt=request.get('run_attempt')
    startup=mode in ('semantic-startup-v1-offline','semantic-startup-v1-isolated')
    semantic=startup or mode in ('semantic-v1-offline','semantic-v1-isolated')
    isolated=semantic or mode in ('isolated-offline','isolated');live=mode in ('observe','isolated','semantic-v1-isolated','semantic-startup-v1-isolated')
    names=STARTUP_FILES if startup else SEMANTIC_FILES if semantic else (ISOLATED_FILES if isolated else FILES)
    if (mode not in ('offline','observe','isolated-offline','isolated','semantic-v1-offline','semantic-v1-isolated','semantic-startup-v1-offline','semantic-startup-v1-isolated') or type(run_id) is not int or not 0<run_id<10**20
            or type(attempt) is not int or not 0<attempt<1000000
            or live and request.get('authorized') is not True):
        raise ValueError('qa_entry_request_rejected')
    encoded=request.get('files')
    if not isinstance(encoded,dict) or set(encoded)!=set(names):raise ValueError('qa_entry_files_rejected')
    sources={}
    for name in names:
        text=encoded[name]
        if not isinstance(text,str) or len(text)>180000:raise ValueError('qa_entry_file_cap')
        sources[name]=base64.b64decode(text,validate=True)
        if len(sources[name])>131072:raise ValueError('qa_entry_file_cap')
    manifest_name=names[3] if semantic else (names[2] if isolated else FILES[3])
    manifest_sha=STARTUP_MANIFEST_SHA if startup else SEMANTIC_MANIFEST_SHA if semantic else ISOLATED_MANIFEST_SHA if isolated else MANIFEST_SHA
    if (hashlib.sha256(sources[manifest_name]).hexdigest()!=manifest_sha
            or not isolated and hashlib.sha256(sources[FILES[4]]).hexdigest()!=CHECKER_SHA):
        raise ValueError('qa_entry_hash_rejected')
    manifest=json.loads(sources[manifest_name])
    if semantic and (manifest.get('criterion_version')!=SEMANTIC_VERSION
            or manifest.get('candidate_id')!=('semantic-startup-v1' if startup else 'semantic-isolated-v1')
            or type(manifest.get('qa_receipt_schema_version')) is not int or manifest['qa_receipt_schema_version']!=3
            or manifest.get('module_order')!=['qa_content','qa_probe','qa_session_isolation']
            or (manifest.get('source_application_sha')!=STARTUP_SHA if startup else
                manifest.get('base_source_sha')!='c4090edb0e8e5db50d8950515522dba733618c51')
            or set(manifest.get('files',{}))!=set(SEMANTIC_FILES[:3])):
        raise ValueError('qa_semantic_manifest_rejected')
    for name in names[:3] if semantic else names[:2] if isolated else FILES[:3]:
        if hashlib.sha256(sources[name]).hexdigest()!=manifest['files'][name]:
            raise ValueError('qa_entry_hash_rejected')
    if isolated:
        import teams_transcription
        base=Path(teams_transcription.__file__).parent
        hashes=manifest.get('physical_runtime_files') if startup else BASE_HASHES
        if startup and (not isinstance(hashes,dict) or set(hashes)!={
                'qa_probe.py','b_no_plc.py','b_live.py','b_media_profile.py','b_browser_tx.py','b_no_plc_startup.py'}):
            raise ValueError('qa_entry_base_rejected')
        for name,want in hashes.items():
            if hashlib.sha256((base/name).read_bytes()).hexdigest()!=want:
                raise ValueError('qa_entry_base_rejected')
    # Source-only offline checks never grant Live admission. Live additionally
    # needs the fixed release profile; the host bridge checks fresh runtime state.
    projection=None
    if startup and live:
        projection=types.ModuleType('qa_public_projection')
        exec(compile(sources['qa_cloud_contract.py'],'qa_cloud_contract.py','exec'),projection.__dict__)
        projection=projection.startup_profile()
        projection.require_release_ready()
    # projection bytes are supplied by the root-owned installed bridge, not a ref/input.
    parent=data_root/'qa-cloud'
    parent.mkdir(mode=0o700,exist_ok=True)
    if parent.is_symlink() or parent.stat().st_uid!=os.getuid() or parent.stat().st_mode&0o077:
        raise ValueError('qa_entry_private_parent_required')
    root=parent/(str(run_id)+'-'+str(attempt)+'-'+mode);root.mkdir(mode=0o700)
    candidate=root/'candidate';candidate.mkdir(mode=0o700)
    for name,raw in sources.items():
        fd=os.open(candidate/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as output:output.write(raw)
    if isolated:
        # Verify both byte hashes before executing either. No A observer imports.
        for name in ('qa_content','qa_probe','qa_session_isolation') if semantic else ('qa_probe','qa_session_isolation'):
            full='teams_transcription.'+name;module=types.ModuleType(full)
            module.__package__,module.__file__='teams_transcription',str(candidate/(name+'.py'))
            sys.modules[full]=module;setattr(teams_transcription,name,module)
            exec(compile(sources[name+'.py'],module.__file__,'exec'),module.__dict__)
        checked={'candidate_verified':True,'base_verified':True,'model_started':False,
            'modules':3 if semantic else 2,'candidate_sha':STARTUP_SHA if startup else SEMANTIC_SHA if semantic else ISOLATED_SHA,
            **({'criterion_version':SEMANTIC_VERSION,'candidate_manifest_sha256':manifest_sha} if semantic else {}),
            **({'release_ready':False,'runtime_source_sha':STARTUP_SHA} if startup else {})}
    else:
        checker=types.ModuleType('qa_candidate_checker')
        exec(compile(sources[FILES[4]],str(candidate/FILES[4]),'exec'),checker.__dict__)
        checked=checker.check(candidate,candidate/FILES[3],MANIFEST_SHA)
        checked['candidate_sha']='d5f729f6db228cc60b67674a031e99bc0545629b'
    if not live:return {'success':True,'operation':mode,**checked}
    if projection is None:
        projection=types.ModuleType('qa_public_projection')
        exec(compile(sources['qa_cloud_contract.py'],str(candidate/'qa_cloud_contract.py'),'exec'),projection.__dict__)
    args=argparse.Namespace(authorize_live=True,qa_start_stage='question',peer_media_profile='legacy',
        config=str(data_root/'config/runtime.yml'),private_result=root/'private-qa.json',
        chromium=None,ice_servers_file=None)
    if isolated:
        args.session_mode='isolated'
        result=asyncio.run(teams_transcription.qa_probe.run_probe(args))
        safe=(projection.public_semantic_result(result,run_id=run_id,run_attempt=attempt) if semantic else
            projection.public_isolated_result(result,run_id=run_id,run_attempt=attempt))
    else:
        from teams_transcription import qa_audio_probe
        args.audio_observer_dir=root/'audio'
        result=asyncio.run(qa_audio_probe.run_observed_probe(args))
        safe=projection.public_result(result,run_id=run_id,run_attempt=attempt)
    return {'success':result.get('accepted') is True,'operation':mode,'result':safe}


def main():
    os.umask(0o077)
    # Neither library prints nor inherited stderr may reach Actions. App-server
    # protocol streams remain its owned pipes. Final output is only projected JSON.
    public=os.dup(1)
    try:
        with open(os.devnull,'w') as discarded:
            os.dup2(discarded.fileno(),1);os.dup2(discarded.fileno(),2)
            try:
                raw=sys.stdin.buffer.read(1048577)
                if len(raw)>1048576:raise ValueError('qa_entry_request_cap')
                result=run(json.loads(raw))
            except BaseException:
                result={'success':False,'error_code':'qa_entry_failed','cleanup_verified':False}
        os.write(public,(json.dumps(result,allow_nan=False)+'\n').encode())
        return 0 if result.get('success') is True else 1
    finally:os.close(public)


if __name__=='__main__':raise SystemExit(main())
