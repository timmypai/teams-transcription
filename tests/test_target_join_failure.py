"""Manual CLI -> real execute/helper/socket/daemon; synthetic host and runner."""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy import target_deploy as deploy, target_runtime as runtime
import test_teams_local_control as fixture


class TargetJoinFailureTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.LocalControlTests.asyncSetUp
    event=fixture.LocalControlTests.event
    until=fixture.LocalControlTests.until

    def invoke(self, host, consent):
        source=host/'source';source.mkdir()
        sha='c'*40;image='sha256:'+'d'*64;cid='e'*64
        original=deploy.Controller
        def process(args,**kwargs):
            self.assertEqual(args[:2],['docker','--host=unix://'+f'/run/user/{os.getuid()}/docker.sock'])
            self.assertEqual(args[2:6],['exec',deploy.APP,'python',deploy.RUNTIME])
            stream=io.StringIO()
            with patch.object(runtime,'Path',side_effect=lambda value:self.root if value=='/data' else Path(value)),\
                    patch.object(sys,'argv',['target_runtime.py',*args[6:]]),contextlib.redirect_stdout(stream):
                status=runtime.main()
            return subprocess.CompletedProcess(args,status,stream.getvalue(),'private stderr must not escape')
        def factory(*args,**kwargs):
            controller=original(*args,**kwargs)
            controller.write(controller.root/'state.json',json.dumps(dict(phase='healthy',source_sha=sha,
                image=image,container_id=cid,release='fixture')))
            controller.preflight=lambda state:[]
            controller.verify_release=lambda release:dict(image=image,source_sha=sha)
            controller.containers=lambda:[]
            controller.health=lambda image:(cid,dict(health=True))
            return controller
        with patch.object(deploy,'ROOT',source),patch.object(deploy.Path,'home',return_value=host),\
                patch.object(deploy,'Controller',side_effect=factory),\
                patch.object(deploy.subprocess,'run',side_effect=process),\
                patch.object(deploy,'manual_environment',return_value=sha),\
                patch.object(deploy,'collect',return_value=dict(repository=deploy.REPO,source_sha=sha)),\
                patch.object(deploy,'GitHub'),patch.dict(os.environ,dict(GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1')),\
                patch.object(sys,'argv',['target_deploy.py','join','--id',self.occurrence['id'],
                    '--request-sha',consent['request_sha256'],'--revision',str(consent['revision'])]),\
                contextlib.redirect_stdout(io.StringIO()):
            status=deploy.main()
        self.assertEqual(status,1)
        return json.loads((source/'.devflow/evidence-deploy-123-1/failure.json').read_text())

    async def test_expired_join_keeps_daemon_code_and_phase_in_manual_failure_json(self):
        consent=self.store.issue_target_consent(self.occurrence['id'],1,present=True,media=True)
        self.now+=3600  # fresh binding, but occurrence has now ended
        async with fixture.LocalTeamsControl(self.daemon,self.settings):
            with tempfile.TemporaryDirectory() as folder:
                document=await asyncio.to_thread(self.invoke,Path(folder),consent)
        self.assertEqual(document['primary_failure'],dict(phase='control_join',
            code='occurrence_outside_join_window',exit_status=None))
        self.assertEqual(self.store.list_runs(),[])
        self.assertFalse(self.started.is_set())
        for secret in (self.occurrence['join_url'],self.occurrence['title'],consent['request_sha256'],'private stderr'):
            self.assertNotIn(secret,json.dumps(document))

    async def test_unknown_daemon_exception_never_exposes_message_in_manual_result(self):
        from teams_transcription.assistant_store import AssistantStoreError
        secret='https://private.invalid/meeting Synthetic Person'
        consent=self.store.issue_target_consent(self.occurrence['id'],1,present=True,media=True)
        async with fixture.LocalTeamsControl(self.daemon,self.settings):
            with patch.object(self.daemon,'join',side_effect=AssistantStoreError(secret)):
                with tempfile.TemporaryDirectory() as folder:
                    document=await asyncio.to_thread(self.invoke,Path(folder),consent)
        self.assertEqual(document['primary_failure'],dict(phase='control_join',
            code='teams_local_control_rejected',exit_status=None))
        self.assertNotIn(secret,json.dumps(document))
        self.assertEqual(self.store.list_runs(),[])
