"""Real host exporter refuses private or malformed settings metadata."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_calendar_export import exporter,png


def safe():
    return dict(ok=True,scheduler_paused=True,saved=False,calendars=[dict(calendar_ref='a'*32,
        revision=1,primary=False,enabled=True,recording_authorized=False,speaking_authorized=False,
        join_early_seconds=300,max_lateness_seconds=300)])


class SettingsExportTests(unittest.TestCase):
    def test_private_fields_bad_types_and_unknown_failure_refuse(self):
        for defect in ('none','extra','row-extra','bad-ref','bad-type','failure-canary','private-reason','missing'):
            value = copy.deepcopy(safe())
            if defect=='extra':value['private']='PRIVATE-canary'
            if defect=='row-extra':value['calendars'][0]['calendar_id']='PRIVATE-canary'
            if defect=='bad-ref':value['calendars'][0]['calendar_ref']='PRIVATE-canary'
            if defect=='bad-type':value['calendars'][0]['enabled']=1
            if defect=='failure-canary':value=dict(ok=False,code='private_secret_canary')
            if defect=='private-reason':value=dict(ok=False,code='calendar_settings_save_failed',reason='PRIVATE-canary')
            if defect=='missing':del value['saved']
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as folder, \
                    patch.object(exporter,'read_helper',side_effect=lambda socket,helper,name,limit:
                    json.dumps(value).encode() if name=='result.json' else png()):
                code = exporter.export('socket','helper',folder,'calendar-settings',0)
                result = json.loads((Path(folder)/'result.json').read_text())
                self.assertEqual(code,0 if defect=='none' else 1)
                self.assertNotIn('canary',json.dumps(result))
                self.assertFalse((Path(folder)/'calendar-settings.png').exists())
                if defect=='none':self.assertNotIn('calendar_ref',result['calendars'][0])
