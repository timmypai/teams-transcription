"""Real Docker + installed application + sandboxed browser on disposable hosted CI.

Never runnable against Oracle: no VM adapter, host credentials, published port,
network access in the app, shared volume, host path data mount or Docker socket
mount. The audit is bounded to this harness; not a whole-host filesystem proof.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'deploy'), str(ROOT/'tools'), str(ROOT/'.devflow')]
from isolated_deployment import OperationAudit, admit_candidate, SOURCE_SHA, REPOSITORY
from check_compose import example_values, render, write_env


def run(args, *, input=None, timeout=120, check=True):
    result = subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        # Project-owned disposable fixtures only. Persist fixed error classes,
        # never raw commands, stderr, configuration or credential material.
        reason = next((code for needle, code in [
            ('read-only', 'readonly_copy_refused'), ('permission denied','permission_denied'),
            ('no space left','scratch_space_exhausted'), ('no such file','fixture_missing')
        ] if needle in result.stderr.lower()), 'command_rejected')
        raise RuntimeError('hosted_' + reason)
    return result


class Harness:
    def __init__(self, image, sha, evidence, scratch):
        self.image, self.sha = image, sha
        self.evidence, self.scratch = evidence, scratch
        self.project = 'teams-transcription-ci-' + uuid.uuid4().hex[:12]
        self.app = self.project + '-app'
        self.foreign = self.project + '-foreign-sentinel'
        self.volume = self.project + '-data'
        self.compose_file = scratch/'compose.json'
        self.audit = OperationAudit([
            (a, k, n) for k, n, actions in [
                ('container', self.app, ['create', 'write', 'stop', 'delete']),
                ('container', self.foreign, ['create', 'delete']),
                ('volume', self.volume, ['create', 'delete']),
                ('directory', str(scratch), ['write']),
                ('directory', str(evidence), ['write']),
            ] for a in actions
        ])
        self.commands = {}
        self.specs = json.loads((ROOT/'.devflow/scenarios.json').read_text())
        self.baseline = os.environ.get('DEVFLOW_BASELINE') == '1'
        self.problems = []
        self.receipt = None
        self.isolation = None

    def docker(self, *parts, **kw):
        return run(['docker', '--host=unix:///var/run/docker.sock', *parts], **kw)

    def mutate(self, action, kind, target, fn):
        self.audit.execute([(action, kind, target)], lambda *_: fn())

    def write(self, path, content):
        parent = self.evidence if path.is_relative_to(self.evidence) else self.scratch
        self.mutate('write', 'directory', str(parent), lambda: path.write_text(content))

    def inspect(self, name):
        d = json.loads(self.docker('inspect', name).stdout)[0]
        return dict(id=d['Id'], started_at=d['State']['StartedAt'],
                    status=d['State']['Status'], health=d['State'].get('Health', {}).get('Status'),
                    image=d['Image'])

    def health(self):
        self.docker('exec', self.app, 'teams-transcription', 'healthcheck', '--port', '18080', timeout=10)
        state = self.inspect(self.app)
        assert state['status'] == 'running'
        assert state['image'] == self.image
        labels = json.loads(self.docker('image', 'inspect', self.image).stdout)[0]['Config']['Labels']
        assert labels['org.opencontainers.image.revision'] == self.sha
        return state

    def snapshot(self):
        return dict(service=self.inspect(self.foreign),
                    file_sha256=hashlib.sha256((self.scratch/'foreign-file').read_bytes()).hexdigest(),
                    database_sha256=hashlib.sha256((self.scratch/'foreign.sqlite3').read_bytes()).hexdigest())

    def event(self, sid, phase, status):
        spec = self.specs[sid]
        event = dict(id=sid, runner='shell', phase=phase, title=spec[phase],
                     status=status, smoke=spec['smoke'], error='' if status!='failed' else 'hosted_scenario_assertion_failed')
        path = self.evidence/'scenario-events.jsonl'
        self.mutate('write', 'directory', str(self.evidence),
                    lambda: path.open('a').write(json.dumps(event, ensure_ascii=False)+'\n'))

    def scenario(self, ac, n, given, when, then):
        sid = f't_a3f4f673/AC-{ac}.S{n}'
        failed = False
        for phase, fn in [('given', given), ('when', when), ('then', then)]:
            if failed:
                self.event(sid, phase, 'not-run')
                continue
            try:
                fn()
            except Exception:
                self.event(sid, phase, 'failed')
                failed = True
            else:
                self.event(sid, phase, 'passed')
        if failed:
            self.problems.append(sid)

    def prepare(self):
        # Preflight rejects collisions; never adopts existing same-named resources.
        for name in [self.app, self.foreign]:
            assert self.docker('container', 'inspect', name, check=False).returncode != 0
        assert self.docker('volume', 'inspect', self.volume, check=False).returncode != 0
        import sqlite3
        self.write(self.scratch/'foreign-file', 'disposable non-project file sentinel\n')
        self.mutate('write', 'directory', str(self.scratch), lambda: sqlite3.connect(self.scratch/'foreign.sqlite3').close())
        self.mutate('create', 'container', self.foreign, lambda: self.docker(
            'run', '-d', '--name', self.foreign, '--label', 'io.teams-transcription.fixture='+self.project,
            '--network', 'none', '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
            '--user', '10001:10001', '--entrypoint', 'sleep', self.image, '1800'))
        self.before = self.snapshot()
        values = example_values()
        values.update(TRANSCRIPTION_IMAGE=self.image, PUID='10001', PGID='10001',
                      APPDATA_HOST=str(self.scratch/'unused'), CONFIG_ENV_FILE=str(self.scratch/'fixture.env'),
                      ASSISTANT_PORT='18080')
        self.mutate('write', 'directory', str(self.scratch), lambda: write_env(self.scratch/'fixture.env', values))
        config = render(self.scratch/'fixture.env')
        service = config['services']['teams-transcription']
        service.update(container_name=self.app, restart='no', network_mode='none',
                       volumes=[dict(type='volume', source='data', target='/data')],
                       environment={'ASSISTANT_HOST':'127.0.0.1', 'ASSISTANT_PORT':'18080',
                                    'ASSISTANT_CHROMIUM':'/usr/bin/chromium', 'CODEX_HOME':'/tmp/empty-codex'})
        service['security_opt'] = ['seccomp:'+str(ROOT/v.split(':',1)[1]) if v.startswith('seccomp:') else v
                                   for v in service['security_opt']]
        service['labels'] = {'io.teams-transcription.fixture': self.project}
        service.pop('networks', None)
        config.pop('networks', None)
        config['volumes'] = {'data': {'external':True, 'name':self.volume}}
        config.pop('name', None)
        self.write(self.compose_file, json.dumps(config))
        self.mutate('create', 'volume', self.volume, lambda: self.docker('volume','create',
                    '--label','io.teams-transcription.fixture='+self.project, self.volume))
        self.password = secrets.token_urlsafe(32)
        # Seed only a disposable data volume. No real credentials or grant.
        seed = """from pathlib import Path
import sys,json
from teams_transcription.assistant_config import load_assistant_settings
from teams_transcription.admin_web import initialize_admin_credentials
p=Path('/data/config');p.mkdir(mode=0o700,exist_ok=True)
f=p/'runtime.yml';f.write_text(json.dumps({'log_level':'error','codex_home':'/tmp/empty-codex'}))
initialize_admin_credentials(load_assistant_settings(f),sys.stdin.read().strip())
"""
        self.mutate('create', 'container', self.app, lambda: self.compose('run','--rm','--no-deps','-T',
                    '--name',self.app, '--entrypoint','python','teams-transcription','-c',seed, input=self.password))

    def compose(self, *parts, **kw):
        return self.docker('compose','-p',self.project,'-f',str(self.compose_file),*parts,**kw)

    def deploy(self):
        self.mutate('create','container',self.app,lambda: self.compose('up','-d','--pull','never'))
        deadline = time.monotonic()+60
        while True:
            try:
                self.health()
                break
            except Exception:
                if time.monotonic()>deadline: raise RuntimeError('hosted_health_timeout') from None
                time.sleep(.5)

    def admit(self):
        # The source lineage is checked against git, not only caller-supplied strings.
        run(['git','merge-base','--is-ancestor',SOURCE_SHA,'HEAD'])
        self.receipt = admit_candidate(REPOSITORY,'main',self.sha,self.sha,SOURCE_SHA,self.image)
        self.receipt.update(scope='disposable-hosted', vm_acceptance=False,
                            source_branch='main', execution_ref=os.environ.get('GITHUB_HEAD_REF') or os.environ.get('GITHUB_REF'),
                            actions_run=f'https://github.com/{REPOSITORY}/actions/runs/{os.environ["GITHUB_RUN_ID"]}')

    def wrong_sha(self):
        self.original = self.inspect(self.app)
        self.original_operations = len(self.audit.events)
        try:
            admit_candidate(REPOSITORY,'main',self.sha,'0'*40,SOURCE_SHA,self.image)
        except ValueError as exc:
            assert str(exc)=='candidate_sha_mismatch'
        else:
            raise AssertionError('mismatched_candidate_accepted')
        assert len(self.audit.events)==self.original_operations

    def refuse_foreign(self):
        self.foreign_original = self.snapshot()
        self.original_operations = len(self.audit.events)
        try:
            self.audit.execute([('stop','container',self.app),('stop','container',self.foreign)],
                               lambda *_: (_ for _ in ()).throw(AssertionError('executor_must_not_run')))
        except ValueError as exc:
            assert str(exc)=='foreign_target_refused'
        else:
            raise AssertionError('foreign_target_accepted')
        assert len(self.audit.events)==self.original_operations

    def compare(self):
        self.isolation = self.audit.report(self.before, self.snapshot())
        assert self.isolation['outside_mutations']==0

    def ui(self):
        # Local files are copied into tmpfs. App remains nonroot, network-none,
        # read-only, drop ALL, with the unchanged Chromium seccomp policy.
        ui = self.scratch/'ui'
        self.mutate('write','directory',str(self.scratch),lambda: shutil.copytree(ROOT/'.devflow/ui',ui))
        self.write(ui/'scenarios.json',(ROOT/'.devflow/scenarios.json').read_text())
        for p in ui.rglob('*'):
            p.chmod(0o755 if p.is_dir() or p.name == 'node' else 0o644)
        ui.chmod(0o755)
        import io
        import tarfile
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode='w') as bundle:
            bundle.add(ui, arcname='devflow-ui')
        def stream_ui():
            result = subprocess.run(['docker','--host=unix:///var/run/docker.sock','exec','-i',
                                     self.app,'tar','-xf','-','-C','/tmp'],
                                    input=archive.getvalue(),capture_output=True,timeout=60)
            if result.returncode:
                raise RuntimeError('fixture_tmpfs_stream_failed')
        self.mutate('write','container',self.app,stream_ui)
        self.mutate('write','container',self.app,lambda: self.docker('exec','-i',self.app,'python','-c',
                    "import sys;from pathlib import Path;p=Path('/tmp/devflow-fixture-password');p.write_text(sys.stdin.read());p.chmod(0o600)",input=self.password))
        # Fixture files must be world-readable, never the private password.
        self.mutate('write','container',self.app,lambda: self.docker('exec',self.app,'mkdir','-p','/tmp/devflow-evidence'))
        results = []
        node = self.docker('exec',self.app,'python','-c',
                           "import pathlib,playwright;print(pathlib.Path(playwright.__file__).parent/'driver/node')").stdout.strip()
        self.write(self.evidence/'browser-runtime.json',json.dumps({
            'system_node':self.docker('exec',self.app,'node','--version').stdout.strip(),
            'playwright_node':self.docker('exec',self.app,node,'--version').stdout.strip()}))
        def browser_run():
            results.append(self.docker('exec','--env','DEVFLOW_EVENTS=/tmp/devflow-evidence/scenario-events.jsonl',
                             self.app,node,'/tmp/devflow-ui/node_modules/@playwright/test/cli.js','test',
                             '--config=/tmp/devflow-ui/playwright.config.ts',check=False,timeout=180))
        self.mutate('write','container',self.app,browser_run)
        result = results[0]
        ui_output = self.scratch/'ui-output'
        self.mutate('write','directory',str(self.scratch),lambda: ui_output.mkdir())
        def export_ui():
            exported = subprocess.run(['docker','--host=unix:///var/run/docker.sock','exec',
                                       self.app,'tar','-cf','-','-C','/tmp/devflow-evidence','.'],
                                      capture_output=True,timeout=60)
            if exported.returncode:
                raise RuntimeError('fixture_evidence_stream_failed')
            with tarfile.open(fileobj=io.BytesIO(exported.stdout)) as bundle:
                bundle.extractall(ui_output,filter='data')
        self.mutate('write','directory',str(self.scratch),export_ui)
        for p in ui_output.iterdir():
            if p.name == 'scenario-events.jsonl':
                events = p.read_text()
                self.mutate('write','directory',str(self.evidence),lambda: (self.evidence/p.name).open('a').write(events))
            else:
                self.mutate('write','directory',str(self.evidence),lambda p=p: shutil.copytree(p,self.evidence/p.name) if p.is_dir() else shutil.copyfile(p,self.evidence/p.name))
        if result.returncode:
            self.problems.append('playwright_failed')
        assert self.health()

    def cleanup(self):
        # Exact resources only. No prune, compose down on shared names, or rm wildcard.
        failures = []
        for name in [self.app,self.foreign]:
            exists = self.docker('inspect',name,check=False).returncode==0
            if exists:
                try:
                    self.mutate('delete','container',name,lambda name=name: self.docker('rm','-f',name))
                except Exception: failures.append('container_cleanup_failed')
        if self.docker('volume','inspect',self.volume,check=False).returncode==0:
            try:
                self.mutate('delete','volume',self.volume,lambda: self.docker('volume','rm',self.volume))
            except Exception: failures.append('volume_cleanup_failed')
        return failures


def main():
    if (os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('RUNNER_ENVIRONMENT')!='github-hosted'
            or os.environ.get('GITHUB_REPOSITORY')!=REPOSITORY):
        raise SystemExit('disposable_github_hosted_only; no VM adapter exists')
    evidence = Path(os.environ.get('DEVFLOW_EVIDENCE','.devflow/evidence')).resolve()
    evidence.mkdir(parents=True,exist_ok=True)
    if any(evidence.iterdir()): raise SystemExit('fresh_evidence_directory_required')
    sha = os.environ['SOURCE_SHA']
    image = Path(os.environ['RUNNER_TEMP'],'teams-image-id').read_text().strip()
    os.environ.setdefault('DEVFLOW_HEAD_SHA', sha)
    os.environ['DEVFLOW_EVIDENCE'] = str(evidence)
    os.environ['DEVFLOW_IMAGE'] = image
    with tempfile.TemporaryDirectory(prefix='teams-transcription-ci-') as directory:
        h = Harness(image,sha,evidence,Path(directory))
        try:
            h.prepare()
            if os.environ.get('DEVFLOW_BASELINE') == '1':
                h.deploy()
                h.ui()
                base_root = Path(os.environ['DEVFLOW_BASE_ROOT'])
                probe = run(['python',str(base_root/'deploy/isolated_deployment.py'),'oracle-preflight'],check=False)
                h.write(evidence/'base-adapter.json',json.dumps(dict(
                    base_sha=sha, adapter_exists=(base_root/'deploy/isolated_deployment.py').exists(),
                    command_exit=probe.returncode,
                    scope='UI ran against base image; CLI scenarios absent, not fake RED')))
            else:
                h.scenario(1,1,h.admit,h.deploy,lambda: (h.health(), h.write(evidence/'deployment.json',json.dumps(h.receipt,indent=2))))
                if h.problems: raise RuntimeError('deployment_scenario_failed')
                h.scenario(1,2,lambda: h.health(),h.wrong_sha,lambda: assert_equal(h.original,h.inspect(h.app)))
                h.scenario(1,3,lambda: h.health(),lambda: h.health(),lambda: assert_equal(h.receipt['deployed_sha'],h.sha))
                h.scenario(2,1,lambda: assert_equal(h.before,h.snapshot()),h.compare,lambda: assert_equal(h.isolation['outside_mutations'],0))
                h.scenario(2,2,lambda: assert_equal(h.before,h.snapshot()),h.refuse_foreign,lambda: assert_equal(h.foreign_original,h.snapshot()))
                h.scenario(2,3,lambda: h.compare(),h.compare,lambda: assert_equal(h.isolation['outside_mutations'],0))
                h.ui()
                h.compare()
                h.write(evidence/'isolation.json',json.dumps(h.isolation,indent=2))
        except Exception as exc:
            h.problems.append('hosted_verification_failed')
            import re
            code = str(exc) if re.fullmatch(r'[a-z_]{1,100}',str(exc)) else type(exc).__name__
            h.write(evidence/'failure.json',json.dumps({'code':code,'last_operation':h.audit.events[-1] if h.audit.events else None}))
        finally:
            h.problems.extend(h.cleanup())
            h.write(evidence/'operations.json',json.dumps(h.audit.events,indent=2))
    baseline = os.environ.get('DEVFLOW_BASELINE') == '1'
    return finish(h, kind='base' if baseline else os.environ.get('DEVFLOW_KIND','pr'), gate=not baseline)


def finish(h, *, kind, gate=True):
    evidence = h.evidence
    os.environ['DEVFLOW_EVIDENCE'] = str(evidence)
    build = ['python',str(ROOT/'.devflow/acceptance.py'),'build','--kind',kind,'--health', 'failed' if h.problems else 'passed']
    for problem in h.problems: build.extend(['--problem',problem])
    run(build,check=False)
    report = json.loads((evidence/'acceptance.json').read_text())
    expected = {sid for sid in h.specs if kind != 'base' or '/AC-3.' in sid}
    present = {s['id'] for s in report['scenarios']}
    if present != expected:
        raise RuntimeError('scenario_set_incomplete')
    for s in report['scenarios']:
        for step in s['steps']:
            if step['title'] != h.specs[s['id']][step['phase']]:
                raise RuntimeError('scenario_text_mismatch')
        if '/AC-3.' in s['id'] and not (evidence/'shots'/(s['id'].replace('/','__')+'.png')).is_file():
            raise RuntimeError('browser_screenshot_missing')
    if not gate:
        return 1 if h.problems or any(s['status']!='passed' for s in report['scenarios']) else 0
    return run(['python',str(ROOT/'.devflow/acceptance.py'),'gate'],check=False).returncode


def assert_equal(a,b):
    assert a==b

if __name__=='__main__':
    raise SystemExit(main())
