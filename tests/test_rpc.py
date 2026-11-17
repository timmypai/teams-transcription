import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from teams_transcription.rpc import CodexRPC, safe_error

SERVER = '''#!/usr/bin/env python3
import sys,json
from pathlib import Path
config={}
for index,arg in enumerate(sys.argv[:-1]):
 if arg=='-c':
  key,value=sys.argv[index+1].split('=',1)
  config[key]=json.loads(value)
payload_file=Path(config['sqlite_home'])/'logs_2.sqlite'
payload_file.parent.mkdir(parents=True,exist_ok=True)
payload_file.write_bytes(b'SYNTHETIC_AUDIO_TRACE')
def send(x): print(json.dumps(x),flush=True)
for line in sys.stdin:
 m=json.loads(line)
 if 'method' not in m: continue
 method=m['method']
 if 'id' not in m: continue
 if method=='fail':
  send({'id':m['id'],'error':{'message':'test request failed'}})
 elif method=='exit':
  sys.exit(0)
 elif method=='slow':
  continue
 elif method=='notification':
  send({'method':'thread/realtime/transcript/done','params':{'role':'user','text':'real fixture','threadId':'fixture'}})
  send({'id':m['id'],'result':{}})
 elif method=='serverTool':
  send({'method':'tool/execute','id':999,'params':{'command':'must not execute'}})
  send({'id':m['id'],'result':{}})
 elif method=='fixturePayloadLocation':
  send({'id':m['id'],'result':{'path':str(payload_file)}})
 else:
  send({'id':m['id'],'result':{'thread':{'id':'fixture'}}})
'''


class RPCTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        script = Path(self.tmp.name) / "fake-codex"
        script.write_text(SERVER)
        script.chmod(0o700)
        self.rpc = CodexRPC(Path(self.tmp.name) / "state", str(script), timeout=.3)
        await self.rpc.start()

    async def asyncTearDown(self):
        await self.rpc.close()
        self.tmp.cleanup()

    async def test_concurrent_responses_and_notifications(self):
        values = await asyncio.gather(self.rpc.request("first", {}), self.rpc.request("second", {}))
        self.assertEqual([x['thread']['id'] for x in values], ['fixture','fixture'])
        await self.rpc.request("notification", {})
        msg = await asyncio.wait_for(self.rpc.notifications.get(), 1)
        self.assertEqual(msg['params']['text'], 'real fixture')

    async def test_errors_timeout_and_exit_do_not_hang(self):
        with self.assertRaisesRegex(RuntimeError, 'test request failed'):
            await self.rpc.request('fail', {})
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            await self.rpc.request('slow', {})
        self.assertEqual(self.rpc.pending, {})
        with self.assertRaisesRegex(RuntimeError, 'exited'):
            await self.rpc.request('exit', {})

    async def test_server_tool_request_is_rejected(self):
        await self.rpc.request('serverTool', {})
        # Connection stays usable after replying with method-not-found.
        self.assertEqual(await self.rpc.thread(), 'fixture')

    async def test_provider_trace_payload_is_removed_after_close(self):
        value = await self.rpc.request('fixturePayloadLocation', {})
        payload_file = Path(value['path'])
        self.assertEqual(payload_file.read_bytes(), b'SYNTHETIC_AUDIO_TRACE')
        await self.rpc.close()
        self.assertIsNotNone(self.rpc.process.returncode)
        self.assertFalse(payload_file.exists())
        self.assertFalse(any(Path(self.tmp.name).rglob('logs_2.sqlite')))


class RedactionTests(unittest.TestCase):
    def test_key_and_link_query_not_logged(self):
        value = safe_error('Bearer secret sk-example https://teams.microsoft.com/meet/123?pwd=secret')
        self.assertNotIn('secret', value)
        self.assertNotIn('sk-example', value)
        self.assertNotIn('pwd=', value)
        self.assertNotIn('/meet/123', value)

    def test_encoded_meeting_identifiers_are_redacted(self):
        value = safe_error('Failed https://teams.microsoft.com/l/meetup-join/19%3ameeting_private/0?context=private and https://example.com/api?key=private')
        self.assertNotIn('meeting_private', value)
        self.assertNotIn('context=', value)
        self.assertIn('teams.microsoft.com/[meeting-link-redacted]', value)
        self.assertIn('https://example.com/api', value)
        self.assertEqual(safe_error('Failed https://[invalid-host'), 'Failed [url-redacted]')
