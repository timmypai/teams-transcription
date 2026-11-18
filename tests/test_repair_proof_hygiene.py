"""Keep private-mode proof execution from contaminating public source copies."""
import os
from pathlib import Path
import subprocess
import sys
import unittest
import yaml

ROOT=Path(__file__).resolve().parents[1]


class RepairProofHygieneTests(unittest.TestCase):
    def test_plain_interpreter_refuses_before_application_import_or_git_lookup(self):
        # -E ignores the worker's ambient PYTHONDONTWRITEBYTECODE. A bare run
        # must refuse; it must not need git, Docker, credentials or live access.
        result=subprocess.run([sys.executable,'-E',str(ROOT/'.devflow/repair_proof.py')],
            cwd=ROOT,text=True,capture_output=True,timeout=10)
        self.assertNotEqual(result.returncode,0)
        self.assertEqual(result.stderr.strip(),'repair_proof_requires_no_bytecode')
        self.assertEqual(result.stdout,'')

    def test_hosted_workflow_preserves_fresh_verifier_then_no_bytecode_proof(self):
        workflow=yaml.load((ROOT/'.github/workflows/ci.yml').read_text(),Loader=yaml.BaseLoader)
        commands=[step['run'] for step in workflow['jobs']['verify']['steps'] if 'run' in step]
        self.assertIn('python -B .devflow/repair_proof.py',commands)
        self.assertLess(commands.index('bash .devflow/verify.sh'),commands.index('python -B .devflow/repair_proof.py'))
        self.assertLess(commands.index('python -B .devflow/repair_proof.py'),commands.index('python .devflow/target_adapter_probe.py'))
