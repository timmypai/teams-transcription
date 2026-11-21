"""Manual CLI -> real Controller argv -> installed main -> real Unix/daemon/DB.

Only GitHub admission/Docker/host identity are injected. Never Oracle or model.
"""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import yaml
from deploy import target_deploy as deploy, target_runtime as runtime
import test_teams_local_control as fixture


class TargetControlCLITests(unittest.IsolatedAsyncioTestCase):
    event=fixture.LocalControlTests.event
    until=fixture.LocalControlTests.until
    asyncSetUp=fixture.LocalControlTests.asyncSetUp

    async def test_manual_menu_through_real_cli_helper_socket_single_claim_and_cleanup(self):
        from teams_transcription.teams_acceptance import FINISHED_EVENT
        class Output:
            def __init__(self):self.enabled=False
            def status(self):return dict(state='sleeping',output_enabled=self.enabled)
            async def enable_output(self):self.enabled=True
            async def disable_output(self):self.enabled=False
        output=Output()
        async def runner(session):
            session.response_controller=output
            session.update(state='recording');self.started.set()
            while not session.stopped():await asyncio.sleep(.01)
            session.update(state='stopped',cleanup_status='cleaned');session.event(FINISHED_EVENT,{})
        self.daemon.runner=runner
        consent=self.store.issue_target_consent(self.occurrence['id'],1,present=True,media=True)
        menu=yaml.load((deploy.ROOT/'.github/workflows/control.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(menu['on']['workflow_dispatch']['inputs']['operation']['options'],
            ['view','join','output-disable','stop','prepare-home'])
        self.assertEqual(set(menu['on']),{'workflow_dispatch'})
        with tempfile.TemporaryDirectory() as temporary:
            host=Path(temporary);source=host/'source';source.mkdir()
            sha='c'*40;image='sha256:'+'d'*64;cid='e'*64
            original=deploy.Controller;calls=[]
            def command(args,**kwargs):
                prefix=['docker','--host=unix://'+f'/run/user/{os.getuid()}/docker.sock',
                    'exec',deploy.APP,'python',deploy.RUNTIME]
                self.assertEqual(args[:len(prefix)],prefix)
                action=args[len(prefix):];calls.append(action)
                stream=io.StringIO()
                with patch.object(runtime,'Path',side_effect=lambda value: self.root if value=='/data' else Path(value)),\
                        patch.object(sys,'argv',['target_runtime.py',*action]),contextlib.redirect_stdout(stream):
                    code=runtime.main()
                if code:raise ValueError('injected-docker-runtime-nonzero')
                return stream.getvalue()
            def factory(*args,**kwargs):
                controller=original(*args,command=command,**kwargs)
                controller.write(controller.root/'state.json',json.dumps(dict(phase='healthy',source_sha=sha,
                    image=image,container_id=cid,release='fixture')))
                controller.preflight=lambda state:[]
                controller.verify_release=lambda release:dict(image=image,source_sha=sha)
                controller.containers=lambda:[]
                controller.health=lambda image:(cid,dict(health=True))
                return controller
            env=dict(GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='1')
            def invoke(action,ident,request='',revision=None):
                argv=['target_deploy.py',action,'--id',ident,'--request-sha',request]
                if revision is not None:argv+=['--revision',str(revision)]
                with patch.object(deploy,'ROOT',source),patch.object(deploy.Path,'home',return_value=host),\
                    patch.object(deploy,'Controller',side_effect=factory),\
                    patch.object(deploy,'manual_environment',return_value=sha),\
                    patch.object(deploy,'collect',return_value=dict(repository=deploy.REPO,source_sha=sha)),\
                    patch.object(deploy,'GitHub'),patch.dict(os.environ,env),\
                    patch.object(sys,'argv',argv),contextlib.redirect_stdout(io.StringIO()):
                    code=deploy.main()
                evidence=source/'.devflow/evidence-deploy-123-1'
                return code,json.loads((evidence/('receipt.json' if code==0 else 'failure.json')).read_text())
            async with fixture.LocalTeamsControl(self.daemon,self.settings):
                code,receipt=await asyncio.to_thread(invoke,'join',self.occurrence['id'],
                    consent['request_sha256'],consent['revision'])
                self.assertEqual(code,0,receipt)
                ident=receipt['result']['result']['session_id']
                await asyncio.wait_for(self.started.wait(),2)
                code,_=await asyncio.to_thread(invoke,'join',self.occurrence['id'],
                    consent['request_sha256'],consent['revision'])
                self.assertEqual(code,1)
                # Runner-start is not completed output admission. Test the
                # intended ready -> revoke -> stop path, not startup revocation.
                await self.until(lambda:any(row['action']=='speech_enabled' and row['target']==ident
                    for row in self.store.audit()))
                self.assertTrue(output.enabled)
                code,receipt=await asyncio.to_thread(invoke,'output-disable',ident,consent['request_sha256'])
                self.assertEqual(code,0,receipt)
                self.assertFalse(output.enabled)
                code,receipt=await asyncio.to_thread(invoke,'stop',ident,consent['request_sha256'])
                self.assertEqual(code,0,receipt)
                self.assertTrue(receipt['result']['cleanup_verified'])
                self.assertEqual(len(self.store.list_runs()),1)
                self.assertIn('--revision',calls[0])
                self.assertEqual(calls[0][-1],str(consent['revision']))
