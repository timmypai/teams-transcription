"""Real authenticated management preconditions; ASGI, no model or live account."""
import json
from pathlib import Path
import unittest
import test_admin_web as fixture
from test_assistant_daemon import iso
import time


class TargetOwnerActionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.AdminWebTests.asyncSetUp
    login=fixture.AdminWebTests.login
    post=fixture.AdminWebTests.post

    async def test_owner_consent_requires_login_origin_csrf_and_both_fresh_confirmations(self):
        occurrence=self.daemon.store.create_manual_occurrence(dict(title='Synthetic',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(time.time()-1),
            end_at=iso(time.time()+600),recording_authorized=True,speaking_authorized=True))
        values=dict(id=occurrence['id'],revision='1',present='on',media='on')
        self.assertEqual((await self.post('target-consent',values,token='a'*64)).status_code,303)
        await self.login()
        for change in ({'present':''},{'media':''}):
            self.assertEqual((await self.post('target-consent',{**values,**change})).status_code,422)
        self.assertEqual((await self.post('target-consent',values,origin='https://foreign.invalid')).status_code,403)
        self.assertEqual((await self.post('target-consent',values,token='b'*64)).status_code,403)
        response=await self.post('target-consent',values)
        self.assertEqual(response.status_code,200,response.text)
        self.assertIn('request_sha256',response.text)
        self.assertIn('不代表已入會',response.text)
        self.assertIsNone(self.daemon.status()['active_session_id'])
        self.assertEqual(self.daemon.store.list_runs(),[])
        self.assertEqual((await self.post('target-consent',values)).status_code,422)  # stale revision

    async def test_owner_media_policy_explicit_confirmation_persists_and_deny_all_stays_valid(self):
        root=Path(self.settings.data_root);path=root/'config/runtime.yml'
        path.write_text(json.dumps(self.settings.model_dump()));path.chmod(0o600)
        await self.login()
        before=path.read_bytes()
        response=await self.post('media-policy',dict(hosts='teams.microsoft.com'))
        self.assertEqual(response.status_code,422)
        self.assertEqual(path.read_bytes(),before)
        response=await self.post('media-policy',dict(hosts='teams.microsoft.com',confirmed='on'))
        self.assertEqual(response.status_code,303,response.text)
        self.assertEqual(self.settings.allowed_egress_hosts,['teams.microsoft.com'])
        self.assertEqual(json.loads(path.read_text())['allowed_egress_hosts'],['teams.microsoft.com'])
        for bad in ('*','https://example.invalid','bad..host'):
            self.assertEqual((await self.post('media-policy',dict(hosts=bad,confirmed='on'))).status_code,422)
        response=await self.post('media-policy',dict(hosts='',confirmed='on'))
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.settings.allowed_egress_hosts,[])
        self.assertEqual(json.loads(path.read_text())['allowed_egress_hosts'],[])
