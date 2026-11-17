"""Deployment's public control boundary, synthetic SQLite only; no VM/Docker."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from teams_transcription.assistant_store import AssistantStore, AssistantStoreError
from teams_transcription.storage import Session
from test_capture_cleanup_review import CleanupFixture

spec = importlib.util.spec_from_file_location('stagingctl',
    Path(__file__).resolve().parent.parent / 'deploy/stagingctl.py')
staging = importlib.util.module_from_spec(spec)
spec.loader.exec_module(staging)


class StagingControlTests(unittest.TestCase):
    def test_provenance_rejection_occurs_before_registry_pull_or_service_change(self):
        class UntrustedRelease:
            def release(self,*args,**kwargs):raise staging.Refused('release_run_not_successful_trusted_main')
            def login(self,*args):raise AssertionError('must not login')
            def docker(self,*args,**kwargs):raise AssertionError('must not pull')
            def compose(self,*args,**kwargs):raise AssertionError('must not mutate service')
        with patch.object(staging.sys,'stdin',io.StringIO('synthetic-token')):
            with self.assertRaisesRegex(staging.Refused,'release_run_not_successful'):
                staging.operate(UntrustedRelease(),'deploy','sha256:'+'a'*64,None)

    def test_pulled_label_must_match_verified_source_before_sandbox_or_stop(self):
        class MismatchedImage:
            def release(self,*args,**kwargs):return {'source_sha':'a'*40}
            def login(self,*args):pass
            def docker(self,*args,**kwargs):pass
            def verify_image(self,*args):return 'b'*40
            def sandbox(self,*args):raise AssertionError('must not execute mismatched image')
            def compose(self,*args,**kwargs):raise AssertionError('must not stop')
        with patch.object(staging.sys,'stdin',io.StringIO('synthetic-token')):
            with self.assertRaisesRegex(staging.Refused,'pulled_image_source_mismatch'):
                staging.operate(MismatchedImage(),'deploy','sha256:'+'a'*64,None)
    def test_digest_rejects_tags_other_registries_and_shell_input(self):
        self.assertEqual(staging.image_ref('sha256:' + 'a'*64), staging.IMAGE + '@sha256:' + 'a'*64)
        for value in ('latest', 'SHA256:'+'a'*64, 'sha256:'+'A'*64, 'sha256:'+'a'*64+'\n',
                'ghcr.io/elsewhere@sha256:'+'a'*64, '$(id)', '; id', '', None):
            with self.subTest(value=value), self.assertRaises(staging.Refused):
                staging.image_ref(value)

    def test_readonly_probe_and_backup_never_export_source_text(self):
        with tempfile.TemporaryDirectory() as root:
            store = AssistantStore(root)
            store.save_glossary({'kind':'term','canonical':'PRIVATE-CONTENT','status':'active'})
            result = staging.database_report(root)
            self.assertTrue(result['quiescent'])
            self.assertNotIn('PRIVATE-CONTENT', json.dumps(result))
            backup = staging.database_report(root, backup='a'*32)
            self.assertTrue(backup['backup_created'])
            path = Path(root)/'deployment-backups'/('a'*32+'.sqlite3')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            store.update_settings({'scheduler_paused':False})
            self.assertFalse(staging.database_report(root)['quiescent'])
            with self.assertRaises(staging.Refused):
                staging.database_report(root, backup='b'*32)

    def test_maintenance_blocks_new_work_at_the_store_claim_boundary(self):
        with tempfile.TemporaryDirectory() as root:
            store = AssistantStore(root)
            marker = Path(root)/'.deployment-maintenance'
            marker.write_text('v1\n')
            with self.assertRaisesRegex(AssistantStoreError, 'deployment_maintenance'):
                store.claim_run('nonexistent-occurrence','controlled-owner')
            self.assertIsNone(store.claim_summary('controlled-owner'))

    def test_schema_change_refuses_rollback_before_stop(self):
        image = staging.image_ref('sha256:'+'a'*64)
        class ControlledDocker:
            def __init__(self): self.commands=[]
            def release(self,image,*args,**kwargs): return {"source_sha":self.verify_image(image),"build_run_id":123,"build_run_attempt":1}
            def login(self,*args): pass
            def docker(self,*args,**kwargs): self.commands.append(args)
            def verify_image(self,*args): return 'a'*40
            def sandbox(self,*args): pass
            def probe(self,*args,**kwargs): return {'quiescent':True,'schema':'changed'}
            def compose(self,*args,**kwargs): raise AssertionError('must not stop')
        controller=ControlledDocker()
        with patch.object(staging.sys,'stdin',io.StringIO('synthetic-token')):
            with self.assertRaisesRegex(staging.Refused,'schema_changed'):
                staging.operate(controller,'rollback','',{'current':image,'previous':image,'rollback_schema':'original','phase':'healthy'})

    def test_failed_candidate_preserves_previous_pin_and_maintenance(self):
        old=staging.image_ref('sha256:'+'a'*64)
        new=staging.image_ref('sha256:'+'b'*64)
        class ControlledDocker:
            def __init__(self): self.maintenance=False; self.stops=0
            def release(self,image,*args,**kwargs): return {"source_sha":self.verify_image(image),"build_run_id":123,"build_run_attempt":1}
            def login(self,*args): pass
            def docker(self,*args,**kwargs): pass
            def verify_image(self,*args): return 'b'*40
            def sandbox(self,*args): pass
            def probe(self,*args,backup=None,maintenance=None):
                if maintenance: self.maintenance=maintenance=='enable'
                return {'quiescent':True,'schema':'same','backup_created':bool(backup)}
            def compose(self,*args,**kwargs):
                if args[0]=='up': raise staging.Refused('docker_command_failed')
                if args[0]=='stop': self.stops+=1
        controller=ControlledDocker()
        with tempfile.TemporaryDirectory() as root, patch.object(staging,'STATE',Path(root)), \
                patch.object(staging.sys,'stdin',io.StringIO('synthetic-token')):
            with self.assertRaisesRegex(staging.Refused,'docker_command_failed'):
                staging.operate(controller,'deploy','sha256:'+'b'*64,
                    {'current':old,'previous':None,'rollback_schema':'same','phase':'healthy'})
            state=staging.read_state()
            self.assertEqual((state['current'],state['previous'],state['phase']),(new,old,'failed'))
            self.assertEqual((Path(root)/'image.env').read_text(),'TRANSCRIPTION_IMAGE='+new+'\n')
            self.assertTrue(controller.maintenance)
            self.assertEqual(controller.stops,2)

    def test_volume_maintenance_marker_is_persistent_and_explicitly_released(self):
        with tempfile.TemporaryDirectory() as root:
            store = AssistantStore(root)
            result = staging.volume_report(root, maintenance='enable')
            self.assertTrue(result['maintenance'])
            self.assertTrue(result['quiescent'])
            self.assertIsNone(store.claim_summary('controlled-owner'))
            self.assertFalse(staging.volume_report(root, maintenance='disable')['maintenance'])

    def test_maintenance_release_refuses_resumed_scheduler_and_keeps_marker(self):
        with tempfile.TemporaryDirectory() as root:
            store = AssistantStore(root)
            staging.volume_report(root, maintenance='enable')
            store.update_settings({'scheduler_paused':False})
            with self.assertRaisesRegex(staging.Refused, 'maintenance_release_not_quiescent'):
                staging.volume_report(root, maintenance='disable')
            self.assertTrue((Path(root)/'.deployment-maintenance').exists())

    def test_pre_stop_abort_does_not_leave_a_new_maintenance_marker(self):
        with tempfile.TemporaryDirectory() as root:
            store=AssistantStore(root)
            store.update_settings({'scheduler_paused':False})
            result=staging.volume_report(root,maintenance='enable')
            self.assertFalse(result['quiescent'])
            self.assertFalse((Path(root)/'.deployment-maintenance').exists())

    def test_repeated_failed_rollback_keeps_last_healthy_target(self):
        good=staging.image_ref('sha256:'+'a'*64)
        bad=staging.image_ref('sha256:'+'b'*64)
        class ControlledDocker:
            def release(self,image,*args,**kwargs): return {"source_sha":self.verify_image(image),"build_run_id":123,"build_run_attempt":1}
            def login(self,*args): pass
            def docker(self,*args,**kwargs): pass
            def verify_image(self,*args): return 'a'*40
            def sandbox(self,*args): pass
            def probe(self,*args,**kwargs): return {'quiescent':True,'schema':'same','backup_created':True}
            def compose(self,*args,**kwargs):
                if args[0]=='up': raise staging.Refused('docker_command_failed')
        with tempfile.TemporaryDirectory() as root, patch.object(staging,'STATE',Path(root)):
            state={'current':bad,'previous':good,'rollback_schema':'same','phase':'failed'}
            for attempt in range(2):
                with patch.object(staging.sys,'stdin',io.StringIO('synthetic-token')):
                    with self.assertRaises(staging.Refused):
                        staging.operate(ControlledDocker(),'rollback','',state)
                state=staging.read_state()
                self.assertEqual(state['previous'],good)

    def test_runner_hook_rejects_prs_branches_and_other_workflows(self):
        hook = Path(__file__).resolve().parent.parent/'deploy/runner-job-started.sh'
        allowed = {'GITHUB_REPOSITORY':'paulpai0412/teams-transcription',
            'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REF':'refs/heads/main',
            'GITHUB_WORKFLOW_REF':'paulpai0412/teams-transcription/.github/workflows/oracle-staging.yml@refs/heads/main'}
        cases = [({},0), ({'GITHUB_EVENT_NAME':'pull_request'},1),
            ({'GITHUB_REF':'refs/heads/feature'},1), ({'GITHUB_REPOSITORY':'elsewhere/repo'},1),
            ({'GITHUB_WORKFLOW_REF':'paulpai0412/teams-transcription/.github/workflows/arm64-image.yml@refs/heads/main'},1)]
        for change,expected in cases:
            result = subprocess.run(['/bin/bash',str(hook)],env={**allowed,**change},capture_output=True)
            self.assertEqual(result.returncode,expected)
            self.assertNotIn(b'paulpai',result.stderr)

    def test_helper_cleanup_uncertainty_preserves_the_first_error(self):
        class UnavailableDocker(staging.Controller):
            def __init__(self): pass
            def docker(self, command, *args, **kwargs):
                raise staging.Refused('docker_command_timeout' if command=='run' else 'docker_command_failed')
        with self.assertRaises(staging.Refused) as caught:
            UnavailableDocker().disposable(staging.image_ref('sha256:'+'a'*64),'print("fixture")')
        self.assertEqual(caught.exception.args,('docker_command_timeout','helper_cleanup_uncertain'))

    def test_termination_cleans_this_jobs_registry_credential(self):
        with tempfile.TemporaryDirectory() as root:
            script = f'''import importlib.util,json,os,pathlib,signal,sys
spec=importlib.util.spec_from_file_location('stagingctl',{str(Path(staging.__file__).resolve())!r})
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m.STATE=pathlib.Path({root!r})
class InterruptedDocker:
    def __init__(self,config):
        (pathlib.Path(config)/'config.json').write_text('synthetic-credential')
        os.kill(os.getpid(),signal.SIGTERM)
m.Controller=InterruptedDocker
sys.argv=['stagingctl','deploy','sha256:'+'a'*64]
sys.exit(m.main())
'''
            result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,
                env={**os.environ,'GITHUB_RUN_ID':'1234','GITHUB_RUN_ATTEMPT':'1'})
            self.assertEqual(result.returncode,1)
            self.assertEqual(json.loads(result.stdout)['error_code'],'staging_controller_cancelled')
            self.assertFalse((Path(root)/'registry'/'1234-1').exists())
            self.assertNotIn('synthetic-credential',result.stdout+result.stderr)


class StagingCleanupParityTests(CleanupFixture, unittest.TestCase):
    def setUp(self):
        self.make_store()

    def test_interrupted_valid_and_stale_reviews_match_application_interlock(self):
        session,lease=self.interrupted(source=False)
        for reviewed in (False,True):
            if reviewed:
                with patch('teams_transcription.assistant_store.os.kill',side_effect=ProcessLookupError):
                    self.review(session)
            result=staging.database_report(self.tmp.name)
            self.assertEqual(result['unreviewed_cleanup'],bool(self.store.capture_interlocks()))
            self.assertEqual(result['unreviewed_cleanup'],not reviewed)
        Session(self.tmp.name,session.ident).update(pid=987654320)
        self.assertTrue(staging.database_report(self.tmp.name)['unreviewed_cleanup'])
        self.assertTrue(self.store.capture_interlocks())


if __name__ == '__main__':
    unittest.main()
