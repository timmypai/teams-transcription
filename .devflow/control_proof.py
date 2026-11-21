"""Immutable real main RED -> candidate GREEN, offline and no production data."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

ROOT=Path(__file__).resolve().parents[1]
BASE='08a5c0a6915ecae57a14d61b08b4ae84d181c9e5'
RUNNER='''import json,sys,unittest
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root/'tests'),str(root)]
from deploy import target_runtime
assert Path(target_runtime.__file__).resolve()==root/'deploy/target_runtime.py'
from test_target_control_path import TargetControlPathTests
from test_target_home import TargetHomeTests
result=unittest.TestResult()
unittest.TestSuite([TargetControlPathTests('test_join_bridge_reaches_real_daemon'),
    TargetHomeTests('test_initialize_prepares_effective_empty_private_home')]).run(result)
print(json.dumps(dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
    skipped=len(result.skipped),join_refused=any('control_operation_rejected' in trace for _,trace in result.failures),
    missing_home=any('effective configured Codex home still missing' in trace for _,trace in result.failures))))
'''


def main():
    archive=subprocess.check_output(['git','archive',BASE],cwd=ROOT)
    with tempfile.TemporaryDirectory(prefix='target-control-main-') as folder:
        root=Path(folder);base=root/'base';base.mkdir()
        with tarfile.open(fileobj=io.BytesIO(archive),mode='r:') as source:
            source.extractall(base,filter='data')
        for name in ('test_target_control_path.py','test_target_home.py'):
            (base/'tests'/name).write_bytes((ROOT/'tests'/name).read_bytes())
        runner=root/'runner.py';runner.write_text(RUNNER)
        env={k:v for k,v in os.environ.items() if not k.startswith('ASSISTANT_') and k not in {'PYTHONPATH','CODEX_HOME'}}
        def run(target):
            process=subprocess.run([sys.executable,'-B',str(runner),str(target)],capture_output=True,
                text=True,timeout=60,cwd=target,env=env)
            if process.returncode:raise ValueError('control_proof_execution_failed')
            return json.loads(process.stdout)
        red=run(base);green=run(ROOT)
        hashes={path:hashlib.sha256((base/path).read_bytes()).hexdigest()
            for path in ('deploy/target_runtime.py','teams_transcription/teams_local_control.py')}
    success=(red['tests']==red['failures']==2 and red['errors']==red['skipped']==0
        and red['join_refused'] and red['missing_home'] and green['tests']==2
        and green['failures']==green['errors']==green['skipped']==0)
    document=dict(scope='offline-real-main-bridge-and-home-not-Oracle-not-human-acceptance',
        base_sha=BASE,baseline_sha256=hashes,red=red,green=green,success=success,
        oracle_executed=False,real_join=False,oauth_or_model=False)
    output=ROOT/'.devflow/evidence';output.mkdir(parents=True,exist_ok=True)
    (output/'control-home-red-green.json').write_text(json.dumps(document,indent=2)+'\n')
    print(json.dumps(document))
    from join_window_proof import main as join_window_proof
    return 0 if success and join_window_proof()==0 else 1


if __name__=='__main__':raise SystemExit(main())
