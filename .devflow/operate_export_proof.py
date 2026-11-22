"""Hosted-only operate.sh -> real sandboxed Playwright -> host evidence proof.

The test adapter changes only the Docker socket and fixed app name to owned
hosted resources. Production still uses its fixed rootless socket/target.
No Oracle, real credentials, OAuth, model, scheduler or Join.
"""
import io
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import uuid
import zlib
import struct

ROOT = Path(__file__).resolve().parents[1]
BASE = '8339fd79769fe42e6418ac95e8af9dae9072cd98'
DOCKER = shutil.which('docker')


def docker(*args, data=None, check=True):
    p = subprocess.run([DOCKER, '--host=unix:///var/run/docker.sock', *args],
                       input=data, capture_output=True, timeout=180)
    if check and p.returncode:
        raise RuntimeError('disposable_docker_failed')
    return p


def png_readable(path):
    data = path.read_bytes()
    assert data[:8] == b'\x89PNG\r\n\x1a\n', 'missing_readable_png'
    offset, pixels, ended = 8, b'', False
    while offset < len(data):
        size = struct.unpack('>I', data[offset:offset+4])[0]
        kind = data[offset+4:offset+8]
        chunk = data[offset+8:offset+8+size]
        assert zlib.crc32(kind+chunk) == struct.unpack('>I', data[offset+8+size:offset+12+size])[0]
        if kind == b'IDAT': pixels += chunk
        if kind == b'IEND': ended = True
        offset += size+12
    assert ended and zlib.decompress(pixels), 'unreadable_png'


def assert_success(folder, op):
    result = json.loads((folder/'result.json').read_text())
    assert result.get('ok') is True, 'host_export_not_success'
    if op == 'look':
        assert result == dict(ok=True, path='/login', status=200, title='管理登入')
    else:
        assert 'PRIVATE' not in json.dumps(result)
        assert result.get('timezone') == 'Asia/Taipei' if op == 'calendar-occurrences' else result.get('expires_in_seconds') == 300
    png_readable(folder/(op+'.png'))
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in folder.iterdir() if p.is_file())
    assert {p.name for p in folder.iterdir()} == {'result.json', op+'.png'}, 'unexpected_export'
    return result


class Harness:
    def __init__(self, scratch, evidence):
        self.scratch, self.evidence = scratch, evidence
        self.app = 'teams-operate-proof-'+uuid.uuid4().hex[:12]
        self.password = secrets.token_urlsafe(24)
        self.sha = os.environ['SOURCE_SHA']
        self.image = Path(os.environ['RUNNER_TEMP'], 'teams-image-id').read_text().strip()
        self.records = []
        self.bin = scratch/'bin'; self.bin.mkdir()
        # Socket/name adapter only: all Docker operations, browser launch, tmpfs,
        # export and cleanup execute unchanged. No synthesized command results.
        wrapper = '''#!/usr/bin/env python3
import os,sys
args=sys.argv[1:]
assert args[:2]==['--host','unix:///run/user/'+str(os.getuid())+'/docker.sock']
args[:2]=['--host=unix:///var/run/docker.sock']
args=[os.environ['PROOF_APP'] if x=='devflow-teams-transcription-app' else
      'container:'+os.environ['PROOF_APP'] if x=='container:devflow-teams-transcription-app' else x for x in args]
os.execv(os.environ['PROOF_DOCKER'],[os.environ['PROOF_DOCKER'],*args])
'''
        (self.bin/'docker').write_text(wrapper); (self.bin/'docker').chmod(0o755)

    def prepare(self):
        assert docker('inspect', self.app, check=False).returncode != 0
        docker('run','-d','--name',self.app,'--user','10001:10001','--read-only',
               '--cap-drop','ALL','--security-opt','no-new-privileges',
               '--security-opt','seccomp='+str(ROOT/'docker/chromium-seccomp.json'),
               '--tmpfs','/tmp:rw,nosuid,nodev,size=128m,mode=1777','--network','bridge',
               '--entrypoint','sleep',self.image,'1800')
        fixture=(ROOT/'.devflow/ui/fixture.py').read_text().replace('port=18081','port=8080')
        archive=io.BytesIO()
        with tarfile.open(fileobj=archive,mode='w') as bundle:
            for name,data in [('fixture.py',fixture.encode()),('devflow-fixture-password',self.password.encode())]:
                info=tarfile.TarInfo(name);info.size=len(data);info.mode=0o600;bundle.addfile(info,io.BytesIO(data))
        docker('exec','-i',self.app,'tar','-xf','-','-C','/tmp',data=archive.getvalue())
        docker('exec','-d',self.app,'python','/tmp/fixture.py')
        for _ in range(60):
            p=docker('exec',self.app,'python','-c',
                'import urllib.request;assert urllib.request.urlopen("http://127.0.0.1:8080/login").status==200',check=False)
            if not p.returncode: break
            time.sleep(.5)
        assert p.returncode == 0, 'given_fixture_not_ready'

    def checkout(self, name, base=False):
        target=self.scratch/name;target.mkdir()
        if base:
            archive=subprocess.check_output(['git','archive',BASE],cwd=ROOT)
            with tarfile.open(fileobj=io.BytesIO(archive)) as bundle: bundle.extractall(target,filter='data')
        else:
            shutil.copytree(ROOT/'.devflow',target/'.devflow',ignore=shutil.ignore_patterns('evidence*','node_modules','__pycache__'))
            shutil.copytree(ROOT/'docker',target/'docker')
        return target

    def operate(self, target, label, op='look', args=None):
        env={**os.environ,'PATH':str(self.bin)+os.pathsep+os.environ['PATH'],
             'PROOF_DOCKER':DOCKER,'PROOF_APP':self.app,'DEVFLOW_OP':op,
             'DEVFLOW_OP_ARGS':json.dumps(args or {}),'DEVFLOW_OPS_ENV':'ADMIN_PASSWORD='+self.password,
             'GITHUB_SHA':self.sha,'GITHUB_RUN_ID':os.environ['GITHUB_RUN_ID']+'-'+label,
             'GITHUB_RUN_ATTEMPT':os.environ['GITHUB_RUN_ATTEMPT']}
        p=subprocess.run(['bash',str(target/'.devflow/operate.sh')],cwd=target,env=env,
                         capture_output=True,timeout=180)
        # The raw process output can include secrets/DOM on the broken base.
        # Persist only safe assertions, not that output or native browser report.
        folder=target/'.devflow/evidence/operate'/(env['GITHUB_RUN_ID']+'-'+env['GITHUB_RUN_ATTEMPT'])
        helper='devflow-teams-transcription-operate-'+env['GITHUB_RUN_ID']+'-'+env['GITHUB_RUN_ATTEMPT']
        assert docker('inspect',helper,check=False).returncode != 0, 'owned_helper_not_cleaned'
        return p.returncode, folder

    def prove(self):
        base=self.checkout('base',base=True)
        rc,folder=self.operate(base,'base')
        try: assert_success(folder,'look')
        except AssertionError as exc:
            assert str(exc)=='host_export_not_success', 'base_environment_failure_not_valid_red'
            result=json.loads((folder/'result.json').read_text())
            assert rc==0 and result==dict(ok=False,code='operation_setup_failed')
            self.records.append(dict(case='base-look',exit=rc,assertion='RED',code=str(exc),png_count=len(list(folder.glob('*.png')))))
        else: raise AssertionError('base_must_be_red')
        head=self.checkout('head')
        for op in ['look','calendar-occurrences','issue-binding']:
            args={}
            if op=='issue-binding': args=dict(occurrence_id=occurrence,present=True,media=True)
            rc,folder=self.operate(head,op,op,args)
            assert rc==0, 'head_operation_failed'
            result=assert_success(folder,op)
            if op=='calendar-occurrences': occurrence=result['occurrences'][0]['occurrence_id']
            dest=self.evidence/op;shutil.copytree(folder,dest)
            self.records.append(dict(case=op,exit=rc,assertion='GREEN'))
        # Exit 0 without a result must not promote the host setup sentinel.
        (head/'.devflow/ui/ops/look.spec.ts').write_text("import { test } from '@playwright/test'; test('empty', async () => {});\n")
        rc,folder=self.operate(head,'missing-result')
        assert rc!=0, 'missing_result_accepted'
        assert json.loads((folder/'result.json').read_text())['ok'] is False
        assert not list(folder.glob('*.png'))
        self.records.append(dict(case='missing-result',exit=rc,assertion='refused'))


def main():
    assert os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    assert os.environ.get('GITHUB_REPOSITORY')=='timmypai/teams-transcription'
    evidence=ROOT/'.devflow/evidence/operate-export';evidence.mkdir(parents=True,exist_ok=True);evidence.chmod(0o700)
    with tempfile.TemporaryDirectory(prefix='operate-export-') as temp:
        h=Harness(Path(temp),evidence)
        try:
            h.prepare();h.prove()
        finally:
            docker('rm','-f',h.app)
            receipt=dict(base_sha=BASE,head_sha=h.sha,cases=h.records,
                         daemon='hosted-rootful-socket-adapter; production-rootless-unchanged',
                         real_join=False,oauth_or_model=False)
            (evidence/'proof.json').write_text(json.dumps(receipt,indent=2)+'\n')
            print(json.dumps(receipt))


if __name__=='__main__': main()
