"""Fixed host CLI and private stdin -> installed CLI -> Unix/daemon/SQLite.

GitHub, Docker and browser results are injected here; the hosted browser
journey separately exercises the real authenticated form and exporter.
"""
import asyncio
import contextlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from deploy import private_control, target_deploy as deploy, target_runtime as runtime
from test_calendar_export import valid
import test_teams_local_control as fixture


class ManualControl:
    def __init__(self, test, host):
        self.test, self.host = test, host
        self.source = host/'source'
        self.source.mkdir()
        self.sha, self.image, self.cid = 'c'*40, 'sha256:'+'d'*64, 'e'*64
        self.slot = host/'.devflow-live'/deploy.PROJECT.replace('devflow-', 'timmypai-')/'single-control.json'
        self.calls, self.secrets = [], []
        self.actor, self.run = 123, 100
        self.browser_args = []
        self.extra_rows = []
        self.wrong_binding_identity = False

    def process(self, args, **kwargs):
        self.test.assertEqual(args, ['docker','--host=unix://'+f'/run/user/{os.getuid()}/docker.sock',
            'exec','-i',deploy.APP,'python',deploy.RUNTIME,'bound-control'])
        self.calls.append(json.loads(kwargs['input']))
        stream = io.StringIO()
        with patch.object(runtime,'Path',side_effect=lambda value:self.test.root if value=='/data' else Path(value)), \
                patch.object(sys,'argv',['target_runtime.py','bound-control']), \
                patch.object(sys,'stdin',io.StringIO(kwargs['input'])),contextlib.redirect_stdout(stream):
            status = runtime.main()
        raw = json.loads(stream.getvalue())
        if 'session_id' in raw:self.secrets.append(raw['session_id'])
        return subprocess.CompletedProcess(args,status,stream.getvalue(),'lowercaseprivatestderr')

    def browser(self, controller, op, args, env, *, owner):
        ident = self.test.occurrence['id']
        self.browser_args.append((op,args))
        if op == 'calendar-occurrences':
            return dict(valid(),occurrences=[dict(occurrence_id=ident,revision=1,joinable=True,
                start_at='2030-01-01T09:00:00+00:00',end_at='2030-01-01T10:00:00+00:00'),*self.extra_rows])
        self.test.assertEqual(op, 'issue-binding')
        self.test.assertIs(args['present'], True)
        self.test.assertIs(args['media'], True)
        issued = self.test.store.issue_target_consent(ident,args['revision'],present=args['present'],media=args['media'])
        self.secrets.append(issued['request_sha256'])
        return dict(issued,ok=True,occurrence_id='9'*32 if self.wrong_binding_identity else ident)

    def invoke(self, action, *, submitted=None, attempt=1):
        self.run += 1
        run = self.run
        original = deploy.Controller
        def factory(*args,**kwargs):
            controller = original(*args,**kwargs)
            controller.write(controller.root/'state.json',json.dumps(dict(phase='healthy',source_sha=self.sha,
                image=self.image,container_id=self.cid,release='fixture')))
            controller.preflight=lambda state:[]
            controller.verify_release=lambda release:dict(image=self.image,source_sha=self.sha)
            controller.containers=lambda:[]
            controller.health=lambda image:(self.cid,dict(health=True))
            return controller
        from datetime import datetime, timezone
        row = dict(id=run,run_attempt=attempt,event='workflow_dispatch',path='.github/workflows/control.yml',
            head_branch='main',head_sha=self.sha, repository=dict(full_name=deploy.REPO),
            head_repository=dict(full_name=deploy.REPO), actor=dict(id=self.actor),triggering_actor=dict(id=self.actor),
            created_at=datetime.fromtimestamp(time.time() if submitted is None else submitted,timezone.utc).isoformat())
        env = dict(GITHUB_RUN_ID=str(run),GITHUB_RUN_ATTEMPT=str(attempt),DEVFLOW_OPS_ENV='fixtureonly')
        stream = io.StringIO()
        with patch.object(deploy,'ROOT',self.source),patch.object(deploy.Path,'home',return_value=self.host), \
                patch.object(deploy,'Controller',side_effect=factory),patch.object(deploy.subprocess,'run',side_effect=self.process), \
                patch.object(deploy,'manual_environment',return_value=self.sha), \
                patch.object(deploy,'collect',return_value=dict(repository=deploy.REPO,source_sha=self.sha)), \
                patch.object(deploy,'GitHub') as github,patch.object(private_control,'browser',side_effect=self.browser), \
                patch.dict(os.environ,env),patch.object(sys,'argv',['target_deploy.py',action]), \
                contextlib.redirect_stdout(stream),contextlib.redirect_stderr(stream):
            github.return_value.request.return_value=(row,{})
            code=deploy.main()
        evidence = self.source/f'.devflow/evidence-deploy-{run}-{attempt}'
        public = stream.getvalue().encode() + b''.join(p.read_bytes() for p in evidence.rglob('*') if p.is_file())
        for secret in [self.test.occurrence['title'],self.test.occurrence['join_url'],self.test.occurrence['id'],
                       'lowercaseprivatestderr','fixtureonly',*self.secrets]:
            self.test.assertFalse(secret.encode() in public, 'private value in complete public control evidence')
        self.test.assertEqual({p.name for p in evidence.iterdir()}, {'receipt.json'} if code==0 else {'failure.json'})
        return code,json.loads((evidence/('receipt.json' if code==0 else 'failure.json')).read_text())


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
        with tempfile.TemporaryDirectory() as folder:
            cli=ManualControl(self,Path(folder))
            async with fixture.LocalTeamsControl(self.daemon,self.settings):
                async def run(action, **kw):return await asyncio.to_thread(cli.invoke,action,**kw)
                code,_=await run('join-confirmed-present-and-media')
                self.assertEqual(code,1)
                code,receipt=await run('preview')
                self.assertEqual(code,0,receipt)
                self.assertEqual(cli.slot.stat().st_mode & 0o777,0o600)
                cli.actor+=1
                self.assertEqual((await run('join-confirmed-present-and-media'))[0],1)
                cli.actor-=1
                self.assertEqual((await run('join-confirmed-present-and-media',attempt=2))[0],1)
                self.assertEqual((await run('join-confirmed-present-and-media',submitted=time.time()-301))[0],1)
                self.assertEqual(cli.calls,[])
                self.assertEqual([op for op,_ in cli.browser_args],['calendar-occurrences'])
                start=time.time()
                code,receipt=await run('join-confirmed-present-and-media')
                self.assertEqual(code,0,receipt)
                self.assertGreaterEqual(receipt['result']['dispatch_at'],start)
                self.assertLessEqual(receipt['result']['dispatch_at'],time.time())
                self.assertTrue(0 <= receipt['result']['issue_to_dispatch_ms'] < 300000)
                ident=cli.secrets[-1]
                await asyncio.wait_for(self.started.wait(),2)
                self.assertEqual((await run('join-confirmed-present-and-media'))[0],1)
                self.assertEqual((await run('preview'))[0],1)
                code,receipt=await run('view')
                self.assertEqual(code,0,receipt)
                self.assertTrue(receipt['result']['authorized_view'])
                await self.until(lambda:any(row['action']=='speech_enabled' and row['target']==ident
                    for row in self.store.audit()))
                self.assertTrue(output.enabled)
                code,receipt=await run('output-disable')
                self.assertEqual(code,0,receipt)
                self.assertFalse(output.enabled)
                for _ in range(2):
                    code,receipt=await run('stop')
                    self.assertEqual(code,0,receipt)
                    self.assertTrue(receipt['result']['cleanup_verified'])
                self.assertEqual(len(self.store.list_runs()),1)
                self.assertFalse(any(v in cli.slot.read_text() for v in cli.secrets))
                self.assertEqual([row['operation'] for row in cli.calls],
                    ['join','view','view','output-disable','view','stop'])

    async def test_ambiguous_or_mismatched_selection_cannot_dispatch(self):
        with tempfile.TemporaryDirectory() as folder:
            cli=ManualControl(self,Path(folder))
            cli.extra_rows=[dict(occurrence_id='9'*32,revision=4,joinable=True,
                start_at='2030-01-01T09:00:00+00:00',end_at='2030-01-01T10:00:00+00:00')]
            self.assertEqual((await asyncio.to_thread(cli.invoke,'preview'))[0],1)
            cli.extra_rows=[]
            self.assertEqual((await asyncio.to_thread(cli.invoke,'preview'))[0],0)
            cli.wrong_binding_identity=True
            code, failure=await asyncio.to_thread(cli.invoke,'join-confirmed-present-and-media')
            self.assertEqual(code,1)
            self.assertEqual(failure['primary_failure']['code'],'binding_result_invalid')
            self.assertEqual((await asyncio.to_thread(cli.invoke,'join-confirmed-present-and-media'))[0],1)
            self.assertEqual(cli.calls,[])

    async def test_unknown_cleanup_leaves_private_interlock(self):
        from teams_transcription.teams_acceptance import FINISHED_EVENT
        async def runner(session):
            session.update(state='recording');self.started.set()
            while not session.stopped():await asyncio.sleep(.01)
            session.update(state='stopped',cleanup_status='unknown');session.event(FINISHED_EVENT,{})
        self.daemon.runner=runner
        with tempfile.TemporaryDirectory() as folder:
            cli=ManualControl(self,Path(folder))
            async with fixture.LocalTeamsControl(self.daemon,self.settings):
                self.assertEqual((await asyncio.to_thread(cli.invoke,'preview'))[0],0)
                self.assertEqual((await asyncio.to_thread(cli.invoke,'join-confirmed-present-and-media'))[0],0)
                await asyncio.wait_for(self.started.wait(),2)
                self.assertEqual((await asyncio.to_thread(cli.invoke,'stop'))[0],1)
                self.assertTrue(self.store.capture_interlocks())
                self.assertEqual(json.loads(cli.slot.read_text())['phase'],'active')
                self.assertEqual((await asyncio.to_thread(cli.invoke,'preview'))[0],1)
