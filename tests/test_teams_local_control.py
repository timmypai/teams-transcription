"""Real Unix transport/SQLite/daemon, synthetic runner; no browser or model."""
import asyncio
import json
import os
import sys
import time
import threading
import unittest
from unittest.mock import patch

import httpx

from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials
from teams_transcription.teams_local_control import LocalControlError, LocalTeamsControl, request, socket_path
import test_assistant_daemon as daemon_fixture
from test_assistant_daemon import iso


class LocalControlTests(unittest.IsolatedAsyncioTestCase):
    event = daemon_fixture.AssistantDaemonTests.event
    until = daemon_fixture.AssistantDaemonTests.until

    async def asyncSetUp(self):
        await daemon_fixture.AssistantDaemonTests.asyncSetUp(self)
        self.settings = self.settings.model_copy(update={'allowed_egress_hosts':['teams.microsoft.com']})
        self.daemon.settings = self.settings
        self.store.update_settings({'scheduler_paused':True})
        self.occurrence = self.store.create_manual_occurrence(dict(title='Synthetic local control',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(self.now-1),end_at=iso(self.now+600),
            recording_authorized=True,speaking_authorized=True))
        self.fields = dict(id=self.occurrence['id'],acceptance_request='a'*64,
            acceptance_revision=1,acceptance_stop_policy='owner_or_meeting_end')

    async def call(self, operation, fields):
        return await request(self.root,dict(operation=operation,fields=fields))

    async def test_join_and_owned_stop_without_login_even_after_session_loss(self):
        initialize_admin_credentials(self.settings,'synthetic-local-password')
        app=create_admin_app(self.daemon,self.settings)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
            self.assertEqual((await client.get('/api/status')).status_code,401)
            app.state.sessions.create(True)
            app.state.sessions.sessions.clear()  # same loss as expiry/restart; no login is required below
            async with LocalTeamsControl(self.daemon,self.settings):
                view=await self.call('view',dict(id=self.occurrence['id']))
                self.assertIsNone(view['session_id'])
                result=await self.call('join',self.fields)
                self.assertTrue(result['ok'])
                await asyncio.wait_for(self.started.wait(),2)
                self.assertEqual((await client.get('/api/status')).status_code,401)
                view=await self.call('view',dict(id=self.occurrence['id']))
                self.assertEqual(view['acceptance']['request_sha256'],'a'*64)
                self.assertNotIn('Synthetic local control',json.dumps(view))
                self.assertNotIn('join_url',view)
                # A runner with no speech controller must still reject unavailable output.
                with self.assertRaises(LocalControlError):
                    await self.call('output',dict(id=result['result']['session_id'],acceptance_request='a'*64,enabled='false'))
                await self.call('stop',dict(id=result['result']['session_id'],acceptance_request='a'*64))
                await self.until(lambda:self.daemon.status()['active_session_id'] is None)
                self.assertEqual((await client.get('/api/status')).status_code,401)
        self.assertFalse(socket_path(self.root).exists())

    async def test_lifespan_opt_in_and_restart_do_not_need_browser_session(self):
        initialize_admin_credentials(self.settings,'synthetic-local-password')
        app=create_admin_app(self.daemon,self.settings,local_teams_control=True)
        async with app.router.lifespan_context(app):
            self.assertEqual((await self.call('view',dict(id=self.occurrence['id'])))['revision'],1)
        self.assertFalse(socket_path(self.root).exists())
        restarted=type(self.daemon)(self.settings,store=self.store,calendar=self.daemon.calendar,clock=lambda:self.now)
        app=create_admin_app(restarted,self.settings,local_teams_control=True)
        async with app.router.lifespan_context(app):
            self.assertEqual(app.state.sessions.sessions,{})
            self.assertEqual((await self.call('view',dict(id=self.occurrence['id'])))['revision'],1)

    async def test_reject_wrong_binding_enable_output_arbitrary_action_and_stale_revision(self):
        async with LocalTeamsControl(self.daemon,self.settings):
            for fields in ({**self.fields,'acceptance_revision':2},{**self.fields,'url':'https://private.invalid'}):
                with self.assertRaises(LocalControlError):await self.call('join',fields)
            self.assertIsNone(self.daemon.status()['active_session_id'])
            result=await self.call('join',self.fields)
            await asyncio.wait_for(self.started.wait(),2)
            ident=result['result']['session_id']
            for action,fields in (
                ('stop',dict(id=ident,acceptance_request='b'*64)),
                ('output',dict(id=ident,acceptance_request='a'*64,enabled='true')),
                ('scheduler',dict(id=ident,acceptance_request='a'*64)),
            ):
                with self.assertRaises(LocalControlError):await self.call(action,fields)
            self.assertEqual(self.daemon.status()['active_session_id'],ident)
            await self.call('stop',dict(id=ident,acceptance_request='a'*64))

    async def test_disabled_by_default_and_public_http_remains_authenticated(self):
        initialize_admin_credentials(self.settings,'synthetic-local-password')
        app=create_admin_app(self.daemon,self.settings)
        async with app.router.lifespan_context(app):
            self.assertFalse(socket_path(self.root).exists())
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://127.0.0.1') as client:
                denied=await client.post('/actions/join',data=self.fields)
                self.assertEqual(denied.status_code,303)
                self.assertEqual(denied.headers['location'],'/login')
                self.assertEqual((await client.get('/api/meetings/'+self.occurrence['id']+'?view=acceptance')).status_code,401)

    async def test_private_socket_lifetime_lock_and_unsafe_paths_fail_closed(self):
        async with LocalTeamsControl(self.daemon,self.settings):
            self.assertEqual(socket_path(self.root).stat().st_mode&0o777,0o600)
            self.assertEqual(socket_path(self.root).parent.stat().st_mode&0o777,0o700)
            with self.assertRaises(BlockingIOError):
                async with LocalTeamsControl(self.daemon,self.settings):pass
            self.assertEqual((await self.call('view',dict(id=self.occurrence['id'])))['revision'],1)
        target=self.root/'unrelated';target.write_text('preserve')
        socket_path(self.root).symlink_to(target)
        with self.assertRaises(LocalControlError):
            async with LocalTeamsControl(self.daemon,self.settings):pass
        self.assertEqual(target.read_text(),'preserve')

    async def test_peer_uid_oversized_duplicate_and_invalid_frames_are_redacted(self):
        async with LocalTeamsControl(self.daemon,self.settings):
            for raw in (b'PRIVATE-SYNTHETIC\n',b'X'*4097+b'\n',
                    b'{"operation":"view","operation":"join","fields":{}}\n'):
                reader,writer=await asyncio.open_unix_connection(socket_path(self.root))
                writer.write(raw);await writer.drain()
                result=await reader.readline()
                self.assertEqual(json.loads(result),{'error':'teams_local_control_rejected'})
                self.assertNotIn(b'PRIVATE',result)
                writer.close();await writer.wait_closed()
            # Emulate only OS credential boundary; actual transport is a Unix socket.
            with patch('teams_transcription.teams_local_control.peer_uid',return_value=os.getuid()+1):
                reader,writer=await asyncio.open_unix_connection(socket_path(self.root))
                self.assertEqual(json.loads(await reader.readline()),{'error':'teams_local_control_rejected'})
                writer.close();await writer.wait_closed()
            self.assertIsNone(self.daemon.status()['active_session_id'])

    async def test_installed_cli_protocol_reads_stdin_without_password_or_cookie(self):
        config=self.root/'runtime.yml'
        config.write_text(json.dumps(self.settings.model_dump()))
        environment={k:v for k,v in os.environ.items() if not k.startswith('ASSISTANT_')}
        async with LocalTeamsControl(self.daemon,self.settings):
            child=await asyncio.create_subprocess_exec(sys.executable,'-m','teams_transcription',
                'local-teams-control','--config',str(config),stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE,env=environment)
            stdout,stderr=await asyncio.wait_for(child.communicate(json.dumps(
                dict(operation='view',fields=dict(id=self.occurrence['id']))).encode()+b'\n'),5)
            self.assertEqual(child.returncode,0,stderr.decode())
            self.assertEqual(json.loads(stdout)['revision'],1)
            self.assertFalse(self.root.joinpath('password.json').exists())
            self.assertFalse(self.root.joinpath('secret.key').exists())

    async def test_post_bind_failure_closes_listener_before_releasing_lifetime_lock(self):
        control=LocalTeamsControl(self.daemon,self.settings)
        with patch('teams_transcription.teams_local_control.os.chmod',side_effect=OSError('synthetic')):
            with self.assertRaises(OSError):await control.__aenter__()
        self.assertFalse(control.server.is_serving())
        self.assertFalse(socket_path(self.root).exists())
        async with LocalTeamsControl(self.daemon,self.settings):
            self.assertEqual((await self.call('view',dict(id=self.occurrence['id'])))['revision'],1)

    async def test_slow_committed_claim_is_owned_after_former_transport_timeout(self):
        original=self.store.claim_run
        def slow_claim(*args,**kwargs):
            value=original(*args,**kwargs)
            time.sleep(10.2)  # exactly the confirmed commit-before-handoff counterexample
            return value
        async with LocalTeamsControl(self.daemon,self.settings):
            with patch.object(self.store,'claim_run',side_effect=slow_claim):
                result=await self.call('join',self.fields)
            await asyncio.wait_for(self.started.wait(),2)
            self.assertEqual(self.daemon.status()['active_session_id'],result['result']['session_id'])
            await self.call('stop',dict(id=result['result']['session_id'],acceptance_request='a'*64))
            await self.until(lambda:self.daemon.status()['active_session_id'] is None)

    async def test_disconnected_caller_does_not_orphan_claim_or_log_private_payload(self):
        original=self.store.claim_run
        claimed=asyncio.Event();release=threading.Event();loop=asyncio.get_running_loop()
        def delayed_claim(*args,**kwargs):
            result=original(*args,**kwargs)
            loop.call_soon_threadsafe(claimed.set)
            if not release.wait(2):raise AssertionError('synthetic release not signaled')
            return result
        async with LocalTeamsControl(self.daemon,self.settings):
            with patch.object(self.store,'claim_run',side_effect=delayed_claim):
                reader,writer=await asyncio.open_unix_connection(socket_path(self.root))
                writer.write(json.dumps(dict(operation='join',fields=self.fields)).encode()+b'\n')
                await writer.drain()
                try:
                    await asyncio.wait_for(claimed.wait(),1)
                    writer.close();await writer.wait_closed()
                finally:release.set()
                await asyncio.wait_for(self.started.wait(),2)
            view=await self.call('view',dict(id=self.occurrence['id']))
            self.assertEqual(self.daemon.status()['active_session_id'],view['session_id'])
            await self.call('stop',dict(id=view['session_id'],acceptance_request='a'*64))
            await self.until(lambda:self.daemon.status()['active_session_id'] is None)
