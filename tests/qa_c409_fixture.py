"""Test-only physical installed-package boundary for unchanged c409 candidate gates.

No hash patch, module-global swap or environment-selectable fake provider. Each
child imports a private package copy; the historical QA/media files differ from
the source tree. Exact guarded base bytes are checked before any child starts.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
QA_SHA = 'c718c7e08600b383a9916be3684393dcd531fb86007be8811b60a2431be39197'
SEMANTIC_QA_SHA = 'e40e2de9dc436573e93d04ea2c970ed519244c17c2ea055728deb136b8dbb862'
BASE = {'qa_probe.py':QA_SHA,
    'b_no_plc.py':'5fb5e4322c27d36f74a31fe8cbc740fd232088d58db5e74ba417fff224525384',
    'b_live.py':'1415852671c4377860511f93df284c12750f88165cbcef8021349c4090304547',
    'b_media_profile.py':'1dfac1d3515b40874fe52408b1540660a2c8ae661620e095feb566f207257882',
    'b_browser_tx.py':'0fac0d1e3d84aa40037ed12f66595fbc87d1b8739f0da7d16f8bc86f2381eda9'}


def baseline_bytes(path=ROOT/'tests/fixtures/qa-c4090ed/qa_probe.py.txt'):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=QA_SHA:
        raise ValueError('c409_fixture_hash_rejected')
    return raw


def semantic_base_bytes(path=ROOT/'tests/fixtures/qa-semantic-v1/qa_probe.py.txt'):
    raw=Path(path).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=SEMANTIC_QA_SHA:
        raise ValueError('semantic_fixture_hash_rejected')
    return raw


def run_c409(script, *, args=(), input=None, timeout=10, startup=False):
    with tempfile.TemporaryDirectory(prefix='qa-c409-test-') as directory:
        site=Path(directory);package=site/'teams_transcription'
        shutil.copytree(ROOT/'teams_transcription',package,
            ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        (package/'qa_probe.py').write_bytes(semantic_base_bytes() if startup else baseline_bytes())
        base=BASE
        if startup:
            raw=(ROOT/'evidence/semantic-startup-candidate-v1.json').read_bytes()
            if hashlib.sha256(raw).hexdigest()!='0e1996c09d73144485b26ff178a9b0b99fe54d3ba5b636698dbc460eb3109292':
                raise ValueError('startup_fixture_manifest_rejected')
            base=json.loads(raw)['physical_runtime_files']
            # Current startup now has a different write path. The old installed
            # release must import its own exact physical module, never a hash
            # repin, current-module alias, or mutable provider override.
            raw=(ROOT/'tests/fixtures/qa-startup-c9d54/b_no_plc_startup.py.txt').read_bytes()
            if hashlib.sha256(raw).hexdigest()!=base['b_no_plc_startup.py']:
                raise ValueError('startup_fixture_hash_rejected')
            (package/'b_no_plc_startup.py').write_bytes(raw)
        for name in ('b_no_plc.py','b_live.py','b_browser_tx.py') if startup else ('b_no_plc.py','b_live.py','b_media_profile.py','b_browser_tx.py'):
            raw=(ROOT/'tests/fixtures/qa-c4090ed'/(name+'.txt')).read_bytes()
            if hashlib.sha256(raw).hexdigest()!=(BASE[name] if startup and name=='b_browser_tx.py' else base[name]):
                raise ValueError('c409_fixture_hash_rejected')
            if startup and name=='b_browser_tx.py':
                # The fixed startup release changed only this receiver import.
                # Restore its physical bytes, then verify the original manifest
                # below; never replace that historical pin with current source.
                before=b'from .b_no_plc import NO_PLC_RECEIVER_JS'
                if raw.count(before)!=1:raise ValueError('startup_fixture_import_rejected')
                raw=raw.replace(before,b'from .b_no_plc_startup import NO_PLC_RECEIVER_JS')
            (package/name).write_bytes(raw)
        for name,want in base.items():
            if hashlib.sha256((package/name).read_bytes()).hexdigest()!=want:
                raise ValueError('c409_fixture_base_rejected')
        env={**os.environ,'PYTHONPATH':os.pathsep.join(map(str,(site,ROOT,ROOT/'tests'))),
            'PYTHONDONTWRITEBYTECODE':'1'}
        return subprocess.run([sys.executable,'-c',script,*args],input=input,
            cwd=site,env=env,text=True,capture_output=True,timeout=timeout)
