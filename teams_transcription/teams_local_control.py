"""Opt-in same-UID Unix control for the installed, authority-checked host helper.

No HTTP routes, password/session tokens, arbitrary target URLs, transcript or arbitrary
commands. OS UID is the local principal; this is not isolation from that UID.
The root-owned host helper remains the authority/ledger/lock admission boundary.
"""
import asyncio
import contextlib
from contextlib import asynccontextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import struct

from .teams_acceptance import VERSION, meeting_view, validate_policy

MAX_REQUEST = 4096
MAX_RESPONSE = 65536


class LocalControlError(ValueError):
    def __init__(self, code='teams_local_control_rejected'):
        self.code = code
        super().__init__(code)


def socket_path(root):
    return Path(root)/'.teams-control'/'control.sock'


def private_directory(path):
    if path.absolute()!=path.resolve():
        raise LocalControlError('teams_local_control_path_rejected')
    info=path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise LocalControlError('teams_local_control_path_rejected')


def peer_uid(connection):
    return struct.unpack('3i',connection.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))[1]


def decode_message(raw):
    def unique(pairs):
        result={}
        for key,value in pairs:
            if key in result:raise LocalControlError()
            result[key]=value
        return result
    return json.loads(raw,object_pairs_hook=unique)


def identifier(value, size):
    if not isinstance(value,str) or not re.fullmatch('[0-9a-f]{'+str(size)+'}',value):
        raise LocalControlError()
    return value


async def dispatch(daemon, settings, message):
    if not isinstance(message,dict) or set(message)!={'operation','fields'} or not isinstance(message['fields'],dict):
        raise LocalControlError()
    operation,fields=message['operation'],message['fields']
    if operation=='prepare':
        async with daemon._join_lock:
            if daemon.closing.is_set() or daemon.status().get('active_session_id') is not None:
                raise LocalControlError()
            source=await asyncio.to_thread(daemon.store.get_occurrence,identifier(fields.get('source_id'),32))
            from urllib.parse import urlsplit
            if urlsplit(source['join_url']).hostname not in settings.allowed_egress_hosts:
                raise LocalControlError()
            return await asyncio.to_thread(daemon.store.prepare_owned_occurrence,fields)
    target=identifier(fields.get('id'),32)
    if operation=='view' and set(fields)=={'id'}:
        view=await asyncio.to_thread(meeting_view,daemon.store,target)
        allowed=sorted(set(settings.allowed_egress_hosts))
        view['egress_policy_sha256']=hashlib.sha256(json.dumps(allowed,separators=(',',':')).encode()).hexdigest()
        view['egress_allows_target_host']=view['join_host'] in allowed
        status=daemon.status()
        view['response_ready']=bool(view.get('session_id') and view['session_id']==status.get('active_session_id')
            and (status.get('response') or {}).get('state') in {'sleeping','listening','thinking','answering'})
        return view
    request_sha=identifier(fields.get('acceptance_request'),64)
    if operation=='join' and set(fields)=={'id','acceptance_request','acceptance_revision','acceptance_stop_policy'}:
        if fields['acceptance_stop_policy']!='owner_or_meeting_end':
            raise LocalControlError()
        policy=validate_policy(dict(version=VERSION,request_sha256=request_sha,
            occurrence_revision=fields['acceptance_revision'],maximum_seconds=None))
        # The host rechecks its root authority and reserves the canonical ledger
        # before this call. Retain runtime policy, egress, revision and lease gates.
        view=await dispatch(daemon,settings,dict(operation='view',fields=dict(id=target)))
        if (view['scheduler_paused'] is not True or view['egress_allows_target_host'] is not True
                or view['recording_authorized'] is not True or view['speaking_authorized'] is not True):
            raise LocalControlError()
        result=await daemon.join(target,actor='local-teams-control',acceptance=policy)
        return dict(ok=True,result=result)
    expected={'id','acceptance_request'} | ({'enabled'} if operation=='output' else set())
    if operation not in {'output','stop'} or set(fields)!=expected:
        raise LocalControlError()
    if operation=='output' and fields['enabled']!='false':
        raise LocalControlError()  # no grant-speaking operation
    run=await asyncio.to_thread(daemon.store.get_run,target)
    policy=run.get('acceptance_policy')
    if not policy or policy['request_sha256']!=request_sha or policy['maximum_seconds'] is not None:
        raise LocalControlError('teams_local_control_binding_rejected')
    if operation=='output':
        await daemon.set_output(target,False,actor='local-teams-control')
    else:
        await daemon.stop(target,actor='local-teams-control')
    return dict(ok=True)


class LocalTeamsControl:
    def __init__(self, daemon, settings):
        self.daemon,self.settings=daemon,settings
        self.path=socket_path(settings.data_root)
        self.server=None
        self.identity=None
        self.lock=None
        self.tasks=set()

    async def __aenter__(self):
        self.path.parent.mkdir(mode=0o700,exist_ok=True)
        private_directory(self.path.parent)
        fd=os.open(self.path.parent/'owner.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        self.lock=os.fdopen(fd,'rb')
        try:
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_nlink!=1:
                raise LocalControlError('teams_local_control_path_rejected')
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if self.path.exists() or self.path.is_symlink():
                info=self.path.lstat()
                if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
                    raise LocalControlError('teams_local_control_path_rejected')
                self.path.unlink()  # stale only; the lifetime lock excludes a live server
            self.server=await asyncio.start_unix_server(self.handle,path=self.path,limit=MAX_REQUEST+1,
                backlog=8,start_serving=False)
            self.identity=self.path.lstat().st_ino
            os.chmod(self.path,0o600)
            await self.server.start_serving()
            return self
        except BaseException:
            try:await self.close_listener()
            finally:
                self.lock.close();self.lock=None
            raise

    async def close_listener(self):
        if self.server:
            self.server.close();await self.server.wait_closed()
            tasks=list(self.tasks)
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            with contextlib.suppress(FileNotFoundError):
                if self.identity is not None and self.path.lstat().st_ino==self.identity:
                    self.path.unlink()

    async def __aexit__(self,*error):
        try:await self.close_listener()
        finally:
            if self.lock:self.lock.close()

    async def handle(self,reader,writer):
        task=asyncio.current_task()
        self.tasks.add(task)
        try:
            try:
                if len(self.tasks)>8 or peer_uid(writer.get_extra_info('socket'))!=os.getuid():
                    raise LocalControlError()
                async with asyncio.timeout(10):
                    raw=await reader.readline()
                if not raw.endswith(b'\n') or len(raw)>MAX_REQUEST:
                    raise LocalControlError()
                # Transport expiry/disconnect must not cancel a committed claim
                # between SQLite's worker thread and daemon ownership handoff.
                value=await dispatch(self.daemon,self.settings,decode_message(raw))
            except Exception:
                # Never serialize exception bodies, configuration, tokens or URLs.
                value=dict(error='teams_local_control_rejected')
            raw=json.dumps(value,allow_nan=False).encode()+b'\n'
            if len(raw)>MAX_RESPONSE:raw=b'{"error":"teams_local_control_rejected"}\n'
            with contextlib.suppress(ConnectionError,TimeoutError):
                writer.write(raw)
                await asyncio.wait_for(writer.drain(),1)
        finally:
            writer.close()
            try:
                with contextlib.suppress(ConnectionError):
                    await asyncio.wait_for(writer.wait_closed(),1)
            finally:
                self.tasks.discard(task)


@asynccontextmanager
async def control_lifespan(daemon,settings,enabled=False):
    if enabled:
        async with LocalTeamsControl(daemon,settings):yield
    else:
        yield


async def request(root,message):
    path=socket_path(root)
    private_directory(path.parent)
    info=path.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
        raise LocalControlError('teams_local_control_path_rejected')
    raw=json.dumps(message,allow_nan=False).encode()+b'\n'
    if len(raw)>MAX_REQUEST:raise LocalControlError()
    async with asyncio.timeout(15):
        reader,writer=await asyncio.open_unix_connection(path,limit=MAX_RESPONSE+1)
        try:
            if peer_uid(writer.get_extra_info('socket'))!=os.getuid():raise LocalControlError()
            writer.write(raw);await writer.drain()
            result=await reader.readline()
            if not result.endswith(b'\n') or len(result)>MAX_RESPONSE:raise LocalControlError()
            value=json.loads(result)
            if not isinstance(value,dict) or 'error' in value:raise LocalControlError()
            return value
        finally:
            writer.close();await writer.wait_closed()
