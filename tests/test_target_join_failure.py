"""Real fixed CLI, installed helper and daemon failure projection."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_target_control_cli import ManualControl
import test_teams_local_control as fixture


class TargetJoinFailureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.LocalControlTests.asyncSetUp
    event=fixture.LocalControlTests.event
    until=fixture.LocalControlTests.until

    async def reject_join(self):
        with tempfile.TemporaryDirectory() as folder:
            cli=ManualControl(self,Path(folder))
            self.assertEqual((await asyncio.to_thread(cli.invoke,'preview'))[0],0)
            code,document=await asyncio.to_thread(cli.invoke,'join-confirmed-present-and-media')
            self.assertEqual(code,1)
            self.assertEqual((await asyncio.to_thread(cli.invoke,'preview'))[0],1)
            return document

    async def test_expired_join_keeps_daemon_code_and_phase_in_manual_failure_json(self):
        self.now+=3600  # fresh binding, but occurrence has now ended in the daemon clock
        async with fixture.LocalTeamsControl(self.daemon,self.settings):
            document=await self.reject_join()
        self.assertEqual(document['primary_failure'],dict(phase='control_join-confirmed-present-and-media',
            code='occurrence_outside_join_window',exit_status=None))
        self.assertEqual(self.store.list_runs(),[])
        self.assertFalse(self.started.is_set())

    async def test_unknown_daemon_exception_never_exposes_message_in_manual_result(self):
        from teams_transcription.assistant_store import AssistantStoreError
        secret='https://private.invalid/meeting Synthetic Person'
        async with fixture.LocalTeamsControl(self.daemon,self.settings):
            with patch.object(self.daemon,'join',side_effect=AssistantStoreError(secret)):
                document=await self.reject_join()
        self.assertEqual(document['primary_failure'],dict(phase='control_join-confirmed-present-and-media',
            code='teams_local_control_rejected',exit_status=None))
        self.assertNotIn(secret,str(document))
        self.assertEqual(self.store.list_runs(),[])
