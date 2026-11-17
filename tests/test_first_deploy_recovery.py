"""First-deploy recovery public operation; synthetic Docker boundary only."""
import json
import contextlib
import os
import stat
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_staging_docker_diagnostics as fixture

staging = fixture.staging
ROOT = Path(__file__).resolve().parent.parent


class FirstDeployRecoveryTests(unittest.TestCase):
    @contextlib.contextmanager
    def prepared(self):
        helper=fixture.StoppedCreateDiagnosticTests()
        state=helper.state()
        controller=helper.controller(state)
        compose=controller.compose
        controller.stop_uncertain=False
        controller.start_fails=False
        controller.started=False
        def command(name,*args,**kwargs):
            if name == 'start':
                controller.started=True
                controller.report={'maintenance':True,'database_present':True,
                    'scheduler_paused':True,'quiescent':True}
                if controller.start_fails:
                    raise staging.Refused('staging_controller_cancelled')
                return ''
            if name == 'stop':
                if controller.stop_uncertain: raise staging.Refused('docker_command_timeout')
                controller.started=False
                return ''
            return compose(name,*args,**kwargs)
        controller.compose=command
        controller.health=lambda: {'status':'running','health':'healthy','image':state['current']}
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as install, \
                patch.object(staging,'STATE',Path(root)),patch.object(staging,'INSTALL',Path(install)):
            root=Path(root)
            for src,dst in [('compose.staging.yml','compose.yml'),('compose.onboarding.yml','compose.onboarding.yml')]:
                (Path(install)/dst).write_bytes((ROOT/'deploy'/src).read_bytes())
            (root/'deployment.json').write_text(json.dumps(state))
            (root/'image.env').write_text('TRANSCRIPTION_IMAGE='+state['current']+'\n')
            (root/'create-diagnostic-123.json').write_text(json.dumps({'operation':'diagnose-create',
                'deployment_run_id':123,'image':state['current'],'revision':state['revision'],'outcome':'attempted'}))
            (root/'create-diagnostic-123.json').chmod(0o600)
            yield state,controller,root,Path(install)

    def test_cancelled_start_stops_but_keeps_failed_bytes_and_consumed_fence(self):
        for uncertain in (False,True):
            with self.subTest(stop_uncertain=uncertain),self.prepared() as (state,controller,root,install):
                original=(root/'deployment.json').read_bytes()
                audit=(root/'create-diagnostic-123.json').read_bytes()
                controller.start_fails=True
                controller.stop_uncertain=uncertain
                with self.assertRaises(staging.Refused) as caught:
                    staging.operate(controller,'recover-first-deploy','',state)
                self.assertEqual(caught.exception.args[0],'staging_controller_cancelled')
                self.assertEqual(len(caught.exception.args)>1,uncertain)
                self.assertEqual((root/'deployment.json').read_bytes(),original)
                self.assertEqual((root/'create-diagnostic-123.json').read_bytes(),audit)
                self.assertTrue(controller.report['maintenance'])
                self.assertTrue((root/'first-recovery-123.json').exists())

    def test_wrong_configs_original_audit_or_nonempty_runtime_refuse_before_create(self):
        for case in ('config','audit','missing-audit','healthy','previous','database','marker','busy','container','pin'):
            with self.subTest(case=case),self.prepared() as (state,controller,root,install):
                if case=='config':(install/'compose.onboarding.yml').write_text('unreviewed')
                elif case=='audit':(root/'create-diagnostic-123.json').write_text('{}')
                elif case=='missing-audit':(root/'create-diagnostic-123.json').unlink()
                elif case=='healthy':state['phase']='healthy'
                elif case=='previous':state['previous']=staging.image_ref('sha256:'+'d'*64)
                elif case=='database':controller.report={**controller.report,'database_present':True}
                elif case=='marker':controller.report={**controller.report,'maintenance':False}
                elif case=='busy':controller.report={**controller.report,'quiescent':False}
                elif case=='container':controller.created=True
                elif case=='pin':(root/'image.env').write_text('wrong')
                with self.assertRaises(staging.Refused):staging.operate(controller,'recover-first-deploy','',state)
                self.assertNotIn('create',controller.commands)
                self.assertFalse((root/'first-recovery-123.json').exists())

    def test_repeat_failure_is_fenced_even_when_no_container_remains(self):
        with self.prepared() as (state,controller,root,install):
            controller.fail_create=True
            with self.assertRaisesRegex(staging.Refused,'docker_command_failed'):
                staging.operate(controller,'recover-first-deploy','',state)
            with self.assertRaisesRegex(staging.Refused,'first_recovery_already_attempted'):
                staging.operate(controller,'recover-first-deploy','',state)
            self.assertEqual(controller.commands.count('create'),1)

    def test_healthy_http_with_unpaused_store_cannot_promote_deployment(self):
        with self.prepared() as (state,controller,root,install):
            controller.health=lambda: (controller.report.update(scheduler_paused=False)
                or {'status':'running','health':'healthy','image':state['current']})
            with self.assertRaisesRegex(staging.Refused,'recovery_started_state_not_paused_idle'):
                staging.operate(controller,'recover-first-deploy','',state)
            self.assertEqual(staging.read_state()['phase'],'failed')
            self.assertFalse(controller.started)

    def test_unsynced_intent_refuses_before_create(self):
        with self.prepared() as (state,controller,root,install):
            fsync=os.fsync
            def fail(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode):raise OSError('synthetic fsync failure')
                return fsync(fd)
            with patch.object(staging.os,'fsync',side_effect=fail),self.assertRaises(OSError):
                staging.operate(controller,'recover-first-deploy','',state)
            self.assertNotIn('create',controller.commands)

    def test_recovery_needs_no_registry_or_job_credential(self):
        with self.prepared() as (state,controller,root,install),patch.dict(os.environ,{},clear=True):
            with staging.registry_config('recover-first-deploy') as config:
                self.assertTrue(Path(config).is_dir())
            self.assertFalse(Path(config).exists())
            self.assertFalse((root/'registry').exists())

    def test_failed_health_keeps_failed_state_and_stops_candidate(self):
        with self.prepared() as (state,controller,root,install):
            controller.health=lambda:{'status':'running','health':'unhealthy','image':state['current']}
            with self.assertRaisesRegex(staging.Refused,'recovery_health_failed'):
                staging.operate(controller,'recover-first-deploy','',state)
            self.assertFalse(controller.started)
            self.assertEqual(staging.read_state()['phase'],'failed')

    def test_stop_os_error_preserves_first_failure_and_uncertain_cleanup(self):
        with self.prepared() as (state,controller,root,install):
            compose=controller.compose
            def unavailable(name,*args,**kwargs):
                if name=='stop':raise OSError('synthetic private failure')
                return compose(name,*args,**kwargs)
            controller.compose=unavailable
            controller.start_fails=True
            with self.assertRaises(staging.Refused) as caught:
                staging.operate(controller,'recover-first-deploy','',state)
            self.assertEqual(caught.exception.args,('staging_controller_cancelled','recovery_stop_uncertain'))
            self.assertEqual(staging.read_state()['phase'],'failed')

    def test_healthy_commit_syncs_directory_and_unknown_outcome_never_blind_stops(self):
        with self.prepared() as (state,controller,root,install):
            fsync=os.fsync
            def unavailable(fd):
                if stat.S_ISDIR(os.fstat(fd).st_mode) and staging.read_state()['phase']=='healthy':
                    raise OSError('synthetic commit sync outcome unknown')
                return fsync(fd)
            with patch.object(staging.os,'fsync',side_effect=unavailable),self.assertRaises(OSError):
                staging.operate(controller,'recover-first-deploy','',state)
            self.assertTrue(controller.started)
            self.assertTrue(controller.report['maintenance'])
            self.assertTrue((root/'first-recovery-123.json').exists())

    def test_recovery_keeps_original_audit_and_failure_history_and_maintenance(self):
        with self.prepared() as (state,controller,root,install):
            original=(root/'deployment.json').read_bytes()
            audit=root/'create-diagnostic-123.json'
            audit_before=audit.read_bytes()
            result=staging.operate(controller,'recover-first-deploy','',state)
            self.assertEqual(result['phase'],'healthy')
            self.assertTrue(result['maintenance_preserved'])
            self.assertTrue(result['scheduler_paused'])
            self.assertEqual(audit.read_bytes(),audit_before)
            history=json.loads((root/'first-recovery-123.json').read_text())
            self.assertEqual(history['original_state_text'].encode(),original)
            self.assertEqual(staging.read_state()['previous'],None)
            with self.assertRaises(staging.Refused):
                staging.operate(controller,'recover-first-deploy','',state)
