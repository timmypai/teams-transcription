"""Real disposable daemon/browser, never Oracle or an authorized occurrence."""
import asyncio
import json
import os
from pathlib import Path
import secrets
import socket
import tempfile
import unittest
import uvicorn
from playwright.async_api import TimeoutError as BrowserTimeout
from teams_transcription.admin_web import create_admin_app
from teams_transcription.assistant_config import load_assistant_settings
from teams_transcription.daemon import AssistantDaemon
from deploy.target_runtime import initialize,browser_readiness,claim_snapshot

class BrowserReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_anonymous_smoke_controls_and_session_instance_binding(self):
        # Explicit disposable loopback DI only: exercises browser selectors and
        # installed-daemon binding, NOT configured HTTPS/operator reachability.
        from unittest.mock import patch
        from deploy.target_runtime import anonymous_login
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);root.chmod(0o700)
            initialize(root,secrets.token_urlsafe(32))
            runtime=root/'config/runtime.yml';value=json.loads(runtime.read_text())
            value['cookie_secure']=True;runtime.write_text(json.dumps(value))
            settings=load_assistant_settings(runtime)
            daemon=AssistantDaemon(settings);app=create_admin_app(daemon,settings,local_teams_control=True)
            sock=socket.socket();sock.bind(('127.0.0.1',8080));sock.listen(128)
            server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='critical'))
            task=asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():await task;self.fail('disposable server failed')
                        await asyncio.sleep(.01)
                with patch('deploy.target_runtime.configured_entrypoint',return_value='http://127.0.0.1:8080'):
                    result=await anonymous_login(root)
                self.assertTrue(all(result[k] for k in ('anonymous_context','login_heading','password_field',
                    'login_button','anonymous_api_rejected','same_deployment_session','no_authenticated_data')))
                self.assertIsNone(daemon.status()['active_session_id'])
                self.assertTrue(daemon.store.settings()['scheduler_paused'])
            finally:
                server.should_exit=True
                try:await asyncio.wait_for(task,5)
                finally:sock.close()

    async def test_actual_login_settings_close_health_and_invalid_secret_no_reset(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);root.chmod(0o700)
            password=secrets.token_urlsafe(32)
            initialize(root,password)
            secret_file=root/'config/admin-password.json';secret_before=secret_file.read_bytes()
            settings=load_assistant_settings(root/'config/runtime.yml')
            daemon=AssistantDaemon(settings)
            app=create_admin_app(daemon,settings,local_teams_control=True)
            sock=socket.socket();sock.bind(('127.0.0.1',8080));sock.listen(128)
            server=uvicorn.Server(uvicorn.Config(app,access_log=False,log_level='critical'))
            task=asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(5):
                    while not server.started:
                        if task.done():await task;self.fail('disposable server failed')
                        await asyncio.sleep(.01)
                before=claim_snapshot(root)
                with self.assertRaisesRegex(ValueError,'browser_secret_required'):
                    await browser_readiness(root,None,expected_pid=os.getpid())
                # An invalid password reaches the actual login, but never gets
                # the authenticated calendar response (specific browser timeout).
                with self.assertRaises(BrowserTimeout):
                    await browser_readiness(root,'synthetic-wrong-password',expected_pid=os.getpid())
                result=await browser_readiness(root,password,expected_pid=os.getpid())
                self.assertTrue(all(result[k] for k in ('browser_login','weekly_calendar','settings',
                    'browser_closed','post_close_health','control_state_unchanged')))
                self.assertEqual(result['readiness']['bound_stop_cleanup'],'NOT EXECUTED')
                self.assertEqual(claim_snapshot(root),before)
                self.assertEqual(secret_file.read_bytes(),secret_before)
                self.assertNotIn(password,json.dumps(result))
                self.assertIsNone(daemon.status()['active_session_id'])
                self.assertTrue(daemon.store.settings()['scheduler_paused'])
            finally:
                server.should_exit=True
                try:await asyncio.wait_for(task,5)
                finally:sock.close()
