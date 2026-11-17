"""Offline browser authorization tests in an existing immutable local image.

Uses the same sandbox policy as run_postmeeting_ui_audit.py. Transfers only
tracked application source and synthetic tests over stdin; no runtime mounts,
ports, credentials, model requests, dependency installation or image pulls.
"""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import tarfile

ROOT = Path(__file__).resolve().parent.parent
TESTS = ('test_web_authorization.py','test_web_authorization_browser.py','test_admin_browser.py')

BOOTSTRAP = '''import base64,hashlib,io,json,os,stat,subprocess,sys,tarfile,unittest
from pathlib import Path
payload=json.loads(sys.stdin.readline())
root=Path('/tmp/web-auth-source');root.mkdir()
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(payload['archive'])),mode='r:gz') as archive:
    archive.extractall(root,filter='data')
manifest={str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest()
    for path in sorted(root.rglob('*')) if path.is_file()}
digest=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
assert digest==payload['manifest_sha256'],'source_manifest_mismatch'
sys.path[:0]=[str(root/'tests'),str(root)]
os.chdir(root)
shots=Path('/tmp/web-auth-shots');shots.mkdir()
import teams_transcription.browser as browser_module
native_launch=browser_module.launch_sandboxed_chromium
async def offline_launch(browser_type,**options):
    if options.get('proxy'):
        proxy=dict(options['proxy'])
        proxy['bypass']=','.join(filter(None,[proxy.get('bypass'),'localhost','127.0.0.1','[::1]']))
        options['proxy']=proxy
    return await native_launch(browser_type,**options)
browser_module.launch_sandboxed_chromium=offline_launch
import test_web_authorization_browser
test_web_authorization_browser.SCREENSHOT_DIR=shots
stream=io.StringIO()
suite=unittest.defaultTestLoader.loadTestsFromNames(['test_web_authorization_browser','test_admin_browser'])
result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
helper=Path('/usr/lib/chromium/chrome-sandbox').stat()
versions={name:subprocess.check_output(command,text=True,timeout=5).strip() for name,command in {
    'chromium':['chromium','--version'],
    'cjk_font':['fc-match',':lang=zh-tw','--format','%{family}'],
    'python':['python','--version']}.items()}
artifacts={path.name:base64.b64encode(path.read_bytes()).decode() for path in sorted(shots.glob('*.png'))}
print(json.dumps(dict(source_manifest_sha256=digest,uid=os.getuid(),gid=os.getgid(),versions=versions,
    sandbox_helper=dict(uid=helper.st_uid,mode=oct(stat.S_IMODE(helper.st_mode))),
    tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),
    successful=result.wasSuccessful(),test_log=stream.getvalue(),screenshots=artifacts,
    proxy_bypass='loopback only; remaining proxy preserved; kernel network=none')),flush=True)
sys.exit(0 if result.wasSuccessful() else 1)
'''


def cleanup_container(name, env):
    """Only this run's unique offline test container; unknown stays explicit."""
    def present():
        result = subprocess.run(['docker','--host=unix:///var/run/docker.sock','ps','-a',
            '--filter','name=^/'+name+'$','--format','{{.Names}}'],env=env,capture_output=True,text=True,timeout=10)
        return None if result.returncode else bool(result.stdout.strip())
    try:
        exists = present()
        if exists is False:
            return 'cleaned'
        if exists is None:
            return 'unknown'
        subprocess.run(['docker','--host=unix:///var/run/docker.sock','stop','--time','5',name],
            env=env,capture_output=True,timeout=15)
        if present() is False:
            return 'cleaned'
        subprocess.run(['docker','--host=unix:///var/run/docker.sock','rm','--force',name],
            env=env,capture_output=True,timeout=10)
        return 'cleaned' if present() is False else 'unknown'
    except (OSError,subprocess.TimeoutExpired):
        return 'unknown'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True,help='Already-built local sha256 ID')
    parser.add_argument('--output',required=True,help='New evidence directory under /tmp or /workspace')
    args = parser.parse_args()
    if not re.fullmatch(r'sha256:[a-f0-9]{64}',args.image):
        parser.error('immutable local image ID required')
    output = Path(args.output)
    if (not output.is_absolute() or output.resolve()!=output or output.exists()
            or not any(output.is_relative_to(root) and output!=root for root in (Path('/tmp'),Path('/workspace')))):
        parser.error('new canonical evidence directory under /tmp or /workspace required')
    tracked = subprocess.check_output(['git','ls-files','-z','teams_transcription'],cwd=ROOT).decode().split('\0')
    files = [ROOT/name for name in tracked if name]
    files += [ROOT/'tests'/name for name in TESTS]
    if any(not path.is_file() or path.is_symlink() or path.resolve()!=path for path in files):
        parser.error('regular canonical source files required')
    manifest = {str(path.relative_to(ROOT)):hashlib.sha256(path.read_bytes()).hexdigest() for path in files}
    digest = hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive,mode='w:gz') as stream:
        for path in files:
            stream.add(path,arcname=str(path.relative_to(ROOT)),recursive=False)
    payload = json.dumps(dict(archive=base64.b64encode(archive.getvalue()).decode(),manifest_sha256=digest))+'\n'
    env = os.environ.copy()
    for key in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):
        env.pop(key,None)
    name = 'web-auth-browser-'+digest[:12]+'-'+secrets.token_hex(4)
    command = ['docker','--host=unix:///var/run/docker.sock','run','--rm','--pull','never','-i','--name',name,
        '--network','none','--user','10001:10001','--read-only','--cap-drop','ALL',
        '--security-opt','no-new-privileges:true','--security-opt','seccomp:'+str(ROOT/'docker/chromium-seccomp.json'),
        '--tmpfs','/tmp:rw,size=512m,mode=1777','--shm-size','1g','--memory','4g','--cpus','2',
        '--pids-limit','512','--init','--entrypoint','python',args.image,'-u','-c',BOOTSTRAP]
    output.mkdir(parents=True)
    (output/'source-manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    (output/'command.json').write_text(json.dumps(command,indent=2)+'\n')
    try:
        result = subprocess.run(command,input=payload,text=True,capture_output=True,env=env,timeout=180)
    except BaseException:
        cleanup = cleanup_container(name,env)
        (output/'cleanup.json').write_text(json.dumps(dict(container=name,outcome=cleanup))+'\n')
        raise
    cleanup = cleanup_container(name,env)
    (output/'cleanup.json').write_text(json.dumps(dict(container=name,outcome=cleanup))+'\n')
    (output/'stderr.log').write_text(result.stderr)
    try:
        report = json.loads(result.stdout)
    except ValueError:
        (output/'stdout.log').write_text(result.stdout)
        print(json.dumps(dict(exit_code=result.returncode,evidence=str(output),error='browser_harness_no_report')))
        return 1
    for filename,encoded in report.pop('screenshots',{}).items():
        if not re.fullmatch(r'[a-z0-9-]+\.png',filename):
            raise ValueError('invalid_synthetic_screenshot_name')
        (output/filename).write_bytes(base64.b64decode(encoded,validate=True))
    report.update(image=args.image,docker_exit_code=result.returncode,synthetic_only=True,cleanup_outcome=cleanup)
    (output/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({key:report[key] for key in ('tests','failures','errors','skipped','successful','source_manifest_sha256','image')}))
    return result.returncode if cleanup=='cleaned' else 1


if __name__=='__main__':
    raise SystemExit(main())
