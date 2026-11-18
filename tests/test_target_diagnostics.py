"""Deployment CLI evidence with disposable real children, never Docker/Oracle.

TARGET_DIAGNOSTICS_SOURCE loads the immutable failed adapter for RED, while the
same test/harness stays on head. No missing-module/import failure is a RED proof.
"""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy import target_deploy as candidate

ROOT=Path(__file__).resolve().parents[1]
if os.environ.get('TARGET_DIAGNOSTICS_SOURCE'):
    spec=importlib.util.spec_from_file_location('immutable_failed_adapter',os.environ['TARGET_DIAGNOSTICS_SOURCE'])
    assert spec is not None and spec.loader is not None
    td=importlib.util.module_from_spec(spec);spec.loader.exec_module(td)
    td.ROOT=ROOT
else:
    td=candidate

SHA='a'*40
MARKER='SYNTHETIC-PRIVATE-password-person@example.invalid-https://private.invalid/\x1b[31m'


class TargetDiagnosticsTests(unittest.TestCase):
    def run_failure(self,phase='image_build',mode='failure',secondary=None):
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder);source=folder/'source';source.mkdir()
            (source/'Dockerfile').write_text((ROOT/'Dockerfile').read_text())
            child=folder/'child.py'
            child.write_text('import os,sys,time\n'
                'from pathlib import Path\nPath(sys.argv[2]).write_text("started")\n'
                'mode=sys.argv[1]\n'
                f'payload={MARKER.encode()!r}\n'
                'if mode=="flood": payload+=b"\\xff\\x00"\n'
                'if mode=="flood":\n'
                ' for _ in range(4096): os.write(1,payload*64);os.write(2,payload*64)\n'
                'elif mode=="timeout":\n'
                ' os.write(1,payload);os.write(2,payload);time.sleep(30)\n'
                'else:\n'
                ' os.write(1,payload);os.write(2,payload);sys.exit(17)\n')
            controller=None
            def command(args,**kw):
                if kw.get('public_probe'):
                    from test_build_evidence import HELP,VERSION
                    probe=folder/'probe.py'
                    probe.write_text('import sys\nsys.stdout.write('+repr(HELP if args[-1]=='--help' else VERSION)+')\n')
                    return td.execute([sys.executable,str(probe)],**kw)
                is_prepare='run' in args
                if (phase=='prepare_contexts')==is_prepare:
                    kw['timeout']=2 if mode=='timeout' else 5
                    return td.execute([sys.executable,str(child),mode,str(folder/'started')],**kw)
                return ''
            original=td.Controller
            def factory(*args,**kw):
                nonlocal controller
                controller=original(*args,command=command,**kw)
                controller.preflight=lambda state:[]
                controller.containers=lambda: (_ for _ in ()).throw(ValueError(MARKER)) if secondary=='snapshot' else []
                write=controller.write
                def checked_write(path,body):
                    if secondary=='audit' and path.name.startswith('audit-'):raise ValueError(MARKER)
                    return write(path,body)
                controller.write=checked_write
                return controller
            env={'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'2','DEVFLOW_APP_ENV':MARKER}
            stdout=io.StringIO();stderr=io.StringIO()
            with patch.object(td,'ROOT',source),patch.object(td,'GitHub',return_value=object()),patch.object(td,'manual_environment',return_value=SHA),\
                 patch.object(td,'collect',return_value={'source_sha':SHA}),\
                 patch.object(td.Path,'home',return_value=folder),patch.object(td,'Controller',side_effect=factory),\
                 patch.dict(os.environ,env),patch.object(sys,'argv',['target_deploy.py','deploy']),\
                 contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                status=td.main()
            self.assertEqual((folder/'started').read_text(),'started','real child must reach the failure boundary')
            evidence=source/'.devflow/evidence-deploy-123-2'
            payloads={p.name:p.read_bytes() for p in evidence.glob('*')}
            all_output=b''.join(payloads.values())+stdout.getvalue().encode()+stderr.getvalue().encode()
            for private in (b'SYNTHETIC-PRIVATE',b'person@example',b'private.invalid',b'\x1b',b'\xff'):
                self.assertNotIn(private,all_output)
            self.assertEqual(status,1)
            self.assertNotIn('receipt.json',payloads)
            self.assertIn('failure.json',payloads,'failed CLI must publish phase/exit evidence, not just generic rejection')
            report=json.loads(payloads['failure.json'])
            self.assertEqual(report['schema_version'],2)
            self.assertEqual({k:report[k] for k in ('repository','source_sha','run_id','run_attempt','action')},
                dict(repository=td.REPO,source_sha=SHA,run_id=123,run_attempt=2,action='deploy'))
            self.assertEqual(report['primary_failure']['phase'],phase)
            self.assertEqual(report['primary_failure']['code'],{'failure':'command_failed','timeout':'command_timeout','flood':'output_limit'}[mode])
            self.assertEqual(report['primary_failure']['exit_status'],17 if mode=='failure' else None)
            self.assertEqual(report['cleanup'],'unknown') # CLI exit cannot prove daemon builder cleanup.
            if secondary:
                self.assertTrue(any(x['phase']=='audit' for x in report['secondary_failures']))
            return report

    def test_failed_image_build_keeps_phase_exit_and_discards_all_untrusted_text(self):
        self.run_failure()

    def test_prepare_failure_is_distinct(self):
        self.run_failure(phase='prepare_contexts')

    def test_real_timeout_has_no_guessed_exit_or_private_text(self):
        self.run_failure(mode='timeout')

    def test_overlong_non_utf8_flood_is_bounded_and_still_nonzero(self):
        report=self.run_failure(mode='flood')
        output=report['primary_failure']['output']
        self.assertLessEqual(output['bytes'],8388608)
        self.assertLessEqual(output['lines'],1024)
        self.assertLessEqual(output['max_line_bytes'],2048)
        self.assertEqual(output['events'],0)
        self.assertTrue(output['truncated'])

    def test_snapshot_failure_does_not_replace_primary(self):
        self.run_failure(secondary='snapshot')

    def test_audit_write_failure_does_not_replace_primary(self):
        self.run_failure(secondary='audit')


class DiagnosticBoundaryTests(unittest.TestCase):
    def test_unknown_admission_failure_has_public_evidence_without_private_state(self):
        with tempfile.TemporaryDirectory() as folder:
            stdout=io.StringIO();stderr=io.StringIO()
            with patch.object(candidate,'ROOT',Path(folder)),\
                 patch.object(candidate,'manual_environment',side_effect=ValueError(MARKER)),\
                 patch.object(candidate.Path,'home',side_effect=AssertionError('no private state before admission')),\
                 patch.dict(os.environ,dict(GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='2')),\
                 patch.object(sys,'argv',['target_deploy.py','deploy']),\
                 contextlib.redirect_stdout(stdout),contextlib.redirect_stderr(stderr):
                self.assertEqual(candidate.main(),1)
            evidence=Path(folder)/'.devflow/evidence-deploy-123-2'
            document=json.loads((evidence/'failure.json').read_text())
            self.assertEqual(document['primary_failure'],dict(phase='unknown',code='unknown',exit_status=None))
            self.assertNotIn('SYNTHETIC',(evidence/'failure.json').read_text()+stdout.getvalue()+stderr.getvalue())
            self.assertFalse((evidence/'receipt.json').exists())

    def test_success_unknown_lines_and_long_line_are_discarded_not_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'public.py'
            script.write_text('import os\nos.write(1,b"x"*4096+b"\\n"*2048)\nos.write(2,b"\\xff\\x00private-marker")\n')
            self.assertEqual(candidate.execute([sys.executable,str(script)],public_phase='image_build'), '')

    def test_environment_stdin_and_spawn_failure_cannot_leak(self):
        from deploy.target_diagnostics import PublicCommandFailure
        with tempfile.TemporaryDirectory() as folder:
            script=Path(folder)/'public.py'
            script.write_text('import os,sys\nassert not any(k in os.environ for k in ("DEVFLOW_APP_ENV","GH_TOKEN","GITHUB_TOKEN"))\nassert not sys.stdin.read()\n')
            with patch.dict(os.environ,dict(DEVFLOW_APP_ENV=MARKER,GH_TOKEN=MARKER,GITHUB_TOKEN=MARKER)):
                self.assertEqual(candidate.execute([sys.executable,str(script)],public_phase='prepare_contexts'),'')
            with self.assertRaises(PublicCommandFailure) as failure:
                candidate.execute([str(Path(folder)/'missing')],public_phase='image_build')
            self.assertEqual(failure.exception.code,'command_start_failed')
            self.assertNotIn(folder,str(failure.exception))
            with self.assertRaisesRegex(ValueError,'public_stdin_rejected'):
                candidate.execute([sys.executable,str(script)],public_phase='image_build',input=MARKER)

    def test_unknown_and_runtime_codes_and_secondary_bounds(self):
        from deploy.target_diagnostics import Failures,failure,bounded_exit
        f=Failures()
        f.record(ValueError(MARKER))
        for _ in range(20):f.record(ValueError(MARKER),'cleanup','cleanup_failed')
        document=f.document(td.REPO,SHA,123,2,'deploy')
        self.assertNotIn('SYNTHETIC',json.dumps(document))
        self.assertEqual(document['primary_failure'],dict(phase='unknown',code='unknown',exit_status=None))
        self.assertEqual(len(document['secondary_failures']),8)
        self.assertEqual(failure(ValueError('target_runtime_rejected:daemon_identity_mismatch'))['code'],
            'target_runtime_rejected:daemon_identity_mismatch')
        for value in (True,'17',256,-256,None):self.assertIsNone(bounded_exit(value))

    def test_capture_then_stop_failure_keeps_original_and_never_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            c=candidate.Controller(folder,1,1)
            release=Path(folder)/'new';calls=[]
            primary=ValueError(MARKER)
            def runtime(action,**kw):
                calls.append(action)
                if action=='enable':return dict(maintenance=True,schema='same',config_sha256='same')
                if action=='backup':return dict(schema='same')
                return {}
            def compose(where,*args):
                calls.append((where,*args))
                if args[0]=='up':raise primary
                if where==release and args[0]=='stop':raise ValueError(MARKER)
            c.runtime=runtime;c.compose=compose;c.write=lambda *a:None
            c.create_release=lambda *a:release;c.verify_release=lambda *a:{}
            c.capture=lambda *a:(_ for _ in ()).throw(ValueError(MARKER))
            with self.assertRaises(ValueError) as caught:
                c.switch(dict(source_sha=SHA),'sha256:'+'b'*64,dict(release='old',image='sha256:'+'c'*64))
            self.assertIs(caught.exception,primary)
            self.assertEqual(c.failures.cleanup,'unknown')
            self.assertEqual([row['phase'] for row in c.failures.secondary],['audit','cleanup'])
            self.assertNotIn('disable',calls)
            self.assertFalse(any(isinstance(row,tuple) and row[0]!=release and row[1]=='up' for row in calls))

    def test_build_success_preserves_exact_image_and_public_phase_routing(self):
        with tempfile.TemporaryDirectory() as folder:
            calls=[];image='sha256:'+'b'*64
            def command(args,**kw):
                calls.append(kw.get('public_phase'))
                if kw.get('public_probe'):
                    from test_build_evidence import HELP,VERSION
                    script=Path(folder)/'probe.py'
                    script.write_text('import sys\nsys.stdout.write('+repr(HELP if args[-1]=='--help' else VERSION)+')\n')
                    return candidate.execute([sys.executable,str(script)],**kw)
                if 'buildx' in args:
                    (Path(folder)/'contexts-1-1/image-id').write_text(image)
                if 'inspect' in args:return json.dumps([dict(Config=dict(Labels={'org.opencontainers.image.revision':SHA}))])
                return ''
            c=candidate.Controller(folder,1,1,command=command)
            self.assertEqual(c.build(SHA),image)
            self.assertEqual(calls,['build_preflight','build_preflight','prepare_contexts','image_build',None])
            self.assertIsNone(c.failures.primary)
