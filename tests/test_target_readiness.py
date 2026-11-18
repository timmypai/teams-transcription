"""Disposable real Unix transport and SQLite; never an authorized occurrence."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from deploy.target_runtime import initialize, rejection_readiness, read_password
from teams_transcription.teams_local_control import LocalTeamsControl
from teams_transcription.assistant_config import load_assistant_settings


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.root.chmod(0o700)
        initialize(self.root,'synthetic-disposable-password')
        self.settings=load_assistant_settings(self.root/'config/runtime.yml')

    async def test_real_server_rejects_fixed_no_id_without_store_or_daemon_call(self):
        # object() has no daemon methods; a valid operation would fail this test.
        async with LocalTeamsControl(object(),self.settings):
            value=await rejection_readiness(self.root)
        self.assertTrue(value['exact_rejection'])
        self.assertTrue(value['sqlite_unchanged'])
        self.assertEqual(value['scope'],'transport-and-rejection-readiness')
        self.assertEqual(value['authorized_view'],'NOT EXECUTED')

    async def test_no_transport_and_wrong_peer_or_ownership_never_count_as_rejection(self):
        with self.assertRaises((ValueError,OSError)):await rejection_readiness(self.root)
        async with LocalTeamsControl(object(),self.settings) as server:
            with patch('deploy.target_runtime.socket_peer_uid',return_value=os.getuid()+1):
                with self.assertRaises(ValueError):await rejection_readiness(self.root)
            server.path.chmod(0o660)
            with self.assertRaises(ValueError):await rejection_readiness(self.root)
            server.path.chmod(0o600)
            server.path.parent.chmod(0o750)
            with self.assertRaises(ValueError):await rejection_readiness(self.root)
            server.path.parent.chmod(0o700)

    async def test_unknown_response_eof_and_timeout_fail(self):
        parent=self.root/'.teams-control';parent.mkdir(mode=0o700)
        path=parent/'control.sock'
        for response in (b'{"ok":true}\n',b'{"error":"other"}\n',b''):
            async def handle(reader,writer):
                await reader.readline();writer.write(response);await writer.drain();writer.close();await writer.wait_closed()
            server=await asyncio.start_unix_server(handle,path=path);path.chmod(0o600)
            try:
                with self.assertRaises(ValueError):await rejection_readiness(self.root)
            finally:server.close();await server.wait_closed();path.unlink()

    async def test_real_timeout_and_wrong_daemon_pid_refuse(self):
        async with LocalTeamsControl(object(),self.settings):
            with self.assertRaisesRegex(ValueError,'control_peer_pid_mismatch'):
                await rejection_readiness(self.root,expected_pid=os.getpid()+1)
        parent=self.root/'.teams-control';path=parent/'control.sock'
        tasks=[]
        async def silent(reader,writer):
            task=asyncio.current_task();tasks.append(task)
            try:await reader.read()
            finally:writer.close();await writer.wait_closed()
        server=await asyncio.start_unix_server(silent,path=path);path.chmod(0o600)
        try:
            with self.assertRaises(TimeoutError):await rejection_readiness(self.root)
        finally:
            server.close();await server.wait_closed()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            path.unlink()

    async def test_response_with_control_mutation_refuses_even_exact_rejection(self):
        from teams_transcription.assistant_store import AssistantStore
        parent=self.root/'.teams-control';parent.mkdir(mode=0o700);path=parent/'control.sock'
        async def changed(reader,writer):
            self.assertEqual(await reader.readline(),b'{}\n')
            AssistantStore(self.root).update_settings({'scheduler_paused':False})
            writer.write(b'{"error": "teams_local_control_rejected"}\n')
            await writer.drain();writer.close();await writer.wait_closed()
        server=await asyncio.start_unix_server(changed,path=path);path.chmod(0o600)
        try:
            with self.assertRaisesRegex(ValueError,'control_state_changed'):await rejection_readiness(self.root)
        finally:server.close();await server.wait_closed();path.unlink()

    def test_secret_parser_missing_unknown_and_newline_refuse_and_existing_not_reset(self):
        for raw in ('','OTHER=synthetic','DEVFLOW_ADMIN_PASSWORD=short','DEVFLOW_ADMIN_PASSWORD='+'x'*16+'\nOTHER=y'):
            with self.assertRaises(ValueError):read_password(raw)
        self.assertEqual(read_password('DEVFLOW_ADMIN_PASSWORD='+'x'*16+'\n'),'x'*16)
        path=self.root/'config/admin-password.json';before=path.read_bytes()
        self.assertFalse(initialize(self.root,None)['initialized'])
        self.assertEqual(path.read_bytes(),before)
