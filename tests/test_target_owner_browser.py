"""Real sandboxed owner UI interaction, synthetic occurrence; never real Join."""
import asyncio
import json
from pathlib import Path
import secrets
import socket
import tempfile
import time
import unittest
import httpx
import uvicorn
from playwright.async_api import async_playwright, expect
from deploy.target_runtime import initialize
from teams_transcription.admin_web import create_admin_app
from teams_transcription.assistant_config import load_assistant_settings
from teams_transcription.daemon import AssistantDaemon
from test_assistant_daemon import iso


class TargetOwnerBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_edits_explicit_media_policy_and_issues_consent_without_join(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);root.chmod(0o700);password=secrets.token_urlsafe(32)
            initialize(root,password);runtime=root/'config/runtime.yml'
            settings=load_assistant_settings(runtime)
            daemon=AssistantDaemon(settings);app=create_admin_app(daemon,settings,local_teams_control=True)
            target=daemon.store.create_manual_occurrence(dict(title='Synthetic owner browser',
                join_url='https://teams.microsoft.com/meet/12345',start_at=iso(time.time()-1),
                end_at=iso(time.time()+600),recording_authorized=True,speaking_authorized=True))
            sock=socket.socket();sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
            sock.bind(('127.0.0.1',8080));sock.listen(128)
            server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='critical'))
            task=asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():await task;self.fail('disposable server failed')
                        await asyncio.sleep(.01)
                async with async_playwright() as p:
                    browser=await p.chromium.launch(executable_path='/usr/bin/chromium',headless=True,chromium_sandbox=True)
                    try:
                        page=await browser.new_page()
                        await page.goto('http://127.0.0.1:8080/login')
                        await page.get_by_label('管理密碼',exact=True).fill(password)
                        await page.get_by_role('button',name='登入',exact=True).click()
                        await page.wait_for_url('http://127.0.0.1:8080/')
                        await page.goto('http://127.0.0.1:8080/settings')
                        form=page.locator('form[action="/actions/media-policy"]')
                        await form.locator('textarea[name=hosts]').fill('teams.microsoft.com')
                        await form.locator('input[name=confirmed]').check()
                        await form.get_by_role('button',name='保存媒體政策',exact=True).click()
                        await expect(page.locator('textarea[name=hosts]')).to_have_value('teams.microsoft.com')
                        self.assertEqual(json.loads(runtime.read_text())['allowed_egress_hosts'],['teams.microsoft.com'])
                        await page.goto('http://127.0.0.1:8080/schedule')
                        form=page.locator('form[action="/actions/target-consent"]')
                        await form.locator('input[name=present]').check()
                        await form.locator('input[name=media]').check()
                        await form.get_by_role('button').click()
                        await expect(page.get_by_role('heading',name='Hermes 單次控制 binding',exact=True)).to_be_visible()
                        binding=json.loads(await page.locator('pre').inner_text())
                        self.assertEqual(binding['occurrence_id'],target['id'])
                        self.assertEqual(binding['revision'],2)
                        self.assertEqual(len(binding['request_sha256']),64)
                        await page.close()
                    finally:await browser.close()
                self.assertEqual(daemon.store.list_runs(),[])
                self.assertIsNone(daemon.status()['active_session_id'])
                self.assertTrue(daemon.store.settings()['scheduler_paused'])
                async with httpx.AsyncClient() as client:
                    self.assertEqual((await client.get('http://127.0.0.1:8080/healthz')).status_code,200)
            finally:
                server.should_exit=True
                try:await asyncio.wait_for(task,5)
                finally:sock.close()
