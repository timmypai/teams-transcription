"""Disposable metadata and entrypoint refusal; no host/provider probes."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from deploy.target_runtime import initialize,runtime_metadata,configured_entrypoint,anonymous_login


class SmokeRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.root.chmod(0o700)
        initialize(self.root,'disposable-test-password')

    def test_missing_and_loopback_safe_access_refuse_before_any_browser_or_network(self):
        runtime=self.root/'config/runtime.yml'
        original=json.loads(runtime.read_text())
        for url in (None,'http://127.0.0.1:8080','https://localhost','https://127.0.0.1',
                    'https://127.0.1.2','https://[::1]','https://[::ffff:127.0.0.1]',
                    'https://alias.localhost','https://2130706433'):
            value={**original,'public_base_url':url,'cookie_secure':bool(url and url.startswith('https'))}
            runtime.write_text(json.dumps(value))
            with patch('playwright.async_api.async_playwright',side_effect=AssertionError('no browser on unverified entry')):
                with self.assertRaisesRegex(ValueError,'approved_safe_entrypoint_unverified'):
                    asyncio.run(anonymous_login(self.root))
        self.assertFalse((self.root/'codex').exists())

    def test_anonymous_route_guard_covers_real_login_stylesheet_not_other_traffic(self):
        import re
        from urllib.parse import urljoin
        from teams_transcription.admin_ui import page
        from deploy.target_runtime import anonymous_route_allowed
        origin='https://operator-approved.invalid'
        html=page('管理登入','').body.decode()
        required=re.findall(r'<link rel="stylesheet" href="([^"]+)"',html)
        self.assertEqual(required,['/assets/calendar.css'])
        self.assertTrue(all(anonymous_route_allowed('GET',urljoin(origin,path),origin) for path in required))
        self.assertTrue(anonymous_route_allowed('GET',origin+'/login',origin))
        for method,url in (('POST',origin+'/login'),('GET',origin+'/settings'),
            ('GET','https://foreign.invalid/assets/calendar.css'),('GET',origin+'/assets/calendar.js'),
            ('GET',origin+'/assets/calendar.css?private=query')):
            self.assertFalse(anonymous_route_allowed(method,url,origin))

    def test_operator_owned_https_configuration_is_not_created_or_changed(self):
        runtime=self.root/'config/runtime.yml'
        value=json.loads(runtime.read_text());value.update(public_base_url='https://operator-approved.invalid',cookie_secure=True)
        runtime.write_text(json.dumps(value));before=runtime.read_bytes()
        self.assertEqual(configured_entrypoint(self.root),'https://operator-approved.invalid')
        self.assertEqual(runtime.read_bytes(),before)
        runtime.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'private_resource_required'):configured_entrypoint(self.root)

    def test_metadata_never_reads_lists_or_copies_codex_credentials(self):
        codex=self.root/'codex';codex.mkdir(mode=0o700)
        auth=codex/'auth.json';auth.write_text('SYNTHETIC-PRIVATE-NOT-READ');auth.chmod(0o600)
        original=Path.open
        def guarded(path,*args,**kwargs):
            if path==auth:raise AssertionError('must not read credentials')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',guarded),patch.object(Path,'iterdir',side_effect=AssertionError('must not list home')):
            value=runtime_metadata(self.root)
        self.assertEqual(value['runtime_uid'],os.getuid())
        self.assertEqual(value['runtime_gid'],os.getgid())
        self.assertEqual(value['codex_home_gid'],os.getgid())
        self.assertTrue(value['runtime_writable'])
        self.assertTrue(value['codex_home_writable'])
        self.assertEqual(value['runtime_mode'],0o600)
        self.assertEqual(value['codex_home_mode'],0o700)
        self.assertFalse(value['credentials_read'])
        self.assertEqual(value['provider_authorization'],'NOT VERIFIED')
        self.assertNotIn('SYNTHETIC',json.dumps(value))
        self.assertNotIn(str(self.root),json.dumps(value))
        codex.chmod(0o755)
        with self.assertRaises(ValueError):runtime_metadata(self.root)

    def test_missing_home_stays_unverified_not_initialized_or_authenticated(self):
        value=runtime_metadata(self.root)
        self.assertFalse(value['codex_home_exists'])
        self.assertIsNone(value['codex_home_private'])
        self.assertIsNone(value['codex_home_mode'])
        self.assertFalse((self.root/'codex').exists())


class AnonymousBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_ui_anonymous_session_reuse_and_foreign_or_stale_refusal(self):
        import httpx
        from teams_transcription.admin_web import create_admin_app
        from teams_transcription.assistant_config import load_assistant_settings
        from teams_transcription.daemon import AssistantDaemon
        from deploy.target_runtime import same_deployment_session
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);root.chmod(0o700);initialize(root,'disposable-test-password')
            settings=load_assistant_settings(root/'config/runtime.yml')
            app=create_admin_app(AssistantDaemon(settings),settings)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://127.0.0.1:8080') as client:
                response=await client.get('/login')
                cookie=response.cookies['assistant_session']
                class InstalledClient:
                    async def get(self,url,**kwargs):
                        local=await client.get(url,headers=kwargs['headers'],follow_redirects=False)
                        class Response:
                            status=local.status_code
                            headers_array=[dict(name=k,value=v) for k,v in local.headers.multi_items()]
                        return Response()
                await same_deployment_session(InstalledClient(),cookie)
                with self.assertRaisesRegex(ValueError,'anonymous_deployment_mismatch'):
                    await same_deployment_session(InstalledClient(),'f'*64+'.'+'f'*64)
                # Same key but a fresh server has no such in-memory session.
                other=create_admin_app(AssistantDaemon(settings),settings)
                async with httpx.AsyncClient(transport=httpx.ASGITransport(other),base_url='http://127.0.0.1:8080') as fresh:
                    class FreshClient:
                        async def get(self,url,**kwargs):
                            local=await fresh.get(url,headers=kwargs['headers'],follow_redirects=False)
                            class Response:
                                status=local.status_code
                                headers_array=[dict(name=k,value=v) for k,v in local.headers.multi_items()]
                            return Response()
                    with self.assertRaisesRegex(ValueError,'anonymous_deployment_mismatch'):
                        await same_deployment_session(FreshClient(),cookie)

