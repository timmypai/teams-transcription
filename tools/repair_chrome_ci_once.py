#!/usr/bin/env python3
"""2026-10-11 user-approved repair for workflow run number 28, attempt 1.

This is not a runtime repair or permission grant. All later runs refuse it.
No SUID, recursive operation, sandbox flags, or other OS policy changes.
"""
import json
import os
from pathlib import Path
import subprocess

from tools.check_existing_chrome import command, inspect_path, package_file_checks

TARGETS = ('/opt/google/chrome/google-chrome', '/opt/google/chrome/chrome',
           '/opt/google/chrome/chrome-sandbox')
REPO = 'paulpai0412/teams-transcription'
REF = 'refs/heads/codex/a-c-integration'
WORKFLOW_REF = REPO + '/.github/workflows/meeting-functional-validation.yml@' + REF
RUN_NUMBER = '28'
VALIDATION_SHA = 'df813ac6dc3824fd91371fa4df905be9cc4b8257'
VALIDATION_TREE = '6884e62e49783c6019e4d3299fbef37aa80d3c77'
APPROVAL_PATHS = {'.github/workflows/meeting-functional-validation.yml',
                 'tools/repair_chrome_ci_once.py', 'tests/test_chrome_ci_once.py'}


def require_authorized_run(env):
    required = {'GITHUB_ACTIONS': 'true', 'GITHUB_REPOSITORY': REPO,
        'GITHUB_REF': REF, 'GITHUB_EVENT_NAME': 'push', 'GITHUB_RUN_NUMBER': RUN_NUMBER,
        'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_WORKFLOW_REF': WORKFLOW_REF,
        'RUNNER_OS': 'Linux', 'ImageVersion': '20261004.327.1'}
    if any(env.get(key) != value for key, value in required.items()):
        raise RuntimeError('one_time_run_binding_mismatch')
    if not env.get('GITHUB_RUN_ID', '').isdigit() or not env.get('RUNNER_TEMP'):
        raise RuntimeError('one_time_run_metadata_missing')
    sha = env.get('GITHUB_SHA', '')
    if len(sha) != 40 or any(c not in '0123456789abcdef' for c in sha):
        raise RuntimeError('one_time_checkout_sha_invalid')
    if command('git', 'rev-parse', 'HEAD') != sha:
        raise RuntimeError('one_time_checkout_sha_mismatch')
    if command('git', 'rev-parse', 'HEAD^') != VALIDATION_SHA:
        raise RuntimeError('one_time_validation_parent_mismatch')
    if command('git', 'rev-parse', VALIDATION_SHA + '^{tree}') != VALIDATION_TREE:
        raise RuntimeError('one_time_validation_tree_mismatch')
    changed = set(command('git', 'diff', '--name-only', VALIDATION_SHA, 'HEAD').splitlines())
    if changed != APPROVAL_PATHS:
        raise RuntimeError('one_time_approval_changed_paths_mismatch')


def verified_records(*, mode):
    records = [inspect_path(path) for path in TARGETS]
    for target, record in zip(TARGETS, records):
        info = record.get('lstat', {})
        if (record.get('error') or not record['present'] or record.get('realpath') != target
                or not info.get('regular') or info.get('symlink')
                or info.get('uid') != 0 or info.get('gid') != 0 or info.get('mode') != mode):
            raise RuntimeError('one_time_target_identity_or_mode_mismatch')
        if command('dpkg-query', '-S', target) != 'google-chrome-stable: ' + target:
            raise RuntimeError('one_time_package_owner_mismatch')
    checks = package_file_checks(records)
    if len(checks) != 3 or any(check['md5_matches_package'] is not True for check in checks):
        raise RuntimeError('one_time_package_content_mismatch')
    return records, checks


def repair_once(env=None):
    env = os.environ if env is None else env
    require_authorized_run(env)
    marker = Path(env['RUNNER_TEMP'], 'meeting-chrome-repair-used-' + env['GITHUB_RUN_ID'])
    try:
        descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise RuntimeError('one_time_repair_already_attempted') from None
    os.close(descriptor)
    before, before_checks = verified_records(mode='0o777')
    identity = [(Path(path).stat().st_dev, Path(path).stat().st_ino) for path in TARGETS]
    print(json.dumps({'one_time_chrome_repair': 'before', 'run_id': env['GITHUB_RUN_ID'],
        'run_number': RUN_NUMBER, 'attempt': 1, 'sha': env['GITHUB_SHA'],
        'validation_sha': VALIDATION_SHA, 'validation_tree': VALIDATION_TREE,
        'files': before, 'package_checks': before_checks}), flush=True)
    # Exact user-approved operation: three physical files, no recursive chmod.
    subprocess.run(['sudo', '/usr/bin/chmod', '0755', '--', *TARGETS],
                   check=True, capture_output=True, text=True, timeout=10)
    after, after_checks = verified_records(mode='0o755')
    if identity != [(Path(path).stat().st_dev, Path(path).stat().st_ino) for path in TARGETS]:
        raise RuntimeError('one_time_target_identity_changed')
    print(json.dumps({'one_time_chrome_repair': 'after', 'run_id': env['GITHUB_RUN_ID'],
        'run_number': RUN_NUMBER, 'attempt': 1, 'sha': env['GITHUB_SHA'],
        'validation_sha': VALIDATION_SHA, 'validation_tree': VALIDATION_TREE,
        'files': after, 'package_checks': after_checks, 'suid_added': False,
        'sandbox_or_os_policy_changed': False}), flush=True)


def main():
    try:
        repair_once()
        return 0
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # Only fixed internal codes; never command output or exception text.
        safe = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith('one_time_') else 'one_time_repair_failed'
        print(json.dumps({'one_time_chrome_repair': 'FAIL', 'error_code': safe}), flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
