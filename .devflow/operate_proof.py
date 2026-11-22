"""Prove new schedule UI metadata assertions RED on fixed main, GREEN on head."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
BASE='168e8ea81409bd6f71e789ce7be394532f4855e7'
TESTS={
    'test_operate_schedule':[
        'OperateScheduleTests.test_schedule_exposes_both_dated_edges_and_timezone',
        'OperateScheduleTests.test_schedule_reports_failed_sync_without_private_calendar_key',
    ],
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
    success=(red==dict(tests=2,failures=2,errors=0,skipped=0)
        and green==dict(tests=2,failures=0,errors=0,skipped=0))
    document=dict(scope='offline-schedule-metadata-not-live-AC4',base_sha=BASE,
        candidate_sha=os.environ.get('SOURCE_SHA') or subprocess.check_output(
            ['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        run_id=os.environ.get('GITHUB_RUN_ID'),run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'),
        tests=TESTS,red=red,green=green,success=success,real_join=False,oauth_or_model=False)
    output=ROOT/'.devflow/evidence';output.mkdir(parents=True,exist_ok=True)
    (output/'operate-red-green.json').write_text(json.dumps(document,indent=2)+'\n')
    print(json.dumps(document));return 0 if success else 1


if __name__=='__main__':raise SystemExit(main())
