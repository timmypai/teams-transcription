"""Fixed CLI consumer seam; real disposable children, no Docker/Oracle/network.

TARGET_BUILD_SOURCE is used only by the immutable RED proof harness.
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

if os.environ.get('TARGET_BUILD_SOURCE'):
    for name in ('target_diagnostics','target_deploy'):
        spec=importlib.util.spec_from_file_location('deploy.'+name,Path(os.environ['TARGET_BUILD_SOURCE'])/(name+'.py'))
        assert spec and spec.loader
        module=importlib.util.module_from_spec(spec);sys.modules['deploy.'+name]=module;spec.loader.exec_module(module)
    td=sys.modules['deploy.target_deploy']
else:
    from deploy import target_deploy as td

ROOT=Path(__file__).resolve().parents[1]
SHA='a'*40
PRIVATE='SYNTHETIC-PRIVATE-person@example.invalid-https://private.invalid/token'
HELP='''Usage:  docker buildx build [OPTIONS] PATH | URL | -

Start a build

Aliases:
  docker buildx build, docker buildx b

Options:
      --build-context stringArray   Additional build contexts (e.g., name=path)
      --iidfile string              Write the image ID to a file
      --load                        Shorthand for "--output=type=docker"
      --provenance string           Shorthand for "--attest=type=provenance"
'''
VERSION='github.com/docker/buildx v0.30.0 abc1234\n'
WITNESSES=0
SIGNATURES=[
 ("docker: 'buildx' is not a docker command.\n",'buildx_unavailable','unknown','unknown_stop'),
 ('unknown flag: --provenance\n','build_option_unsupported','provenance','source_compatibility_review'),
 ('unknown flag: --build-context\n','named_context_unsupported','build_context','source_compatibility_review'),
 ('ERROR: Cannot connect to the Docker daemon. Is the docker daemon running?\n','daemon_unreachable','unknown','host_capability_review'),
 ('ERROR: permission denied while trying to connect to the Docker daemon socket\n','daemon_permission_denied','unknown','host_capability_review'),
 ('ERROR: failed to build: no space left on device\n','storage_exhausted','unknown','host_capability_review'),
 ('ERROR: failed to build: cannot allocate memory\n','resource_exhausted','unknown','host_capability_review'),
 ('ERROR: failed to solve: failed to fetch anonymous token: unexpected status: 503 Service Unavailable\n','public_dependency_fetch_failed','unknown','public_build_review'),
 ('ERROR: failed to solve: process "/bin/sh -c exit 1" did not complete successfully: exit code: 1\n','build_step_failed','unknown','public_build_review'),
]


def exercise(case,*,capability='supported',status=125,secondary=None,mode='normal',phase='image_build'):
    """Whole main/artifact/stdout path with the controller's original command DI."""
    with tempfile.TemporaryDirectory() as directory:
        folder=Path(directory);source=folder/'source';source.mkdir()
        (source/'Dockerfile').write_text((ROOT/'Dockerfile').read_text())
        script=folder/'child.py';witness=folder/'started'
        script.write_text('import os,sys,time\nfrom pathlib import Path\n'
            'Path(sys.argv[1]).write_text("started")\n'
            f'payload={case!r}\n'
            f'mode={mode!r}\n'
            'if mode=="timeout": time.sleep(30)\n'
            'elif mode=="flood":\n'
            ' for _ in range(1100): os.write(1,b"x"*8192)\n'
            'elif mode=="split":\n'
            ' for c in payload: os.write(1,bytes([c])); time.sleep(.001)\n'
            'elif mode=="streams":\n'
            ' os.write(1,b"unknown flag: --"); os.write(2,b"provenance\\n")\n'
            'else: os.write(1,payload)\n'
            f'os.write(2,{(PRIVATE+chr(10)).encode()!r})\n'
            f'sys.exit({status})\n')
        calls=[];controller=None
        def command(args,**kw):
            calls.append(args)
            if args[-2:]==['buildx','version'] or args[-3:]==['buildx','build','--help']:
                help_probe=args[-1]=='--help'
                payload=HELP if help_probe else VERSION
                exit_status=0
                if capability=='missing':payload="docker: 'buildx' is not a docker command.\n";exit_status=1
                if capability=='unknown':payload=PRIVATE+'\n'
                if capability=='partial':payload=HELP.replace('      --provenance string           Shorthand for "--attest=type=provenance"\n','') if help_probe else VERSION
                if capability=='version_bad' and not help_probe:payload='buildx version '+PRIVATE+'\n'
                if capability=='missing_contradiction':payload=VERSION+"docker: 'buildx' is not a docker command.\n";exit_status=125
                if capability=='help_bad' and help_probe:payload=HELP+'unexpected footer\n'
                if capability=='help_tail' and help_probe:payload=HELP.rstrip('\n')
                if capability=='help_control' and help_probe:payload=HELP+'\x1b[31m\n'
                if capability=='help_overlong' and help_probe:payload=HELP+' '*2049+'\n'
                if capability=='help_chunked' and help_probe:payload=HELP+('      --synthetic string    Public fixture option\n'*200)
                if capability=='official_footer' and help_probe:
                    payload=HELP+'\nExperimental commands and flags are hidden. Set BUILDX_EXPERIMENTAL=1 to show them.\n'
                raw=payload.encode()
                if capability=='help_nonutf8' and help_probe:raw+=b'\xff\n'
                probe=folder/'probe.py';probe.write_text(f'import os,sys\nos.write(1,{raw!r})\nsys.exit({exit_status})\n')
                return td.execute([sys.executable,str(probe)],**kw)
            if ('run' in args if phase=='prepare_contexts' else 'buildx' in args):
                kw['timeout']=.5 if mode=='timeout' else 10
                result=td.execute([sys.executable,str(script),str(witness)],**kw)
                (folder/'.devflow-live/timmypai-teams-transcription/contexts-123-2/image-id').write_text('sha256:'+'b'*64)
                return result
            if 'inspect' in args:return json.dumps([dict(Config=dict(Labels={'org.opencontainers.image.revision':SHA}))])
            return ''
        original=td.Controller
        def factory(*args,**kw):
            nonlocal controller
            controller=original(*args,command=command,**kw)
            controller.preflight=lambda state:[]
            controller.containers=lambda: (_ for _ in ()).throw(ValueError(PRIVATE)) if secondary=='snapshot' else []
            controller.switch=lambda *args:dict(health=True)
            write=controller.write
            def checked_write(path,body):
                if secondary=='audit' and path.name.startswith('audit-'):raise ValueError(PRIVATE)
                return write(path,body)
            controller.write=checked_write
            return controller
        out=io.StringIO();err=io.StringIO()
        write_text=Path.write_text
        def evidence_write(path,*args,**kw):
            if secondary=='diagnostic_write' and path.name=='failure.json':raise OSError(PRIVATE)
            return write_text(path,*args,**kw)
        with patch.object(td,'ROOT',source),patch.object(td,'manual_environment',return_value=SHA),\
             patch.object(td,'GitHub',return_value=object()),patch.object(td,'collect',return_value=dict(source_sha=SHA)),\
             patch.object(td.Path,'home',return_value=folder),patch.object(td,'Controller',side_effect=factory),\
             patch.object(Path,'write_text',evidence_write),\
             patch.dict(os.environ,dict(GITHUB_RUN_ID='123',GITHUB_RUN_ATTEMPT='2',DEVFLOW_APP_ENV=PRIVATE)),\
             patch.object(sys,'argv',['target_deploy.py','deploy']),\
             contextlib.redirect_stdout(out),contextlib.redirect_stderr(err):
            result=td.main()
        files={p.name:p.read_bytes() for p in (source/'.devflow/evidence-deploy-123-2').glob('*')}
        combined=b''.join(files.values())+out.getvalue().encode()+err.getvalue().encode()
        for marker in (b'SYNTHETIC-PRIVATE',b'person@example',b'private.invalid',b'\x1b',b'\xff'):
            if marker in combined:raise AssertionError('private marker escaped complete artifact/stdout/stderr')
        global WITNESSES
        if witness.exists():WITNESSES+=1
        return result,json.loads(files.get('failure.json',out.getvalue().encode())),calls,witness.exists(),files


class BuildEvidenceRedTests(unittest.TestCase):
    def test_distinct_failures_reach_real_child_and_publish_fixed_consumer_routes(self):
        for text,classification,option,action in SIGNATURES:
            with self.subTest(classification=classification):
                phase='prepare_contexts' if classification in {'build_option_unsupported','named_context_unsupported'} else 'image_build'
                status,report,calls,started,files=exercise(text.encode(),phase=phase)
                self.assertTrue(started,'child-start witness required before RED assertion')
                self.assertEqual(status,1)
                self.assertEqual(report['schema_version'],2,'schema_version_uncollected')
                from deploy.build_consumer import consume
                self.assertEqual(consume(report)['next_action'],action)
                self.assertIn(dict(classification=classification,option=option),report['build_diagnostics']['classifications'])
                self.assertEqual(report['primary_failure']['exit_status'],125)
                self.assertEqual(report['cleanup'],'unknown')
                self.assertNotIn('receipt.json',files)


class BuildEvidenceBoundaryTests(unittest.TestCase):
    def test_official_buildx_additional_help_footer_is_complete_known_grammar(self):
        # docker/buildx v0.30.0 commands/root.go experimentalCommandHint; no
        # runner raw help captured. The exact official sentence is grammar, not
        # an instruction to enable experimental flags.
        status,doc,_,started,_=exercise(b'',capability='official_footer',status=0)
        self.assertTrue(started,'official_help_grammar_rejected')
        self.assertEqual(status,0)
        self.assertEqual(doc['build_diagnostics']['capabilities'][1]['state'],'available')

    def test_capabilities_missing_unknown_and_partial_stop_before_any_context_or_build(self):
        from deploy.build_consumer import consume
        for capability,code,action in [('missing','capability_missing','host_capability_review'),
            ('unknown','capability_unknown','unknown_stop'),('partial','capability_unknown','unknown_stop')]:
            with self.subTest(capability=capability):
                status,doc,calls,started,files=exercise(b'',capability=capability)
                self.assertFalse(started);self.assertEqual(status,1)
                self.assertEqual(doc['primary_failure'],dict(phase='build_preflight',code=code,exit_status=None))
                self.assertEqual(doc['cleanup'],'not_required')
                self.assertEqual(consume(doc)['next_action'],action)
                self.assertEqual(len(calls),2)
                self.assertEqual(calls[0][:-2],calls[1][:-3])
                audit=json.loads(files['audit.json'])
                self.assertFalse(any(row['action'] in {'run','build','create','initialize','up','stop','exec'} for row in audit['operations']))

    def test_success_retains_capabilities_without_turning_errorlike_text_into_failure(self):
        from deploy.build_consumer import consume
        status,doc,calls,started,files=exercise(SIGNATURES[3][0].encode(),status=0)
        self.assertTrue(started);self.assertEqual(status,0);self.assertIsNone(doc['primary_failure'])
        self.assertEqual(doc['build_diagnostics']['classifications'],[])
        self.assertIn('receipt.json',files)
        self.assertEqual([row['state'] for row in doc['build_diagnostics']['capabilities']],['available','available'])
        self.assertTrue(consume(doc)['valid']);self.assertEqual(consume(doc)['next_action'],'unknown_stop')

    def test_probe_grammar_invalid_incomplete_contradictory_never_claims_missing_or_supported(self):
        from deploy.build_consumer import consume
        for capability in ('version_bad','missing_contradiction','help_bad','help_tail','help_control','help_overlong','help_nonutf8'):
            status,doc,calls,started,_=exercise(b'',capability=capability)
            self.assertEqual(status,1);self.assertFalse(started);self.assertEqual(len(calls),2)
            self.assertEqual(doc['primary_failure']['code'],'capability_unknown')
            self.assertEqual(consume(doc)['next_action'],'unknown_stop')
        status,doc,_,started,_=exercise(b'',capability='help_chunked',status=0)
        self.assertTrue(started);self.assertEqual(status,0)
        self.assertEqual(doc['build_diagnostics']['capabilities'][1]['state'],'available')

    def test_successful_help_then_incompatible_same_image_command_is_contradiction_stop(self):
        from deploy.build_consumer import consume
        for option in ('load','provenance','build-context','iidfile'):
            _,doc,_,started,_=exercise(('unknown flag: --'+option+'\n').encode())
            self.assertTrue(started);self.assertEqual(consume(doc)['next_action'],'unknown_stop')

    def test_success_diagnostic_write_failure_cannot_leave_healthy_receipt(self):
        status,doc,_,started,files=exercise(b'',status=0,secondary='diagnostic_write')
        self.assertTrue(started);self.assertEqual(status,1)
        self.assertNotIn('receipt.json',files)
        self.assertEqual(doc['primary_failure']['code'],'unknown')
        self.assertEqual(doc['secondary_failures'][0]['code'],'evidence_write_failed')

    def test_complete_lines_cross_chunks_but_never_cross_streams(self):
        from deploy.build_consumer import consume
        for mode,expected in [('split','source_compatibility_review'),('streams','unknown_stop')]:
            _,doc,_,started,_=exercise(b'unknown flag: --provenance\n',mode=mode,phase='prepare_contexts')
            self.assertTrue(started);self.assertEqual(consume(doc)['next_action'],expected)

    def test_invalid_utf8_control_tail_overlong_and_line_budget_are_unknown(self):
        from deploy.build_consumer import consume
        for payload in [b'unknown flag: --load',b'unknown flag: --load\x1b\n',b'unknown flag: --load\xff\n',
            b'x'*2049+b'unknown flag: --load\n',b'\n'*1024+b'unknown flag: --load\n',
            b'prefix unknown flag: --load\n',b'unknown flag: --load suffix\n',
            'unknown flag: --load\u0085\n'.encode(),PRIVATE.encode()+b'\n']:
            _,doc,_,started,_=exercise(payload)
            self.assertTrue(started);self.assertEqual(doc['build_diagnostics']['classifications'],[])
            self.assertEqual(consume(doc)['next_action'],'unknown_stop')

    def test_timeout_flood_preserve_primary_and_unknown_cleanup(self):
        from deploy.build_consumer import consume
        for mode,code in [('timeout','command_timeout'),('flood','output_limit')]:
            _,doc,_,started,_=exercise(b'',mode=mode)
            self.assertTrue(started);self.assertEqual(doc['primary_failure']['code'],code)
            self.assertEqual(doc['primary_failure']['exit_status'],None)
            self.assertEqual(doc['cleanup'],'unknown')
            self.assertEqual(consume(doc)['next_action'],'unknown_stop')

    def test_classification_bounds_dedup_and_conflict_stop(self):
        from deploy.build_consumer import consume
        for lines,count in [(SIGNATURES[3][0]*20,1),(''.join(row[0] for row in SIGNATURES),8),
            (SIGNATURES[3][0]+SIGNATURES[4][0],2)]:
            _,doc,_,_,_=exercise(lines.encode())
            self.assertEqual(len(doc['build_diagnostics']['classifications']),count)
            if count!=1:self.assertEqual(consume(doc)['next_action'],'unknown_stop')

    def test_audit_secondary_does_not_replace_primary_or_route_partial_evidence(self):
        from deploy.build_consumer import consume
        for secondary in ('audit','snapshot'):
            _,doc,_,started,_=exercise(SIGNATURES[3][0].encode(),secondary=secondary)
            self.assertTrue(started);self.assertEqual(doc['primary_failure']['exit_status'],125)
            self.assertEqual(doc['secondary_failures'][0]['phase'],'audit')
            self.assertEqual(doc['cleanup'],'unknown');self.assertEqual(consume(doc)['next_action'],'unknown_stop')

    def test_full_schema_rejects_unknown_fields_types_limits_enums_and_contradictions(self):
        from copy import deepcopy
        from deploy.build_consumer import consume
        _,good,_,_,_=exercise(SIGNATURES[3][0].encode())
        mutations=[lambda d:d.update(schema_version=True),lambda d:d.update(repository='other/repo'),
            lambda d:d.update(run_id=True),lambda d:d.update(run_attempt=2**31),lambda d:d.update(source_sha='notsha'),
            lambda d:d.update(action='retry'),lambda d:d.update(cleanup='cleaned'),lambda d:d.update(raw=PRIVATE),
            lambda d:d['primary_failure'].update(exit_status=256),lambda d:d['primary_failure'].update(code=PRIVATE),
            lambda d:d['primary_failure'].pop('output'),
            lambda d:d['primary_failure']['output'].update(bytes=8388609),lambda d:d['primary_failure']['output'].update(events=1),
            lambda d:d['primary_failure']['output'].update(truncated=1),lambda d:d.update(secondary_failures=[d['primary_failure']]*9),
            lambda d:d['build_diagnostics'].update(classifications=[{'classification':'unknown','option':'unknown'}]*9),
            lambda d:d['build_diagnostics']['classifications'][0].update(option='load'),
            lambda d:d['build_diagnostics']['capabilities'][0].update(state='available',exit_status=125),
            lambda d:d['build_diagnostics']['required_options'].update(load='unsupported'),
            lambda d:d['build_diagnostics']['capabilities'].reverse(),
            lambda d:d['build_diagnostics'].update(projection_incomplete='false')]
        for mutate in mutations:
            d=deepcopy(good);mutate(d)
            self.assertEqual(consume(d),dict(valid=False,evidence_schema=None,capabilities_collected=False,next_action='unknown_stop'))
        d=deepcopy(good);d['build_diagnostics']['projection_incomplete']=True
        self.assertEqual(consume(d)['next_action'],'unknown_stop')
        d=deepcopy(good);d['primary_failure']['code']='unknown'
        self.assertEqual(consume(d)['next_action'],'unknown_stop')
        legacy=deepcopy(good);legacy['schema_version']=1;del legacy['build_diagnostics']
        original=deepcopy(legacy);result=consume(legacy)
        self.assertTrue(result['valid']);self.assertFalse(result['capabilities_collected'])
        self.assertEqual(result['next_action'],'unknown_stop');self.assertEqual(legacy,original)

    def test_readonly_consumer_cli_rejects_bad_json_duplicates_and_oversize_without_text(self):
        import subprocess
        for raw in [b'{"schema_version":2,"schema_version":1}',b'null',b'\xff',PRIVATE.encode(),b'x'*65537]:
            with tempfile.TemporaryDirectory() as folder:
                file=Path(folder)/'failure.json';file.write_bytes(raw)
                result=subprocess.run([sys.executable,'-m','deploy.build_consumer',str(file)],cwd=ROOT,capture_output=True)
                self.assertEqual(result.returncode,1);self.assertEqual(result.stderr,b'')
                self.assertEqual(json.loads(result.stdout)['next_action'],'unknown_stop')
                self.assertNotIn(b'SYNTHETIC',result.stdout)


if __name__=='__main__':unittest.main()
