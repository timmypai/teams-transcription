"""Fixed-main real-entry assertion RED/GREEN; no Oracle/client/account/OAuth."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASE = '17074a7d851b92d92db8509048f7dd2d52317db7'
CASES = [
    'test_google_client_install.GoogleClientInstallTests.test_fixed_cli_installs_private_persistent_client_and_repeats_without_replacing',
    'test_calendar_settings.CalendarSettingsTests.test_readonly_primary_form_creates_read_only_calendar_without_grant_or_poll',
    'test_calendar_settings_export.SettingsExportTests.test_private_fields_bad_types_and_unknown_failure_refuse',
]


def probe():
    sys.path[:0] = [str(ROOT/'tests'),str(ROOT)]
    suite = unittest.defaultTestLoader.loadTestsFromNames(CASES)
    result = unittest.TestResult()
    # Baseline argparse rejection is expected assertion RED, not import RED.
    import contextlib
    with contextlib.redirect_stderr(io.StringIO()):suite.run(result)
    print(json.dumps(dict(tests=result.testsRun,failures=len(result.failures),errors=len(result.errors),skips=len(result.skipped))))


def run(root):
    output = subprocess.run([sys.executable,'-B',str(root/'.devflow/google_setup_proof.py'),'--probe'],
        cwd=root,capture_output=True,text=True,check=True,env={**os.environ,'PYTHONPATH':str(root/'tests')+':'+str(root)})
    return json.loads(output.stdout)


def main():
    with tempfile.TemporaryDirectory(prefix='google-setup-fixed-main-') as folder:
        base = Path(folder)
        archive = subprocess.check_output(['git','archive',BASE],cwd=ROOT)
        with tarfile.open(fileobj=io.BytesIO(archive)) as source:source.extractall(base,filter='data')
        for file in ['.devflow/google_setup_proof.py','tests/test_google_client_install.py',
                'tests/test_calendar_settings.py','tests/test_calendar_settings_export.py']:
            shutil.copyfile(ROOT/file,base/file)
        red = run(base)
    green = run(ROOT)
    success = red['tests']==3 and red['failures']>=3 and red['errors']==red['skips']==0 and green==dict(tests=3,failures=0,errors=0,skips=0)
    report = dict(scope='offline-fixed-main-installed-cli-authenticated-form-host-export-not-Google-not-Oracle',
        base_sha=BASE,red=red,green=green,success=success)
    output = ROOT/'.devflow/evidence';output.mkdir(parents=True,exist_ok=True)
    (output/'google-setup-red-green.json').write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps(report))
    return 0 if success else 1


if __name__=='__main__':
    if '--probe' in sys.argv:probe()
    else:raise SystemExit(main())
