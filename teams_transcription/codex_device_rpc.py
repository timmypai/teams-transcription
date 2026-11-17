"""Narrow native ChatGPT device-login transport, without loggable provider text."""
import asyncio
import contextlib
import json
import os
from pathlib import Path

from .codex_auth import configured_home, private_path
from .rpc import CodexRPC, _provider_runtime


class DeviceRPCError(RuntimeError):
    def __init__(self, code='codex_device_rpc_failed'):
        self.code = code
        super().__init__(code)


class CodexDeviceRPC(CodexRPC):
    """Only login/cancel are exposed; no model, thread, tool or network listener."""
    def __init__(self, settings):
        home = configured_home(settings)
        super().__init__(home,settings.codex_bin)
        self.home = home
        self.notifications = asyncio.Queue(maxsize=16)

    async def start(self):
        if self.process is not None or self._close_task is not None:
            raise DeviceRPCError('codex_device_rpc_already_started')
        if not private_path(self.home,True):
            raise DeviceRPCError('codex_home_private_uid_gid_required')
        env = dict(os.environ)
        if any(env.get(key) for key in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN')):
            raise DeviceRPCError('api_key_or_access_token_environment_rejected')
        env['CODEX_HOME'] = str(self.home)
        self._provider_runtime = _provider_runtime()
        runtime = Path(self._provider_runtime.name)
        args = [self.executable,'app-server','--stdio',
            '-c','cli_auth_credentials_store="file"',
            '-c','sqlite_home='+json.dumps(str(runtime)),
            '-c','log_dir='+json.dumps(str(runtime/'logs'))]
        try:
            self.process = await asyncio.create_subprocess_exec(*args,env=env,
                stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,limit=65536)
            self.tasks = [asyncio.create_task(self._read())]
            await self.request('initialize',{'clientInfo':{'name':'meeting_account_authorization','version':'0.3.0'}})
            await self.notify('initialized',{})
        except BaseException as error:
            with contextlib.suppress(Exception):
                await self.close()
            if isinstance(error,asyncio.CancelledError):
                raise
            raise DeviceRPCError() from None
        return self

    async def request(self, method, params):
        allowed = (method=='initialize' or method=='account/login/start' and params=={'type':'chatgptDeviceCode'}
            or method=='account/login/cancel' and isinstance(params,dict) and set(params)=={'loginId'}
            and isinstance(params['loginId'],str) and 1<=len(params['loginId'])<=128)
        if not allowed:
            raise DeviceRPCError('codex_device_rpc_method_rejected')
        try:
            return await super().request(method,params)
        except Exception:
            raise DeviceRPCError() from None

    async def notify(self, method, params):
        if method!='initialized' or params!={}:
            raise DeviceRPCError('codex_device_rpc_method_rejected')
        try:
            await super().notify(method,params)
        except Exception:
            raise DeviceRPCError() from None

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                message = json.loads(line)
                if not isinstance(message,dict):
                    raise DeviceRPCError()
                if 'id' in message:
                    if 'method' in message:
                        await self._write({'id':message['id'],'error':{
                            'code':-32601,'message':'Client does not execute tools'}})
                        continue
                    future = self.pending.pop(message['id'],None)
                    if future is not None and not future.done():
                        if 'error' in message:
                            future.set_exception(DeviceRPCError())
                        else:
                            future.set_result(message.get('result'))
                elif message.get('method')=='account/login/completed':
                    params = message.get('params')
                    if not isinstance(params,dict) or type(params.get('success')) is not bool:
                        raise DeviceRPCError()
                    login_id = params.get('loginId')
                    if login_id is not None and (not isinstance(login_id,str) or not 1<=len(login_id)<=128):
                        raise DeviceRPCError()
                    await self.notifications.put({'method':'account/login/completed',
                        'params':{'loginId':login_id,'success':params['success']}})
        except (OSError,ValueError,TypeError,DeviceRPCError):
            pass
        for future in self.pending.values():
            if not future.done():
                future.set_exception(DeviceRPCError())
        self.pending.clear()
        await self.notifications.put({'method':'client/exit','params':{'error':'codex_device_rpc_failed'}})

    async def close(self):
        try:
            await super().close()
        except Exception:
            raise DeviceRPCError('codex_device_rpc_cleanup_failed') from None
