"""Exercise one-time scope and exact chmod arguments with no privileged action."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from tools import repair_chrome_ci_once as repair


class OneTimeChromeRepairTests(unittest.TestCase):
    def env(self, root):
        return {'GITHUB_ACTIONS':'true', 'GITHUB_REPOSITORY':repair.REPO,
            'GITHUB_REF':repair.REF, 'GITHUB_EVENT_NAME':'push', 'GITHUB_RUN_NUMBER':repair.RUN_NUMBER,
            'GITHUB_RUN_ATTEMPT':'1', 'GITHUB_WORKFLOW_REF':repair.WORKFLOW_REF,
            'RUNNER_OS':'Linux', 'ImageVersion':'20261004.327.1',
            'GITHUB_RUN_ID':'123456', 'GITHUB_SHA':'a'*40, 'RUNNER_TEMP':root}

    def git_command(self, *args):
        if args == ('git', 'rev-parse', 'HEAD'):
            return 'a'*40
        if args == ('git', 'rev-parse', 'HEAD^'):
            return repair.VALIDATION_SHA
        if args == ('git', 'rev-parse', repair.VALIDATION_SHA + '^{tree}'):
            return repair.VALIDATION_TREE
        if args == ('git', 'diff', '--name-only', repair.VALIDATION_SHA, 'HEAD'):
            return '\n'.join(sorted(repair.APPROVAL_PATHS))
        raise AssertionError('Unexpected command')

    def test_later_run_rerun_other_ref_or_non_ci_rejected_before_any_action(self):
        for change in ({'GITHUB_RUN_NUMBER':'14'}, {'GITHUB_RUN_NUMBER':'27'}, {'GITHUB_RUN_NUMBER':'29'}, {'GITHUB_RUN_ATTEMPT':'2'},
                       {'GITHUB_REF':'refs/heads/main'}, {'GITHUB_ACTIONS':'false'},
                       {'GITHUB_EVENT_NAME':'pull_request'}, {'GITHUB_REPOSITORY':'other/repo'},
                       {'ImageVersion':'other-image'}):
            with self.subTest(change=change), patch.object(repair, 'command') as command, \
                    patch.object(repair.subprocess, 'run') as mutation:
                with self.assertRaisesRegex(RuntimeError, 'run_binding_mismatch'):
                    repair.repair_once({**self.env('/fixture'), **change})
                command.assert_not_called()
                mutation.assert_not_called()

    def test_checkout_mismatch_rejected_before_mutation(self):
        with patch.object(repair, 'command', return_value='b'*40), \
                patch.object(repair.subprocess, 'run') as mutation:
            with self.assertRaisesRegex(RuntimeError, 'checkout_sha_mismatch'):
                repair.repair_once(self.env('/fixture'))
            mutation.assert_not_called()

    def test_wrong_parent_tree_or_application_changes_rejected_before_mutation(self):
        targets = [
            (('git','rev-parse','HEAD^'), 'b'*40, 'validation_parent_mismatch'),
            (('git','rev-parse',repair.VALIDATION_SHA+'^{tree}'), 'b'*40, 'validation_tree_mismatch'),
            (('git','diff','--name-only',repair.VALIDATION_SHA,'HEAD'),
             '\n'.join(sorted(repair.APPROVAL_PATHS | {'teams_transcription/provider_state.py'})),
             'approval_changed_paths_mismatch'),
        ]
        for target, value, code in targets:
            def command(*args):
                return value if args == target else self.git_command(*args)
            with self.subTest(code=code), patch.object(repair, 'command', side_effect=command), \
                    patch.object(repair.subprocess, 'run') as mutation:
                with self.assertRaisesRegex(RuntimeError, code):
                    repair.repair_once(self.env('/fixture'))
                mutation.assert_not_called()

    def test_all_targets_verified_before_exact_three_file_chmod_and_no_second_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            env = self.env(root)
            output = io.StringIO()
            with patch.object(repair, 'command', side_effect=self.git_command), \
                    patch.object(repair, 'verified_records', side_effect=[([], []), ([], [])]) as verify, \
                    patch.object(repair.Path, 'stat', return_value=SimpleNamespace(st_dev=1, st_ino=2)), \
                    patch.object(repair.subprocess, 'run') as mutation, redirect_stdout(output):
                repair.repair_once(env)
                mutation.assert_called_once_with(['sudo', '/usr/bin/chmod', '0755', '--', *repair.TARGETS],
                    check=True, capture_output=True, text=True, timeout=10)
                self.assertEqual([call.kwargs for call in verify.call_args_list], [{'mode':'0o777'}, {'mode':'0o755'}])
                with self.assertRaisesRegex(RuntimeError, 'already_attempted'):
                    repair.repair_once(env)
                self.assertEqual(mutation.call_count, 1)
            records = [json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual([r['one_time_chrome_repair'] for r in records], ['before', 'after'])
            self.assertFalse(records[-1]['suid_added'])

    def test_failed_preverification_does_not_chmod_or_retry(self):
        with tempfile.TemporaryDirectory() as root, patch.object(repair, 'command', side_effect=self.git_command), \
                patch.object(repair, 'verified_records', side_effect=RuntimeError('one_time_package_content_mismatch')), \
                patch.object(repair.subprocess, 'run') as mutation:
            with self.assertRaisesRegex(RuntimeError, 'package_content_mismatch'):
                repair.repair_once(self.env(root))
            with self.assertRaisesRegex(RuntimeError, 'already_attempted'):
                repair.repair_once(self.env(root))
            mutation.assert_not_called()

    def test_unexpected_symlink_target_owner_and_mode_are_rejected(self):
        base = {'path':repair.TARGETS[0], 'present':True, 'realpath':repair.TARGETS[0],
            'lstat':{'regular':True, 'symlink':False, 'uid':0, 'gid':0, 'mode':'0o777'}}
        variants = [{**base, 'realpath':'/fixture/other'}, {**base, 'lstat':{**base['lstat'], 'symlink':True}},
            {**base, 'lstat':{**base['lstat'], 'uid':1001}}, {**base, 'lstat':{**base['lstat'], 'gid':1001}},
            {**base, 'present':False}, {**base, 'error':'stat_probe_mismatch'},
            {**base, 'lstat':{**base['lstat'], 'regular':False}},
            {**base, 'lstat':{**base['lstat'], 'mode':'0o4755'}},
            {**base, 'lstat':{**base['lstat'], 'mode':'0o755'}}]
        for value in variants:
            with self.subTest(value=value), patch.object(repair, 'inspect_path', return_value=value), \
                    patch.object(repair, 'command') as command:
                with self.assertRaisesRegex(RuntimeError, 'target_identity_or_mode_mismatch'):
                    repair.verified_records(mode='0o777')
                command.assert_not_called()

    def test_mismatched_package_bytes_are_rejected(self):
        def inspect(path):
            return {'path':path, 'present':True, 'realpath':path,
                'lstat':{'regular':True, 'symlink':False, 'uid':0, 'gid':0, 'mode':'0o777'}}
        with patch.object(repair, 'inspect_path', side_effect=inspect), \
                patch.object(repair, 'command', side_effect=lambda *args: 'google-chrome-stable: '+args[2]), \
                patch.object(repair, 'package_file_checks', return_value=[{'md5_matches_package':False}]*3):
            with self.assertRaisesRegex(RuntimeError, 'package_content_mismatch'):
                repair.verified_records(mode='0o777')

    def test_missing_run_metadata_or_invalid_sha_never_performs_mutation(self):
        for change in ({'GITHUB_RUN_ID':''}, {'GITHUB_RUN_ID':'not-a-number'},
                       {'RUNNER_TEMP':''}, {'GITHUB_SHA':'short'}, {'GITHUB_SHA':'A'*40}):
            with self.subTest(change=change), patch.object(repair, 'command') as command, \
                    patch.object(repair.subprocess, 'run') as mutation:
                with self.assertRaisesRegex(RuntimeError, 'one_time_'):
                    repair.repair_once({**self.env('/fixture'), **change})
                command.assert_not_called()
                mutation.assert_not_called()

    def test_wrong_official_package_owner_rejected_without_chmod(self):
        def inspect(path):
            return {'path':path, 'present':True, 'realpath':path,
                'lstat':{'regular':True, 'symlink':False, 'uid':0, 'gid':0, 'mode':'0o777'}}
        with patch.object(repair, 'inspect_path', side_effect=inspect), \
                patch.object(repair, 'command', return_value='other-package: '+repair.TARGETS[0]), \
                patch.object(repair, 'package_file_checks') as md5, \
                patch.object(repair.subprocess, 'run') as mutation:
            with self.assertRaisesRegex(RuntimeError, 'package_owner_mismatch'):
                repair.verified_records(mode='0o777')
            md5.assert_not_called()
            mutation.assert_not_called()

