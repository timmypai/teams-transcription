"""Hosted controlled RED/GREEN; separate from nine EPIC scenario evidence.

No Docker/Oracle/browser calls: actual probe coroutine with injected RPC/peer,
and the manual CLI with injected Docker/GitHub boundaries. Baseline bytes are
read from the immutable failed main, not copied from any cancelled branch.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tests'),str(ROOT)]
FAILED='b7d2f54b53123cf01d5381e0ad45c4e69c1910ac'


def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def totals(result):
    return dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skipped=len(result.skipped))


def smoke(source=None):
    env={'ORACLE_SMOKE_BASE':str(source)} if source is not None else {}
    with patch.dict(os.environ,env):
        module=load('repair_smoke_'+('base' if source else 'head'),ROOT/'tests/test_oracle_smoke_evidence.py')
    result=unittest.TestResult()
    unittest.defaultTestLoader.loadTestsFromTestCase(module.SmokeCLITests).run(result)
    return totals(result)


def budget(source):
    import asyncio
    module=load('repair_budget_'+source.parent.name,source)
    name='test_overall_budget_identifies_each_blocked_stage_and_ack_evidence'
    case=module.LiveSummaryDiagnosticsTests(name);factory=case.make_probe
    reached=[]
    def make(*args,**kwargs):
        probe,calls,notify=factory(*args,**kwargs)
        observe=probe.observe_stage
        def observation(name,state,failure_kind=None):
            observe(name,state,failure_kind)
            if name=='sdp_apply' and state=='entered':reached.append(True)
        probe.observe_stage=observation
        async def predecessor():
            # A controlled scheduling gate, not a production timeout increase.
            ready=asyncio.Event()
            handle=asyncio.get_running_loop().call_later(.08,ready.set)
            try:await ready.wait()
            finally:handle.cancel()
            return 'controlled-offer'
        probe.peer.offer.side_effect=predecessor
        return probe,calls,notify
    result=unittest.TestResult()
    with patch.object(case,'make_probe',side_effect=make):case.run(result)
    return dict(**totals(result),sdp_apply_entered=bool(reached),
        specific_unentered_assertion=any("phase='sdp_apply'" in str(test)
            and 'self.assertTrue(entered.is_set())' in trace for test,trace in result.failures))


def main():
    hashes={}
    with tempfile.TemporaryDirectory(prefix='repair-fixed-main-') as directory:
        base=Path(directory)
        for name,path in [('deploy.py','deploy/target_deploy.py'),('budget.py','tests/test_live_summary_diagnostics.py')]:
            data=subprocess.check_output(['git','show',FAILED+':'+path],cwd=ROOT)
            (base/name).write_bytes(data);hashes[path]=hashlib.sha256(data).hexdigest()
        red_smoke=smoke(base/'deploy.py');green_smoke=smoke()
        red_budget=budget(base/'budget.py');green_budget=budget(ROOT/'tests/test_live_summary_diagnostics.py')
    success=(red_smoke==dict(tests=6,failures=6,errors=0,skipped=0)
        and green_smoke==dict(tests=6,failures=0,errors=0,skipped=0)
        and red_budget['specific_unentered_assertion'] and red_budget['errors']==red_budget['skipped']==0
        and not red_budget['sdp_apply_entered'] and green_budget['sdp_apply_entered']
        and green_budget['failures']==green_budget['errors']==green_budget['skipped']==0)
    document=dict(scope='hosted-controlled-repair-not-Oracle-not-EPIC-acceptance',failed_source_sha=FAILED,
        baseline_sha256=hashes,budget=dict(red=red_budget,green=green_budget),
        consumer=dict(red=red_smoke,green=green_smoke),success=success,
        historical_integration_run=44339519115,oracle_executed=False)
    output=ROOT/'.devflow/evidence';output.mkdir(parents=True,exist_ok=True)
    (output/'repair-red-green.json').write_text(json.dumps(document,indent=2)+'\n')
    print(json.dumps(document));return 0 if success else 1


if __name__=='__main__':raise SystemExit(main())
