"""Fixed in-image deployment tasks. No URLs, paths or shell from Actions inputs.

Runtime root is always /data at the CLI. Functions take a root only for offline
SQLite/Unix-socket integration tests. No model/account/grant operation exists.
"""
import argparse
import asyncio
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import sys

# Public fixed rejection codes only. No exception/DOM/secret text may escape.
SAFE_FAILURE_CODES=frozenset({'daemon_identity_unknown','daemon_identity_mismatch',
    'control_peer_uid_mismatch','control_peer_pid_mismatch','exact_rejection_required',
    'control_parent_unsafe','control_socket_unsafe','readiness_not_quiescent',
    'control_state_changed','browser_secret_required','browser_not_quiescent',
    'browser_login_page_failed','browser_login_failed','daemon_status_failed','daemon_not_idle',
    'browser_egress_rejected','browser_control_state_changed','post_browser_health_failed',
    'secret_input_rejected','secret_keys_rejected','initial_password_required',
    'approved_safe_entrypoint_unverified','anonymous_login_failed','anonymous_api_not_rejected',
    'authenticated_data_visible','anonymous_control_state_changed','codex_home_scope_unverified',
    'anonymous_deployment_mismatch'})


def require(ok,code):
    if not ok:raise ValueError(code)


def private(path,directory=False):
    info=path.lstat()
    require(path.absolute()==path.resolve() and info.st_uid==os.getuid()
        and not info.st_mode&0o077 and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)),
        'private_resource_required')


def initialize(root,password):
    from teams_transcription.assistant_config import load_assistant_settings
    from teams_transcription.admin_web import initialize_admin_credentials
    from teams_transcription.assistant_store import AssistantStore
    root=Path(root);private(root,True)
    config=root/'config';config.mkdir(mode=0o700,exist_ok=True);private(config,True)
    runtime=config/'runtime.yml'
    paths=[config/'admin-password.json',config/'admin-session.key']
    if runtime.exists():
        private(runtime)
        for path in paths:private(path)
        settings=load_assistant_settings(runtime)
        require(settings.data_root==str(root),'runtime_root_mismatch')
        # Never reset an existing operator configuration/password/consent.
        require((root/'.meeting-store.sqlite3').exists(),'partial_initialization_requires_review')
        return dict(initialized=False,existing=True)
    require(not any(p.exists() or p.is_symlink() for p in paths)
        and not (root/'.meeting-store.sqlite3').exists(),'partial_initialization_requires_review')
    require(isinstance(password,str) and 16<=len(password)<=1024 and '\n' not in password,'initial_password_required')
    settings=dict(data_root=str(root),host='0.0.0.0',port=8080,log_level='error',
        admin_password_file=str(paths[0]),admin_session_secret_file=str(paths[1]),
        google_client_config_file=str(config/'google-client.json'),
        google_credentials_file=str(config/'google-credentials.json'),
        codex_home=str(root/'codex'),allowed_egress_hosts=[],cookie_secure=False)
    with runtime.open('x') as out:out.write(json.dumps(settings))
    runtime.chmod(0o600)
    initialize_admin_credentials(load_assistant_settings(runtime),password)
    AssistantStore(root)  # schema only, no daemon/browser/network/credentials
    return dict(initialized=True,scheduler_paused=True,summary_disabled=True)


def database_state(root):
    root=Path(root);private(root,True)
    path=root/'.meeting-store.sqlite3';private(path)
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        paused=json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0]).get('scheduler_paused') is True
        active=bool(db.execute("SELECT EXISTS(SELECT 1 FROM meetings WHERE status NOT IN ('completed','failed','stopped'))").fetchone()[0])
        leases=bool(db.execute("SELECT EXISTS(SELECT 1 FROM execution_leases WHERE state='active')").fetchone()[0])
        summaries=bool(db.execute("SELECT EXISTS(SELECT 1 FROM summary_jobs WHERE state IN ('pending','running'))").fetchone()[0])
        rows=db.execute("SELECT r.session_id,r.state,r.ended_at,r.end_reason,r.error_code,s.status_json,c.review_stamp "
            "FROM assistant_runs r LEFT JOIN session_runtime s ON s.meeting_id=r.session_id "
            "LEFT JOIN capture_cleanup_reviews c ON c.session_id=r.session_id "
            "WHERE r.end_reason='service_interrupted' OR r.error_code='capture_cleanup_unknown' "
            "OR json_extract(s.status_json,'$.cleanup_status')='unknown' OR c.session_id IS NOT NULL")
        interlock=False
        for row in rows:
            value=dict(zip(('session_id','state','ended_at','end_reason','error_code','status_json'),row[:6]))
            stamp=hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if row[1] not in {'completed','failed','stopped','interrupted'} or row[2] is None or row[6]!=stamp:
                interlock=True
        schema=hashlib.sha256(json.dumps(db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall(),separators=(',',':')).encode()).hexdigest()
        return dict(scheduler_paused=paused,active_meeting=active,active_lease=leases,
            pending_summary=summaries,unreviewed_cleanup=interlock,schema=schema,
            config_sha256=hashlib.sha256((root/'config/runtime.yml').read_bytes()).hexdigest(),
            quiescent=paused and not(active or leases or summaries or interlock))


def maintenance(root,operation,backup_id=None):
    """Use the application's actual claim lock/fence; never restore any data."""
    root=Path(root);private(root,True)
    lock=root/'.meeting-store.lock';private(lock)
    with lock.open('r+b') as fd:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        value=database_state(root)
        require(value['quiescent'],'runtime_not_quiescent')
        marker=root/'.deployment-maintenance'
        if operation=='enable':
            if marker.exists():private(marker)
            else:
                with marker.open('x'):pass
                marker.chmod(0o600)
        elif operation=='disable':
            if marker.exists():private(marker);marker.unlink()
        elif operation=='backup':
            require(marker.is_file() and re.fullmatch('[0-9a-f]{32}',backup_id or ''),'backup_not_admitted')
            directory=root/'deployment-backups';directory.mkdir(mode=0o700,exist_ok=True);private(directory,True)
            target=directory/(backup_id+'.sqlite3')
            with target.open('xb'):pass
            target.chmod(0o600)
            with contextlib.closing(sqlite3.connect((root/'.meeting-store.sqlite3').as_uri()+'?mode=ro',uri=True)) as source,contextlib.closing(sqlite3.connect(target)) as dest:
                source.backup(dest)
                require(dest.execute('PRAGMA integrity_check').fetchone()[0]=='ok','backup_integrity_failed')
            value['backup_created']=True
        else:raise ValueError('maintenance_operation_rejected')
        value['maintenance']=marker.exists()
        return value


async def control(root,operation,ident,request_sha=None):
    from teams_transcription.teams_local_control import request, LocalControlError
    require(operation in {'view','stop'},'control_operation_rejected')
    require(re.fullmatch('[0-9a-f]{32}',ident or ''),'control_id_invalid')
    fields=dict(id=ident)
    if operation=='stop':
        require(re.fullmatch('[0-9a-f]{64}',request_sha or ''),'control_binding_required')
        fields['acceptance_request']=request_sha
    failure=None
    try:
        result=await request(root,dict(operation=operation,fields=fields))
    except LocalControlError as exc:
        if operation!='stop':raise
        failure=exc  # already-stopped retries still need the exact durable proof
    # view is the original privacy-filtered acceptance projection; no URL/text.
    if operation=='stop':
        # Stop-return alone is NOT cleanup. Re-read the real SQLite receipt.
        from teams_transcription.teams_acceptance import receipt
        with contextlib.closing(sqlite3.connect((Path(root)/'.meeting-store.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
            db.row_factory=sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            status=json.loads(db.execute('SELECT status_json FROM session_runtime WHERE meeting_id=?',(ident,)).fetchone()[0])
            proof=receipt(db,ident,status)
            state=db.execute('SELECT state FROM assistant_runs WHERE session_id=?',(ident,)).fetchone()[0]
        verified=bool(proof and proof['request_sha256']==request_sha
            and proof['cleanup_verified'] is True and state in {'completed','failed','stopped','interrupted'})
        if failure and not verified:raise failure
        require(verified,'stop_cleanup_unverified')
        return dict(ok=True,cleanup_verified=True)
    require(result.get('recording_authorized') is True and result.get('speaking_authorized') is True,
        'authorized_occurrence_required')
    return result


def smoke():
    import urllib.request
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8080/healthz',timeout=3) as response:
        require(response.status==200,'health_failed')
    with opener.open('http://127.0.0.1:8080/login',timeout=3) as response:
        body=response.read(65537)
        require(response.status==200 and response.headers.get_content_type()=='text/html'
            and len(body)<=65536 and b'password' in body,'login_smoke_failed')
    return dict(health=True,anonymous_login_page=True,scope='readonly-no-auth-or-join')


def sandbox():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        with p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,chromium_sandbox=True) as browser:
            page=browser.new_page()
            page.set_content('<title>Local sandbox deployment probe</title>')
            require(page.title()=='Local sandbox deployment probe','sandbox_page_failed')
    return dict(sandboxed_local_page=True,no_private_data_mount=True)


def read_password(raw):
    """Only the one documented secret entry. Never return raw input in errors."""
    require(isinstance(raw,str) and len(raw)<=4096 and '\r' not in raw,'secret_input_rejected')
    lines=raw.splitlines()
    require(len(lines)==1 and lines[0].startswith('DEVFLOW_ADMIN_PASSWORD='),'secret_keys_rejected')
    password=lines[0].split('=',1)[1]
    require(16<=len(password)<=1024 and not any(ord(c)<32 or ord(c)==127 for c in password),'initial_password_required')
    return password


def claim_snapshot(root):
    """Readonly consistent control/authorization snapshot; values never leave RAM.

    Do not hash transcripts, credentials, or login audit. These fixed tables own
    paused, occurrence consent, run/claim and Calendar/series policy. Stream rows
    under a bounded SQLite transaction; no constructor/migration/seed is called.
    """
    import time
    deadline=time.monotonic()+5
    path=Path(root)/'.meeting-store.sqlite3';private(path)
    digest=hashlib.sha256()
    with contextlib.closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        db.set_progress_handler(lambda:int(time.monotonic()>deadline),1000)
        db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        for table in ('assistant_settings','assistant_calendars','meeting_series',
                      'calendar_occurrences','assistant_runs','execution_leases'):
            digest.update(table.encode())
            for row in db.execute('SELECT * FROM '+table+' ORDER BY rowid'):
                digest.update(json.dumps(row,separators=(',',':'),default=str).encode())
                digest.update(b'\n')
    return digest.digest()


def socket_peer_uid(connection):
    import socket,struct
    return struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]


def deployed_daemon_peer(pid):
    """The kernel peer is the fixed installed daemon, not Docker's init PID.

    Compose init:true makes PID1 docker-init; the actual CLI is its child. Read
    bounded proc metadata only, never emit argv/status or accept a caller PID.
    """
    proc=Path('/proc')/str(pid)
    with (proc/'cmdline').open('rb') as source:raw=source.read(4097)
    require(len(raw)<=4096,'daemon_identity_unknown')
    argv=raw.rstrip(b'\0').decode().split('\0')
    expected=[str(Path(sys.executable).parent/'teams-transcription'),'serve','--config',
              '/data/config/runtime.yml','--local-teams-control']
    with (proc/'status').open() as source:status=source.read(16385)
    require(len(status)<=16384,'daemon_identity_unknown')
    fields=dict(line.split(':',1) for line in status.splitlines() if ':' in line)
    require((proc/'exe').resolve()==Path(sys.executable).resolve() and argv[1:]==expected
        and all(int(uid)==os.getuid() for uid in fields['Uid'].split())
        and (pid==1 or fields['PPid'].strip()=='1'),'daemon_identity_mismatch')


async def rejection_readiness(root,*,expected_pid=None,require_daemon=False):
    """Fixed malformed/no-ID message, NOT a positive authorized control probe."""
    import socket,struct
    root=Path(root);before=claim_snapshot(root)
    require(database_state(root)['quiescent'],'readiness_not_quiescent')
    parent=root/'.teams-control';info=parent.lstat()
    require(parent.absolute()==parent.resolve() and stat.S_ISDIR(info.st_mode)
        and stat.S_IMODE(info.st_mode)==0o700 and info.st_uid==os.getuid(),'control_parent_unsafe')
    path=parent/'control.sock';info=path.lstat()
    require(path.absolute()==path.resolve() and stat.S_ISSOCK(info.st_mode)
        and stat.S_IMODE(info.st_mode)==0o600 and info.st_uid==os.getuid(),'control_socket_unsafe')
    async with asyncio.timeout(3):
        reader,writer=await asyncio.open_unix_connection(path,limit=256)
        try:
            peer=writer.get_extra_info('socket')
            require(socket_peer_uid(peer)==os.getuid(),'control_peer_uid_mismatch')
            pid=struct.unpack('3i',peer.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[0]
            require(expected_pid is None or pid==expected_pid,'control_peer_pid_mismatch')
            if require_daemon:deployed_daemon_peer(pid)
            # Exact existing malformed schema; dispatch rejects before ID lookup.
            writer.write(b'{}\n');await writer.drain()
            response=await reader.readline()
            require(response==b'{"error": "teams_local_control_rejected"}\n','exact_rejection_required')
        finally:
            writer.close();await writer.wait_closed()
    require(before==claim_snapshot(root) and database_state(root)['quiescent'],'control_state_changed')
    return dict(scope='transport-and-rejection-readiness',same_uid=True,private_socket=True,
        exact_rejection=True,sqlite_unchanged=True,scheduler_paused=True,
        authorized_view='NOT EXECUTED',bound_stop_cleanup='NOT EXECUTED')


async def browser_readiness(root,password,*,expected_pid=None):
    """Short-lived real sandboxed Chromium; never records private UI or secrets."""
    from playwright.async_api import async_playwright,expect
    from urllib.parse import urlsplit
    require(isinstance(password,str) and 16<=len(password)<=1024,'browser_secret_required')
    root=Path(root);before=claim_snapshot(root)
    require(database_state(root)['quiescent'],'browser_not_quiescent')
    origin='http://127.0.0.1:8080'
    violations=[]
    async with asyncio.timeout(45),async_playwright() as p:
        browser=await p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,chromium_sandbox=True)
        try:
            context=await browser.new_context(service_workers='block',accept_downloads=False)
            async def guard(route):
                req=route.request;url=urlsplit(req.url)
                safe=(url.scheme=='http' and url.netloc=='127.0.0.1:8080'
                    and ((req.method=='GET' and (url.path in {'/login','/','/settings','/api/status','/api/calendar','/healthz'}
                         or url.path in {'/assets/calendar.css','/assets/calendar.js'}))
                         or req.method=='POST' and url.path=='/login'))
                if safe:await route.continue_()
                else:violations.append(True);await route.abort()
            await context.route('**/*',guard)
            page=await context.new_page()
            # No handlers that store request/response/console/error bodies; no
            # tracing, screenshots, video, HAR, downloads or storage_state files.
            page.set_default_timeout(10000)
            response=await page.goto(origin+'/login')
            require(response.status==200,'browser_login_page_failed')
            await expect(page.get_by_role('heading',name='管理登入',exact=True)).to_be_visible()
            await page.locator('input[name=password]').fill(password)
            async with page.expect_response(lambda r:urlsplit(r.url).path=='/api/calendar') as pending:
                await page.get_by_role('button',name='登入',exact=True).click()
            response=await pending.value
            require(response.status==200 and (await response.json())['scheduler_paused'] is True,'browser_login_failed')
            await expect(page.locator('.day-column:not(.time-axis)')).to_have_count(7)
            await page.goto(origin+'/settings')
            await expect(page.get_by_role('heading',name='設定',exact=True)).to_be_visible()
            async def daemon_idle():
                response=await context.request.get(origin+'/api/status')
                require(response.status==200,'daemon_status_failed')
                status=await response.json()
                require(status.get('started') is True and status.get('closing') is False
                    and status.get('active_session_id') is None,'daemon_not_idle')
            await daemon_idle()
            readiness=await rejection_readiness(root,expected_pid=expected_pid,require_daemon=expected_pid is None)
            await daemon_idle()
            require(not violations,'browser_egress_rejected')
            await page.close();await context.close()
        finally:await browser.close()
    # Browser context, cookies and entered password die before the health read.
    require(before==claim_snapshot(root) and database_state(root)['quiescent'],'browser_control_state_changed')
    require((await asyncio.to_thread(smoke))['health'],'post_browser_health_failed')
    return dict(browser_login=True,weekly_calendar=True,settings=True,browser_closed=True,
        post_close_health=True,daemon_idle_before_after=True,control_state_unchanged=True,
        no_secret_artifacts=True,user_reachable_entrypoint='NOT VERIFIED',readiness=readiness)


def runtime_metadata(root):
    """Fixed permission/persistence metadata only; never list/read Codex files."""
    from teams_transcription.assistant_config import load_assistant_settings
    root=Path(root);private(root,True)
    runtime=root/'config/runtime.yml';private(runtime)
    settings=load_assistant_settings(runtime)
    require(settings.data_root==str(root),'runtime_root_mismatch')
    require(isinstance(settings.codex_home,str),'codex_home_scope_unverified')
    codex=Path(settings.codex_home)
    require(codex.is_relative_to(root) and codex.absolute()==codex.resolve(),'codex_home_scope_unverified')
    home_exists=codex.exists()
    if home_exists:private(codex,True)
    return dict(runtime_private=True,runtime_uid=os.getuid(),runtime_gid=runtime.stat().st_gid,
        runtime_mode=stat.S_IMODE(runtime.stat().st_mode),runtime_writable=os.access(runtime,os.W_OK,effective_ids=True),
        codex_home_in_same_data_root=True,codex_home_exists=home_exists,
        codex_home_private=True if home_exists else None,
        codex_home_uid=codex.stat().st_uid if home_exists else None,
        codex_home_gid=codex.stat().st_gid if home_exists else None,
        codex_home_writable=os.access(codex,os.W_OK|os.X_OK,effective_ids=True) if home_exists else None,
        codex_home_mode=stat.S_IMODE(codex.stat().st_mode) if home_exists else None,
        credentials_read=False,provider_authorization='NOT VERIFIED')


PRIVATE_ORIGIN='http://127.0.0.1:8080'
PRIVATE_ACCESS='approved-private-ssh-tunnel'
HTTPS_ACCESS='configured-https'


def access_evidence(origin):
    """Fixed transport names, never a caller-supplied approval or URL payload."""
    return dict(access_mode=PRIVATE_ACCESS if origin==PRIVATE_ORIGIN else HTTPS_ACCESS)


def configured_entrypoint(root):
    """Operator-owned existing runtime configuration, never a workflow URL input.

    The operator approved host 127.0.0.1:18101 -> this installed :8080 UI.
    Only that fixed private mode may use HTTP/non-Secure cookies. Host binding
    and the installed container identity are independently checked by the deploy
    consumer. No tunnel, proxy, runtime config or public listener is created.
    """
    from teams_transcription.assistant_config import load_assistant_settings
    from urllib.parse import urlsplit
    root=Path(root);private(root,True)
    runtime=root/'config/runtime.yml';private(runtime)
    settings=load_assistant_settings(runtime)
    require(settings.data_root==str(root),'approved_safe_entrypoint_unverified')
    if (settings.public_base_url in {None,PRIVATE_ORIGIN,PRIVATE_ORIGIN+'/'}
        and settings.cookie_secure is False and settings.host=='0.0.0.0' and settings.port==8080):
        return PRIVATE_ORIGIN
    value=urlsplit(settings.public_base_url or '')
    import ipaddress
    host=value.hostname or ''
    try:
        address=ipaddress.ip_address(host)
        mapped=address.ipv4_mapped if isinstance(address,ipaddress.IPv6Address) else None
        loopback=address.is_loopback or address.is_unspecified or bool(mapped is not None and mapped.is_loopback)
    except ValueError:
        loopback=host=='localhost' or host.endswith('.localhost') or bool(re.fullmatch('[0-9.]+',host))
    require(not loopback and settings.data_root==str(root) and value.scheme=='https' and settings.cookie_secure is True
        and bool(value.hostname) and value.hostname not in {'localhost','127.0.0.1','::1'}
        and not value.username and not value.password and not value.query and not value.fragment
        and value.path in {'','/'},'approved_safe_entrypoint_unverified')
    return value.scheme+'://'+value.netloc


async def same_deployment_session(client,cookie):
    """Reuse of an anonymous session binds ingress to the installed daemon.

    No key read, authentication, caller URL or session persistence. The exact
    localhost GET is the existing installed UI; operator reachability remains
    limited to the explicitly approved private-tunnel equivalence when used.
    """
    from http.cookies import SimpleCookie
    import hmac
    require(isinstance(cookie,str) and re.fullmatch('[a-f0-9]{64}\\.[a-f0-9]{64}',cookie),'anonymous_login_failed')
    local=await client.get(PRIVATE_ORIGIN+'/login',
        headers={'Cookie':'assistant_session='+cookie},max_redirects=0)
    cookies=[row['value'] for row in local.headers_array if row['name'].lower()=='set-cookie']
    require(len(cookies)==1,'anonymous_deployment_mismatch')
    parsed=SimpleCookie();parsed.load(cookies[0])
    require(local.status==200 and 'assistant_session' in parsed
        and hmac.compare_digest(parsed['assistant_session'].value,cookie),'anonymous_deployment_mismatch')


def anonymous_route_allowed(method,url,origin):
    from urllib.parse import urlsplit
    value=urlsplit(url)
    return (method=='GET' and value.scheme+'://'+value.netloc==origin
        and value.path in {'/login','/api/status','/assets/calendar.css'}
        and not value.query and not value.fragment)


def anonymous_cookie_valid(cookie,origin):
    from urllib.parse import urlsplit
    return (cookie.get('secure') is (origin!=PRIVATE_ORIGIN)
        and cookie.get('httpOnly') is True and cookie.get('sameSite')=='Lax'
        and cookie.get('domain')==urlsplit(origin).hostname and cookie.get('path')=='/')


async def anonymous_login(root):
    """Anonymous GET-only browser on fixed private or configured HTTPS; no secrets."""
    from playwright.async_api import async_playwright,expect
    root=Path(root);origin=configured_entrypoint(root);before=claim_snapshot(root)
    violations=[]
    async with asyncio.timeout(30),async_playwright() as p:
        browser=await p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,chromium_sandbox=True)
        try:
            context=await browser.new_context(service_workers='block',accept_downloads=False)
            async def guard(route):
                request=route.request
                if anonymous_route_allowed(request.method,request.url,origin):
                    await route.continue_()
                else:violations.append(True);await route.abort()
            await context.route('**/*',guard)
            page=await context.new_page();page.set_default_timeout(10000)
            response=await page.goto(origin+'/login')
            require(response is not None and response.status==200 and page.url==origin+'/login','anonymous_login_failed')
            await expect(page.get_by_role('heading',name='管理登入',exact=True)).to_be_visible()
            field=page.get_by_label('管理密碼',exact=True)
            await expect(field).to_be_visible();await expect(field).to_have_attribute('type','password')
            await expect(page.get_by_role('button',name='登入',exact=True)).to_be_visible()
            # Bind the configured ingress to THIS installed daemon without
            # reading its session key or authenticating. The real daemon reuses
            # its anonymous signed/in-memory session on GET /login; a foreign
            # cookie creates a different session. Keep all cookie bytes in RAM.
            cookies=[cookie for cookie in await context.cookies(origin) if cookie['name']=='assistant_session']
            require(len(cookies)==1 and anonymous_cookie_valid(cookies[0],origin),
                'anonymous_login_failed')
            await same_deployment_session(context.request,cookies[0]['value'])
            # No authenticated data is fetched or retained. An unauthed API must
            # reject; never parse its body on a surprising success.
            response=await context.request.get(origin+'/api/status',max_redirects=0)
            require(response.status==401,'anonymous_api_not_rejected')
            require(not violations,'browser_egress_rejected')
            require(await page.locator('.day-column').count()==0
                and await page.get_by_role('heading',name='設定',exact=True).count()==0,'authenticated_data_visible')
            await page.close();await context.close()
        finally:await browser.close()
    require(before==claim_snapshot(root),'anonymous_control_state_changed')
    return dict(approved_safe_entrypoint=True,origin_bound=True,anonymous_context=True,
        login_heading=True,password_field=True,login_button=True,anonymous_api_rejected=True,
        no_authenticated_data=True,no_secret_artifacts=True,same_deployment_session=True,
        **access_evidence(origin))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['init','state','enable','disable','backup','smoke','sandbox','view','stop','verify','entrypoint','login','metadata'])
    p.add_argument('--id');p.add_argument('--request-sha');args=p.parse_args()
    try:
        root=Path('/data')
        if args.action in {'init','verify'}:
            raw=sys.stdin.read(4097)
            password=read_password(raw) if raw else None
            if args.action=='init':value=initialize(root,password)
            else:
                # Do not let debug env enable driver protocol/secret logging.
                for key in ('DEBUG','PWDEBUG'):os.environ.pop(key,None)
                value=asyncio.run(browser_readiness(root,password))
        elif args.action=='entrypoint':
            origin=configured_entrypoint(root)
            value=dict(approved_safe_entrypoint=True,origin_bound=True,**access_evidence(origin))
        elif args.action=='login':
            for key in ('DEBUG','PWDEBUG'):os.environ.pop(key,None)
            value=asyncio.run(anonymous_login(root))
        elif args.action=='metadata':value=runtime_metadata(root)
        elif args.action=='state':value=database_state(root)
        elif args.action in {'enable','disable','backup'}:value=maintenance(root,args.action,args.id)
        elif args.action in {'view','stop'}:value=asyncio.run(control(root,args.action,args.id,args.request_sha))
        elif args.action=='sandbox':value=sandbox()
        else:value=smoke()
        print(json.dumps(value));return 0
    except Exception as error:
        code=str(error) if type(error) is ValueError and str(error) in SAFE_FAILURE_CODES else 'runtime_failed'
        print(json.dumps(dict(error='target_runtime_rejected',code=code)));return 1


if __name__=='__main__':raise SystemExit(main())
