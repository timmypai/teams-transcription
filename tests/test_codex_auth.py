"""Onboarding guards and persistent fake auth metadata; no real grant/token reads."""
import os
import asyncio
import contextlib
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from teams_transcription import codex_auth as auth
from teams_transcription.assistant_config import AssistantSettings


class CodexAuthTests(unittest.TestCase):
    def settings(self,root):
        return AssistantSettings(data_root=str(root),codex_home=str(root/'codex'),
            admin_password_file=str(root/'password'),admin_session_secret_file=str(root/'session'),
            google_credentials_file=str(root/'google'))

    def test_no_grant_from_ci_noninteractive_or_missing_explicit_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=self.settings(Path(directory))
            with patch.object(auth.subprocess,'run',side_effect=AssertionError('must not run Codex')):
                for confirm,terminal,env in ((False,True,{}),(True,False,{}),(True,True,{'GITHUB_ACTIONS':'true'})):
                    with self.assertRaises(auth.AuthError):
                        auth.device_login(settings,private_terminal=True,confirmed=confirm,interactive=terminal,environ=env)
            self.assertFalse(Path(settings.codex_home).exists())
            with self.assertRaisesRegex(auth.AuthError,'disabled_container_logging'):
                auth.device_login(settings,confirmed=True,private_terminal=False,interactive=True,environ={})

    def test_authorization_created_during_preflight_is_not_overwritten_after_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=self.settings(Path(directory))
            def another_login_finishes(*args,**kwargs):
                f=Path(settings.codex_home)/'auth.json';f.write_text('SYNTHETIC');f.chmod(0o600)
                return {'login_ready':True}
            with patch.object(auth,'preflight',side_effect=another_login_finishes), \
                    patch.object(auth.subprocess,'run',side_effect=AssertionError('must not overwrite')):
                with self.assertRaisesRegex(auth.AuthError,'reauthorization_required'):
                    auth.device_login(settings,private_terminal=True,confirmed=True,interactive=True,environ={})

    def test_fake_login_uses_device_flow_only_and_survives_new_settings_instance(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings=self.settings(root)
            def preflight(*args,**kwargs):return {'login_ready':True,'auth_material_present':False,'checks':{}}
            def fake_login(command,**kwargs):
                self.assertEqual(command[1:3],['login','--device-auth'])
                self.assertEqual(kwargs['env']['CODEX_HOME'],settings.codex_home)
                file=Path(settings.codex_home)/'auth.json';file.write_text('SYNTHETIC-NOT-A-TOKEN');file.chmod(0o600)
                return SimpleNamespace(returncode=0)
            with patch.object(auth,'preflight',side_effect=preflight),patch.object(auth.subprocess,'run',side_effect=fake_login):
                result=auth.device_login(settings,private_terminal=True,confirmed=True,interactive=True,environ={})
            self.assertTrue(result['login_process_completed'])
            new_settings=self.settings(root)
            self.assertTrue(auth.private_auth_present(Path(new_settings.codex_home)))
            self.assertEqual(result['live_rx_tx'],'not_tested')
            # Missing-token contents are never read by metadata checks.
            with patch.object(Path,'read_text',side_effect=AssertionError('no token reads')):
                self.assertTrue(auth.private_auth_present(Path(new_settings.codex_home)))

    def test_existing_auth_needs_explicit_reauthorization_and_private_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            settings=self.settings(Path(directory));home=Path(settings.codex_home);home.mkdir(mode=0o700)
            file=home/'auth.json';file.write_text('SYNTHETIC');file.chmod(0o600)
            with self.assertRaisesRegex(auth.AuthError,'reauthorization_required'):
                auth.device_login(settings,private_terminal=True,confirmed=True,interactive=True,environ={})
            file.chmod(0o644)
            self.assertFalse(auth.private_auth_present(home))
            with self.assertRaisesRegex(auth.AuthError,'private_uid_gid_required'):
                auth.device_login(settings,private_terminal=True,confirmed=True,reauthorize=True,interactive=True,environ={})

    def test_preflight_checks_versions_mounts_permissions_without_auth_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings=self.settings(root);Path(settings.codex_home).mkdir(mode=0o700)
            mounts=f'1 1 0:1 / {root} rw - ext4 /dev/fixture rw\n2 1 0:2 / /dev/shm rw - tmpfs shm rw\n'
            def version(executable,pattern):
                return {'codex':auth.PINNED,'node':'18.20.0','chromium':'155.0.0.0','ffmpeg':'5.1.0'}.get(executable)
            with patch.object(auth,'software',return_value=({'python_supported':True},{})),patch.object(auth,'tool_version',side_effect=version):
                result=auth.preflight(settings,mounts=mounts)
                self.assertTrue(result['login_ready'])
                self.assertIn('chatgpt_authorization_required',result['error_codes'])
                self.assertEqual(result['account_admission'],'not_checked')
                self.assertFalse(auth.preflight(settings,mounts=mounts.replace(' rw - ext4',' ro - ext4'))['login_ready'])
                self.assertFalse(auth.preflight(settings,mounts='')['checks']['persistent_writable_mount'])

    def test_nested_ephemeral_or_readonly_home_overrides_persistent_ancestor(self):
        mounts='1 1 0:1 / /data rw - ext4 /dev/fixture rw\n2 1 0:2 / /data/codex rw - tmpfs tmpfs rw\n'
        self.assertFalse(auth.persisted(Path('/data/codex'),'/data',mounts))
        self.assertFalse(auth.persisted(Path('/data/codex'),'/data',mounts.replace('rw - tmpfs','ro - ext4')))
        self.assertTrue(auth.persisted(Path('/data/codex'),'/data',mounts.splitlines()[0]))

    def test_overlay_backed_external_docker_volume_is_not_container_rootfs(self):
        volume='1 1 0:1 /var/lib/docker/volumes/fixture/_data /data rw - overlay overlay rw\n'
        self.assertTrue(auth.persisted(Path('/data/codex'),'/data',volume))
        self.assertFalse(auth.persisted(Path('/data/codex'),'/data',volume.replace('/var/lib/docker/volumes/fixture/_data','/')))
        self.assertFalse(auth.persisted(Path('/data/codex'),'/data',volume.replace('/var/lib/docker/volumes/fixture/_data','/ephemeral-container-directory')))

    def test_ffmpeg_version_uses_its_supported_single_dash_option(self):
        def process(command,**kwargs):
            self.assertEqual(command,['ffmpeg','-version'])
            return SimpleNamespace(returncode=0,stdout='ffmpeg version 5.1.8 Copyright')
        with patch.object(auth.shutil,'which',return_value='/usr/bin/ffmpeg'),patch.object(auth.subprocess,'run',side_effect=process):
            self.assertEqual(auth.tool_version('ffmpeg',r'ffmpeg version ([0-9.]+)'),'5.1.8')

    def test_cancel_timeout_and_denial_do_not_claim_completed_authorization(self):
        for event,code in ((KeyboardInterrupt(),'cancelled'),(subprocess.TimeoutExpired('codex',600),'timeout'),
                (SimpleNamespace(returncode=1),'denied_or_failed')):
            with self.subTest(code=code),tempfile.TemporaryDirectory() as directory:
                settings=self.settings(Path(directory))
                kwargs={'side_effect':event} if isinstance(event,BaseException) else {'return_value':event}
                with patch.object(auth,'preflight',return_value={'login_ready':True}),patch.object(auth.subprocess,'run',**kwargs):
                    with self.assertRaisesRegex(auth.AuthError,code):
                        auth.device_login(settings,private_terminal=True,confirmed=True,interactive=True,environ={})

    def test_running_daemon_blocks_login_before_starting_native_flow(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings=self.settings(root)
            with auth.exclusive(root/'.assistant-daemon.lock','fixture'),patch.object(auth,'preflight',return_value={'login_ready':True}), \
                    patch.object(auth.subprocess,'run',side_effect=AssertionError('must not login')):
                with self.assertRaisesRegex(auth.AuthError,'stop_assistant'):
                    auth.device_login(settings,private_terminal=True,confirmed=True,interactive=True,environ={})

    def test_readonly_account_check_separates_auth_from_media_and_restores_home(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);settings=self.settings(root);home=Path(settings.codex_home);home.mkdir(mode=0o700)
            file=home/'auth.json';file.write_text('SYNTHETIC');file.chmod(0o600)
            class Provider:
                def __init__(self,settings):pass
                async def refresh(self):return False
                def describe(self):return {'auth_error_code':'chatgpt_auth_required','provider_status':'blocked'}
            @contextlib.contextmanager
            def runtime(**kwargs):yield str(root)
            with patch.object(auth,'preflight',return_value={'login_ready':True,'auth_material_private':True}), \
                    patch.object(auth.tempfile,'TemporaryDirectory',side_effect=runtime),patch.dict(os.environ,{'CODEX_HOME':'/old-fixture-home'}):
                result=asyncio.run(auth.check_account(settings,provider_factory=Provider))
                self.assertFalse(result['account_admitted'])
                self.assertEqual(result['next_action'],'reauthorize_in_private_terminal')
                self.assertEqual(result['live_rx_tx'],'not_tested')
                self.assertEqual(os.environ['CODEX_HOME'],'/old-fixture-home')


if __name__=='__main__':unittest.main()
