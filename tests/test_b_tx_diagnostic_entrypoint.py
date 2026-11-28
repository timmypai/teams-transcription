"""Exercise the streamed fixture against the installed product, not source shadows."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class DiagnosticEntrypointTests(unittest.TestCase):
    def test_fixture_uses_installed_product_despite_container_workdir_and_pythonpath(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory)
            shadow = scratch / 'source'
            package = shadow / 'teams_transcription'
            package.mkdir(parents=True)
            (package / '__init__.py').write_text("raise AssertionError('source overlay imported')\n")
            # Transport-only shim: emulate the image's source-containing WORKDIR,
            # then execute the actual streamed Python command and installed app.
            docker = scratch / 'docker'
            docker.write_text(f'#!{sys.executable}\n' + '''import os, sys
args = sys.argv[1:]
assert args[:5] == ['--host=unix:///var/run/docker.sock', 'exec', '-i', 'fixture', 'python']
os.chdir(os.environ['TX_FIXTURE_WORKDIR'])
os.execv(sys.executable, [sys.executable, *args[5:]])
''')
            docker.chmod(0o700)
            def execute(phase):
                result = subprocess.run(['bash', str(root / '.devflow/verify.sh'),
                    '--tx-receipts', 'fixture', phase], cwd=root, text=True,
                    capture_output=True, timeout=30,
                    env={**os.environ, 'PATH': str(scratch) + os.pathsep + os.environ['PATH'],
                        'PYTHONPATH': str(shadow), 'TX_FIXTURE_WORKDIR': str(shadow)})
                self.assertEqual(result.returncode, 0, result.stderr)
                return json.loads(result.stdout)
            self.assertEqual(execute('given'),
                dict(bound=True, initially_muted=True, cleanup=True, synthetic_audio=True))
            report = execute('when')
            self.assertEqual(report['tests'], 12)
            self.assertEqual(report['errors'], 0)
            self.assertEqual(report['skipped'], 0)
            self.assertEqual(report['unexpected_failures'], 0)
            self.assertEqual(len(report['behavior']), 11)
