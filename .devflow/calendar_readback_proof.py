"""Prove real authenticated readback and host export RED on fixed main, GREEN on head."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
BASE='5f48e38f8e925d57a5d188cfbd189c75728eb452'
TESTS={
    'test_calendar_readback':[
        'CalendarReadbackTests.test_zero_enabled_is_not_attempted_or_ready',
        'CalendarReadbackTests.test_unconfigured_and_unauthorized_are_distinct',
        'CalendarReadbackTests.test_successful_empty_read_is_proven_and_failure_is_not_ready',
        'CalendarReadbackTests.test_schedule_projects_existing_join_window_without_expired_joinable',
    ],
    'test_calendar_export':['CalendarExportTests.test_missing_unknown_private_or_inconsistent_fields_fail_closed'],
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
    with tempfile.TemporaryDirectory(prefix='operate-proof-') as folder:
        root=Path(folder);base=root/'base';base.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive),mode='r:') as source:
            source.extractall(base,filter='data')
        for module in TESTS:
            (base/'tests'/(module+'.py')).write_bytes((ROOT/'tests'/(module+'.py')).read_bytes())
        runner=root/'runner.py';runner.write_text(RUNNER)
        names=json.dumps([module+'.'+test for module,tests in TESTS.items() for test in tests])
        env={key:value for key,value in os.environ.items() if not key.startswith('ASSISTANT_')
            and key not in {'PYTHONPATH','CODEX_HOME'}}
        def run(target):
            process=subprocess.run([sys.executable,'-B',str(runner),str(target),names],cwd=target,
                env=env,capture_output=True,text=True,timeout=60,check=True)
            return json.loads(process.stdout)
        red=run(base);green=run(ROOT)
    success=(red==dict(tests=5,failures=18,errors=0,skipped=0)
        and green==dict(tests=5,failures=0,errors=0,skipped=0))
    document=dict(scope='offline-calendar-readback-not-live-AC4',base_sha=BASE,
        candidate_sha=os.environ.get('SOURCE_SHA') or subprocess.check_output(
            ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        run_id=os.environ.get('GITHUB_RUN_ID'),run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'),
        tests=TESTS,red=red,green=green,success=success,real_join=False,oauth_or_model=False)
    output=ROOT/'.devflow/evidence';output.mkdir(parents=True,exist_ok=True)
    (output/'calendar-readback-red-green.json').write_text(json.dumps(document,indent=2)+'\n')
    print(json.dumps(document));return 0 if success else 1


if __name__=='__main__':raise SystemExit(main())
