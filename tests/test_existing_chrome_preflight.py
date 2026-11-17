"""Symlink and namespace regressions without executing Chrome or changing modes."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools.check_existing_chrome import CHROME_PATHS, inspect_path, main, preflight_errors, snapshot


class ExistingChromePreflightTests(unittest.TestCase):
    def record(self, mode=0o755, *, link_mode=0o777, uid=0):
        target = snapshot(SimpleNamespace(st_mode=stat.S_IFREG | mode, st_uid=uid, st_gid=uid))
        return {'path': '/fixture/chrome', 'present': True, 'realpath': '/fixture/real-chrome',
                'lstat': snapshot(SimpleNamespace(st_mode=stat.S_IFLNK | link_mode, st_uid=uid, st_gid=uid)),
                'stat_follow': target}

    def test_actual_symlink_0777_is_separate_from_target_stat_and_realpath(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root, 'target')
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o500)
            os.close(descriptor)
            middle = Path(root, 'middle')
            middle.symlink_to('target')
            link = Path(root, 'launcher')
            link.symlink_to('middle')
            result = inspect_path(link)
            self.assertNotIn('error', result)
            self.assertTrue(result['lstat']['symlink'])
            self.assertEqual(result['lstat']['mode'], '0o777')
            self.assertFalse(result['stat_follow']['symlink'])
            self.assertFalse(result['stat_follow']['world_writable'])
            self.assertEqual(result['realpath'], str(target))
            self.assertEqual(result['readlink_f'], str(target))
            self.assertEqual(len(result['symlink_hops']), 2)

    def test_symlink_world_write_bit_does_not_block_safe_target(self):
        self.assertEqual(preflight_errors([self.record()]), [])

    def test_world_writable_real_target_is_blocked_even_if_link_mode_is_safe(self):
        record = self.record(0o777, link_mode=0o555)
        self.assertEqual(preflight_errors([record]), [{'path': record['path'],
            'realpath': record['realpath'], 'error': 'world_writable_target'}])

    def test_helper_without_suid_is_not_a_sandbox_failure(self):
        records = [self.record(), self.record(), self.record(0o755)]
        self.assertFalse(records[2]['stat_follow']['setuid'])
        self.assertEqual(preflight_errors(records), [])

    def test_absent_helper_does_not_block_user_namespace_candidate(self):
        self.assertEqual(preflight_errors([self.record(), self.record(),
            {'path': '/fixture/helper', 'present': False, 'error': 'path_unavailable'}]), [])

    def test_original_target_owner_and_group_write_checks_are_preserved(self):
        for record in (self.record(0o775), self.record(uid=1001)):
            self.assertEqual(preflight_errors([record]), [{'path': record['path'],
                'realpath': record['realpath'], 'error': 'target_ownership_or_group_write_invalid'}])

    def test_conflicting_stat_probes_are_reported_before_launch(self):
        record = {**self.record(), 'error': 'stat_probe_mismatch'}
        self.assertEqual(preflight_errors([record]), [{'path': record['path'], 'error': 'stat_probe_mismatch'}])

    def test_missing_required_executable_is_reported(self):
        record = inspect_path('/fixture/nonexistent-required-chrome')
        self.assertEqual(preflight_errors([record]), [{'path': record['path'], 'error': 'path_unavailable'}])

    def package_command(self, *args):
        if args[:2] == ('dpkg-query', '-S'):
            return 'google-chrome-stable: ' + args[2]
        if args[:2] == ('dpkg-query', '-W'):
            return '154.0.8037.97-1'
        if args == (CHROME_PATHS[0], '--version'):
            return 'Google Chrome 154.0.8037.97'
        raise AssertionError('unexpected command')

    def test_main_accepts_non_suid_helper_and_checks_existing_chrome_version(self):
        records = [{**self.record(), 'path': path} for path in CHROME_PATHS]
        output = io.StringIO()
        with patch('tools.check_existing_chrome.inspect_path', side_effect=records), \
                patch('tools.check_existing_chrome.command', side_effect=self.package_command) as command, \
                patch('tools.check_existing_chrome.package_file_checks', return_value=[]), \
                redirect_stdout(output):
            self.assertEqual(main(), 0)
        command.assert_any_call(CHROME_PATHS[0], '--version')
        report = json.loads(output.getvalue())
        self.assertFalse(report['helper_suid_required'])
        self.assertEqual(report['errors'], [])

    def test_main_never_executes_world_writable_launcher(self):
        records = [{**self.record(0o777), 'path': path} for path in CHROME_PATHS]
        output = io.StringIO()
        with patch('tools.check_existing_chrome.inspect_path', side_effect=records), \
                patch('tools.check_existing_chrome.command', side_effect=self.package_command) as command, \
                patch('tools.check_existing_chrome.package_file_checks', return_value=[]), \
                redirect_stdout(output):
            self.assertEqual(main(), 1)
        self.assertNotIn((CHROME_PATHS[0], '--version'), [call.args for call in command.call_args_list])
        report = json.loads(output.getvalue())
        self.assertEqual([e['error'] for e in report['errors']], ['world_writable_target'] * 3)
