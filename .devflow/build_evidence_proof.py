"""Immutable schema1 RED -> schema2 GREEN; fixed numeric hosted evidence only.

No raw unittest traces, commands, help or public/private child output persisted.
Original EPIC nine scenarios/27 steps and original diagnostics proof are separate.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
FAILED=('b7d2f54b53123cf01d5381e0ad45c4e69c1910ac','32ae40683493722a546938fe820d7eb8b91d14e3')


def worker():
    import test_build_evidence as tests
    suite=unittest.defaultTestLoader.loadTestsFromTestCase(tests.BuildEvidenceRedTests)
    if not os.environ.get('TARGET_BUILD_SOURCE'):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(tests.BuildEvidenceBoundaryTests))
    result=unittest.TestResult();suite.run(result)
    print(json.dumps(dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        skipped=len(result.skipped),real_build_children=tests.WITNESSES,
        expected_red=bool(result.failures) and all('schema_version_uncollected' in trace for _,trace in result.failures))))
    return 0


def run(source=None):
    env=dict(os.environ);env.pop('TARGET_BUILD_SOURCE',None)
    if source:env['TARGET_BUILD_SOURCE']=str(source)
    result=subprocess.run([sys.executable,str(Path(__file__).resolve()),'worker'],env=env,
        cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90)
    # Worker output is authored numeric JSON, not child output. Anything else
    # fails the proof without printing its contents.
    if result.returncode or result.stderr:raise ValueError('proof_worker_failed')
    return json.loads(result.stdout)


def main():
    reds=[]
    for sha in FAILED:
        with tempfile.TemporaryDirectory(prefix='build-evidence-immutable-') as folder:
            hashes={}
            for name in ('target_deploy.py','target_diagnostics.py'):
                source=subprocess.run(['git','show',sha+':deploy/'+name],cwd=ROOT,check=True,
                    stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout
                (Path(folder)/name).write_bytes(source);hashes[name]=hashlib.sha256(source).hexdigest()
            reds.append(dict(source_sha=sha,source_sha256=hashes,result=run(Path(folder))))
    green=run()
    success=all(row['result']==dict(tests=1,failures=9,errors=0,skipped=0,real_build_children=9,expected_red=True) for row in reds)
    success=success and green['tests']>=10 and green['real_build_children']>=9 and green['failures']==green['errors']==green['skipped']==0
    report=dict(schema_version=1,scope='disposable-CLI-schema2-consumer-not-Oracle-not-EPIC-AC',
        repository='timmypai/teams-transcription',
        candidate_sha=os.environ.get('SOURCE_SHA') or subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        run_id=os.environ.get('GITHUB_RUN_ID'),run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'),
        red=reds,green=green,success=success,oracle_executed=False,oracle_root_cause='unknown')
    output=ROOT/'.devflow/evidence-check';output.mkdir(parents=True,exist_ok=True)
    (output/'build-evidence-red-green.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report));return 0 if success else 1


if __name__=='__main__':
    raise SystemExit(worker() if sys.argv[1:]==['worker'] else main())
