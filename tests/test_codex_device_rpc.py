"""Native login transport with a local synthetic child; never an OAuth grant."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from teams_transcription.codex_device_rpc import CodexDeviceRPC, DeviceRPCError


SERVER = '''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
config={}
for index,arg in enumerate(sys.argv[:-1]):
 if arg=='-c':
  key,value=sys.argv[index+1].split('=',1)
  config[key]=json.loads(value)
trace=Path(config['sqlite_home'])/'synthetic-trace'
trace.write_text('SYNTHETIC-DEVICE-SECRET')
def send(value): print(json.dumps(value),flush=True)
for line in sys.stdin:
 message=json.loads(line)
 if 'id' not in message: continue
 method=message.get('method')
 if method=='initialize':
  send({'id':message['id'],'result':{}})
 elif method=='account/login/start':
  send({'id':message['id'],'result':{'type':'chatgptDeviceCode','loginId':'fixture-login',
   'verificationUrl':'https://auth.openai.com/codex/device','userCode':'SYNTHETIC-CODE',
   'fixture':{'args':sys.argv[1:],'home':os.environ.get('CODEX_HOME'),'trace':str(trace)}}})
 else:
  send({'id':message['id'],'result':{'status':'canceled'}})
'''


class CodexDeviceRPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root/'home'
        self.home.mkdir(mode=0o700)
        self.script = self.root/'fake-codex'
        self.script.write_text(SERVER)
        self.script.chmod(0o700)
        self.settings = SimpleNamespace(codex_bin=str(self.script),codex_home=str(self.home),data_root=str(self.root))

    async def test_native_device_login_uses_explicit_home_stdio_file_store_and_reaps_private_trace(self):
        rpc = CodexDeviceRPC(self.settings)
        self.addAsyncCleanup(rpc.close)
        await rpc.start()
        result = await rpc.request('account/login/start',{'type':'chatgptDeviceCode'})
        fixture = result['fixture']
        args = fixture['args']
        self.assertEqual(args[:2],['app-server','--stdio'])
        self.assertNotIn('--listen',args)
        self.assertNotIn('realtime_conversation',args)
        self.assertIn('cli_auth_credentials_store="file"',args)
        self.assertEqual(fixture['home'],str(self.home))
        trace = Path(fixture['trace'])
        self.assertEqual(trace.parent.parent,Path('/dev/shm'))
        self.assertTrue(trace.exists())
        await rpc.close()
        await rpc.close()
        self.assertIsNotNone(rpc.process.returncode)
        self.assertFalse(trace.exists())
        self.assertFalse(any(self.root.rglob('synthetic-trace')))

    async def test_provider_error_and_stderr_never_escape_transport(self):
        self.script.write_text(SERVER.replace("elif method=='account/login/start':", """elif method=='account/login/start':
  print('SYNTHETIC-STDERR-DEVICE-CODE',file=sys.stderr,flush=True)
  send({'id':message['id'],'error':{'message':'SYNTHETIC-ERROR-DEVICE-CODE'}})
 elif method=='fixture-unreachable':"""))
        rpc = CodexDeviceRPC(self.settings)
        self.addAsyncCleanup(rpc.close)
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured),contextlib.redirect_stderr(captured):
            await rpc.start()
            with self.assertRaises(DeviceRPCError) as failed:
                await rpc.request('account/login/start',{'type':'chatgptDeviceCode'})
            await rpc.close()
        self.assertEqual(str(failed.exception),'codex_device_rpc_failed')
        self.assertEqual(rpc.stderr_tail,[])
        self.assertEqual(captured.getvalue(),'')

    async def test_completion_only_forwards_login_id_and_success_and_rejects_server_tools(self):
        self.script.write_text(SERVER.replace("elif method=='account/login/start':", """elif method=='account/login/start':
  send({'id':987,'method':'tool/execute','params':{'command':'SYNTHETIC-NEVER-EXECUTE'}})
  rejected=json.loads(sys.stdin.readline())
  if rejected.get('id')!=987 or rejected.get('error',{}).get('code')!=-32601: sys.exit(1)
  send({'method':'account/updated','params':{'secret':'SYNTHETIC-ACCOUNT-SECRET'}})
  send({'method':'account/login/completed','params':{'loginId':'fixture-login','success':False,
   'error':'SYNTHETIC-COMPLETION-SECRET','extra':'SYNTHETIC-UNKNOWN-SECRET'}})
  send({'id':message['id'],'result':{'type':'chatgptDeviceCode'}})
 elif method=='fixture-unreachable':"""))
        rpc = CodexDeviceRPC(self.settings)
        self.addAsyncCleanup(rpc.close)
        await rpc.start()
        await rpc.request('account/login/start',{'type':'chatgptDeviceCode'})
        notification = await asyncio.wait_for(rpc.notifications.get(),1)
        self.assertEqual(notification,{'method':'account/login/completed',
            'params':{'loginId':'fixture-login','success':False}})
        self.assertTrue(rpc.notifications.empty())
        self.assertEqual(await rpc.request('account/login/cancel',{'loginId':'fixture-login'}),{'status':'canceled'})

    async def test_non_device_methods_and_api_credentials_cannot_start_other_auth_or_model_work(self):
        rpc = CodexDeviceRPC(self.settings)
        self.addAsyncCleanup(rpc.close)
        for method,params in (('thread/start',{}),('account/login/start',{'type':'chatgpt'}),
                ('account/login/start',{'type':'apiKey','apiKey':'SYNTHETIC'}),
                ('account/login/cancel',{'loginId':'fixture','unexpected':'SYNTHETIC'})):
            with self.assertRaisesRegex(DeviceRPCError,'method_rejected'):
                await rpc.request(method,params)
        with self.assertRaisesRegex(DeviceRPCError,'method_rejected'):
            await rpc.notify('thread/start',{})
        with patch.dict(os.environ,{'OPENAI_API_KEY':'SYNTHETIC'},clear=True):
            with self.assertRaisesRegex(DeviceRPCError,'api_key_or_access_token_environment_rejected'):
                await rpc.start()
        self.assertIsNone(rpc.process)
        self.assertIsNone(rpc._provider_runtime)

    async def test_cancelling_partial_initialization_reaps_child_and_tmpfs_trace(self):
        self.script.write_text(SERVER.replace("send({'id':message['id'],'result':{}})","continue"))
        rpc = CodexDeviceRPC(self.settings)
        self.addAsyncCleanup(rpc.close)
        starting = asyncio.create_task(rpc.start())
        async with asyncio.timeout(1):
            while rpc.process is None:
                await asyncio.sleep(.001)
        runtime = Path(rpc._provider_runtime.name)
        starting.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await starting
        self.assertIsNotNone(rpc.process.returncode)
        self.assertFalse(runtime.exists())
        self.assertFalse(rpc.pending)

    async def test_malformed_or_oversized_provider_output_fails_without_exposing_its_body(self):
        for output in ("print('SYNTHETIC-MALFORMED-CODE',flush=True)",
                "print('SYNTHETIC-LARGE-CODE'*8192,flush=True)"):
            with self.subTest(output=output[:16]):
                self.script.write_text(SERVER.replace("send({'id':message['id'],'result':{}})",output))
                rpc = CodexDeviceRPC(self.settings)
                with self.assertRaisesRegex(DeviceRPCError,'^codex_device_rpc_failed$'):
                    await rpc.start()
                self.assertIsNotNone(rpc.process.returncode)
                self.assertIsNone(rpc._provider_runtime)


if __name__=='__main__':
    unittest.main()
