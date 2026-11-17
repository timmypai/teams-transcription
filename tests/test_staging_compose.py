"""Render installed Compose templates with synthetic input; no daemon or secrets."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent


class StagingComposeTests(unittest.TestCase):
    def test_staging_and_onboarding_have_one_bounded_absolute_tmp_mount(self):
        docker = shutil.which('docker')
        self.assertIsNotNone(docker, 'Docker CLI/Compose required; no daemon needed')
        for name, service in (('compose.staging.yml', 'teams-transcription'),
                ('compose.onboarding.yml', 'codex-onboarding')):
            with self.subTest(template=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory)
                shutil.copyfile(ROOT/'deploy'/name, path/'compose.yml')
                shutil.copyfile(ROOT/'docker/chromium-seccomp.json', path/'chromium-seccomp.json')
                (path/'runtime.env').write_text('')
                (path/'image.env').write_text('TRANSCRIPTION_IMAGE=example.invalid/test@sha256:'+'a'*64+'\n')
                result = subprocess.run([docker, '--host=unix:///var/run/docker.sock', 'compose',
                    '--project-directory', directory, '--env-file', str(path/'image.env'),
                    '-f', str(path/'compose.yml'), 'config', '--format', 'json'],
                    env={'PATH':os.environ['PATH'], 'HOME':directory}, capture_output=True, timeout=30)
                self.assertEqual(result.returncode, 0, 'synthetic Compose render failed')
                value = json.loads(result.stdout)['services'][service]
                self.assertEqual(value['tmpfs'], ['/tmp:size=512m,mode=1777'])
                self.assertTrue(all(Path(item.split(':', 1)[0]).is_absolute() for item in value['tmpfs']))
                self.assertTrue(value['read_only'])
                self.assertEqual(value['cap_drop'], ['ALL'])
                self.assertIn('no-new-privileges:true', value['security_opt'])
                self.assertIn('seccomp:./chromium-seccomp.json', value['security_opt'])
