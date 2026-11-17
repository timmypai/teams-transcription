import io,json,os,pathlib,subprocess,tarfile,time,uuid

root=pathlib.Path('/workspace/teams-native-start-20261103')
out=pathlib.Path('/workspace/shared/b-native-start-browser-paired-20261103')
out.mkdir(parents=True,exist_ok=True)
env=os.environ.copy()
for key in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):
    env.pop(key,None)
docker=['docker','--host=unix:///var/run/docker.sock']
name='b-native-start-'+uuid.uuid4().hex[:16]
network=name+'-internal'
image='sha256:c397b661c65a3c689e43b92b1a3656bfd6fa6163182d7f0e117803fdc53928d0'
files=subprocess.check_output(['git','ls-files','-z'],cwd=root).decode().split('\0')
payload=io.BytesIO()
with tarfile.open(fileobj=payload,mode='w') as archive:
    for relative in files:
        if not relative or not (relative.startswith(('teams_transcription/','tests/','tools/'))
                or relative=='pyproject.toml'):
            continue
        path=root/relative
        if path.is_file() and not path.is_symlink():
            archive.add(path,arcname=relative,recursive=False)
baseline_archive=subprocess.check_output(['git','archive','45caaed44dbb40be73308e6a00056659223ae6fa','teams_transcription','tests','tools','pyproject.toml'],cwd=root)
current_payload=payload.getvalue()
payload=io.BytesIO()
with tarfile.open(fileobj=payload,mode='w') as outer:
    for label, data in [('baseline.tar',baseline_archive),('current.tar',current_payload)]:
        info=tarfile.TarInfo(label);info.size=len(data);outer.addfile(info,io.BytesIO(data))
script='''import json,os,pathlib,sys,tarfile,time,unittest,subprocess
assert os.getuid()==10001
bundle=pathlib.Path('/tmp/comparison');bundle.mkdir(mode=0o700)
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:archive.extractall(bundle,filter='data')
for key in ('NO_PROXY','no_proxy'):
 os.environ[key]=','.join(dict.fromkeys(os.environ.get(key,'').split(',')+['localhost','127.0.0.1','::1']))
reports=[]
for label in ('baseline','current'):
 source=bundle/label;source.mkdir()
 with tarfile.open(bundle/(label+'.tar')) as archive:archive.extractall(source,filter='data')
 program="""import sys,json,unittest,asyncio
sys.path[:0]=['.','tests']
from teams_transcription import browser,native_media
import tests.test_worker_minutes as fixture
print('ORIGINS '+json.dumps({'browser':browser.__file__,'fixture':fixture.__file__}),flush=True)
known={'native_media_setup_failed','native_media_already_connected','native_media_isolation_required','native_media_source_scope_lost','native_media_cleanup_unconfirmed'}
original=native_media.NativeMeetingMedia.connect
async def connect(self,target):
 try:return await original(self,target)
 except BaseException as exc:
  print('NATIVE_CONNECT_FAILURE '+json.dumps({'type':type(exc).__name__,'fixed_reason':str(exc) if str(exc) in known else 'other'}),flush=True)
  raise
native_media.NativeMeetingMedia.connect=connect
suite=unittest.defaultTestLoader.loadTestsFromNames(['test_response_runtime.ResponseRuntimeTests.test_missing_allowlist_never_starts_B_rpc_and_A_keeps_transcribing','test_b_live.IncomingTrackTests.test_input_keeps_real_silent_samples_between_utterances_on_same_track'])
r=unittest.TextTestRunner(verbosity=2).run(suite)
print('PAIR_RESULT '+json.dumps({'tests':r.testsRun,'failures':len(r.failures),'errors':len(r.errors),'skipped':len(r.skipped),'success':r.wasSuccessful()}),flush=True)
"""
 r=subprocess.run([sys.executable,'-c',program],cwd=source,capture_output=True,text=True,timeout=80)
 print('PAIR_LABEL '+label,flush=True);print(r.stdout,flush=True);print(r.stderr,file=sys.stderr,flush=True)
 marker=next((line for line in r.stdout.splitlines() if line.startswith('PAIR_RESULT ')),None)
 reports.append({'label':label,'result':json.loads(marker.split(' ',1)[1]) if marker else None})
print('B_NATIVE_BROWSER_RESULT '+json.dumps({'paired_results':reports,'synthetic_only':True,'uid':os.getuid(),'success':all(r['result'] and r['result']['success'] for r in reports)}))
'''
command=docker+['run','--rm','--pull','never','-i','--name',name,'--init',
    '--user','10001:10001','--read-only','--network',network,'--cap-drop','ALL',
    '--security-opt','no-new-privileges:true','--security-opt','seccomp:'+str(root/'docker/chromium-seccomp.json'),
    '--tmpfs','/tmp:rw,size=1g,mode=1777','--shm-size','1g','--memory','4g','--cpus','2','--pids-limit','512',image,'-c',script]
code=None
report={'success':False,'reason':'run_not_completed'}
subprocess.run(docker+['network','create','--internal',network],env=env,capture_output=True,check=True,timeout=15)
try:
    result=subprocess.run(command,input=payload.getvalue(),capture_output=True,env=env,timeout=240)
    code=result.returncode
    (out/'stdout.log').write_bytes(result.stdout)
    (out/'stderr.log').write_bytes(result.stderr)
    marker=next((line for line in result.stdout.decode(errors='replace').splitlines() if line.startswith('B_NATIVE_BROWSER_RESULT ')),None)
    report=json.loads(marker.split(' ',1)[1]) if marker else {'success':False,'reason':'no_result_marker'}
finally:
    check=subprocess.run(docker+['ps','-a','--filter','name=^/'+name+'$','--format','{{.ID}}'],env=env,capture_output=True,text=True,timeout=10)
    if check.returncode==0 and check.stdout.strip():
        subprocess.run(docker+['stop','--time','5',name],env=env,capture_output=True,timeout=10)
        subprocess.run(docker+['rm','--force',name],env=env,capture_output=True,timeout=10)
    check=subprocess.run(docker+['ps','-a','--filter','name=^/'+name+'$','--format','{{.ID}}'],env=env,capture_output=True,text=True,timeout=10)
    clean=check.returncode==0 and not check.stdout.strip()
    network_cleanup=subprocess.run(docker+['network','rm',network],env=env,capture_output=True,timeout=10)
    clean=clean and network_cleanup.returncode==0
report.update(image=image,docker_exit_code=code,cleanup_outcome='cleaned' if clean else 'unknown',
    network='isolated internal Docker bridge; no external route',read_only=True,cap_drop='ALL',sandbox='repository seccomp; no-new-privileges; Chromium sandbox enabled',credentials_mounted=False)
(out/'result.json').write_text(json.dumps(report,indent=2)+'\n')
print(json.dumps(report,indent=2))
if code or not clean:
    print((out/'stderr.log').read_text()[-4000:] if (out/'stderr.log').exists() else 'no stderr log')
raise SystemExit(code or (0 if clean else 1))
