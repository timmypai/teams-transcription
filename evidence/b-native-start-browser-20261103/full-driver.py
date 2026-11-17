import io,json,os,pathlib,subprocess,tarfile,time,uuid

root=pathlib.Path('/workspace/teams-native-start-20261103')
out=pathlib.Path('/workspace/shared/b-native-start-browser-internal-20261103')
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
script='''import json,os,pathlib,sys,tarfile,time,unittest
assert os.getuid()==10001
source=pathlib.Path('/tmp/current-source');source.mkdir(mode=0o700)
with tarfile.open(fileobj=sys.stdin.buffer,mode='r|') as archive:archive.extractall(source,filter='data')
os.chdir(source);sys.path[:0]=[str(source),str(source/'tests')]
for key in ('NO_PROXY','no_proxy'):
 os.environ[key]=','.join(dict.fromkeys(os.environ.get(key,'').split(',')+['localhost','127.0.0.1','::1']))
started=time.monotonic()
suite=unittest.defaultTestLoader.loadTestsFromNames(['test_b_live.IncomingTrackTests','test_response_runtime'])
result=unittest.TextTestRunner(verbosity=2).run(suite)
print('B_NATIVE_BROWSER_RESULT '+json.dumps(dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped),seconds=round(time.monotonic()-started,3),success=result.wasSuccessful(),uid=os.getuid(),synthetic_only=True)))
raise SystemExit(0 if result.wasSuccessful() else 1)
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
