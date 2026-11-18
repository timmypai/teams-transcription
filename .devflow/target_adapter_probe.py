"""Actual target build/switch/rollback on DISPOSABLE hosted Docker only.

No Oracle, native review claim, real password, occurrence, OAuth or provider.
The production CLI has no hosted/fake-provider switch. This harness calls the
component seam with its own executor, as do the original hosted tests.
"""
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from deploy.target_deploy import Controller, execute, snapshot, APP, VOLUME, PROJECT, REPO, OWNER, RUNTIME


class HostedController(Controller):
    def __init__(self,*args,**kwargs):
        kwargs['command']=execute
        super().__init__(*args,**kwargs)
        self.docker_prefix=['docker','--host=unix:///var/run/docker.sock']



class BrokenCandidate(HostedController):
    def create_release(self,*args):
        release=super().create_release(*args)
        # Controlled bad candidate configuration, never modify source checkout.
        original=(release/'compose.yml').read_text()
        changed=original.replace('command: [serve, --config, /data/config/runtime.yml, --local-teams-control]',
            'command: [healthcheck, --port, "65534"]')
        assert original!=changed
        self.write(release/'compose.yml',changed)
        info=self.read(release/'release.json')
        info['files']['compose.yml']=hashlib.sha256((release/'compose.yml').read_bytes()).hexdigest()
        self.write(release/'release.json',json.dumps(info))
        return release


def sentinel_command(image,name):
    # Image labels are inherited! Explicitly exclude this foreign fixture from
    # the target owner; otherwise preflight correctly treats it as a stale helper
    # and the foreign snapshot would omit the very service it must observe.
    return ['run','-d','--name',name,'--label',OWNER+'=fixture-foreign',
        '--network','none','--read-only','--user','10001:10001',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--entrypoint','sleep',image,'1800']


def main():
    assert os.environ.get('RUNNER_ENVIRONMENT')=='github-hosted'
    sha=os.environ['SOURCE_SHA'];run=int(os.environ['GITHUB_RUN_ID']);attempt=int(os.environ['GITHUB_RUN_ATTEMPT'])
    evidence=ROOT/'.devflow/evidence/target-adapter';evidence.mkdir(parents=True,exist_ok=True)
    report=dict(scope='disposable-hosted-component-probe-not-Oracle',source_sha=sha,
        actions_run_id=run,actions_run_attempt=attempt,rootless_vm_unverified=True,real_accounts=False)
    with tempfile.TemporaryDirectory(prefix='teams-target-adapter-',dir=os.environ['RUNNER_TEMP']) as directory:
        scratch=Path(directory);c=HostedController(scratch/'state',run,attempt)
        c.root.mkdir(mode=0o700)
        foreign='teams-transcription-target-sentinel-'+uuid.uuid4().hex[:12]
        # Fail closed if fixed namespace belongs to anything in this disposable VM.
        assert not c.probe('volume',VOLUME) and not c.probe('network',PROJECT)
        assert not any(x['Name'].lstrip('/')==APP for x in c.containers())
        sentinel=scratch/'foreign.txt';sentinel.write_text('preserve fixture\n')
        dbfile=scratch/'foreign.sqlite3'
        with sqlite3.connect(dbfile) as db:db.execute('CREATE TABLE sentinel (value TEXT)');db.execute("INSERT INTO sentinel VALUES('preserve')")
        hashes=lambda:[hashlib.sha256(p.read_bytes()).hexdigest() for p in (sentinel,dbfile)]
        file_before=hashes()
        cleanup=[]
        try:
            # Real public-only native context preparation and actual Docker build.
            image=c.build(sha);report['image']=image
            c.docker(*sentinel_command(image,foreign))
            cleanup.append(foreign)
            before=snapshot(c.containers())
            assert any(row['id']==json.loads(c.docker('inspect',foreign))[0]['Id'] for row in before)
            c.docker('volume','create','--label','fixture=foreign-collision',VOLUME)
            try:
                try:c.resources(None)
                except ValueError as exc:assert str(exc)=='resource_owner_collision'
                else:raise AssertionError('foreign volume adopted')
            finally:c.docker('volume','rm',VOLUME)
            report['foreign_volume_refused']=True
            with socket.socket() as occupied:
                occupied.bind(('127.0.0.1',18101));occupied.listen()
                try:c.resources(None)
                except OSError:pass
                else:raise AssertionError('occupied port admitted')
            report['port_collision_refused']=True
            assert c.resources(None)==before
            os.environ['DEVFLOW_APP_ENV']='DEVFLOW_ADMIN_PASSWORD='+secrets.token_urlsafe(32)
            # Explicitly synthetic receipt provenance; real image/source/run above.
            receipt=dict(repository=REPO,source_sha=sha,scope='hosted-component-no-github-review-attestation')
            smoke=c.switch(receipt,image,None)
            cleanup.append(APP)
            state=c.read(c.root/'state.json')
            assert state['phase']=='healthy'
            assert c.resources(state)==before
            report['initial_smoke']=smoke
            # Actual installed short-lived consumer on disposable storage only.
            # No source injection, argv password, tracing or private artifacts.
            secret=os.environ['DEVFLOW_APP_ENV']
            password_before=c.runtime('state')
            try:c.runtime('verify',input='')
            except ValueError:pass
            else:raise AssertionError('missing verification secret accepted')
            browser=c.runtime('verify',input=secret)
            assert browser['browser_login'] and browser['weekly_calendar'] and browser['settings']
            assert browser['browser_closed'] and browser['post_close_health']
            assert browser['readiness']['exact_rejection'] and browser['readiness']['sqlite_unchanged']
            assert browser['readiness']['authorized_view']=='NOT EXECUTED'
            assert browser['readiness']['bound_stop_cleanup']=='NOT EXECUTED'
            assert c.runtime('state')==password_before
            report['deployed_browser_and_no_id_readiness']=browser
            report['missing_secret_refused']=True
            assert secret.split('=',1)[1] not in json.dumps(report)
            script="""import sqlite3,json
p='/data/.meeting-store.sqlite3'
with sqlite3.connect(p) as db:
 db.execute('CREATE TABLE devflow_fixture (value TEXT)')
 db.execute('INSERT INTO devflow_fixture VALUES (?)',('preserve-across-rollback',))
print(json.dumps({'seeded':True}))
"""
            c.mutate('exec','container',APP,lambda:c.docker('exec','-i',APP,'python','-',input=script))
            old_compose=(c.root/'releases'/state['release']/'compose.yml').read_bytes()
            broken=BrokenCandidate(c.root,run,attempt)
            try:broken.switch(receipt,image,state)
            except ValueError as exc:assert str(exc)=='target_command_failed'
            else:raise AssertionError('bad candidate passed')
            restored=c.read(c.root/'state.json')
            assert restored['phase']=='healthy' and restored['image']==image and restored['release']==state['release']
            assert (c.root/'releases'/state['release']/'compose.yml').read_bytes()==old_compose
            ident,smoke=c.health(image)
            check="""import sqlite3,json
with sqlite3.connect('file:/data/.meeting-store.sqlite3?mode=ro',uri=True) as db:
 print(json.dumps({'preserved':db.execute('SELECT value FROM devflow_fixture').fetchone()[0]=='preserve-across-rollback'}))
"""
            assert json.loads(c.docker('exec','-i',APP,'python','-',input=check))['preserved']
            assert c.runtime('state')['quiescent']
            # Real opt-in socket exists; no fabricated production occurrence.
            try:c.runtime('view',ident='0'*32)
            except ValueError:pass
            else:raise AssertionError('nonexistent occurrence accepted')
            report.update(rollback_healthy=True,exact_previous_compose=True,data_preserved=True,
                nonexistent_occurrence_refused=True,stop_cleanup='covered by real Unix+SQLite synthetic runner regression',
                foreign_files_equal=file_before==hashes(),foreign_snapshot_equal=before==snapshot(c.containers()),
                initial_audit=c.audit.report(before,snapshot(c.containers())),
                failed_candidate_audit=broken.audit.report(before,snapshot(c.containers())))
            assert report['foreign_files_equal'] and report['foreign_snapshot_equal']
        finally:
            os.environ.pop('DEVFLOW_APP_ENV',None)
            # These exact names were created only in this disposable runner; no prune.
            for name in [APP,c.helper,c.builder,foreign]:
                found=[x for x in c.containers() if x['Name'].lstrip('/')==name]
                if found:
                    c.docker('rm','-f',name);cleanup.append('removed:'+name)
            if c.probe('network',PROJECT):c.docker('network','rm',PROJECT)
            if c.probe('volume',VOLUME):c.docker('volume','rm',VOLUME)
            report['cleanup']=cleanup
            (evidence/'probe.json').write_text(json.dumps(report,indent=2)+'\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
