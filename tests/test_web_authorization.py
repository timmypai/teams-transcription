"""Authenticated web authorization, using only synthetic provider replies."""
import asyncio
import json
import os
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch

import httpx

from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials
from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.daemon import AssistantDaemon

ORIGIN = 'http://127.0.0.1:18437'
PASSWORD = 'web-auth-fixture-password'


class FakeDeviceRPC:
    instances = []

    def __init__(self, settings):
        self.settings = settings
        self.notifications = asyncio.Queue()
        self.calls = []
        self.closed = False
        self.instances.append(self)

    async def start(self):
        return self

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == 'account/login/start':
            return dict(type='chatgptDeviceCode', loginId='fixture-login-id',
                verificationUrl='https://auth.openai.com/codex/device', userCode='ABCD-12345')
        return {'status':'canceled'}

    async def close(self):
        self.closed = True


class WebAuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = AssistantSettings(data_root=str(root), port=18437, codex_home=str(root/'codex'),
            admin_password_file=str(root/'config/password'), admin_session_secret_file=str(root/'config/session'),
            google_credentials_file=str(root/'config/google'))
        Path(self.settings.codex_home).mkdir(mode=0o700)
        initialize_admin_credentials(self.settings, PASSWORD)
        FakeDeviceRPC.instances = []
        self.rpc_patch = patch('teams_transcription.web_authorization.CodexDeviceRPC', FakeDeviceRPC)
        self.ready_patch = patch('teams_transcription.web_authorization.preflight',
            return_value={'login_ready':True, 'error_codes':[]})
        self.rpc_patch.start()
        self.ready_patch.start()
        self.addCleanup(self.rpc_patch.stop)
        self.addCleanup(self.ready_patch.stop)
        self.daemon = AssistantDaemon(self.settings)
        self.app = create_admin_app(self.daemon, self.settings)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)

    async def login(self, client=None):
        client = client or self.client
        response = await client.get('/login')
        csrf = re.search(r'name="csrf" value="([a-f0-9]+)"', response.text)[1]
        response = await client.post('/login', data={'csrf':csrf, 'password':PASSWORD}, headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code, 303)
        response = await client.get('/settings')
        return re.search(r'name="csrf" value="([a-f0-9]+)"', response.text)[1]

    async def start_login(self, token):
        response = await self.client.post('/chatgpt/connect', data={'csrf':token, 'confirm_account':'on'},
            headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code, 303, response.text)
        async with asyncio.timeout(2):
            while self.app.state.chatgpt_auth.state == 'starting':
                await asyncio.sleep(.01)
        return FakeDeviceRPC.instances[-1]

    async def wait_finished(self):
        async with asyncio.timeout(2):
            while self.app.state.chatgpt_auth.busy:
                await asyncio.sleep(.01)

    async def test_start_requires_session_same_origin_csrf_and_explicit_consent(self):
        response = await self.client.post('/chatgpt/connect',data={'confirm_account':'on'},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,303)
        self.assertEqual(response.headers['location'],'/login')
        token = await self.login()
        for origin, csrf, confirm, status in (('https://evil.invalid',token,'on',403),
                (ORIGIN,'0'*64,'on',403), (ORIGIN,token,'',422)):
            response = await self.client.post('/chatgpt/connect',data={
                'csrf':csrf,'confirm_account':confirm},headers={'Origin':origin})
            self.assertEqual(response.status_code,status)
        self.assertEqual((await self.client.get('/chatgpt/connect')).status_code,405)
        self.assertEqual(FakeDeviceRPC.instances,[])

    async def test_busy_slot_blocks_duplicate_login_join_and_foreign_cancel(self):
        token = await self.login()
        rpc = await self.start_login(token)
        again = await self.client.post('/chatgpt/connect',data={'csrf':token,'confirm_account':'on'},headers={'Origin':ORIGIN})
        self.assertEqual(again.status_code,422)
        from teams_transcription.assistant_store import AssistantStoreError
        with self.assertRaisesRegex(AssistantStoreError,'codex_auth_operation_in_progress'):
            await self.daemon.join('irrelevant-while-login-reserved')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url=ORIGIN) as other:
            foreign = await self.login(other)
            response = await other.post('/chatgpt/cancel',data={'csrf':foreign},headers={'Origin':ORIGIN})
            self.assertEqual(response.status_code,403)
        self.assertEqual(len(FakeDeviceRPC.instances),1)
        response = await self.client.post('/chatgpt/cancel',data={'csrf':token},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,303)
        self.assertTrue(rpc.closed)
        self.assertIn(('account/login/cancel',{'loginId':'fixture-login-id'}),rpc.calls)
        self.assertNotIn('ABCD-12345',(await self.client.get('/chatgpt')).text)
        self.assertFalse(self.daemon.auth_operation_in_progress)

    async def test_scheduler_cannot_be_enabled_while_authorization_owns_slot(self):
        token = await self.login()
        await self.start_login(token)
        response = await self.client.post('/actions/scheduler',data={'csrf':token,'paused':'false'},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,422)
        self.assertTrue(self.daemon.store.settings()['scheduler_paused'])

    async def test_enabled_scheduler_and_capture_interlock_block_login(self):
        token = await self.login()
        self.daemon.store.update_settings({'scheduler_paused':False})
        response = await self.client.post('/chatgpt/connect',data={'csrf':token,'confirm_account':'on'},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,422)
        self.daemon.store.update_settings({'scheduler_paused':True})
        self.daemon._capture_interlock = True
        response = await self.client.post('/chatgpt/connect',data={'csrf':token,'confirm_account':'on'},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,422)
        self.assertEqual(FakeDeviceRPC.instances,[])

    async def test_existing_auth_is_untouched_without_explicit_reauthorization(self):
        auth = Path(self.settings.codex_home)/'auth.json'
        auth.write_text('SYNTHETIC-EXISTING-AUTH')
        auth.chmod(0o600)
        token = await self.login()
        response = await self.client.post('/chatgpt/connect',data={'csrf':token,'confirm_account':'on'},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,303)
        await self.wait_finished()
        self.assertEqual(FakeDeviceRPC.instances,[])
        self.assertEqual(auth.read_text(),'SYNTHETIC-EXISTING-AUTH')
        self.assertIn('explicit_reauthorization_required',(await self.client.get('/chatgpt')).text)

    async def test_logout_and_expired_session_cancel_native_flow(self):
        token = await self.login()
        rpc = await self.start_login(token)
        response = await self.client.post('/logout',data={'csrf':token},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,303)
        self.assertTrue(rpc.closed)
        token = await self.login()
        rpc = await self.start_login(token)
        sessions = self.app.state.sessions
        now = sessions.clock()
        sessions.clock = lambda: now+self.settings.admin_session_seconds+1
        await self.wait_finished()
        self.assertTrue(rpc.closed)
        self.assertIsNone(self.app.state.chatgpt_auth.code)
        self.assertFalse(self.daemon.auth_operation_in_progress)

    async def test_denial_or_wrong_login_id_never_reports_success_or_provider_error(self):
        token = await self.login()
        rpc = await self.start_login(token)
        await rpc.notifications.put({'method':'account/login/completed','params':{
            'loginId':'another-flow','success':True}})
        response = await self.client.get('/chatgpt')
        self.assertIn('ABCD-12345',response.text)
        await rpc.notifications.put({'method':'account/login/completed','params':{
            'loginId':'fixture-login-id','success':False,'error':'PRIVATE-PROVIDER-ERROR'}})
        await self.wait_finished()
        response = await self.client.get('/chatgpt')
        self.assertNotIn('PRIVATE-PROVIDER-ERROR',response.text)
        self.assertNotIn('ABCD-12345',response.text)
        self.assertIn('codex_device_login_denied_or_failed',response.text)
        self.assertNotIn('授權程序已完成',response.text)

    async def test_missing_google_client_has_actionable_setup_and_no_dead_connect_button(self):
        await self.login()
        response = await self.client.get('/settings')
        self.assertIn('google_client_config_file',response.text)
        self.assertIn(ORIGIN+'/google/callback',response.text)
        self.assertNotIn('action="/google/connect"',response.text)

    async def test_cleanup_holds_login_lock_and_join_reservation_until_native_reaped(self):
        from teams_transcription.codex_auth import exclusive, AuthError
        token = await self.login()
        rpc = await self.start_login(token)
        closing, released = asyncio.Event(), asyncio.Event()
        async def delayed_close():
            closing.set()
            await released.wait()
            rpc.closed = True
        rpc.close = delayed_close
        cancelling = asyncio.create_task(self.client.post('/chatgpt/cancel',data={'csrf':token},headers={'Origin':ORIGIN}))
        await asyncio.wait_for(closing.wait(),1)
        try:
            self.assertTrue(self.daemon.auth_operation_in_progress)
            with self.assertRaisesRegex(AuthError,'locked'):
                with exclusive(Path(self.settings.codex_home)/'.onboarding.lock','locked'):
                    self.fail('login lock released before process reaped')
        finally:
            released.set()
            await cancelling
        self.assertFalse(self.daemon.auth_operation_in_progress)

    async def test_cleanup_failure_blocks_new_login_and_join(self):
        token = await self.login()
        rpc = await self.start_login(token)
        async def failed_close():
            raise RuntimeError('PRIVATE-CLEANUP-ERROR')
        rpc.close = failed_close
        response = await self.client.post('/chatgpt/cancel',data={'csrf':token},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,303)
        self.assertTrue(self.daemon.auth_operation_in_progress)
        response = await self.client.post('/chatgpt/connect',data={'csrf':token,'confirm_account':'on'},headers={'Origin':ORIGIN})
        self.assertEqual(response.status_code,422)
        response = await self.client.get('/chatgpt')
        self.assertIn('codex_device_login_cleanup_unverified',response.text)
        self.assertNotIn('PRIVATE-CLEANUP-ERROR',response.text)

    async def test_logout_waits_for_success_cleanup_without_cancelling_reap(self):
        token = await self.login()
        rpc = await self.start_login(token)
        closing, released = asyncio.Event(), asyncio.Event()
        async def delayed_close():
            closing.set()
            await released.wait()
            rpc.closed = True
        rpc.close = delayed_close
        auth = Path(self.settings.codex_home)/'auth.json'
        auth.write_text('SYNTHETIC')
        auth.chmod(0o600)
        await rpc.notifications.put({'method':'account/login/completed','params':{
            'loginId':'fixture-login-id','success':True}})
        await asyncio.wait_for(closing.wait(),1)
        logging_out = asyncio.create_task(self.client.post('/logout',data={'csrf':token},headers={'Origin':ORIGIN}))
        await asyncio.sleep(0)
        self.assertTrue(self.daemon.auth_operation_in_progress)
        released.set()
        response = await asyncio.wait_for(logging_out,1)
        self.assertEqual(response.status_code,303)
        self.assertTrue(rpc.closed)
        self.assertFalse(self.daemon.auth_operation_in_progress)

    async def test_timeout_clears_short_lived_code_without_retry(self):
        from types import SimpleNamespace
        import time
        token = await self.login()
        rpc = await self.start_login(token)
        with patch('teams_transcription.web_authorization.time',SimpleNamespace(monotonic=lambda:time.monotonic()+1000)):
            await self.wait_finished()
        response = await self.client.get('/chatgpt')
        self.assertIn('codex_device_login_timeout_outcome_unverified',response.text)
        self.assertNotIn('ABCD-12345',response.text)
        self.assertEqual(len(FakeDeviceRPC.instances),1)
        self.assertTrue(rpc.closed)

    async def test_google_denial_bad_state_and_replay_make_no_token_request(self):
        from urllib.parse import parse_qs, urlsplit
        from unittest.mock import AsyncMock
        config = Path(self.tmp.name)/'config/client.json'
        config.write_text(json.dumps({'web':{'client_id':'synthetic','client_secret':'SYNTHETIC-SECRET',
            'redirect_uris':[ORIGIN+'/google/callback']}}))
        config.chmod(0o600)
        self.daemon.calendar.client_config_path = config
        token = await self.login()
        response = await self.client.post('/google/connect',data={'csrf':token},headers={'Origin':ORIGIN})
        state = parse_qs(urlsplit(response.headers['location']).query)['state'][0]
        with patch.object(self.daemon,'google_exchange_code',new_callable=AsyncMock) as exchange:
            response = await self.client.get('/google/callback',params={'state':'非 ASCII','code':'PRIVATE-CODE'})
            self.assertEqual(response.status_code,403)
            response = await self.client.get('/google/callback',params={'state':state,'error':'access_denied'})
            self.assertEqual(response.status_code,303)
            self.assertIn('Google 授權未完成',(await self.client.get('/settings')).text)
            response = await self.client.get('/google/callback',params={'state':state,'code':'PRIVATE-CODE'})
            self.assertEqual(response.status_code,403)
            exchange.assert_not_awaited()

    async def test_session_expiring_after_request_lookup_never_renders_code_or_errors(self):
        token = await self.login()
        await self.start_login(token)
        sessions = self.app.state.sessions
        now = sessions.clock()
        calls = 0
        def clock():
            nonlocal calls
            calls += 1
            return now if calls == 1 else now+self.settings.admin_session_seconds+1
        sessions.clock = clock
        response = await self.client.get('/chatgpt')
        self.assertEqual(response.status_code,200)
        self.assertNotIn('ABCD-12345',response.text)
        self.assertIn('authorization_session_expired',response.text)

    async def test_denial_keeps_original_failure_when_cleanup_also_fails(self):
        token = await self.login()
        rpc = await self.start_login(token)
        async def failed_close():
            raise RuntimeError('PRIVATE-CLEANUP-ERROR')
        rpc.close = failed_close
        await rpc.notifications.put({'method':'account/login/completed','params':{
            'loginId':'fixture-login-id','success':False}})
        await self.wait_finished()
        response = await self.client.get('/chatgpt')
        self.assertIn('codex_device_login_denied_or_failed',response.text)
        self.assertIn('codex_device_login_cleanup_unverified',response.text)

    async def test_device_flow_is_private_to_owner_and_clears_code_on_success(self):
        token = await self.login()
        rpc = await self.start_login(token)
        self.assertEqual(rpc.calls, [('account/login/start', {'type':'chatgptDeviceCode'})])
        response = await self.client.get('/chatgpt')
        self.assertIn('ABCD-12345', response.text)
        self.assertIn('https://auth.openai.com/codex/device', response.text)
        self.assertIn('no-store', response.headers['cache-control'])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=ORIGIN) as other:
            await self.login(other)
            response = await other.get('/chatgpt')
            self.assertNotIn('ABCD-12345', response.text)
            self.assertNotIn('fixture-login-id', response.text)
        for path in ('/settings', '/api/status', '/audit'):
            self.assertNotIn('ABCD-12345', (await self.client.get(path)).text)
        auth = Path(self.settings.codex_home)/'auth.json'
        auth.write_text('SYNTHETIC-NOT-A-GRANT')
        auth.chmod(0o600)
        await rpc.notifications.put({'method':'account/login/completed', 'params':{
            'loginId':'fixture-login-id', 'success':True, 'error':None}})
        async with asyncio.timeout(2):
            while self.app.state.chatgpt_auth.busy:
                await asyncio.sleep(.01)
        response = await self.client.get('/chatgpt')
        self.assertIn('授權程序已完成', response.text)
        self.assertNotIn('ABCD-12345', response.text)
        self.assertTrue(rpc.closed)
        self.assertFalse(self.daemon.auth_operation_in_progress)
        self.assertTrue(self.daemon.store.settings()['scheduler_paused'])
