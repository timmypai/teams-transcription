"""Run the available source regressions inside the real installed image, no network."""
import json
import os
from pathlib import Path
import subprocess
import uuid

ROOT=Path(__file__).resolve().parents[1]

def main():
    assert os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    image=Path(os.environ['RUNNER_TEMP'],'teams-image-id').read_text().strip()
    name='teams-transcription-regression-'+uuid.uuid4().hex[:12]
    prefix=['docker','--host=unix:///var/run/docker.sock']
    output=ROOT/'.devflow/evidence-test'
    output.mkdir(parents=True,exist_ok=True)
    def docker(*args, **kwargs):
        return subprocess.run(prefix+list(args),text=True,timeout=1200,**kwargs)
    try:
        docker('create','--name',name,'--network','none','--read-only','--user','10001:10001',
               '--cap-drop','ALL','--security-opt','no-new-privileges',
               '--security-opt','seccomp='+str(ROOT/'docker/chromium-seccomp.json'),
               '--tmpfs','/tmp:size=1g,mode=1777','--shm-size','1g',
               '--mount',f'type=bind,src={ROOT},dst=/workspace,readonly',
               '--env','PYTHONPATH=/workspace/tests:/workspace','--env','ASSISTANT_CHROMIUM=/usr/bin/chromium',
               '--env','RUNNER_ENVIRONMENT=github-hosted',
               '--entrypoint','python',image,'/workspace/.devflow/regression.py','/tmp/regression',check=True)
        with (output/'execution.log').open('w') as log:
            result=docker('start','-a',name,stdout=log,stderr=log)
        docker('cp',name+':/tmp/regression/.',str(output),check=True)
        receipt=json.loads((output/'regression.json').read_text())
        return 0 if result.returncode==0 and receipt['success'] else 1
    finally:
        docker('rm','-f',name,check=True)

if __name__=='__main__':
    raise SystemExit(main())
