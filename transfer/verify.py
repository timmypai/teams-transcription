"""Read-only package integrity verification; no application/model/network action."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'TRANSFER-MANIFEST.json').read_text())
expected = manifest['files']
for name, record in expected.items():
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise SystemExit('Unsafe manifest path')
    path = root / relative
    if path.is_symlink() or any(p.is_symlink() for p in path.parents if p.is_relative_to(root)):
        raise SystemExit('Symlinks are forbidden')
    data = path.read_bytes()
    if len(data) != record['bytes'] or hashlib.sha256(data).hexdigest() != record['sha256']:
        raise SystemExit('Hash/length mismatch: ' + name)
actual = set()
for path in root.rglob('*'):
    if path.is_symlink():
        raise SystemExit('Unexpected symlink')
    if path.is_file():
        actual.add(str(path.relative_to(root)))
if actual != set(expected) | {'TRANSFER-MANIFEST.json'}:
    raise SystemExit('Unexpected or missing files')
print(json.dumps({'result': 'PASS', 'verified_files': len(expected),
                  'source_commit': manifest['source_commit'], 'model_calls': 0,
                  'network_calls': 0, 'real_meeting_inputs': 0}))
