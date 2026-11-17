"""Exercise the production image/Compose contract using private disposable SQLite.

No credentials, calendars, meeting URLs, model requests or published ports.
The sandboxed browser visits an in-memory page and the authenticated loopback UI;
container network is none.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid
import runpy

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT/'tools'))
sys.path.insert(0,str(ROOT))
from check_compose import example_values,render,write_env
from teams_transcription.storage import atomic_json,stamp


def history_probe_script(tool_source,fixture_source):
    """Only trusted source strings become files, never shell fragments."""
    return f"""import pathlib,tempfile,runpy,asyncio,json
with tempfile.TemporaryDirectory(prefix='history-smoke-') as directory:
 p=pathlib.Path(directory)
 (p/'decoder_history_check.py').write_text({tool_source!r})
 (p/'decoder_audio_check.js').write_text({fixture_source!r})
 try:
  module=runpy.run_path(str(p/'decoder_history_check.py'),run_name='history_image_probe')
  result=asyncio.run(module['check']())
 except Exception:
  result=dict(success=False,failure_code='synthetic_history_probe_failed',cases=[])
 print(json.dumps(result))
"""


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True,help='Already-built immutable local sha256 image ID')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not args.image.startswith('sha256:') or len(args.image)!=71:
        parser.error('Use the immutable local image ID')
    if args.output and args.output.exists():
        parser.error('Evidence destination already exists')
    environment=os.environ.copy()
    for name in ('DOCKER_HOST','DOCKER_CONTEXT','DOCKER_TLS','DOCKER_TLS_VERIFY','DOCKER_CERT_PATH'):
        environment.pop(name,None)
    prefix=['docker','--host=unix:///var/run/docker.sock']
    def docker(*parts,input=None,timeout=90):
        result=subprocess.run(prefix+list(parts),env=environment,input=input,capture_output=True,text=True,timeout=timeout)
        if result.returncode:
            raise RuntimeError('local_docker_'+parts[0]+'_failed: '+result.stderr[-2000:])
        return result.stdout.strip()
    name='assistant-smoke-'+uuid.uuid4().hex[:12]
    checks={}
    result=dict(test_id='standalone-production-compose',tested_at=stamp(),image_id=args.image,
        input_kind='controlled private SQLite',network='none',credentials_mounted=False,
        model_or_meeting_called=False,checks=checks)
    with tempfile.TemporaryDirectory(prefix=name+'-') as directory:
        directory=Path(directory)
        env_file=directory/'fixture.env'
        values=example_values()
        values.update(TRANSCRIPTION_IMAGE=args.image,PUID='10001',PGID='10001',
            APPDATA_HOST=str(directory/'unused-bind'),CONFIG_ENV_FILE=str(env_file),ASSISTANT_PORT='18080')
        write_env(env_file,values)
        config=render(env_file)
        service=config['services']['teams-transcription']
        service['security_opt']=[
            'seccomp:'+str((ROOT/value.split(':',1)[1]).resolve()) if value.startswith('seccomp:') else value
            for value in service['security_opt']]
        # Replace only fixture storage/network; retain production security,
        # identity, resource, health and command settings from Compose render.
        service['volumes']=[dict(type='volume',source='smoke-data',target='/data')]
        service.pop('networks',None)
        service['network_mode']='none'
        service['restart']='no'
        config.pop('networks',None)
        config['volumes']={'smoke-data':{}}
        config.pop('name',None)
        fixture_file=directory/'compose.json'
        fixture_file.write_text(json.dumps(config))
        compose=['compose','-p',name,'-f',str(fixture_file)]
        def command(*parts,input=None):
            return docker(*(compose+list(parts)),input=input)
        container=None
        try:
            synthesis="""import json
from teams_transcription.qa_probe import synthetic_pcm,WAKE,QUESTION
for text in (WAKE,QUESTION):
 data=synthetic_pcm(text)
 assert 0<len(data)<=576000 and len(data)%2==0 and any(data)
print(json.dumps({'fixed_qa_synthetic_audio_available':True,'fixed_qa_mandarin_phonemes_verified':True}))
"""
            # Actual installed image, network-none, before any provider/account
            # operation. Missing eSpeak/FFmpeg fails CI without spending Live runs.
            checks.update(json.loads(command('run','--rm','--no-deps','--pull','never','-T',
                '--entrypoint','python','teams-transcription','-c',synthesis)))
            # Exercise installed receiver/consumer code under policy-equivalent
            # CSP before any model/account operation. Only public fixture bytes
            # enter tmpfs; no package/source override hides missing wheel files.
            tool_source=(ROOT/'tools/decoder_history_check.py').read_text()
            fixture_source=(ROOT/'tools/decoder_audio_check.js').read_text()
            history_probe=history_probe_script(tool_source,fixture_source)
            raw=command('run','--rm','--no-deps','--pull','never','-T',
                '--entrypoint','python','teams-transcription','-c',history_probe)
            history_result=json.loads(raw.splitlines()[-1])
            history_module=runpy.run_path(str(ROOT/'tools/decoder_history_check.py'))
            history_checks=history_module['qualification_checks'](history_result)
            checks['actual_installed_policy_history']=all(history_checks.values())
            if args.output:
                args.output.with_name('render-history.json').write_text(json.dumps(history_result,indent=2)+'\n')
            if not checks['actual_installed_policy_history']:
                raise RuntimeError('actual_installed_policy_history_failed')
            seed="from pathlib import Path;import json;d=Path('/data/config');d.mkdir(mode=0o700,parents=True,exist_ok=True);(d/'runtime.yml').write_text(json.dumps({'log_level':'error','codex_home':'/data/codex'}));a=Path('/data/codex');a.mkdir(mode=0o700);f=a/'auth.json';f.write_text('SYNTHETIC-NOT-A-REAL-GRANT');f.chmod(0o600)"
            command('run','--rm','--no-deps','--pull','never','-T','--entrypoint','python','teams-transcription','-c',seed)
            preflight=json.loads(command('run','--rm','--no-deps','--pull','never','-T','teams-transcription',
                'auth-preflight','--config','/data/config/runtime.yml'))
            checks['actual_software_and_synthetic_auth_metadata_preflight']=(preflight['login_ready']
                and preflight['auth_material_private'] and preflight['account_admission']=='not_checked'
                and preflight['live_rx_tx']=='not_tested')
            command('run','--rm','--no-deps','--pull','never','-T','teams-transcription','init-admin',
                '--config','/data/config/runtime.yml','--password-stdin',input='controlled-compose-password-26fb\n')
            command('up','-d','--pull','never')
            container=command('ps','-a','-q','teams-transcription')
            deadline=time.monotonic()+45
            while True:
                try:
                    docker('exec',container,'teams-transcription','healthcheck','--port','18080',timeout=5)
                    break
                except RuntimeError:
                    if time.monotonic()>=deadline:
                        raise RuntimeError('actual_compose_health_timeout') from None
                    time.sleep(.2)
            checks['actual_default_daemon_healthy']=True
            http="""import httpx,re,json
with httpx.Client(base_url='http://127.0.0.1:18080',trust_env=False) as c:
 r=c.get('/api/status');assert r.status_code==401 and r.json()['error']=='admin_login_required'
 p=c.get('/login');csrf=re.search('name="csrf" value="([^"]+)"',p.text).group(1)
 r=c.post('/login',data={'csrf':csrf,'password':'controlled-compose-password-26fb'},headers={'Origin':'https://foreign.invalid'});assert r.status_code==403
 r=c.post('/login',data={'csrf':csrf,'password':'controlled-compose-password-26fb'},headers={'Origin':'http://127.0.0.1:18080'});assert r.status_code==303
 r=c.get('/api/status');assert r.status_code==200 and r.json()['started']
 assert c.get('/mcp').status_code==404
 print(json.dumps({'authenticated_admin_http':True,'foreign_origin_rejected':True,'mcp_route_absent':True}))
"""
            checks.update(json.loads(docker('exec',container,'python','-c',http)))
            fixture="from teams_transcription.assistant_store import AssistantStore;import json;s=AssistantStore('/data');r=s.save_glossary({'kind':'term','canonical':'Controlled restart term','status':'active'});print(json.dumps({'id':r['id']}))"
            entry=json.loads(docker('exec',container,'python','-c',fixture))
            context_probe="""import asyncio,json
from teams_transcription.storage import Session
from teams_transcription.response_runtime import _AsyncStore
from teams_transcription.b_response import MeetingStoreMinutesReader,MeetingMinutesTool
from teams_transcription.b_context import MeetingQuestionContext
async def main():
 prior={'available':True,'session_id':'controlled-prior','summary':{key:[] for key in ('重點','決策','待辦','未決問題')},'open_tasks':[]}
 session=Session('/data').create({'provider':'mock','source':'synthetic','prior_context':prior})
 tool=MeetingMinutesTool(session.ident,MeetingStoreMinutesReader(_AsyncStore('/data')))
 context=MeetingQuestionContext(tool);context.bind_session('controlled-container-thread')
 plan=await context.prepare('previous decisions',session_id='controlled-container-thread')
 assert not plan.result['available'] and plan.result['history_status']['summary_available']
 assert plan.result['history_status']['summary_item_counts']['決策']==0
 context.record_sent(plan,len(plan.payload.encode())+len('RESULT_COMPLETE'),session_id='controlled-container-thread')
 context.commit(plan,session_id='controlled-container-thread')
 for revision in (1,2):
  text='Controlled budget decision '+str(revision)+': '+'完整保存。'*3000+' tail-'+str(revision)
  session.append_final({'event_id':'controlled-revised-source','revision':revision,'text':text,'timestamp':'2026-10-16T00:00:00Z'})
  plan=await context.prepare('budget',session_id='controlled-container-thread')
  assert plan.result['available'] and [row['text'] for row in json.loads(plan.payload)['selected_source_reading']]==[text]
  context.record_sent(plan,len(plan.payload.encode())+len('RESULT_COMPLETE'),session_id='controlled-container-thread')
  context.commit(plan,session_id='controlled-container-thread')
 print(json.dumps({'empty_saved_history_distinct_from_missing':True,'bound_context_over_old_32k_limit':True}))
asyncio.run(main())
"""
            checks.update(json.loads(docker('exec',container,'python','-c',context_probe)))
            inspect=json.loads(docker('inspect',container))[0]
            host=inspect['HostConfig']
            checks['nonroot_readonly_dropcaps']=inspect['Config']['User']=='10001:10001' and host['ReadonlyRootfs'] and host['CapDrop']==['ALL'] and not host['Privileged']
            checks['network_none_no_published_ports']=host['NetworkMode']=='none' and not host['PortBindings']
            checks['no_auth_or_docker_socket_mount']=all(m['Destination'] not in {'/auth/codex','/var/run/docker.sock'} for m in inspect['Mounts'])
            limits="""from pathlib import Path
cpu=Path('/sys/fs/cgroup/cpu.max').read_text().split()
assert cpu[0]!='max' and int(cpu[0])==2*int(cpu[1])
assert Path('/sys/fs/cgroup/memory.max').read_text().strip()=='4294967296'
assert Path('/sys/fs/cgroup/pids.max').read_text().strip()=='512'
print('enforced')
"""
            checks['effective_cgroup_cpu_memory_pids_limits']=docker('exec',container,'python','-c',limits)=='enforced'
            denied="import ctypes,errno,os;libc=ctypes.CDLL(None,use_errno=True);assert os.getuid()==10001;assert libc.chroot(b'/')==-1 and ctypes.get_errno()==errno.EPERM;print('denied')"
            checks['container_chroot_still_kernel_denied']=docker('exec',container,'python','-c',denied)=='denied'
            browser="""import asyncio,json
from teams_transcription.browser import Browser
from playwright.async_api import expect
async def main():
 async with asyncio.timeout(30):
  async with Browser(executable='/usr/bin/chromium') as browser:
   context=await browser.browser.new_context()
   await context.route('**/*',lambda route:route.continue_() if route.request.url.startswith('http://127.0.0.1:18080/') else route.abort())
   page=await context.new_page();await page.set_content('<h1>controlled local sandbox</h1>')
   assert await page.locator('h1').inner_text()=='controlled local sandbox'
   errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
   await page.goto('http://127.0.0.1:18080/login')
   await page.locator('input[name=password]').fill('controlled-compose-password-26fb')
   async with page.expect_response(lambda response:'/api/calendar?' in response.url) as pending:
    await page.get_by_role('button',name='登入',exact=True).click()
   response=await pending.value
   assert response.status==200 and (await response.json())['meetings']==[]
   await page.locator('.day-column:not(.time-axis)').nth(6).wait_for()
   assert await page.locator('.day-column:not(.time-axis)').count()==7
   await expect(page.locator('#workspace')).to_have_css('display','grid')
   assert not errors
   await context.close()
 print(json.dumps({'actual_sandboxed_chromium_local_page':True,'authenticated_calendar_ui':True}))
asyncio.run(main())
"""
            checks.update(json.loads(docker('exec',container,'python','-c',browser,timeout=40)))
            result['codex_version']=docker('exec',container,'codex','--version')
            checks['pinned_codex_installed']=result['codex_version']=='codex-cli 0.159.0-alpha.3'
            started=time.monotonic()
            command('stop','-t','35')
            state=json.loads(docker('inspect',container))[0]['State']
            result['stop_seconds']=round(time.monotonic()-started,3)
            checks['graceful_sigterm']=state['ExitCode'] in {0,143} and result['stop_seconds']<35
            command('start')
            auth_persist="from pathlib import Path;p=Path('/data/codex/auth.json');assert p.read_text()=='SYNTHETIC-NOT-A-REAL-GRANT' and p.stat().st_mode&0o777==0o600;print('synthetic-persisted')"
            checks['synthetic_auth_home_survives_restart']=docker('exec',container,'python','-c',auth_persist)=='synthetic-persisted'
            persisted="from teams_transcription.assistant_store import AssistantStore;import json;print(json.dumps([r['id'] for r in AssistantStore('/data').list_glossary()]))"
            checks['sqlite_survives_restart']=entry['id'] in json.loads(docker('exec',container,'python','-c',persisted))
            before_recreate=container
            command('up','-d','--force-recreate','--pull','never')
            container=command('ps','-a','-q','teams-transcription')
            assert container!=before_recreate
            deadline=time.monotonic()+45
            while True:
                try:
                    docker('exec',container,'teams-transcription','healthcheck','--port','18080',timeout=5)
                    break
                except RuntimeError:
                    if time.monotonic()>=deadline:raise RuntimeError('recreated_compose_health_timeout') from None
                    time.sleep(.2)
            checks['synthetic_auth_home_survives_container_recreation']=docker('exec',container,'python','-c',auth_persist)=='synthetic-persisted'
            checks['sqlite_survives_container_recreation']=entry['id'] in json.loads(docker('exec',container,'python','-c',persisted))
            nofiles="from pathlib import Path;assert not list(Path('/data').rglob('*.md'));assert not list(Path('/data').rglob('*.wav'));print('ok')"
            checks['no_meeting_markdown_or_raw_audio']=docker('exec',container,'python','-c',nofiles)=='ok'
        finally:
            command('down','--volumes','--timeout','35')
    result['success']=all(checks.values())
    if args.output:
        atomic_json(args.output,result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['success'] else 1


if __name__=='__main__':
    raise SystemExit(main())
