"""Fixed Docker/Compose diagnostics: local synthetic children, never VM/Docker."""
import contextlib
import io
import json
import os
import stat
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_staging_control import staging

CONTROLLER = staging.Controller


class DockerErrorProjectionTests(unittest.TestCase):
    def test_nonabsolute_mount_path_exports_only_exact_fixed_classification(self):
        text = "Error response from daemon: invalid mount path: 'mode=1777' mount path must be absolute"
        script = 'import sys;sys.stderr.write(' + repr(text) + ');sys.exit(1)'
        with self.synthetic_cli(script), self.assertRaises(staging.Refused) as caught:
            self.controller().compose('create', 'teams-transcription')
        report = staging.public_docker_error(caught.exception.docker_error)
        self.assertEqual(report['category'], 'mount_path_not_absolute')
        self.assertNotIn('mode=1777', json.dumps(report))

    @contextlib.contextmanager
    def synthetic_cli(self, script):
        real_popen = subprocess.Popen
        def launch(command, **kwargs):
            self.assertEqual(command[0], '/usr/bin/docker')
            return real_popen([sys.executable, '-c', script], **kwargs)
        with patch.object(staging.subprocess, 'Popen', side_effect=launch):
            yield

    def controller(self):
        controller = object.__new__(CONTROLLER)
        controller.env = {'PATH':'/usr/bin:/bin'}
        return controller

    def test_create_error_exports_only_fixed_category_and_exit_code(self):
        secret = 'Bearer synthetic-private-token owner@example.invalid transcript-secret'
        message = secret + '\nError response from daemon: failed to create network: permission denied\n'
        script = 'import sys;sys.stderr.write(' + repr(message) + ');sys.exit(42)'
        with self.synthetic_cli(script), self.assertRaises(staging.Refused) as caught:
            self.controller().compose('create', '--no-build', '--pull', 'never', 'teams-transcription')
        self.assertEqual(caught.exception.args, ('docker_command_failed',))
        self.assertEqual(caught.exception.docker_error, {
            'operation':'compose_create', 'category':'network_create_failed',
            'exit_code':42, 'stderr_truncated':False, 'stdout_truncated':False})
        self.assertNotIn('synthetic', json.dumps(caught.exception.docker_error))
        self.assertNotIn('owner@', json.dumps(caught.exception.docker_error))

    def test_cli_failure_report_preserves_category_without_raw_error(self):
        secret = 'synthetic-secret-email@example.invalid'
        script = 'import sys;sys.stderr.write(' + repr(secret+'\nunknown flag: --fixture\n') + ');sys.exit(2)'
        def unavailable(config):
            self.controller().compose('create', '--no-build', '--pull', 'never', 'teams-transcription')
        with tempfile.TemporaryDirectory() as root, self.synthetic_cli(script), \
                patch.object(staging, 'STATE', Path(root)), \
                patch.object(staging, 'Controller', side_effect=unavailable), \
                patch.object(staging.sys, 'argv', ['stagingctl', 'diagnostics']), \
                patch.object(staging.sys, 'stdout', io.StringIO()) as output, \
                patch.object(staging.signal, 'signal'):
            result = staging.main()
        report = json.loads(output.getvalue())
        self.assertEqual(result, 1)
        self.assertEqual(report['docker']['category'], 'compose_flag_unsupported')
        self.assertNotIn(secret, output.getvalue())

    def test_unknown_and_oversized_stderr_has_no_raw_fallback(self):
        script = "import sys;sys.stderr.buffer.write(b'PRIVATE-TRANSCRIPT-'*20000+b'failed to create network');sys.exit(3)"
        with self.synthetic_cli(script), self.assertRaises(staging.Refused) as caught:
            self.controller().compose('create', 'teams-transcription')
        report = caught.exception.docker_error
        self.assertEqual(report['category'], 'unclassified')
        self.assertTrue(report['stderr_truncated'])
        self.assertNotIn('PRIVATE', json.dumps(report))

    def test_stdout_limit_terminates_the_cli_instead_of_waiting_for_unbounded_output(self):
        script = "import sys,time;sys.stdout.buffer.write(b'X'*200000);sys.stdout.flush();time.sleep(20)"
        started = time.monotonic()
        with self.synthetic_cli(script), self.assertRaises(staging.Refused) as caught:
            self.controller().docker('inspect', 'synthetic', timeout=2)
        self.assertEqual(caught.exception.docker_error['category'], 'stdout_limit')
        self.assertTrue(caught.exception.docker_error['stdout_truncated'])
        self.assertLess(time.monotonic()-started, 2)

    def test_large_stderr_before_token_read_cannot_deadlock_or_echo_token(self):
        script = "import sys;sys.stderr.buffer.write(b'X'*200000);sys.stderr.flush();v=sys.stdin.buffer.read();print(len(v))"
        with self.synthetic_cli(script):
            value = self.controller().docker('login', 'ghcr.io',
                input=b'SYNTHETIC-TOKEN-' + b'X'*(16384-16), timeout=2)
        self.assertEqual(value, '16384')

    def test_timeout_kills_owned_cli_children_without_touching_unrelated_process(self):
        real_popen = subprocess.Popen
        unrelated = real_popen([sys.executable, '-c', 'import time;time.sleep(20)'])
        try:
            with tempfile.TemporaryDirectory() as root:
                pidfile = Path(root)/'child.pid'
                script = ('import subprocess,sys,time;from pathlib import Path;'
                    'p=subprocess.Popen([sys.executable,"-c","import time;time.sleep(20)"]);'
                    f'Path({str(pidfile)!r}).write_text(str(p.pid));time.sleep(20)')
                with self.synthetic_cli(script), self.assertRaises(staging.Refused) as caught:
                    self.controller().docker('run', 'synthetic', timeout=.5)
                self.assertEqual(caught.exception.args, ('docker_command_timeout',))
                self.assertEqual(caught.exception.docker_error['category'], 'command_timeout')
                pid = int(pidfile.read_text())
                stat = Path('/proc')/str(pid)/'stat'
                # A killed grandchild may briefly await init's reaping as Z.
                for _ in range(100):
                    if not stat.exists() or stat.read_text().split()[2] == 'Z':
                        break
                    time.sleep(.01)
                else:
                    self.fail('owned CLI child still running after timeout')
                self.assertIsNone(unrelated.poll())
        finally:
            unrelated.terminate()
            unrelated.wait(timeout=2)

    def test_cli_discards_unknown_or_malformed_error_metadata(self):
        base = {'operation':'compose_create','category':'unclassified','exit_code':1,
            'stderr_truncated':False,'stdout_truncated':False}
        cases = [{**base,'raw':'PRIVATE-SECRET'}, {**base,'category':'PRIVATE-SECRET'},
            {**base,'operation':['PRIVATE-SECRET']}, {**base,'exit_code':True}]
        for metadata in cases:
            def unavailable(config):
                raise staging.Refused('docker_command_failed', docker_error=metadata)
            with self.subTest(metadata=metadata), tempfile.TemporaryDirectory() as root, \
                    patch.object(staging,'STATE',Path(root)), \
                    patch.object(staging,'Controller',side_effect=unavailable), \
                    patch.object(staging.sys,'argv',['stagingctl','diagnostics']), \
                    patch.object(staging.sys,'stdout',io.StringIO()) as output, \
                    patch.object(staging.signal,'signal'):
                self.assertEqual(staging.main(), 1)
            report = json.loads(output.getvalue())
            self.assertNotIn('docker', report)
            self.assertNotIn('PRIVATE-SECRET', output.getvalue())


class StoppedCreateDiagnosticTests(unittest.TestCase):
    def state(self):
        return {'phase':'failed', 'current':staging.image_ref('sha256:'+'a'*64),
            'previous':None, 'revision':'b'*40, 'deployment_run_id':123,
            'release':{'source_sha':'b'*40}}

    def controller(self, state):
        class ControlledCreate:
            created = False
            fail_create = False
            report = {'maintenance':True, 'database_present':False, 'quiescent':True}
            commands = None
            def __init__(self): self.commands = []
            def verify_image(self, image):
                self.assert_image(image)
                return state['revision']
            def assert_image(self, image):
                if image != state['current']: raise AssertionError('wrong image')
            def probe(self, image, **kwargs):
                self.assert_image(image)
                if kwargs: raise AssertionError('must not mutate maintenance/DB')
                return dict(self.report)
            def compose(self, command, *args, **kwargs):
                self.commands.append(command)
                if command == 'ps': return 'c'*64 if self.created else ''
                if command != 'create': raise AssertionError('must never up/start/stop/remove')
                if args != ('--no-build','--pull','never','teams-transcription'):
                    raise AssertionError('unexpected create flags')
                if self.fail_create: raise staging.Refused('docker_command_failed')
                self.created = True
                return ''
            def docker(self, *args, **kwargs):
                if args[:2] == ('network', 'inspect'):
                    return json.dumps({'driver':'bridge','project':'teams-staging'})
                if args[0] != 'inspect': raise AssertionError('must not login/pull/run')
                return json.dumps({'status':'created','running':False,'pid':0,'image':state['current']})
        return ControlledCreate()

    def test_one_stopped_create_keeps_failed_state_and_maintenance(self):
        state = self.state()
        controller = self.controller(state)
        with tempfile.TemporaryDirectory() as root, patch.object(staging, 'STATE', Path(root)):
            path = Path(root)/'deployment.json'
            before = json.dumps(state).encode()
            path.write_bytes(before)
            (Path(root)/'image.env').write_text('TRANSCRIPTION_IMAGE='+state['current']+'\n')
            result = staging.operate(controller, 'diagnose-create', '', state)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(result['phase'], 'failed')
            self.assertTrue(result['container_created_not_started'])
            self.assertTrue(controller.report['maintenance'])
            self.assertEqual(controller.commands.count('create'), 1)

    def test_failed_create_is_recorded_once_without_recovery_or_retry(self):
        state = self.state()
        controller = self.controller(state)
        controller.fail_create = True
        with tempfile.TemporaryDirectory() as root, patch.object(staging, 'STATE', Path(root)):
            path = Path(root)/'deployment.json'
            path.write_text(json.dumps(state))
            (Path(root)/'image.env').write_text('TRANSCRIPTION_IMAGE='+state['current']+'\n')
            before = path.read_bytes()
            with self.assertRaisesRegex(staging.Refused, 'docker_command_failed'):
                staging.operate(controller, 'diagnose-create', '', state)
            with self.assertRaisesRegex(staging.Refused, 'create_diagnostic_already_attempted'):
                staging.operate(controller, 'diagnose-create', '', state)
            self.assertEqual(controller.commands.count('create'), 1)
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(controller.report['maintenance'])

    def test_healthy_previous_database_or_missing_marker_refuses_before_create(self):
        cases = [({'phase':'healthy'}, {}), ({'previous':staging.image_ref('sha256:'+'d'*64)}, {}),
            ({}, {'database_present':True}), ({}, {'maintenance':False}), ({}, {'quiescent':False})]
        for changes, report in cases:
            state = {**self.state(), **changes}
            controller = self.controller(state)
            controller.report = {**controller.report, **report}
            with self.subTest(changes=changes, report=report), tempfile.TemporaryDirectory() as root, \
                    patch.object(staging, 'STATE', Path(root)):
                (Path(root)/'deployment.json').write_text(json.dumps(state))
                (Path(root)/'image.env').write_text('TRANSCRIPTION_IMAGE='+state['current']+'\n')
                with self.assertRaises(staging.Refused):
                    staging.operate(controller, 'diagnose-create', '', state)
                self.assertNotIn('create', controller.commands)

    def test_create_diagnostic_needs_no_job_token_or_registry_directory(self):
        with tempfile.TemporaryDirectory() as root, patch.object(staging, 'STATE', Path(root)), \
                patch.dict(staging.os.environ, {}, clear=True):
            with staging.registry_config('diagnose-create') as config:
                self.assertTrue(Path(config).is_dir())
            self.assertFalse(Path(config).exists())
            self.assertFalse((Path(root)/'registry').exists())

    def test_existing_container_or_wrong_pin_refuses_without_create(self):
        for existing in (False, True):
            state = self.state()
            controller = self.controller(state)
            controller.created = existing
            with self.subTest(existing=existing), tempfile.TemporaryDirectory() as root, \
                    patch.object(staging,'STATE',Path(root)):
                (Path(root)/'deployment.json').write_text(json.dumps(state))
                image = state['current'] if existing else staging.image_ref('sha256:'+'f'*64)
                (Path(root)/'image.env').write_text('TRANSCRIPTION_IMAGE='+image+'\n')
                with self.assertRaises(staging.Refused):
                    staging.operate(controller,'diagnose-create','',state)
                self.assertNotIn('create', controller.commands)
                self.assertFalse(list(Path(root).glob('create-diagnostic-*')))

    def test_unexpected_running_result_is_never_promoted_or_stopped(self):
        state = self.state()
        controller = self.controller(state)
        normal = controller.docker
        controller.docker = lambda *args,**kwargs: (normal(*args,**kwargs)
            if args[0] == 'network' else json.dumps({
                'status':'running','running':True,'pid':123,'image':state['current']}))
        with tempfile.TemporaryDirectory() as root, patch.object(staging,'STATE',Path(root)):
            path = Path(root)/'deployment.json'
            path.write_text(json.dumps(state))
            before = path.read_bytes()
            (Path(root)/'image.env').write_text('TRANSCRIPTION_IMAGE='+state['current']+'\n')
            with self.assertRaisesRegex(staging.Refused,'create_diagnostic_not_stopped'):
                staging.operate(controller,'diagnose-create','',state)
            self.assertEqual(path.read_bytes(),before)
            self.assertNotIn('stop',controller.commands)
            self.assertTrue(controller.report['maintenance'])

    def test_existing_deploy_gate_still_refuses_failed_first_deployment(self):
        state = self.state()
        controller = self.controller(state)
        with self.assertRaisesRegex(staging.Refused,'unfinished_deployment_requires_review'):
            staging.operate(controller,'deploy','sha256:'+'a'*64,state)
        with self.assertRaisesRegex(staging.Refused,'rollback_target_unavailable'):
            staging.operate(controller,'rollback','',state)
        self.assertEqual(controller.commands,[])

    def test_audit_directory_sync_failure_refuses_before_real_create(self):
        state = self.state()
        controller = self.controller(state)
        fsync = os.fsync
        def fail_directory(fd):
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                raise OSError('synthetic directory sync failure')
            return fsync(fd)
        with tempfile.TemporaryDirectory() as root, patch.object(staging,'STATE',Path(root)):
            (Path(root)/'deployment.json').write_text(json.dumps(state))
            (Path(root)/'image.env').write_text('TRANSCRIPTION_IMAGE='+state['current']+'\n')
            with patch.object(staging.os,'fsync',side_effect=fail_directory), self.assertRaises(OSError):
                staging.operate(controller,'diagnose-create','',state)
            self.assertNotIn('create',controller.commands)
