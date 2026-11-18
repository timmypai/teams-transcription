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


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=['init','state','enable','disable','backup','smoke','view','stop'])
    p.add_argument('--id');p.add_argument('--request-sha');args=p.parse_args()
    try:
        root=Path('/data')
        if args.action=='init':
            raw=sys.stdin.read(4097)
            require(len(raw)<=4096,'secret_input_rejected')
            pairs=[line.split('=',1) for line in raw.splitlines() if line.strip()]
            require(len(pairs)<=1 and all(len(x)==2 and x[0]=='DEVFLOW_ADMIN_PASSWORD' for x in pairs),'secret_keys_rejected')
            value=initialize(root,pairs[0][1] if pairs else None)
        elif args.action=='state':value=database_state(root)
        elif args.action in {'enable','disable','backup'}:value=maintenance(root,args.action,args.id)
        elif args.action in {'view','stop'}:value=asyncio.run(control(root,args.action,args.id,args.request_sha))
        else:value=smoke()
        print(json.dumps(value));return 0
    except Exception:
        print('{"error":"target_runtime_rejected"}');return 1


if __name__=='__main__':raise SystemExit(main())
