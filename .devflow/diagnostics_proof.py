"""Hosted/std-only RED->GREEN at the CLI seam, separate from EPIC AC evidence.

Run: python .devflow/diagnostics_proof.py
The six existing-seam tests must all assert-fail on the immutable failed adapter
(no import errors), and pass on head. Additional boundary tests run only on head.
No raw unittest failure trace, subprocess output or synthetic marker is persisted.
"""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tests'),str(ROOT)]
FAILED_SHA='da251b1f4fb95620271191f0bdc9d9f71b96bde3'


def suite(source=None):
    if source is not None:os.environ['TARGET_DIAGNOSTICS_SOURCE']=str(source)
    else:os.environ.pop('TARGET_DIAGNOSTICS_SOURCE',None)
    spec=importlib.util.spec_from_file_location('diagnostics_proof_tests',ROOT/'tests/test_target_diagnostics.py')
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    selected=unittest.defaultTestLoader.loadTestsFromTestCase(module.TargetDiagnosticsTests)
    if source is None:selected.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(module.DiagnosticBoundaryTests))
    result=unittest.TestResult();selected.run(result)
    return dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped))


def main():
    source=subprocess.run(['git','show',FAILED_SHA+':deploy/target_deploy.py'],cwd=ROOT,
        check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout
    with tempfile.TemporaryDirectory(prefix='diagnostics-immutable-') as folder:
        immutable=Path(folder)/'target_deploy.py';immutable.write_bytes(source)
        red=suite(immutable)
        green=suite()
    report=dict(schema_version=1,scope='disposable-subprocess-CLI-diagnostics-not-Oracle-not-EPIC-AC',
        repository='timmypai/teams-transcription',failed_source_sha=FAILED_SHA,
        failed_adapter_sha256=hashlib.sha256(source).hexdigest(),
        candidate_sha=os.environ.get('SOURCE_SHA') or subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        run_id=os.environ.get('GITHUB_RUN_ID'),run_attempt=os.environ.get('GITHUB_RUN_ATTEMPT'),
        red=red,green=green,oracle_root_cause='unknown',oracle_executed=False)
    report['success']=(red==dict(tests=6,failures=6,errors=0,skipped=0)
        and green['tests']>=6 and green['failures']==green['errors']==green['skipped']==0)
    output=ROOT/'.devflow/evidence-check';output.mkdir(parents=True,exist_ok=True)
    (output/'diagnostics-red-green.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if report['success'] else 1


if __name__=='__main__':raise SystemExit(main())
