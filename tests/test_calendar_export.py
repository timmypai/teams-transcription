"""Host export must refuse malformed or private metadata even with valid PNG."""
import importlib.util
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('export_operation', ROOT/'.devflow/export_operation.py')
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


def png():
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data))
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',1,1,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(b'\0\0\0\0'))+chunk(b'IEND',b'')


def valid():
    return dict(ok=True,timezone='UTC',poll_id='a'*32,google=dict(configured=True,authorized=True,
        reauthorization_required=False,error_code=None),enabled_calendar_count=1,
        read=dict(attempted=1,succeeded=1,failed=0,event_count=0,completed=True),error_codes=[],ready=True,occurrences=[])


class CalendarExportTests(unittest.TestCase):
    def export(self, result):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(exporter, 'read_helper', side_effect=lambda socket,helper,name,limit:
                    json.dumps(result).encode() if name=='result.json' else png()):
                code = exporter.export('socket','helper',folder,'calendar-occurrences',0)
            return code, json.loads((Path(folder)/'result.json').read_text()), (Path(folder)/'calendar-occurrences.png').exists()

    def test_occurrence_export_requires_an_integer_revision(self):
        occurrence = dict(occurrence_id='a'*32, revision=1, joinable=True,
            start_at='2030-01-01T09:00:00+00:00', end_at='2030-01-01T10:00:00+00:00')
        value = {**valid(), 'occurrences':[occurrence]}
        self.assertEqual(self.export(value), (0,value,True))
        missing = {key:item for key,item in occurrence.items() if key!='revision'}
        for row in [missing, *[{**occurrence,'revision':revision} for revision in (True,'1',0,2**53)]]:
            with self.subTest(row=row):
                self.assertEqual(self.export({**value,'occurrences':[row]}),
                    (1,dict(ok=False,code='operation_export_failed'),False))

    def test_missing_unknown_private_or_inconsistent_fields_fail_closed(self):
        cases = []
        for key in valid():
            value = valid(); del value[key]; cases.append(value)
        value = valid(); value['private'] = 'PRIVATE'; cases.append(value)
        value = valid(); value['google']['error_code'] = 'private_code'; cases.append(value)
        value = valid(); value['read']['succeeded'] = 0; cases.append(value)
        value = valid(); value['google']['authorized'] = 'true'; cases.append(value)
        value = valid(); value['timezone'] = 'PRIVATE@example.com'; cases.append(value)
        value = valid(); value['occurrences'] = [dict(occurrence_id='PRIVATE',start_at='PRIVATE',end_at='PRIVATE',joinable=True)]; cases.append(value)
        for value in cases:
            with self.subTest(value=value):
                code, result, screenshot = self.export(value)
                self.assertEqual(code,1)
                self.assertEqual(result,dict(ok=False,code='operation_export_failed'))
                self.assertFalse(screenshot)

    def test_ready_and_not_attempted_are_exportable_but_distinct(self):
        for ready in (True, False):
            value = valid()
            if not ready:
                value.update(ready=False,enabled_calendar_count=0,read=dict(attempted=0,succeeded=0,failed=0,event_count=0,completed=True))
            code, result, screenshot = self.export(value)
            self.assertEqual(code,0)
            self.assertEqual(result,value)
            self.assertTrue(screenshot)
