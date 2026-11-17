"""Inspect only the candidate image, with no runtime mounts or network.

Known credential patterns and private-path exclusion are a bounded scan, not a
proof that arbitrary secrets cannot be encoded in source or base dependencies.
Only booleans/architecture leave the container, never matched source bytes.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

PATTERNS = (
    rb'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----',
    rb'(?:ghp_|gho_|ghu_|ghs_|github_pat_)[A-Za-z0-9_]{20,}',
    rb'sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{24,}',
    rb'ya29\.[A-Za-z0-9_-]{20,}',
    rb'1//[A-Za-z0-9_-]{24,}',
    rb'https://teams\.(?:microsoft\.com|live\.com|cloud\.microsoft)/(?:l/meetup-join|meet)/[^\s\x27\x22<>]{30,}',
)


def contains_sensitive(data):
    return any(re.search(pattern,data) for pattern in PATTERNS)


def sensitive_metadata(config):
    return contains_sensitive(json.dumps({'environment':config.get('Env',[]),
        'labels':config.get('Labels',{})},ensure_ascii=False).encode())


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',required=True,type=Path)
    args=parser.parse_args()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}',args.image):parser.error('immutable local image ID required')
    env={key:value for key,value in os.environ.items() if key not in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH')}
    prefix=['docker','--host=unix:///var/run/docker.sock']
    def probe(user,script):
        name='teams-image-content-'+uuid.uuid4().hex
        try:
            with tempfile.TemporaryFile() as output:
                try:
                    result=subprocess.run(prefix+['run','--name',name,'--rm','--pull=never','--network=none','--read-only',
                        '--cap-drop=ALL','--security-opt=no-new-privileges:true','--user='+user,
                        '--memory=256m','--cpus=1','--pids-limit=64','--entrypoint=python',args.image,'-c',script],
                        env=env,stdout=output,stderr=subprocess.DEVNULL,timeout=45)
                except subprocess.TimeoutExpired:raise SystemExit('image_content_check_timeout') from None
                output.seek(0);data=output.read(65537)
            if result.returncode or len(data)>65536:raise SystemExit('image_content_check_execution_failed')
            value=json.loads(data)
            if not isinstance(value,dict) or any(type(v) is not bool for v in value.values()):
                raise SystemExit('image_content_check_report_rejected')
            return value
        finally:
            cleanup=subprocess.run(prefix+['rm','--force',name],env=env,stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,timeout=10)
            if cleanup.returncode:
                check=subprocess.run(prefix+['container','ls','--all','--filter','name=^/'+name+'$','--format','{{.ID}}'],
                    env=env,capture_output=True,timeout=10)
                if check.returncode or check.stdout.strip():raise SystemExit('image_content_check_cleanup_uncertain')
    config=json.loads(subprocess.check_output(prefix+['image','inspect',args.image],env=env,text=True,timeout=10))[0]
    forbidden_env=any(re.search(r'(?:TOKEN|PASSWORD|SECRET|API_KEY|CREDENTIAL)',item.partition('=')[0],re.I)
        for item in config['Config'].get('Env',[]))
    script='''from pathlib import Path
import json,re
patterns=PATTERNS
private=['/home/recorder/.codex','/opt/meeting/.env','/opt/meeting/.tools','/opt/meeting/.git']
checks={'private_paths_absent':not any(Path(p).exists() for p in private),'data_root_empty':not any(Path('/data').iterdir())}
files=[p for p in Path('/opt/meeting').rglob('*') if p.is_file()]
checks['application_files_bounded']=len(files)<256 and all(p.stat().st_size<2*1024*1024 for p in files)
checks['known_sensitive_patterns_absent']=checks['application_files_bounded'] and not any(re.search(pattern,p.read_bytes()) for p in files for pattern in patterns)
print(json.dumps(checks))
'''.replace('PATTERNS',repr(PATTERNS),1)
    checks=probe('10001:10001',script)
    if set(checks)!={'private_paths_absent','data_root_empty','application_files_bounded','known_sensitive_patterns_absent'}:
        raise SystemExit('image_content_check_report_rejected')
    # UID10001 cannot inspect /root (0700). A separate image-only metadata probe
    # runs as its owner, without runtime mounts, network or any capabilities; it
    # never starts the app or reads credential contents. EACCES is never absence.
    # `codex --version` may create an empty/cache home during build. Reject
    # credential/config files; the mere existence of that directory is not auth.
    root_probe="from pathlib import Path;import json;print(json.dumps({'root_auth_files_absent':not any((Path('/root/.codex')/p).exists() for p in ('auth.json','config.toml','.credentials.json'))}))"
    root=probe('0:0',root_probe)
    if set(root)!={'root_auth_files_absent'}:raise SystemExit('image_root_metadata_report_rejected')
    checks.update(root)
    checks['credential_environment_absent']=not forbidden_env
    checks['known_sensitive_metadata_absent']=not sensitive_metadata(config['Config'])
    receipt={'scope':'candidate application paths and image environment; no runtime data',
        'architecture':config['Architecture'],'checks':checks,'success':all(checks.values())}
    args.output.write_text(json.dumps(receipt,indent=2)+'\n')
    print(json.dumps(receipt))
    return 0 if receipt['success'] else 1


if __name__=='__main__':raise SystemExit(main())
