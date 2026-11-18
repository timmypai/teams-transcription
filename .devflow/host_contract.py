"""Actual Docker/Compose CLI-only checks on the disposable hosted runner; no daemon."""
import json
import os
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'tests'),str(ROOT)]
output=ROOT/'.devflow/evidence-check'
output.mkdir(parents=True,exist_ok=True)
with (output/'host-compose.log').open('w') as log:
    result=unittest.TextTestRunner(stream=log,verbosity=2).run(
        unittest.defaultTestLoader.loadTestsFromName('test_staging_compose'))
receipt=dict(scope='hosted-compose-config-only-no-daemon',head_sha=os.environ['SOURCE_SHA'],
             suites=['test_staging_compose'],tests=result.testsRun,failures=len(result.failures),
             errors=len(result.errors),skipped=len(result.skipped),
             success=result.wasSuccessful() and not result.skipped)
(output/'host-compose.json').write_text(json.dumps(receipt,indent=2)+'\n')
print(json.dumps(receipt))
raise SystemExit(0 if receipt['success'] else 1)
