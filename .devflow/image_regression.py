"""Run regressions inside the real image on an owned internal-only ICE network."""
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
    # Public synthetic test evidence only, on a disposable hosted runner.
    # UID10001 writes directly here; stopped container tmpfs cannot retain logs.
    output.chmod(0o777)
    def docker(*args, **kwargs):
        return subprocess.run(prefix+list(args),text=True,timeout=1200,**kwargs)
    network=name+'-network'
    docker('network','create','--internal',network,check=True)
    try:
        docker('create','--name',name,'--network',network,'--read-only','--user','10001:10001',
               '--cap-drop','ALL','--security-opt','no-new-privileges',
               '--security-opt','seccomp='+str(ROOT/'docker/chromium-seccomp.json'),
               '--tmpfs','/tmp:rw,exec,size=1g,mode=1777','--shm-size','1g',
               '--mount',f'type=bind,src={ROOT},dst=/workspace,readonly',
               '--mount',f'type=bind,src={output},dst=/evidence',
               '--env','PYTHONPATH=/workspace/tests:/workspace','--env','ASSISTANT_CHROMIUM=/usr/bin/chromium',
               '--env','RUNNER_ENVIRONMENT=github-hosted',
               '--env','TEST_SHARD='+os.environ.get('TEST_SHARD','0'),
               '--env','TEST_SHARDS='+os.environ.get('TEST_SHARDS','1'),
               '--entrypoint','python',image,'/workspace/.devflow/regression.py','/evidence',check=True)
        import time
        docker('start',name,check=True)
        deadline=time.monotonic()+1200
        while True:
            state=json.loads(docker('inspect',name,capture_output=True,check=True).stdout)[0]['State']
            if not state['Running']: break
            if time.monotonic()>deadline: raise RuntimeError('regression_deadline_exceeded')
            progress=output/'progress.json'
            if progress.exists():
                try:
                    p=json.loads(progress.read_text())
                    print('regression progress:',p['started'],p['test'],flush=True)
                except json.JSONDecodeError:
                    pass  # producer may be replacing its single bounded record
            time.sleep(20)
        with (output/'execution.log').open('w') as log:
            docker('logs',name,stdout=log,stderr=log)
        receipt=json.loads((output/'regression.json').read_text())
        return 0 if state['ExitCode']==0 and receipt['success'] else 1
    finally:
        docker('rm','-f',name,check=True)
        docker('network','rm',network,check=True)

if __name__=='__main__':
    raise SystemExit(main())
