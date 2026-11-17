"""Run current source in an immutable, sandboxed local Chromium test image.

This is an offline test launcher, never a deployment or external dispatcher.
The supplied image must already contain the repo's declared dependencies,
sandboxed Chromium and CJK fonts. No install, bind mount, ports or auth import.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import tarfile

ROOT = Path(__file__).resolve().parent.parent

BOOTSTRAP = '''import base64,hashlib,io,json,sys,tarfile
from pathlib import Path
payload=json.loads(sys.stdin.readline())
root=Path('/tmp/ui-audit-source');root.mkdir()
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload['archive'])),mode='r:gz') as archive:
    archive.extractall(root,filter='data')
manifest={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted((root/'teams_transcription').rglob('*')) if path.is_file()}
digest=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
assert digest==payload['manifest_sha256'],'source_manifest_mismatch'
print(json.dumps(dict(verified_source_manifest_sha256=digest,source_root=str(root))),flush=True)
sys.path.insert(0,str(root))
exec(compile((root/'postmeeting_ui_audit.py').read_text(),str(root/'postmeeting_ui_audit.py'),'exec'))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True,help='Already built local sha256 image ID')
    parser.add_argument('--name',default='postmeeting-ui-audit-20261102')
    args = parser.parse_args()
    if not re.fullmatch('sha256:[a-f0-9]{64}',args.image) or not re.fullmatch('[a-z0-9-]+',args.name):
        parser.error('immutable local image ID and simple container name required')
    files = [path for path in sorted((ROOT/'teams_transcription').rglob('*'))
        if path.is_file() and '__pycache__' not in path.parts and path.suffix!='.pyc']
    manifest = {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    digest = hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
        for path in files:
            archive.add(path,arcname=str(path.relative_to(ROOT)),recursive=False)
        archive.add(ROOT/'tools/postmeeting_ui_audit.py',arcname='postmeeting_ui_audit.py',recursive=False)
    payload = json.dumps(dict(archive=base64.b64encode(buffer.getvalue()).decode(),manifest_sha256=digest))+'\n'
    env = os.environ.copy()
    # Explicit local daemon. Preserve Docker config, proxy and CA trust.
    for key in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):
        env.pop(key,None)
    command = ['docker','--host=unix:///var/run/docker.sock','run','--rm','-i','--name',args.name,
        '--network','none','--user','10001:10001','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges:true','--security-opt','seccomp:'+str(ROOT/'docker/chromium-seccomp.json'),
        '--tmpfs','/tmp:rw,size=512m,mode=1777','--shm-size','1g','--memory','4g','--cpus','2',
        '--pids-limit','512','--init','--entrypoint','python',args.image,'-u','-c',BOOTSTRAP]
    process = subprocess.Popen(command,stdin=subprocess.PIPE,env=env)
    try:
        process.stdin.write(payload.encode())
        process.stdin.flush()
        while process.poll() is None:
            ready,_,_ = select.select([sys.stdin],[],[],.5)
            if ready:
                line = os.read(sys.stdin.fileno(),65536)
                if not line:
                    process.stdin.close()
                    break
                process.stdin.write(line)
                process.stdin.flush()
        return process.wait()
    except BaseException:
        if process.poll() is None:
            process.stdin.close()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                subprocess.run(['docker','--host=unix:///var/run/docker.sock','stop','--time','5',args.name],env=env,check=False)
        raise


if __name__=='__main__':
    raise SystemExit(main())
