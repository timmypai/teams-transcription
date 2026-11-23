"""Public boundary contract. The canaries are never valid authorities."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import time
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
OCCURRENCE = 'a1' * 16
BINDING = 'b2' * 32
PRIVATE_CODE = 'alowercaseprivatecanary'


def png():
    def chunk(kind, value):
        return struct.pack('>I', len(value)) + kind + value + struct.pack('>I', zlib.crc32(kind + value))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 6, 0, 0, 0))
            + chunk(b'tEXt', PRIVATE_CODE.encode()) + chunk(b'IDAT', zlib.compress(bytes(5))) + chunk(b'IEND', b''))


class PublicBoundaryTests(unittest.TestCase):
    def test_actual_binding_export_does_not_publish_private_payload_or_image(self):
        spec = importlib.util.spec_from_file_location('export_operation', ROOT / '.devflow/export_operation.py')
        exporter = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(exporter)
        now = time.time()
        private_result = dict(ok=True, occurrence_id=OCCURRENCE, request_sha256=BINDING,
                              revision=2, issued_at=now, expires_at=now + 300, expires_in_seconds=300)
        with tempfile.TemporaryDirectory() as tmp:
            for result, status in [(private_result, 0), (dict(ok=False, code=PRIVATE_CODE), 1), (private_result, 1)]:
                with self.subTest(status=status, ok=result['ok']):
                    folder = Path(tmp)
                    with patch.object(exporter, 'read_helper', side_effect=lambda s, h, n, limit:
                                      json.dumps(result).encode() if n == 'result.json' else png()):
                        exporter.export('synthetic', 'synthetic', folder, 'issue-binding', status)
                    public = b''.join(p.read_bytes() for p in folder.iterdir())
                    leaked = any(v.encode() in public for v in (OCCURRENCE, BINDING, PRIVATE_CODE))
                    self.assertFalse(leaked, 'private value in complete public operate artifact')
                    self.assertEqual({p.name for p in folder.iterdir()}, {'result.json'})

    def test_control_dispatch_has_no_freeform_binding_fields(self):
        import yaml
        menu = yaml.load((ROOT / '.github/workflows/control.yml').read_text(), Loader=yaml.BaseLoader)
        fields = menu['on']['workflow_dispatch']['inputs']
        self.assertFalse({'id', 'request_sha', 'revision'} & fields.keys(), 'private fields in public control dispatch')
