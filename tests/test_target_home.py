"""Effective same-deployment empty home readiness; no token/account/model reads."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from deploy import target_runtime as runtime


class TargetHomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.root.chmod(0o700)
        runtime.initialize(self.root,'synthetic-private-admin-password')

    def test_initialize_prepares_effective_empty_private_home(self):
        home=self.root/'codex'
        self.assertTrue(home.is_dir(),'effective configured Codex home still missing after initialization')
        self.assertEqual(list(home.iterdir()),[])
        self.assertEqual(home.stat().st_mode&0o777,0o700)
        self.assertEqual((home.stat().st_uid,home.stat().st_gid),(os.getuid(),os.getgid()))

    def test_existing_configuration_and_home_preserved_without_reading_credentials(self):
        path=self.root/'config/runtime.yml';value=json.loads(path.read_text())
        value['codex_home']=str(self.root/'private'/'custom-codex')
        path.write_text(json.dumps(value));before=path.read_bytes()
        result=runtime.prepare_home(self.root)
        home=Path(value['codex_home']);marker=home/'auth.json'
        marker.write_text('synthetic-secret-not-to-read');marker.chmod(0o600)
        with patch.object(Path,'read_bytes',side_effect=AssertionError('must not read tokens')):
            second=runtime.prepare_home(self.root)
        self.assertTrue(result['codex_home_created'])
        self.assertFalse(second['codex_home_created'])
        self.assertEqual(second['codex_home'],str(home))
        self.assertEqual(path.read_bytes(),before)
        self.assertEqual(marker.read_text(),'synthetic-secret-not-to-read')
        self.assertTrue(second['codex_home_writable'])
        self.assertEqual(second['effective_allowed_egress_hosts'],[])
        self.assertTrue(second['egress_deny_all'])
        self.assertEqual(second['provider_authorization'],'NOT VERIFIED')

    def test_symlink_wrong_owner_or_group_and_nonprivate_directory_rejected(self):
        home=self.root/'codex';home.mkdir(mode=0o700,exist_ok=True)
        home.rmdir();home.symlink_to(self.root/'config',target_is_directory=True)
        with self.assertRaises(ValueError):runtime.prepare_home(self.root)
        home.unlink();home.mkdir(mode=0o755)
        with self.assertRaises(ValueError):runtime.prepare_home(self.root)
        home.chmod(0o700)
        original=os.fstat
        def wrong(fd):
            info=original(fd)
            if info.st_ino==home.stat().st_ino:
                values=list(info);values[4]=os.getuid()+1;return os.stat_result(values)
            return info
        with patch('os.fstat',side_effect=wrong):
            with self.assertRaises(ValueError):runtime.prepare_home(self.root)
        def group(fd):
            info=original(fd)
            if info.st_ino==home.stat().st_ino:
                values=list(info);values[5]=os.getgid()+1;return os.stat_result(values)
            return info
        with patch('os.fstat',side_effect=group):
            with self.assertRaises(ValueError):runtime.prepare_home(self.root)

    def test_effective_environment_home_is_used_and_outside_root_denied(self):
        with patch.dict(os.environ,{'ASSISTANT_CODEX_HOME':str(self.root/'effective')}):
            value=runtime.prepare_home(self.root)
            self.assertEqual(value['codex_home'],str(self.root/'effective'))
        with patch.dict(os.environ,{'ASSISTANT_CODEX_HOME':'/outside-codex'}):
            with self.assertRaises(ValueError):runtime.prepare_home(self.root)
