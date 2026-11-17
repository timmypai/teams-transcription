"""Actual admin forms/callbacks with synthetic providers; no external grants.

The Google fixture checks the real local POST's native 303, then replaces that
response with same-origin synthetic consent HTML. It never navigates to Google.
"""
import asyncio
from html import escape
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import uvicorn

from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials
from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.browser import Browser
from teams_transcription.calendar_client import GoogleCalendarClient, READONLY_SCOPE, TOKEN_URL
from teams_transcription.daemon import AssistantDaemon
from test_web_authorization import FakeDeviceRPC, PASSWORD

SCREENSHOT_DIR = None  # Optional synthetic artifacts supplied by the offline launcher.


class WebAuthorizationBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_google_native_web_flow_saves_private_tokens_rejects_replay_and_handles_denial(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            sock = socket.socket()
            sock.bind(('127.0.0.1',0))
            sock.listen(128)
            port = sock.getsockname()[1]
            origin = f'http://127.0.0.1:{port}'
            settings = AssistantSettings(data_root=root,port=port,
                admin_password_file=str(path/'config/password'),
                admin_session_secret_file=str(path/'config/session'),
                google_credentials_file=str(path/'config/google'),
                google_client_config_file=str(path/'config/google-client'))
            initialize_admin_credentials(settings,PASSWORD)
            client_secret = 'SYNTHETIC-GOOGLE-CLIENT-SECRET'
            access_token = 'SYNTHETIC-GOOGLE-ACCESS-TOKEN'
            refresh_token = 'SYNTHETIC-GOOGLE-REFRESH-TOKEN'
            authorization_code = 'SYNTHETIC-GOOGLE-AUTHORIZATION-CODE'
            client_file = Path(settings.google_client_config_file)
            client_file.write_text(json.dumps({'web':{'client_id':'synthetic-browser-client',
                'client_secret':client_secret,'redirect_uris':[origin+'/google/callback']}}))
            client_file.chmod(0o600)
            token_requests = []

            def token_reply(request):
                self.assertEqual(str(request.url),TOKEN_URL)
                self.assertEqual(request.method,'POST')
                token_requests.append(request)
                return httpx.Response(200,json={'access_token':access_token,'refresh_token':refresh_token,
                    'expires_in':3600,'scope':READONLY_SCOPE})

            calendar = GoogleCalendarClient(settings.google_credentials_file,
                client_config_path=settings.google_client_config_file,transport=httpx.MockTransport(token_reply))
            daemon = AssistantDaemon(settings,calendar=calendar)
            app = create_admin_app(daemon,settings)
            server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,
                access_log=False,log_level='critical'))
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if serving.done():
                            await serving
                            self.fail('Local management server did not start')
                        await asyncio.sleep(.01)
                async with Browser() as runtime:
                    context = await runtime.browser.new_context()
                    authorizations, blocked_requests, page_errors = [], [], []

                    async def offline_google(route):
                        url = urlsplit(route.request.url)
                        if (url.netloc==urlsplit(origin).netloc and url.path=='/google/connect'
                                and route.request.method=='POST'):
                            # Exercise the real local handler, including browser cookies,
                            # Origin and CSRF, without following its external redirect.
                            native = await route.fetch(max_redirects=0)
                            self.assertEqual(native.status,303)
                            authorization = urlsplit(native.headers['location'])
                            self.assertEqual((authorization.scheme,authorization.netloc,authorization.path),
                                ('https','accounts.google.com','/o/oauth2/v2/auth'))
                            query = parse_qs(authorization.query)
                            await native.dispose()
                            authorizations.append(query)
                            callback = origin+'/google/callback?'
                            state = query['state'][0]
                            allowed = callback+urlencode({'state':state,'code':authorization_code})
                            denied = callback+urlencode({'state':state,'error':'access_denied'})
                            await route.fulfill(status=200,content_type='text/html',body=(
                                '<!doctype html><html lang="zh-Hant"><meta charset="utf-8"><title>合成 Google 同意頁</title><body>'
                                '<p>離線合成頁面，不會連線至 Google。</p><a href="'+escape(allowed,quote=True)+
                                '">同意測試授權</a><a href="'+escape(denied,quote=True)+'">拒絕測試授權</a></body></html>'))
                        elif url.netloc == urlsplit(origin).netloc:
                            await route.continue_()
                        else:
                            blocked_requests.append(route.request.url)
                            await route.abort()

                    await context.route('**/*',offline_google)
                    page = await context.new_page()
                    page.on('pageerror',lambda error:page_errors.append(str(error)))
                    await page.goto(origin+'/login')
                    await page.locator('input[name=password]').fill(PASSWORD)
                    async with page.expect_navigation():
                        await page.get_by_role('button',name='登入',exact=True).click()
                    await page.goto(origin+'/settings')
                    async with page.expect_navigation(),page.expect_request(lambda request:
                            request.method=='POST' and request.url==origin+'/google/connect') as posted:
                        await page.get_by_role('button',name='連接 Google Calendar',exact=True).click()
                    post = await posted.value
                    self.assertEqual(post.headers.get('origin'),origin)
                    self.assertRegex(parse_qs(post.post_data)['csrf'][0],r'^[a-f0-9]{64}$')
                    self.assertEqual(len(authorizations),1)
                    query = authorizations[0]
                    self.assertRegex(query['state'][0],r'^[A-Za-z0-9_-]{32,256}$')
                    self.assertEqual(query['scope'],[READONLY_SCOPE])
                    self.assertEqual(query['access_type'],['offline'])
                    self.assertEqual(query['response_type'],['code'])
                    self.assertEqual(query['redirect_uri'],[origin+'/google/callback'])
                    self.assertNotIn(client_secret,page.url)
                    async with page.expect_navigation():
                        await page.get_by_role('link',name='同意測試授權',exact=True).click()
                    self.assertEqual(page.url,origin+'/settings')
                    self.assertIn('Google 授權已保存',await page.locator('body').inner_text())
                    self.assertTrue(calendar.status()['authorized'])
                    self.assertEqual(len(token_requests),1)
                    exchanged = parse_qs(token_requests[0].content.decode())
                    self.assertEqual(exchanged['code'],[authorization_code])
                    self.assertEqual(exchanged['redirect_uri'],[origin+'/google/callback'])
                    for route in ('/settings','/api/status','/audit'):
                        await page.goto(origin+route)
                        content = await page.content()
                        for secret in (client_secret,access_token,refresh_token,authorization_code):
                            self.assertNotIn(secret,content)
                    self.assertIn('google_authorized',await page.locator('body').inner_text())
                    replay = await page.goto(origin+'/google/callback?'+urlencode({
                        'state':query['state'][0],'code':authorization_code}))
                    self.assertEqual(replay.status,403)
                    self.assertIn('google_oauth_state_rejected',await page.content())
                    self.assertEqual(len(token_requests),1)
                    await page.goto(origin+'/settings')
                    async with page.expect_navigation():
                        await page.get_by_role('button',name='重新授權 Google Calendar',exact=True).click()
                    self.assertEqual(len(authorizations),2)
                    self.assertNotEqual(authorizations[1]['state'],query['state'])
                    async with page.expect_navigation():
                        await page.get_by_role('link',name='拒絕測試授權',exact=True).click()
                    self.assertEqual(page.url,origin+'/settings')
                    self.assertIn('Google 授權未完成，既有連線未變更',await page.locator('body').inner_text())
                    self.assertTrue(calendar.status()['authorized'])
                    self.assertEqual(len(token_requests),1)
                    self.assertTrue(daemon.store.settings()['scheduler_paused'])
                    self.assertEqual(blocked_requests,[])
                    self.assertEqual(page_errors,[])
                    await context.close()
            finally:
                server.should_exit = True
                done,_ = await asyncio.wait({serving},timeout=5)
                if not done:
                    serving.cancel()
                    await asyncio.gather(serving,return_exceptions=True)
                sock.close()

    async def test_private_device_form_survives_tab_close_and_native_cancel_clears_code(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            sock = socket.socket()
            sock.bind(('127.0.0.1',0))
            sock.listen(128)
            port = sock.getsockname()[1]
            origin = f'http://127.0.0.1:{port}'
            settings = AssistantSettings(data_root=root,port=port,codex_home=str(path/'codex'),
                admin_password_file=str(path/'config/password'),
                admin_session_secret_file=str(path/'config/session'),
                google_credentials_file=str(path/'config/google'))
            Path(settings.codex_home).mkdir(mode=0o700)
            initialize_admin_credentials(settings,PASSWORD)
            daemon = AssistantDaemon(settings)
            app = create_admin_app(daemon,settings)
            server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,
                access_log=False,log_level='critical'))
            FakeDeviceRPC.instances = []
            with patch('teams_transcription.web_authorization.CodexDeviceRPC',FakeDeviceRPC), \
                    patch('teams_transcription.web_authorization.preflight',
                        return_value={'login_ready':True,'error_codes':[]}):
                serving = asyncio.create_task(server.serve(sockets=[sock]))
                try:
                    async with asyncio.timeout(5):
                        while not server.started:
                            if serving.done():
                                await serving
                                self.fail('Local management server did not start')
                            await asyncio.sleep(.01)
                    async with Browser() as runtime:
                        self.assertIsNotNone(runtime.browser)
                        context = await runtime.browser.new_context()
                        external_requests, page_errors = [], []

                        async def local_only(route):
                            if urlsplit(route.request.url).netloc != urlsplit(origin).netloc:
                                external_requests.append(route.request.url)
                                await route.abort()
                            else:
                                await route.continue_()

                        await context.route('**/*',local_only)
                        page = await context.new_page()
                        page.on('pageerror',lambda error:page_errors.append(str(error)))
                        await page.goto(origin+'/chatgpt')
                        self.assertEqual(page.url,origin+'/login')
                        if SCREENSHOT_DIR:
                            await page.screenshot(path=str(SCREENSHOT_DIR/'01-login.png'),full_page=True)
                        await page.locator('input[name=password]').fill(PASSWORD)
                        async with page.expect_navigation():
                            await page.get_by_role('button',name='登入',exact=True).click()
                        await page.goto(origin+'/settings')
                        self.assertIn('google_client_config_file',await page.locator('body').inner_text())
                        self.assertIn(origin+'/google/callback',await page.locator('body').inner_text())
                        self.assertEqual(await page.locator('form[action="/google/connect"]').count(),0)
                        if SCREENSHOT_DIR:
                            await page.screenshot(path=str(SCREENSHOT_DIR/'02-settings.png'),full_page=True)
                        async with page.expect_navigation():
                            await page.get_by_role('link',name='登入或重新授權 ChatGPT',exact=True).click()
                        consent = page.locator('input[name=confirm_account]')
                        self.assertFalse(await consent.is_checked())
                        self.assertFalse(await page.locator('input[name=reauthorize]').is_checked())
                        self.assertEqual(await page.locator('input[type=password]').count(),0)
                        async with page.expect_response(lambda response:
                                response.request.method=='POST' and response.url==origin+'/chatgpt/connect') as rejected:
                            async with page.expect_navigation():
                                await page.get_by_role('button',name='開始 ChatGPT 授權',exact=True).click()
                        self.assertEqual((await rejected.value).status,422)
                        self.assertIn('請本人先勾選同意',await page.locator('body').inner_text())
                        await page.get_by_text('技術詳情',exact=True).click()
                        self.assertIn('explicit_account_authorization_required',await page.locator('body').inner_text())
                        self.assertEqual(FakeDeviceRPC.instances,[])
                        await page.goto(origin+'/chatgpt')
                        rejected = await page.evaluate('''async () => {
                            const response = await fetch('/chatgpt/connect', {
                                method: 'POST', body: new URLSearchParams({csrf: '0'.repeat(64), confirm_account: 'on'})
                            });
                            return response.status;
                        }''')
                        self.assertEqual(rejected,403)
                        self.assertEqual(FakeDeviceRPC.instances,[])
                        consent = page.locator('input[name=confirm_account]')
                        await consent.check()
                        async with page.expect_navigation():
                            await page.get_by_role('button',name='開始 ChatGPT 授權',exact=True).click()
                        async with asyncio.timeout(2):
                            while app.state.chatgpt_auth.state=='starting':
                                await asyncio.sleep(.01)
                        await page.reload()
                        self.assertIn('ABCD-12345',await page.locator('body').inner_text())
                        official = page.get_by_role('link',name='開啟 OpenAI 授權網站',exact=True)
                        self.assertEqual(await official.get_attribute('href'),'https://auth.openai.com/codex/device')
                        self.assertEqual(await official.get_attribute('target'),'_blank')
                        self.assertEqual(set((await official.get_attribute('rel')).split()),{'noreferrer','noopener'})
                        self.assertEqual(len(FakeDeviceRPC.instances),1)
                        rpc = FakeDeviceRPC.instances[0]
                        foreign_context = await runtime.browser.new_context()
                        await foreign_context.route('**/*',local_only)
                        foreign = await foreign_context.new_page()
                        foreign.on('pageerror',lambda error:page_errors.append(str(error)))
                        await foreign.goto(origin+'/login')
                        await foreign.locator('input[name=password]').fill(PASSWORD)
                        async with foreign.expect_navigation():
                            await foreign.get_by_role('button',name='登入',exact=True).click()
                        await foreign.goto(origin+'/chatgpt')
                        self.assertNotIn('ABCD-12345',await foreign.locator('body').inner_text())
                        self.assertEqual(await foreign.locator('form[action="/chatgpt/cancel"]').count(),0)
                        rejected = await foreign.evaluate('''async () => {
                            const csrf = document.querySelector('input[name=csrf]').value;
                            const response = await fetch('/chatgpt/cancel', {
                                method: 'POST', body: new URLSearchParams({csrf})
                            });
                            return response.status;
                        }''')
                        self.assertEqual(rejected,403)
                        self.assertTrue(app.state.chatgpt_auth.busy)
                        self.assertFalse(rpc.closed)
                        await foreign_context.close()
                        await page.close()
                        self.assertTrue(app.state.chatgpt_auth.busy)
                        self.assertFalse(rpc.closed)
                        self.assertTrue(daemon.auth_operation_in_progress)
                        page = await context.new_page()
                        await page.set_viewport_size({'width':390,'height':844})
                        page.on('pageerror',lambda error:page_errors.append(str(error)))
                        response = await page.goto(origin+'/chatgpt')
                        self.assertIn('no-store',response.headers['cache-control'])
                        self.assertIn('ABCD-12345',await page.locator('body').inner_text())
                        self.assertEqual(len(FakeDeviceRPC.instances),1)
                        self.assertTrue(await page.get_by_role('button',name='取消此次授權',exact=True).is_visible())
                        async with page.expect_navigation():
                            await page.get_by_role('button',name='取消此次授權',exact=True).click()
                        self.assertNotIn('ABCD-12345',await page.locator('body').inner_text())
                        self.assertTrue(rpc.closed)
                        self.assertIn(('account/login/cancel',{'loginId':'fixture-login-id'}),rpc.calls)
                        self.assertFalse(app.state.chatgpt_auth.busy)
                        self.assertFalse(daemon.auth_operation_in_progress)
                        self.assertTrue(await page.evaluate('document.documentElement.scrollWidth <= window.innerWidth'))
                        if SCREENSHOT_DIR:
                            await page.screenshot(path=str(SCREENSHOT_DIR/'03-mobile-cancelled.png'),full_page=True)
                        # A native denial is rendered as a fixed code, never raw provider text.
                        await page.locator('input[name=confirm_account]').check()
                        async with page.expect_navigation():
                            await page.get_by_role('button',name='開始 ChatGPT 授權',exact=True).click()
                        async with asyncio.timeout(2):
                            while app.state.chatgpt_auth.state=='starting':
                                await asyncio.sleep(.01)
                        denied_rpc = FakeDeviceRPC.instances[-1]
                        await denied_rpc.notifications.put({'method':'account/login/completed','params':{
                            'loginId':'fixture-login-id','success':False,'error':'SYNTHETIC-PROVIDER-PRIVATE-ERROR'}})
                        async with asyncio.timeout(2):
                            while app.state.chatgpt_auth.busy:
                                await asyncio.sleep(.01)
                        await page.reload()
                        await page.get_by_text('技術詳情',exact=True).click()
                        body = await page.locator('body').inner_text()
                        self.assertIn('codex_device_login_denied_or_failed',body)
                        self.assertNotIn('SYNTHETIC-PROVIDER-PRIVATE-ERROR',body)
                        self.assertNotIn('ABCD-12345',body)
                        self.assertTrue(denied_rpc.closed)
                        # Logging out cancels the next pending login and invalidates its owner session.
                        await page.locator('input[name=confirm_account]').check()
                        async with page.expect_navigation():
                            await page.get_by_role('button',name='開始 ChatGPT 授權',exact=True).click()
                        async with asyncio.timeout(2):
                            while app.state.chatgpt_auth.state=='starting':
                                await asyncio.sleep(.01)
                        logout_rpc = FakeDeviceRPC.instances[-1]
                        await page.reload()
                        self.assertIn('ABCD-12345',await page.locator('body').inner_text())
                        async with page.expect_navigation():
                            await page.get_by_role('button',name='登出',exact=True).click()
                        self.assertEqual(page.url,origin+'/login')
                        self.assertTrue(logout_rpc.closed)
                        self.assertIn(('account/login/cancel',{'loginId':'fixture-login-id'}),logout_rpc.calls)
                        await page.goto(origin+'/chatgpt')
                        self.assertEqual(page.url,origin+'/login')
                        self.assertNotIn('ABCD-12345',await page.locator('body').inner_text())
                        self.assertFalse(app.state.chatgpt_auth.busy)
                        self.assertFalse(daemon.auth_operation_in_progress)
                        self.assertTrue(daemon.store.settings()['scheduler_paused'])
                        self.assertEqual(external_requests,[])
                        self.assertEqual(page_errors,[])
                        await context.close()
                finally:
                    server.should_exit = True
                    done,_ = await asyncio.wait({serving},timeout=5)
                    if not done:
                        serving.cancel()
                        await asyncio.gather(serving,return_exceptions=True)
                    sock.close()


if __name__=='__main__':
    unittest.main()
