"""Offline hash verification for the separately versioned semantic QA candidate.

The expected manifest digest must come from review. This never imports candidate
code, calls a model, installs files, or grants permission to an old cloud loader.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re

from tools.check_qa_audio_candidate import read_fixed

VERSION = 'synthetic_current_fact_semantic_v1'
FILES = {'teams_transcription/qa_content.py','teams_transcription/qa_probe.py'}
MANIFEST = 'evidence/qa-semantic-candidate-v1.json'


def check(root, expected_manifest_sha256):
    root=Path(root)
    raw=read_fixed(root/MANIFEST, maximum=16384)
    if (not isinstance(expected_manifest_sha256,str)
            or not re.fullmatch('[0-9a-f]{64}',expected_manifest_sha256)
            or hashlib.sha256(raw).hexdigest()!=expected_manifest_sha256):
        raise ValueError('semantic_manifest_hash_rejected')
    value=json.loads(raw)
    if (type(value.get('schema_version')) is not int or value['schema_version']!=1
            or value.get('criterion_version')!=VERSION
            or type(value.get('qa_receipt_schema_version')) is not int or value['qa_receipt_schema_version']!=3
            or value.get('runtime_authorized') is not False
            or not isinstance(value.get('files'),dict) or set(value['files'])!=FILES):
        raise ValueError('semantic_candidate_contract_rejected')
    for path,want in value['files'].items():
        raw=read_fixed(root/path)
        if not isinstance(want,str) or not re.fullmatch('[0-9a-f]{64}',want) or hashlib.sha256(raw).hexdigest()!=want:
            raise ValueError('semantic_candidate_source_rejected')
    return {'candidate_verified':True,'criterion_version':VERSION,
        'manifest_sha256':expected_manifest_sha256,'model_started':False,
        'runtime_authorized':False,'legacy_candidates_compatible':False}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',type=Path,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    args=parser.parse_args()
    try:
        print(json.dumps(check(args.root,args.manifest_sha256)))
    except Exception:
        print(json.dumps({'candidate_verified':False,'model_started':False,
            'runtime_authorized':False,'error_code':'semantic_candidate_check_failed'}))
        raise SystemExit(1)
