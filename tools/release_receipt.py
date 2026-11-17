"""Write the small published-image proof only after all local checks passed."""
import json
import os
from pathlib import Path
import re
import sys

sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from deploy.release_gate import IMAGE, REPOSITORY, VALIDATION, identity


def write_receipt(directory, environ):
    directory=Path(directory)
    image=(directory/'repo-digest.txt').read_text().strip()
    if not image.startswith(IMAGE+'@'):raise ValueError('unexpected_package')
    sha,run,attempt=environ['GITHUB_SHA'],environ['GITHUB_RUN_ID'],environ['GITHUB_RUN_ATTEMPT']
    identity(image.split('@',1)[1],sha,run)
    if environ['GITHUB_REPOSITORY']!=REPOSITORY or not re.fullmatch(r'[1-9][0-9]{0,5}',attempt):
        raise ValueError('unexpected_build_identity')
    contracts=json.loads((directory/'contracts.json').read_text())
    if (contracts['success'] is not True or type(contracts['tests']) is not int or contracts['tests']<=0
            or any(contracts[name]!=0 for name in ('failures','errors','skipped'))):
        raise ValueError('offline_contract_gate_failed')
    for filename in ('compose-config.json','compose-smoke.json','image-content.json'):
        result=json.loads((directory/filename).read_text())
        required={
            'compose-config.json':{'nonroot_explicit_identity','read_only_drop_caps','sandbox_without_privilege_bypass','bounded_resources'},
            'compose-smoke.json':{'actual_default_daemon_healthy','actual_sandboxed_chromium_local_page','pinned_codex_installed','effective_cgroup_cpu_memory_pids_limits'},
            'image-content.json':{'private_paths_absent','data_root_empty','application_files_bounded','known_sensitive_patterns_absent',
                'root_auth_files_absent','credential_environment_absent','known_sensitive_metadata_absent'},
        }[filename]
        if result['success'] is not True or not required.issubset(result['checks']) or any(v is not True for v in result['checks'].values()):
            raise ValueError('image_validation_gate_failed')
        if filename=='image-content.json' and result['architecture']!='arm64':
            raise ValueError('native_arm64_image_required')
    proof={'schema_version':1,'repository':REPOSITORY,'source_sha':sha,'image':image,
        'platform':'linux/arm64','build_run_id':int(run),'build_run_attempt':int(attempt),
        'validation':dict(VALIDATION)}
    (directory/'published-image.json').write_text(json.dumps(proof,indent=2)+'\n')
    return proof


if __name__=='__main__':
    try:print(json.dumps(write_receipt(sys.argv[1],os.environ)))
    except (ValueError,KeyError,OSError,IndexError):raise SystemExit('published_image_evidence_rejected') from None
