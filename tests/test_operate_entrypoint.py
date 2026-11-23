"""Run the actual shell entrypoint's credential/argument preflight, without Docker."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


class OperateEntrypointTests(unittest.TestCase):
    def run_preflight(self, args):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root/'.devflow/operate.sh'
            script.parent.mkdir()
            shutil.copyfile(Path(__file__).resolve().parents[1]/'.devflow/operate.sh', script)
            env = {key:value for key,value in os.environ.items() if not key.startswith('DEVFLOW_')
                and key not in {'GITHUB_RUN_ID','GITHUB_RUN_ATTEMPT'}}
            env.update(DEVFLOW_OP='calendar-occurrences', DEVFLOW_OP_ARGS=args)
            process = subprocess.run(['bash',str(script)], env=env, capture_output=True, text=True, timeout=10)
            result = json.loads((root/'.devflow/evidence/operate/local-1/result.json').read_text())
            return process, result

    def test_missing_secret_fails_with_actionable_safe_result(self):
        process, result = self.run_preflight('{}')
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(result, dict(ok=False, code='missing_ADMIN_PASSWORD'))
        self.assertIn('DEVFLOW_OPS_ENV', process.stderr)

    def test_invalid_args_do_not_echo_private_input(self):
        process, result = self.run_preflight('SYNTHETIC-PRIVATE-INPUT')
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual(result, dict(ok=False, code='invalid_DEVFLOW_OP_ARGS'))
        self.assertNotIn('SYNTHETIC-PRIVATE-INPUT', process.stdout+process.stderr+json.dumps(result))
