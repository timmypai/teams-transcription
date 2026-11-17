"""Offline-only loader for a reviewed, hash-pinned three-module QA candidate."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import stat
import types

MODULES=('qa_audio_observation','qa_audio_tap','qa_audio_probe')


def read_fixed(path,maximum=131072):
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError('candidate_regular_file_required')
        raw=stream.read(maximum+1)
    if len(raw)>maximum:raise ValueError('candidate_file_cap')
    return raw


def check(root,manifest_path,expected_manifest_sha256):
    raw=read_fixed(manifest_path)
    if hashlib.sha256(raw).hexdigest()!=expected_manifest_sha256:
        raise ValueError('candidate_manifest_mismatch')
    manifest=json.loads(raw)
    if (manifest.get('schema_version')!=1 or manifest.get('base_source_sha')!='c4090edb0e8e5db50d8950515522dba733618c51'
            or manifest.get('module_order')!=list(MODULES)
            or set(manifest.get('files',{}))!={name+'.py' for name in MODULES}):
        raise ValueError('candidate_contract_mismatch')
    root=Path(root)
    if root.is_symlink() or root.stat().st_mode&0o077:
        raise ValueError('candidate_private_directory_required')
    sources={}
    for name in MODULES:
        source=read_fixed(root/(name+'.py'))
        if hashlib.sha256(source).hexdigest()!=manifest['files'][name+'.py']:
            raise ValueError('candidate_source_mismatch')
        sources[name]=source
    import teams_transcription
    for name in MODULES:
        full='teams_transcription.'+name
        module=types.ModuleType(full)
        module.__package__,module.__file__='teams_transcription',str(root/(name+'.py'))
        sys.modules[full]=module;setattr(teams_transcription,name,module)
        exec(compile(sources[name],module.__file__,'exec'),module.__dict__)
    teams_transcription.qa_audio_probe.verify_base()
    return {'candidate_verified':True,'base_verified':True,'model_started':False,'modules':3}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--candidate-root',type=Path,required=True)
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--manifest-sha256',required=True)
    args=p.parse_args()
    try:print(json.dumps(check(args.candidate_root,args.manifest,args.manifest_sha256)))
    except Exception as error:
        print(json.dumps({'candidate_verified':False,'model_started':False,
            'error_code':'candidate_check_failed','exception_type':type(error).__name__}));raise SystemExit(1)
