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

    def test_approved_default_private_tunnel_uses_only_fixed_installed_origin(self):
        runtime=self.root/'config/runtime.yml';before=runtime.read_bytes()
        try:
            observed=configured_entrypoint(self.root)
        except ValueError as error:
            self.fail('approved private tunnel rejected: '+str(error))
        self.assertEqual(observed,'http://127.0.0.1:8080')
        self.assertEqual(runtime.read_bytes(),before)
        self.assertFalse((self.root/'codex').exists())

    def test_unapproved_urls_ports_and_loopbacks_refuse_before_browser_or_network(self):
        runtime=self.root/'config/runtime.yml'
        original=json.loads(runtime.read_text())
        for url in ('http://127.0.0.1:18101','http://localhost:8080','http://127.0.0.1:8081',
                    'http://192.0.2.1:8080','http://operator-approved.invalid','https://localhost','https://127.0.0.1',
                    'https://127.0.1.2','https://[::1]','https://[::ffff:127.0.0.1]',
                    'https://alias.localhost','https://2130706433'):
            value={**original,'public_base_url':url,'cookie_secure':bool(url and url.startswith('https'))}
            runtime.write_text(json.dumps(value))
            with patch('playwright.async_api.async_playwright',side_effect=AssertionError('no browser on unverified entry')):
                with self.assertRaises(ValueError):
                    asyncio.run(anonymous_login(self.root))
        self.assertFalse((self.root/'codex').exists())

    def test_private_mode_requires_exact_runtime_origin_listener_and_cookie_policy(self):
        runtime=self.root/'config/runtime.yml';original=json.loads(runtime.read_text())
        for url in ('http://127.0.0.1:8080','http://127.0.0.1:8080/'):
            runtime.write_text(json.dumps({**original,'public_base_url':url}))
            self.assertEqual(configured_entrypoint(self.root),'http://127.0.0.1:8080')
        for changes in ({'cookie_secure':True},{'host':'127.0.0.1'},{'port':8081},
            {'public_base_url':'http://127.0.0.1:8080/login'},
            {'public_base_url':'http://127.0.0.1:8080/?approved=true'},
            {'public_base_url':'http://127.0.0.1:8080/#approved'},
            {'public_base_url':'http://user@127.0.0.1:8080'},
            {'data_root':str(self.root/'foreign')}):
            runtime.write_text(json.dumps({**original,**changes}))
            with self.subTest(changes=changes),self.assertRaises(ValueError):configured_entrypoint(self.root)

    def test_cookie_transport_exception_is_fixed_private_only(self):
        from deploy.target_runtime import anonymous_cookie_valid
        for origin,secure,host in (('http://127.0.0.1:8080',False,'127.0.0.1'),
            ('https://operator-approved.invalid',True,'operator-approved.invalid')):
            cookie=dict(secure=secure,httpOnly=True,sameSite='Lax',domain=host,path='/')
            self.assertTrue(anonymous_cookie_valid(cookie,origin))
            for changes in ({'secure':not secure},{'httpOnly':False},{'sameSite':'None'},
                {'domain':'foreign.invalid'},{'path':'/settings'}):
                self.assertFalse(anonymous_cookie_valid({**cookie,**changes},origin))
        self.assertFalse(anonymous_cookie_valid(dict(secure=False,httpOnly=True,
            sameSite='Lax',domain='127.0.0.1',path='/'),'http://127.0.0.1:8081'))

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
        origin='http://127.0.0.1:8080'
        for method,url in (('POST',origin+'/login'),('GET',origin+'/settings'),
            ('GET','http://127.0.0.1:18101/login'),('GET','http://localhost:8080/login'),
            ('GET',origin+'/assets/calendar.js'),('GET',origin+'/api/status?x=1'),
            ('GET',origin+'/login#redirect')):
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

