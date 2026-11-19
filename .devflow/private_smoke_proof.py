"""Actual fixed consumer RED/GREEN for approved private tunnel compatibility.

Runs offline with a disposable initialized runtime and injected Docker/GitHub,
not an Oracle/browser/network probe. Browser controls run in image regressions.
"""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
BASE='51e7ee1a06e455f114581df6d5ce68da442f6ddc'
sys.path[:0]=[str(ROOT/'tests'),str(ROOT)]


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def checks(base_runtime=None,base_smoke=None):
    import test_oracle_smoke_runtime as runtime_tests
    from deploy import target_smoke
    from test_oracle_smoke_evidence import PrivateSmokeCLITests
    result=unittest.TestResult()
    suite=unittest.TestSuite([
        runtime_tests.SmokeRuntimeTests('test_approved_default_private_tunnel_uses_only_fixed_installed_origin'),
        PrivateSmokeCLITests('test_approved_private_mode_is_named_truthfully_in_formal_consumer')])
    with patch.object(runtime_tests,'configured_entrypoint',
            base_runtime.configured_entrypoint if base_runtime else runtime_tests.configured_entrypoint),\
        patch.object(target_smoke,'OracleSmoke',base_smoke.OracleSmoke if base_smoke else target_smoke.OracleSmoke):
        suite.run(result)
    # Store only counts/fixed assertion categories, never tracebacks, config,
    # session material, runtime paths or exception bodies.
    return dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),
        skipped=len(result.skipped),private_origin_assertion=any(
            'approved private tunnel rejected' in trace for _,trace in result.failures),
        transport_name_assertion=any("None != 'approved-private-ssh-tunnel'" in trace for _,trace in result.failures))


def main():
    if not sys.dont_write_bytecode:raise SystemExit('private_proof_requires_no_bytecode')
    hashes={}
    with tempfile.TemporaryDirectory(prefix='private-smoke-base-') as directory:
        base=Path(directory)
        for path in ('deploy/target_runtime.py','deploy/target_smoke.py','deploy/target_deploy.py',
            '.devflow/acceptance.py','.devflow/scenarios.json'):
            data=subprocess.check_output(['git','show',BASE+':'+path],cwd=ROOT)
            destination=base/path;destination.parent.mkdir(parents=True,exist_ok=True);destination.write_bytes(data)
            hashes[path]=hashlib.sha256(data).hexdigest()
        runtime=load('private_fixed_base_runtime',base/'deploy/target_runtime.py')
        smoke=load('private_fixed_base_smoke',base/'deploy/target_smoke.py')
        red=checks(runtime,smoke);green=checks()
    success=(red['tests']==red['failures']==2 and red['errors']==red['skipped']==0
        and red['private_origin_assertion'] and red['transport_name_assertion']
        and green['tests']==2 and green['failures']==green['errors']==green['skipped']==0)
    document=dict(scope='offline-fixed-private-consumer-not-Oracle-not-EPIC-acceptance',
        base_sha=BASE,baseline_sha256=hashes,red=red,green=green,success=success,
        oracle_executed=False,operator_runtime_changed=False,credentials_read=False)
    output=ROOT/'.devflow/evidence';output.mkdir(parents=True,exist_ok=True)
    (output/'private-smoke-red-green.json').write_text(json.dumps(document,indent=2)+'\n')
    print(json.dumps(document));return 0 if success else 1


if __name__=='__main__':raise SystemExit(main())
