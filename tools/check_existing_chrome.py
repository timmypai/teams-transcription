#!/usr/bin/env python3
"""Read-only audit of the runner's existing Chrome; never change permissions.

Symlink modes and helper SUID availability are observations, not sandbox tests.
Only actual launch with the project's sandbox=True policy tests compatibility.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
from types import SimpleNamespace

CHROME_PATHS = ('/usr/bin/google-chrome', '/opt/google/chrome/chrome',
                '/opt/google/chrome/chrome-sandbox')


def snapshot(info):
    return {'mode': oct(stat.S_IMODE(info.st_mode)), 'uid': info.st_uid,
            'gid': info.st_gid, 'regular': stat.S_ISREG(info.st_mode),
            'symlink': stat.S_ISLNK(info.st_mode),
            'world_writable': bool(info.st_mode & stat.S_IWOTH),
            'group_writable': bool(info.st_mode & stat.S_IWGRP),
            'executable': bool(info.st_mode & 0o111),
            'setuid': bool(info.st_mode & stat.S_ISUID)}


def command(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True,
                          timeout=5).stdout.strip()


def system_stat(path, *, follow):
    args = ['stat'] + (['-L'] if follow else []) + ['-c', '%f %u %g', str(path)]
    mode, uid, gid = command(*args).split()
    return snapshot(SimpleNamespace(st_mode=int(mode, 16), st_uid=int(uid), st_gid=int(gid)))


def inspect_path(path):
    path = Path(path)
    result = {'path': str(path), 'present': False}
    try:
        result['lstat'] = snapshot(path.lstat())
        result['present'] = True
        resolved = path.resolve(strict=True)
        result['realpath'] = str(resolved)
        result['readlink_f'] = command('readlink', '-f', str(path))
        result['stat_follow'] = snapshot(path.stat(follow_symlinks=True))
        result['realpath_lstat'] = snapshot(resolved.lstat())
        result['system_lstat'] = system_stat(path, follow=False)
        result['system_stat_follow'] = system_stat(path, follow=True)
        result['realpath_system_stat'] = system_stat(resolved, follow=True)
        result['effective_write_access'] = os.access(resolved, os.W_OK, effective_ids=True)
        result['filesystem'] = command('stat', '-f', '-c', '%T %t', str(resolved))
        result['mount'] = mount_info(resolved)
        hops, cursor = [], path
        for _ in range(16):
            if not cursor.is_symlink():
                break
            target = cursor.readlink()
            hops.append({'path': str(cursor), 'target': str(target)})
            cursor = target if target.is_absolute() else cursor.parent / target
        result['symlink_hops'] = hops
        if (result['realpath'] != result['readlink_f']
                or result['lstat'] != result['system_lstat']
                or any(result['stat_follow'] != result[key] for key in
                       ('realpath_lstat', 'system_stat_follow', 'realpath_system_stat'))):
            result['error'] = 'stat_probe_mismatch'
    except FileNotFoundError:
        result['error'] = 'path_unavailable'
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        result['error'] = 'path_probe_failed'
    return result


def mount_info(path):
    """Only mount type and standard flags, never sources or overlay paths."""
    candidate = None
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        left, right = line.split(' - ', 1)
        fields, extra = left.split(), right.split()
        mountpoint = fields[4].replace(r'\040', ' ').replace(r'\134', '\\')
        if str(path) == mountpoint or str(path).startswith(mountpoint.rstrip('/') + '/'):
            if candidate is None or len(mountpoint) > len(candidate['target']):
                flags = (fields[5] + ',' + extra[2]).split(',')
                candidate = {'target': mountpoint, 'fstype': extra[0],
                    'flags': sorted(set(flags) & {'ro', 'rw', 'nosuid', 'nodev', 'noexec',
                        'relatime', 'noatime', 'strictatime'})}
    return candidate


def package_file_checks(records):
    expected = {}
    for line in Path('/var/lib/dpkg/info/google-chrome-stable.md5sums').read_text().splitlines():
        digest, name = line.split(maxsplit=1)
        expected['/' + name.lstrip('/')] = digest
    result = []
    for record in records:
        name = record.get('realpath')
        if name is None:
            continue
        if name not in expected:
            result.append({'realpath': name, 'md5_matches_package': None})
            continue
        digest = hashlib.md5(usedforsecurity=False)
        with Path(name).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        result.append({'realpath': name, 'md5_matches_package': digest.hexdigest() == expected[name]})
    return result


def preflight_errors(records):
    errors = []
    for index, record in enumerate(records):
        # An absent helper is allowed: Chromium may use a user namespace.
        if index == 2 and not record['present']:
            continue
        if record.get('error'):
            errors.append({'path': record['path'], 'error': record['error']})
            continue
        target = record['stat_follow']
        if target['world_writable']:
            errors.append({'path': record['path'], 'realpath': record['realpath'],
                           'error': 'world_writable_target'})
        elif target['group_writable'] or target['uid'] != 0:
            errors.append({'path': record['path'], 'realpath': record['realpath'],
                           'error': 'target_ownership_or_group_write_invalid'})
        elif not target['regular'] or not target['executable']:
            errors.append({'path': record['path'], 'realpath': record['realpath'],
                           'error': 'not_regular_executable'})
    return errors


def main():
    records = [inspect_path(path) for path in CHROME_PATHS]
    errors = preflight_errors(records)
    status = dict(line.split(':', 1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    report = {'existing_chrome_files': records, 'errors': errors,
              'image': os.environ.get('ImageVersion', 'unknown'), 'uid': os.getuid(),
              'NoNewPrivs': status['NoNewPrivs'].strip(), 'Seccomp': status['Seccomp'].strip(),
              'helper_suid_required': False, 'security_settings_changed': False}
    try:
        owners = {path: command('dpkg-query', '-S', path) for path in CHROME_PATHS[1:]
                  if records[CHROME_PATHS.index(path)]['present']}
        report['package_owners'] = owners
        report['package_version'] = command('dpkg-query', '-W', '-f=${Version}', 'google-chrome-stable')
        report['package_file_checks'] = package_file_checks(records)
        if any(check['md5_matches_package'] is False for check in report['package_file_checks']):
            errors.append({'error': 'chrome_package_file_mismatch'})
        if any(owner != 'google-chrome-stable: ' + path for path, owner in owners.items()):
            errors.append({'error': 'chrome_package_mismatch'})
        # Do not execute a launcher whose real target failed the file audit.
        if not errors:
            version = command(CHROME_PATHS[0], '--version')
            if not re.fullmatch(r'Google Chrome \d+\.\d+\.\d+\.\d+', version):
                errors.append({'error': 'unexpected_chrome_version'})
            else:
                report['version'] = version
    except (OSError, ValueError, subprocess.SubprocessError):
        errors.append({'error': 'chrome_package_probe_failed'})
    print(json.dumps(report), flush=True)
    return 1 if errors else 0


if __name__ == '__main__':
    raise SystemExit(main())
