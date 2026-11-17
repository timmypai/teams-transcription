"""Available regression, not original full discovery. No accounts or probes."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tests'), str(ROOT)]
EXCLUDED = {'test_bridge_integration.py', 'test_media_bridge.py'}

class AvailableLoader(unittest.TestLoader):
    def _match_path(self, path, full_path, pattern):
        return path not in EXCLUDED and super()._match_path(path, full_path, pattern)


def main():
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='teams-empty-codex-') as home:
        os.environ['CODEX_HOME'] = home
        if os.environ.get('RUNNER_ENVIRONMENT') == 'github-hosted' and Path('/workspace/.devflow/fixture-wheels').is_dir():
            import subprocess
            subprocess.run([sys.executable,'-m','pip','install','--no-index','--target','/tmp/testlibs',
                            '--find-links','/workspace/.devflow/fixture-wheels','websockets>=16,<17','jsonschema==4.26.0'],check=True)
            sys.path.insert(0,'/tmp/testlibs')
        suite = AvailableLoader().discover(str(ROOT/'tests'))
        shard = int(os.environ.get('TEST_SHARD','0'))
        shards = int(os.environ.get('TEST_SHARDS','1'))
        assert 0 <= shard < shards <= 4
        suite = unittest.TestSuite(list(suite)[shard::shards])
        class ProgressResult(unittest.TextTestResult):
            def startTest(self, test):
                super().startTest(test)
                (output/'progress.json').write_text(json.dumps({'test':test.id(),'started':self.testsRun}))
        with (output/'regression.log').open('w') as log:
            result = unittest.TextTestRunner(stream=log, verbosity=2, resultclass=ProgressResult).run(suite)
    receipt = dict(scope='available-regression-not-original-full-discovery',
                   python=sys.version, shard=shard, shards=shards, tests=result.testsRun, failures=len(result.failures),
                   errors=len(result.errors), skipped=len(result.skipped),
                   excluded_missing_source=sorted(EXCLUDED),
                   success=result.wasSuccessful() and not result.skipped,
                   real_accounts_or_meetings=False)
    (output/'regression.json').write_text(json.dumps(receipt, indent=2)+'\n')
    print(json.dumps(receipt))
    return 0 if receipt['success'] else 1

if __name__ == '__main__':
    raise SystemExit(main())
