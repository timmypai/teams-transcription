"""Public deployment admission/audit and hosted workflow seams. No accounts."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('isolated_deployment', ROOT/'deploy/isolated_deployment.py')
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class AdmissionTests(unittest.TestCase):
    def test_wrong_sha_refuses_before_calling_mutator(self):
        calls = []
        def deploy(candidate):
            receipt = module.admit_candidate('timmypai/teams-transcription','main','a'*40,candidate,
                                             module.SOURCE_SHA,'sha256:'+'c'*64)
            calls.append(receipt)
        with self.assertRaisesRegex(ValueError,'candidate_sha_mismatch'):
            deploy('b'*40)
        self.assertEqual(calls, [])
        deploy('a'*40)
        self.assertEqual(len(calls),1)

    def test_identity_lineage_and_immutable_image_required(self):
        good=['timmypai/teams-transcription','main','a'*40,'a'*40,module.SOURCE_SHA,'sha256:'+'c'*64]
        for index,value in [(0,'paulpai0412/teams-transcription'),(1,'wt/foreign'),(2,'bad'),
                            (4,'b'*40),(5,'teams-transcription:latest')]:
            args=good.copy();args[index]=value
            with self.subTest(index=index), self.assertRaises(ValueError):
                module.admit_candidate(*args)

    def test_whole_plan_refused_without_partial_write(self):
        audit=module.OperationAudit([('stop','container','teams-transcription-app')])
        calls=[]
        with self.assertRaisesRegex(ValueError,'foreign_target_refused'):
            audit.execute([('stop','container','teams-transcription-app'),('stop','container','foreign')],
                          lambda *row:calls.append(row))
        self.assertEqual(calls,[])
        self.assertEqual(audit.events,[])
        audit.execute([('stop','container','teams-transcription-app')],lambda *row:calls.append(row))
        self.assertEqual(calls,[('stop','container','teams-transcription-app')])
        report=audit.report({'id':'foreign','started':'unchanged'},{'id':'foreign','started':'unchanged'})
        self.assertEqual(report['outside_mutations'],0)
        self.assertFalse(report['vm_acceptance'])

    def test_changed_foreign_or_failed_operation_cannot_report_success(self):
        audit=module.OperationAudit([('write','volume','teams-transcription-data')])
        def failed(*_): raise RuntimeError('controlled failure')
        with self.assertRaises(RuntimeError):
            audit.execute([('write','volume','teams-transcription-data')],failed)
        self.assertEqual(audit.events[0]['outcome'],'failed')
        with self.assertRaisesRegex(ValueError,'operation_audit_incomplete'):audit.report({}, {})
        with self.assertRaisesRegex(ValueError,'foreign_snapshot_changed'):audit.report({}, {'changed':True})

    def test_oracle_cli_has_no_enable_flag_and_always_refuses(self):
        result=subprocess.run([sys.executable,str(ROOT/'deploy/isolated_deployment.py'),'oracle-preflight'],
                              capture_output=True,text=True,timeout=5)
        self.assertEqual(result.returncode,1)
        receipt=json.loads(result.stdout)
        self.assertFalse(receipt['enabled'])
        self.assertEqual(receipt['vm_mutation_allowlist'],[])

    def test_hosted_ci_never_runs_vm_or_privileged_events(self):
        workflow=yaml.load((ROOT/'.github/workflows/ci.yml').read_text(),Loader=yaml.BaseLoader)
        self.assertEqual(set(workflow['on']),{'pull_request','workflow_dispatch'})
        self.assertEqual(workflow['permissions'],{'contents':'read'})
        self.assertEqual(set(workflow['jobs']),{'test','check','verify','verify-base','devflow-ci'})
        for job in workflow['jobs'].values():
            self.assertEqual(job['runs-on'],'ubuntu-24.04')
            self.assertNotIn('environment',job)
            for step in job['steps']:
                if 'actions/checkout@' in step.get('uses',''):
                    self.assertEqual(step['with']['ref'],'${{ env.SOURCE_SHA }}')
                    self.assertEqual(step['with']['persist-credentials'],'false')
        text=(ROOT/'.github/workflows/ci.yml').read_text()
        self.assertNotIn('${{ secrets.',text)
        self.assertNotIn('packages: write',text)
        self.assertNotIn('pull_request_target',text)

if __name__ == '__main__':
    unittest.main()
