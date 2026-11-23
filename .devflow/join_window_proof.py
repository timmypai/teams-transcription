"""Reproduce both defects on immutable main, then verify the same public seams."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
BASE='cfc5493eec7c3f85707399a67fff5665db482839'
LEGACY_TEST='168e8ea81409bd6f71e789ce7be394532f4855e7:tests/test_target_join_failure.py'
TESTS={
    'test_target_join_window':'TargetJoinWindowTests.test_expired_binding_is_refused_without_revision_or_consent_write',
    'test_target_consent_ui':'TargetConsentUITests.test_schedule_expired_consent_shows_reason_and_does_not_write_binding',
    'test_target_join_failure':'TargetJoinFailureTests.test_expired_join_keeps_daemon_code_and_phase_in_manual_failure_json',
}
RUNNER='''import json,sys,unittest
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root/'tests'),str(root)]
from teams_transcription import daemon
assert Path(daemon.__file__).resolve()==root/'teams_transcription/daemon.py'
suite=unittest.defaultTestLoader.loadTestsFromNames(json.loads(sys.argv[2]))
result=unittest.TestResult();suite.run(result)
print(json.dumps(dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped))))
'''


def main():
    archive=subprocess.check_output(['git','archive',BASE],cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix='join-window-proof-') as folder:
        root=Path(folder);base=root/'base';base.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive),mode='r:') as source:
            source.extractall(base,filter='data')
        for module in TESTS:
            (base/'tests'/(module+'.py')).write_bytes((ROOT/'tests'/(module+'.py')).read_bytes())
        # The historical adapter takes argv. Only the isolated baseline runs
        # its fixed-era test; head exercises the private adapter.
        (base/'tests/test_target_join_failure.py').write_bytes(subprocess.check_output(['git','show',LEGACY_TEST],cwd=ROOT))
        runner=root/'runner.py';runner.write_text(RUNNER)
        names=json.dumps([module+'.'+test for module,test in TESTS.items()])
        env={key:value for key,value in os.environ.items() if not key.startswith('ASSISTANT_')
            and key not in {'PYTHONPATH','CODEX_HOME'}}
        def run(target):
            process=subprocess.run([sys.executable,'-B',str(runner),str(target),names],cwd=target,
                env=env,capture_output=True,text=True,timeout=60,check=True)
            return json.loads(process.stdout)
        red=run(base);green=run(ROOT)
    success=(red==dict(tests=3,failures=3,errors=0,skipped=0)
        and green==dict(tests=3,failures=0,errors=0,skipped=0))
    document=dict(scope='offline-binding-ui-control-not-live-AC4',base_sha=BASE,
        candidate_sha=os.environ.get('SOURCE_SHA') or subprocess.check_output(
            ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        run_id=os.environ.get('GITHUB_RUN_ID'),run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'),
        tests=TESTS,red=red,green=green,success=success,real_join=False,oauth_or_model=False)
    output=ROOT/'.devflow/evidence';output.mkdir(parents=True,exist_ok=True)
    (output/'join-window-red-green.json').write_text(json.dumps(document,indent=2)+'\n')
    print(json.dumps(document));return 0 if success else 1


if __name__=='__main__':raise SystemExit(main())
