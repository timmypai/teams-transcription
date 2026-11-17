"""Actual local HTML forms; no Calendar, meeting, model or microphone access."""
import asyncio
import socket
import tempfile
import unittest
from pathlib import Path

import uvicorn

from teams_transcription.admin_web import create_admin_app,initialize_admin_credentials
from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.browser import Browser
from teams_transcription.daemon import AssistantDaemon


class AdminBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_login_and_csrf_form_keep_browser_origin_without_exposing_query_referrer(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)
            sock = socket.socket()
            sock.bind(('127.0.0.1',0))
            sock.listen(128)
            port = sock.getsockname()[1]
            settings = AssistantSettings(data_root=root,port=port,admin_password_file=str(path/'config/password.json'),
                admin_session_secret_file=str(path/'config/session.key'),google_credentials_file=str(path/'config/google.json'))
            initialize_admin_credentials(settings,'controlled-browser-password')
            daemon = AssistantDaemon(settings)
            app = create_admin_app(daemon,settings)
            server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,access_log=False,log_level='critical'))
            serving = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if serving.done():
                            await serving
                            self.fail('Local management server did not start')
                        await asyncio.sleep(.01)
                async with Browser() as runtime:
                    assert runtime.browser is not None
                    page = await runtime.browser.new_page()
                    await page.goto(f'http://127.0.0.1:{port}/login?private=controlled-query-marker')
                    await page.locator('input[name=password]').fill('controlled-browser-password')
                    async with page.expect_navigation(),page.expect_request(lambda request: request.method=='POST') as posted:
                        await page.get_by_role('button',name='登入',exact=True).click()
                    request = await posted.value
                    self.assertEqual(request.headers.get('origin'),f'http://127.0.0.1:{port}')
                    self.assertEqual(request.headers.get('referer'),f'http://127.0.0.1:{port}/')
                    self.assertEqual(page.url,f'http://127.0.0.1:{port}/')
                    await page.goto(f'http://127.0.0.1:{port}/settings')
                    async with page.expect_navigation():
                        await page.get_by_role('button',name='啟用排程',exact=True).click()
                    self.assertFalse(daemon.store.settings()['scheduler_paused'])
                    self.assertNotIn('admin_origin_rejected',await page.locator('body').inner_text())
                    await page.close()
            finally:
                server.should_exit = True
                done,_ = await asyncio.wait({serving},timeout=5)
                if not done:
                    serving.cancel()
                    await asyncio.gather(serving,return_exceptions=True)
                sock.close()


if __name__=='__main__':
    unittest.main()
