#!/usr/bin/python3
"""Installed VM controller. Fixed commands only; never import checkout code.

Run as the dedicated non-root rootless-Docker owner. This is an operational
boundary, not a security sandbox against workflows executing as that same user.
All stdout is a small metadata allowlist; subprocess output/errors stay private.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import selectors
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

IMAGE = 'ghcr.io/paulpai0412/teams-transcription'
SOURCE = 'https://github.com/paulpai0412/teams-transcription'
INSTALL = Path('/opt/teams-staging/compose')
STATE = Path('/srv/teams-staging/state')
VOLUME = 'teams-staging-data'
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


class Refused(Exception):
    def __init__(self, *args, docker_error=None):
        super().__init__(*args)
        self.docker_error = docker_error


def docker_operation(args):
    """Only fixed vocabulary leaves this boundary, never command arguments."""
    if args and args[0] == 'compose':
        for name in ('create', 'start', 'up', 'stop', 'ps', 'config'):
            if name in args:
                return 'compose_' + name
        return 'compose_other'
    names = {('info',):'runtime_info', ('login',):'registry_login',
        ('pull',):'image_pull', ('image', 'inspect'):'image_inspect',
        ('volume', 'inspect'):'volume_inspect', ('run',):'helper_run',
        ('network', 'inspect'):'network_inspect',
        ('rm',):'helper_remove', ('container', 'ls'):'container_list',
        ('inspect',):'container_inspect', ('stats',):'container_stats'}
    for prefix, name in names.items():
        if tuple(args[:len(prefix)]) == prefix:
            return name
    return 'unknown'


def docker_error_report(args, result):
    """Classify a bounded byte sample; no raw/sanitized-text fallback."""
    category = 'unclassified'
    if result['timed_out']:
        category = 'command_timeout'
    elif result['stdout_truncated']:
        category = 'stdout_limit'
    else:
        sample = result['stderr'].decode('utf-8', errors='replace').lower()
        patterns = (
            ('mount_path_not_absolute', r'invalid mount path:[^\n]*mount path must be absolute'),
            ('network_create_failed', r'failed to create network'),
            ('mount_source_missing', r'bind source path does not exist|invalid mount config'),
            ('port_conflict', r'address already in use|port is already allocated|ports are not available'),
            ('compose_flag_unsupported', r'unknown flag|unknown shorthand flag'),
            ('compose_config_invalid', r'invalid compose project|error while interpolating|yaml: line'),
            ('container_runtime_create_failed', r'oci runtime create failed|failed to create task for container'),
            ('registry_access_denied', r'unauthorized:|denied: requested access|authentication required'),
            ('image_unavailable', r'no such image|manifest unknown'),
            ('daemon_unavailable', r'cannot connect to the docker daemon|is the docker daemon running'),
            ('permission_denied', r'permission denied|operation not permitted'),
        )
        for name, pattern in patterns:
            if re.search(pattern, sample):
                category = name
                break
    code = result['returncode']
    return {'operation':docker_operation(args), 'category':category,
        'exit_code':code if type(code) is int and 0 <= code <= 255 else None,
        'stderr_truncated':result['stderr_truncated'],
        'stdout_truncated':result['stdout_truncated']}


def public_docker_error(value):
    operations = {'compose_start','compose_create','compose_up','compose_stop','compose_ps','compose_config',
        'compose_other','runtime_info','registry_login','image_pull','image_inspect',
        'volume_inspect','helper_run','helper_remove','container_list','container_inspect',
        'container_stats','network_inspect','unknown'}
    categories = {'unclassified','command_timeout','stdout_limit','network_create_failed',
        'mount_source_missing','mount_path_not_absolute','port_conflict','compose_flag_unsupported','compose_config_invalid',
        'container_runtime_create_failed','registry_access_denied','image_unavailable',
        'daemon_unavailable','permission_denied'}
    if (not isinstance(value, dict) or set(value) != {'operation','category','exit_code',
            'stderr_truncated','stdout_truncated'} or not isinstance(value['operation'], str)
            or not isinstance(value['category'], str) or value['operation'] not in operations
            or value['category'] not in categories
            or value['exit_code'] is not None and (type(value['exit_code']) is not int
                or not 0 <= value['exit_code'] <= 255)
            or any(type(value[k]) is not bool for k in ('stderr_truncated','stdout_truncated'))):
        return None
    return dict(value)


def capture_docker(args, env, *, input=None, timeout=60):
    """Drain both pipes without raw disk files or unbounded capture memory."""
    process = None
    buffers = {'stdout':bytearray(), 'stderr':bytearray()}
    limits = {'stdout':65536, 'stderr':16384}
    truncated = {'stdout':False, 'stderr':False}
    deadline = time.monotonic() + timeout
    sent = 0
    timed_out = complete = cleanup_uncertain = False
    pending = None
    selector = selectors.DefaultSelector()
    try:
        process = subprocess.Popen(['/usr/bin/docker', *args], env=env,
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        for name in ('stdout', 'stderr'):
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        if input is not None:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            for key, _ in selector.select(remaining):
                pipe, name = key.fileobj, key.data
                if name == 'stdin':
                    try:
                        sent += os.write(pipe.fileno(), input[sent:sent+4096])
                    except BrokenPipeError:
                        sent = len(input)
                    if sent == len(input):
                        selector.unregister(pipe)
                        pipe.close()
                    continue
                chunk = os.read(pipe.fileno(), 4096)
                if not chunk:
                    selector.unregister(pipe)
                    pipe.close()
                    continue
                room = limits[name] - len(buffers[name])
                buffers[name].extend(chunk[:room])
                truncated[name] |= len(chunk) > room
            if truncated['stdout']:
                break
        if not timed_out and not truncated['stdout']:
            try:
                process.wait(timeout=max(.001, deadline-time.monotonic()))
                complete = True
            except subprocess.TimeoutExpired:
                timed_out = True
    except BaseException as failure:
        pending = failure
        raise
    finally:
        selector.close()
        if process is not None and not complete:
            # This session contains only this invocation's CLI/plugin children.
            # Docker daemon/container processes are outside its process group.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                cleanup_uncertain = True
        if process is not None:
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe:
                    pipe.close()
        if cleanup_uncertain and isinstance(pending, Refused) and len(pending.args) == 1:
            pending.args += ('docker_cli_cleanup_uncertain',)
    return {'returncode':process.returncode, 'stdout':bytes(buffers['stdout']),
        'stderr':bytes(buffers['stderr']), 'stdout_truncated':truncated['stdout'],
        'stderr_truncated':truncated['stderr'], 'timed_out':timed_out,
        'cleanup_uncertain':cleanup_uncertain}


def image_ref(digest):
    if not isinstance(digest, str) or not DIGEST.fullmatch(digest):
        raise Refused('invalid_image_digest')
    return IMAGE + '@' + digest


def checked_ref(value):
    if not isinstance(value, str) or not value.startswith(IMAGE + '@'):
        raise Refused('invalid_image_reference')
    return image_ref(value[len(IMAGE) + 1:])


def schema_hash(db):
    rows = db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()


def database_report(root, backup=None):
    """Only booleans/hash leave the volume. A ro connection never migrates data."""
    path = Path(root) / '.meeting-store.sqlite3'
    if not path.exists():
        return {'database_present': False, 'quiescent': True, 'schema': None,
            'backup_created': False}
    with contextlib.closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=5)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        policy = json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0])
        paused = policy.get('scheduler_paused') is True
        active = db.execute("SELECT EXISTS(SELECT 1 FROM meetings WHERE status NOT IN "
            "('completed','failed','stopped'))").fetchone()[0]
        leases = db.execute("SELECT EXISTS(SELECT 1 FROM execution_leases WHERE state='active')").fetchone()[0]
        jobs = db.execute("SELECT EXISTS(SELECT 1 FROM summary_jobs WHERE state IN ('pending','running'))").fetchone()[0]
        # Unknown cleanup must be reviewed through the application before deployment.
        rows = db.execute("SELECT r.session_id,r.state,r.ended_at,r.end_reason,r.error_code,"
            "s.status_json,c.review_stamp FROM assistant_runs r LEFT JOIN session_runtime s "
            "ON s.meeting_id=r.session_id LEFT JOIN capture_cleanup_reviews c ON c.session_id=r.session_id "
            "WHERE r.end_reason='service_interrupted' OR r.error_code='capture_cleanup_unknown' "
            "OR json_extract(s.status_json,'$.cleanup_status')='unknown' OR c.session_id IS NOT NULL")
        interlock = False
        for row in rows:
            value = dict(zip(('session_id','state','ended_at','end_reason','error_code','status_json'), row[:6]))
            stamp = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                separators=(',', ':')).encode()).hexdigest()
            if row[1] not in {'completed','failed','stopped','interrupted'} or row[2] is None or row[6] != stamp:
                interlock = True
        quiet = paused and not (active or leases or jobs or interlock)
        report = {'database_present': True, 'scheduler_paused': paused,
            'active_meeting': bool(active), 'active_lease': bool(leases),
            'pending_summary': bool(jobs), 'unreviewed_cleanup': bool(interlock),
            'quiescent': bool(quiet), 'schema': schema_hash(db), 'backup_created': False}
        db.rollback()
        if backup:
            if not quiet or not re.fullmatch(r'[0-9a-f]{32}', backup):
                raise Refused('backup_precondition_failed')
            directory = Path(root) / 'deployment-backups'
            directory.mkdir(mode=0o700, exist_ok=True)
            destination = directory / (backup + '.sqlite3')
            # UUID names, no overwrite or restore of live data.
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with contextlib.closing(sqlite3.connect(destination)) as target:
                db.backup(target)
                if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise Refused('backup_integrity_failed')
            report['backup_created'] = True
        return report


def volume_report(root, backup=None, maintenance=None):
    """Serialize with application claim_run/claim_summary using their actual lock."""
    root = Path(root)
    lock = root / '.meeting-store.lock'
    writable = backup is not None or maintenance is not None
    if not lock.exists() and not writable:
        if (root / '.meeting-store.sqlite3').exists():
            raise Refused('database_lock_missing')
        return database_report(root)
    fd = os.open(lock, (os.O_CREAT | os.O_RDWR) if writable else os.O_RDONLY, 0o600)
    try:
        fcntl.flock(fd, (fcntl.LOCK_EX if writable else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        marker = root / '.deployment-maintenance'
        report = database_report(root, backup)
        if maintenance == 'enable':
            if report['quiescent']:
                marker.touch(mode=0o600, exist_ok=True)
        elif maintenance == 'disable':
            if not report['quiescent']:
                raise Refused('maintenance_release_not_quiescent')
            marker.unlink(missing_ok=True)
        report['maintenance'] = marker.exists()
        return report
    finally:
        os.close(fd)


class Controller:
    def __init__(self, docker_config):
        uid = os.getuid()
        if uid == 0:
            raise Refused('root_runner_rejected')
        socket = Path(f'/run/user/{uid}/docker.sock')
        if not socket.is_socket() or socket.stat().st_uid != uid:
            raise Refused('rootless_socket_required')
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': pwd.getpwuid(uid).pw_dir,
            'XDG_RUNTIME_DIR': f'/run/user/{uid}', 'DOCKER_HOST': f'unix://{socket}',
            'DOCKER_CONFIG': docker_config}
        info = json.loads(self.docker('info', '--format',
            '{"security":{{json .SecurityOptions}},"version":{{json .CgroupVersion}},'
            '"driver":{{json .CgroupDriver}}}'))
        if 'name=rootless' not in info['security']:
            raise Refused('rootless_daemon_required')
        if info['version'] != '2' or info['driver'] != 'systemd':
            raise Refused('rootless_cgroup2_systemd_required')
        if self.docker('volume', 'inspect', '--format', '{{.Name}}', VOLUME) != VOLUME:
            raise Refused('initialized_staging_volume_required')
        for path in (Path('/opt'), INSTALL.parent, INSTALL, INSTALL.parent/'bin'):
            if path.is_symlink() or not path.is_dir() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
                raise Refused('untrusted_installation_directory')
        for name in ('compose.yml', 'runtime.env', 'chromium-seccomp.json', 'compose.onboarding.yml'):
            path = INSTALL / name
            # Parent directories must also be administrator-owned; see installation contract.
            if path.is_symlink() or not path.is_file() or path.stat().st_uid != 0 or path.stat().st_mode & 0o022:
                raise Refused('untrusted_installed_configuration')
        for name in ('release_gate.py','diagnostic_projection.py'):
            path = INSTALL.parent/'bin'/name
            if path.is_symlink() or not path.is_file() or path.stat().st_uid!=0 or path.stat().st_mode & 0o022:
                raise Refused('untrusted_installed_support_module')

    def docker(self, *args, timeout=60, input=None):
        result = capture_docker(args, self.env, input=input, timeout=timeout)
        if result['timed_out'] or result['returncode'] or result['stdout_truncated'] or result['cleanup_uncertain']:
            code = 'docker_command_timeout' if result['timed_out'] else 'docker_command_failed'
            cleanup = ('docker_cli_cleanup_uncertain',) if result['cleanup_uncertain'] else ()
            raise Refused(code, *cleanup, docker_error=docker_error_report(args, result))
        value = result['stdout'].decode('utf-8')
        return value.strip()

    def compose(self, *args, timeout=90):
        return self.docker('compose', '--project-name', 'teams-staging',
            '--project-directory', str(INSTALL), '--env-file', str(STATE / 'image.env'),
            '-f', str(INSTALL / 'compose.yml'), *args, timeout=timeout)

    def login(self, token, actor):
        if not token or len(token) > 16384 or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}(?:\[bot\])?', actor):
            raise Refused('job_registry_credential_required')
        self.docker('login', 'ghcr.io', '--username', actor, '--password-stdin', input=token.encode())

    def verify_image(self, image):
        value = json.loads(self.docker('image', 'inspect', '--format',
            '{"architecture":{{json .Architecture}},"labels":{{json .Config.Labels}}}', image))
        arch, labels = value['architecture'], value['labels']
        if (arch != 'arm64' or not labels or labels.get('org.opencontainers.image.source') != SOURCE
                or labels.get('io.teams-transcription.staging-maintenance') != 'v1'
                or not re.fullmatch(r'[0-9a-f]{40}', labels.get('org.opencontainers.image.revision', ''))):
            raise Refused('image_identity_or_architecture_rejected')
        return labels['org.opencontainers.image.revision']

    def release(self, image, token, *, cached=None, rollback=False):
        from release_gate import verify_release, Rejected
        try:
            if rollback:
                if not cached or os.environ.get('STAGING_SOURCE_SHA') or os.environ.get('STAGING_BUILD_RUN_ID'):
                    raise Refused('verified_rollback_release_required')
                sha,run=cached['source_sha'],str(cached['build_run_id'])
            else:
                sha,run=os.environ.get('STAGING_SOURCE_SHA',''),os.environ.get('STAGING_BUILD_RUN_ID','')
            return verify_release(image.split('@',1)[1],sha,run,token=token,cached=cached)
        except Rejected as error:
            raise Refused(error.args[0]) from None

    def probe(self, image, backup=None, maintenance=None):
        # Execute this installed, reviewed function, never an input or checkout script.
        import inspect
        script = ('import contextlib,fcntl,hashlib,json,os,re,sqlite3\nfrom pathlib import Path\n'
            'class Refused(Exception): pass\n' + inspect.getsource(schema_hash) + '\n'
            + inspect.getsource(database_report) + '\n' + inspect.getsource(volume_report)
            + f'\nprint(json.dumps(volume_report("/data", {backup!r}, {maintenance!r})))')
        mount = f'type=volume,source={VOLUME},target=/data' + ('' if backup or maintenance else ',readonly')
        value = self.disposable(image, script, ('--mount', mount))
        return json.loads(value)

    def disposable(self, image, script, extra=(), browser=False):
        name = 'teams-staging-check-' + uuid.uuid4().hex
        limits = ('--pids-limit=512', '--memory=4g', '--cpus=2', '--init',
            '--security-opt=seccomp=' + str(INSTALL / 'chromium-seccomp.json'),
            '--tmpfs=/tmp:size=512m,mode=1777', '--shm-size=1g') if browser else (
            '--pids-limit=64', '--memory=256m', '--cpus=1')
        try:
            return self.docker('run', '--name', name, '--rm', '--pull=never', '--network=none',
                '--read-only', '--cap-drop=ALL', '--security-opt=no-new-privileges:true',
                '--user=10001:10001', *limits, *extra, '--entrypoint=python', image, '-c', script)
        except Refused as failure:
            # Killing a timed-out docker CLI does not stop its container. Remove only
            # this invocation's randomly named helper; never remove the app service.
            try:
                self.docker('rm', '--force', name, timeout=10)
            except Refused:
                try:
                    remaining = self.docker('container', 'ls', '--all', '--filter',
                        'name=^/' + name + '$', '--format', '{{.ID}}', timeout=10)
                except Refused:
                    remaining = 'unknown'
                if remaining:
                    raise Refused(failure.args[0], 'helper_cleanup_uncertain',
                        docker_error=failure.docker_error) from None
            raise

    def sandbox(self, image):
        script = '''import asyncio,ctypes,errno,os,platform,subprocess
from pathlib import Path
from teams_transcription.browser import Browser
assert platform.machine() == 'aarch64' and os.getuid() == 10001
cpu=Path('/sys/fs/cgroup/cpu.max').read_text().split()
assert cpu[0] != 'max' and int(cpu[0]) == 2 * int(cpu[1])
assert Path('/sys/fs/cgroup/memory.max').read_text().strip() == '4294967296'
assert Path('/sys/fs/cgroup/pids.max').read_text().strip() == '512'
libc=ctypes.CDLL(None,use_errno=True)
assert libc.chroot(b'/') == -1 and ctypes.get_errno() == errno.EPERM
assert subprocess.check_output(['codex','--version'],text=True).strip() == 'codex-cli 0.159.0-alpha.3'
async def main():
    async with asyncio.timeout(30):
        async with Browser(executable='/usr/bin/chromium') as browser:
            page=await browser.browser.new_page()
            await page.set_content('<h1>staging sandbox check</h1>')
            assert await page.locator('h1').inner_text() == 'staging sandbox check'
asyncio.run(main())
print('sandbox_verified')
'''
        if self.disposable(image, script, browser=True) != 'sandbox_verified':
            raise Refused('staging_sandbox_validation_failed')

    def health(self):
        ident = self.compose('ps', '-a', '-q', 'teams-transcription')
        if not re.fullmatch(r'[0-9a-f]{64}', ident):
            raise Refused('staging_container_missing')
        result = json.loads(self.docker('inspect', '--format',
            '{"status":{{json .State.Status}},"health":{{json .State.Health.Status}},'
            '"image":{{json .Config.Image}},"oom":{{json .State.OOMKilled}},'
            '"exit_code":{{json .State.ExitCode}}}', ident))
        checked_ref(result['image'])
        if result['status'] not in {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'}:
            raise Refused('unknown_container_status')
        if result['health'] not in {'starting', 'healthy', 'unhealthy'}:
            raise Refused('unknown_container_health')
        if type(result['oom']) is not bool or type(result['exit_code']) is not int or not 0<=result['exit_code']<=255:
            raise Refused('container_metadata_invalid')
        return result

    def diagnostics(self,state):
        from diagnostic_projection import recent_logs,resource_usage,database_flags,release_link
        ident=self.compose('ps','-a','-q','teams-transcription')
        if not re.fullmatch(r'[0-9a-f]{64}',ident):raise Refused('staging_container_missing')
        health=self.health()
        limits=json.loads(self.docker('inspect','--format',
            '{"memory_limit_bytes":{{json .HostConfig.Memory}},"cpu_limit_nano":{{json .HostConfig.NanoCpus}},'
            '"pids_limit":{{json .HostConfig.PidsLimit}}}',ident))
        if set(limits)!={'memory_limit_bytes','cpu_limit_nano','pids_limit'} or any(type(v) is not int or not 0<v<2**63 for v in limits.values()):
            raise Refused('resource_limits_invalid')
        resources={'limits':limits,'usage_available':health['status']=='running'}
        if resources['usage_available']:
            resources['usage']=resource_usage(json.loads(self.docker('stats','--no-stream','--format',
                '{"CPUPerc":{{json .CPUPerc}},"MemUsage":{{json .MemUsage}},"PIDs":{{json .PIDs}}}',ident,timeout=15)))
        with tempfile.TemporaryFile() as output:
            try:
                result=subprocess.run(['/usr/bin/docker','logs','--since','15m','--tail','100','--timestamps',ident],
                    env=self.env,stdout=output,stderr=subprocess.STDOUT,timeout=15)
            except subprocess.TimeoutExpired:
                raise Refused('recent_log_read_timeout') from None
            if result.returncode:raise Refused('recent_log_read_failed')
            output.seek(0)
            logs=recent_logs(output.read(65537))
        run=os.environ.get('GITHUB_RUN_ID','')
        workflow_sha=os.environ.get('GITHUB_SHA','')
        deployment=state.get('deployment_run_id')
        if deployment is not None and (type(deployment) is not int or not 0<deployment<10**20):
            raise Refused('deployment_run_identity_invalid')
        return {'phase':state['phase'],'image':state['current'],'container':health,
            'database':database_flags(self.probe(state['current'])),'resources':resources,'recent_logs':logs,
            'release':release_link(state.get('release'),state['current']),
            'diagnostic_run_id':int(run) if re.fullmatch(r'[1-9][0-9]{0,19}',run) else None,
            'workflow_source_sha':workflow_sha if re.fullmatch(r'[0-9a-f]{40}',workflow_sha) else None,
            'deployment_run_id':deployment}


def save_state(value):
    temp = STATE / 'deployment.tmp'
    with temp.open('w') as output:
        json.dump(value, output)
        output.flush()
        os.fsync(output.fileno())
    temp.replace(STATE / 'deployment.json')


def read_state():
    path = STATE / 'deployment.json'
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    checked_ref(value['current'])
    if value.get('previous'):
        checked_ref(value['previous'])
    if value.get('phase') not in {'switching', 'healthy', 'failed'}:
        raise Refused('deployment_state_invalid')
    return value


def first_failed_candidate(controller, digest, state):
    """Common read-only first-failure preconditions; never reset failed history."""
    if digest or not state or state.get('phase') != 'failed' or state.get('previous') is not None:
        raise Refused('create_diagnostic_first_failed_deploy_required')
    target = checked_ref(state['current'])
    revision, run = state.get('revision'), state.get('deployment_run_id')
    if (not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{40}', revision)
            or type(run) is not int or not 0 < run < 10**20
            or state.get('release', {}).get('source_sha') != revision or read_state() != state):
        raise Refused('create_diagnostic_identity_rejected')
    pin = STATE / 'image.env'
    if pin.is_symlink() or not pin.is_file() or pin.read_text() != 'TRANSCRIPTION_IMAGE=' + target + '\n':
        raise Refused('create_diagnostic_pin_mismatch')
    if controller.verify_image(target) != revision:
        raise Refused('create_diagnostic_image_mismatch')
    network = json.loads(controller.docker('network', 'inspect', 'teams-staging_default', '--format',
        '{"driver":{{json .Driver}},"project":{{json (index .Labels "com.docker.compose.project")}}}'))
    if network.get('driver') != 'bridge' or network.get('project') != 'teams-staging':
        raise Refused('create_diagnostic_existing_network_required')
    def guarded():
        report = controller.probe(target)
        if (report.get('maintenance') is not True or report.get('quiescent') is not True
                or report.get('database_present') is not False):
            raise Refused('create_diagnostic_maintenance_no_database_required')
    guarded()
    if controller.compose('ps', '-a', '-q', 'teams-transcription'):
        raise Refused('create_diagnostic_container_exists')
    return target, revision, run


def diagnose_create(controller, digest, state):
    """One first-deploy create probe, never a recovery/start operation."""
    target, revision, run = first_failed_candidate(controller, digest, state)
    audit = STATE / f'create-diagnostic-{run}.json'
    try:
        fd = os.open(audit, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Refused('create_diagnostic_already_attempted') from None
    with os.fdopen(fd, 'w') as output:
        json.dump({'operation':'diagnose-create','deployment_run_id':run,
            'image':target,'revision':revision,'outcome':'attempted'}, output)
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    # No registry credential or app up/start/stop/removal/maintenance mutation.
    # Even an unknown/failed outcome keeps the audit file and forbids repetition.
    controller.compose('create', '--no-build', '--pull', 'never', 'teams-transcription', timeout=60)
    ident = controller.compose('ps', '-a', '-q', 'teams-transcription')
    if not re.fullmatch(r'[0-9a-f]{64}', ident):
        raise Refused('create_diagnostic_container_missing')
    value = json.loads(controller.docker('inspect', '--format',
        '{"status":{{json .State.Status}},"running":{{json .State.Running}},'
        '"pid":{{json .State.Pid}},"image":{{json .Config.Image}}}', ident))
    if (value.get('status') != 'created' or value.get('running') is not False
            or type(value.get('pid')) is not int or value['pid'] != 0 or value.get('image') != target):
        raise Refused('create_diagnostic_not_stopped')
    report = controller.probe(target)
    if (report.get('maintenance') is not True or report.get('quiescent') is not True
            or report.get('database_present') is not False):
        raise Refused('create_diagnostic_maintenance_no_database_required')
    if read_state() != state:
        raise Refused('create_diagnostic_state_changed')
    return {'operation':'diagnose-create','phase':'failed','image':target,'revision':revision,
        'deployment_run_id':run,'container_created_not_started':True,
        'deployment_state_preserved':True,'maintenance_preserved':True,
        'database_present':False,'teams_or_model_called':False,'audio_e2e_tested':False}


RECOVERY_COMPOSE_HASHES = {
    'compose.yml':'c9c7fda103df0eb9945abeb472ce7e5e02204b1e72159a62c8546f01dfa19b31',
    'compose.onboarding.yml':'acf8e42e79cb33dee3a4671bc6d16c7e79094920709a8ee568e36c0214ee8d9d',
}


def recover_first_deploy(controller, digest, state):
    """Explicit once-only recovery of a reviewed empty first deployment."""
    target, revision, run = first_failed_candidate(controller, digest, state)
    hashes = {}
    for name, expected in RECOVERY_COMPOSE_HASHES.items():
        path = INSTALL/name
        if path.is_symlink() or not path.is_file():
            raise Refused('recovery_reviewed_compose_required')
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if hashes[name] != expected:
            raise Refused('recovery_reviewed_compose_required')
    audit = STATE/f'create-diagnostic-{run}.json'
    if (audit.is_symlink() or not audit.is_file() or audit.stat().st_uid != os.getuid()
            or audit.stat().st_mode & 0o777 != 0o600 or audit.stat().st_size > 8192):
        raise Refused('recovery_original_diagnostic_required')
    original_audit = audit.read_bytes()
    if json.loads(original_audit) != {'operation':'diagnose-create','deployment_run_id':run,
            'image':target,'revision':revision,'outcome':'attempted'}:
        raise Refused('recovery_original_diagnostic_required')
    original = (STATE/'deployment.json').read_bytes().decode('utf-8')
    if len(original.encode()) > 65536 or json.loads(original) != state:
        raise Refused('recovery_original_state_changed')
    # This intent is distinct from, and never replaces, the original diagnostic.
    path = STATE/f'first-recovery-{run}.json'
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Refused('first_recovery_already_attempted') from None
    with os.fdopen(fd, 'w') as output:
        json.dump({'operation':'recover-first-deploy','outcome':'attempted',
            'original_state_text':original,'original_state_sha256':hashlib.sha256(original.encode()).hexdigest(),
            'original_diagnostic_sha256':hashlib.sha256(original_audit).hexdigest(),
            'compose_sha256':hashes}, output)
        output.flush()
        os.fsync(output.fileno())
    directory = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    try:
        controller.compose('create', '--no-build', '--pull', 'never', 'teams-transcription', timeout=60)
        before_start = controller.probe(target)
        if (before_start.get('maintenance') is not True or before_start.get('database_present') is not False
                or before_start.get('quiescent') is not True):
            raise Refused('recovery_before_start_guard_failed')
        controller.compose('start', '--wait', '--wait-timeout', '90', 'teams-transcription', timeout=120)
        health = controller.health()
        if health.get('status') != 'running' or health.get('health') != 'healthy' or health.get('image') != target:
            raise Refused('recovery_health_failed')
        report = controller.probe(target)
        if (report.get('maintenance') is not True or report.get('database_present') is not True
                or report.get('scheduler_paused') is not True or report.get('quiescent') is not True):
            raise Refused('recovery_started_state_not_paused_idle')
        if read_state() != state or audit.read_bytes() != original_audit:
            raise Refused('recovery_original_state_changed')
    except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as error:
        failure = error if isinstance(error, Refused) else Refused('recovery_verification_failed')
        try:
            controller.compose('stop', '--timeout', '45', 'teams-transcription')
        except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
            raise Refused(failure.args[0], 'recovery_stop_uncertain', docker_error=failure.docker_error) from None
        raise failure from None
    # Commit after guarded lifecycle; do not stop a committed healthy service on
    # an uncertain state-write outcome. Maintenance still prevents admission.
    save_state({**state,'phase':'healthy','first_recovery_intent':path.name})
    directory = os.open(STATE, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {'operation':'recover-first-deploy','phase':'healthy','image':target,'revision':revision,
        'deployment_run_id':run,'maintenance_preserved':True,'scheduler_paused':True,
        'original_failure_archived':True,'previous_image_available':False,
        'teams_or_model_called':False,'audio_e2e_tested':False}


def operate(controller, operation, digest, state):
    # A crashed QA exec may outlive its Docker CLI. No automatic deploy/rollback
    # may remove the app until the consumed reservation and owned cleanup reconcile.
    marker=STATE/'qa-cleanup-interlock.json'
    if operation in {'deploy','rollback','recover-first-deploy','diagnose-create'} and (marker.exists() or marker.is_symlink()):
        raise Refused('qa_cleanup_requires_reconciliation')
    if operation == 'recover-first-deploy':
        return recover_first_deploy(controller, digest, state)
    if operation == 'diagnose-create':
        return diagnose_create(controller, digest, state)
    if operation == 'diagnostics':
        if digest:
            raise Refused('unexpected_argument')
        if not state:
            return {'phase': 'uninitialized'}
        return controller.diagnostics(state)
    if operation not in {'deploy', 'rollback'}:
        raise Refused('unsupported_operation')
    if operation == 'deploy':
        target = image_ref(digest)
        if state and state['phase'] != 'healthy':
            raise Refused('unfinished_deployment_requires_review_or_rollback')
    else:
        if digest or not state or not state.get('previous'):
            raise Refused('rollback_target_unavailable')
        target = checked_ref(state['previous'])
    # Before any stop, download and verify exactly the requested private image.
    token = sys.stdin.read(16385).strip()
    release = controller.release(target, token, cached=state.get('previous_release') if operation=='rollback' else None,
        rollback=operation=='rollback')
    controller.login(token, os.environ.get('GITHUB_ACTOR', ''))
    controller.docker('pull', '--quiet', target, timeout=240)
    revision = controller.verify_image(target)
    if revision != release['source_sha']:
        raise Refused('pulled_image_source_mismatch')
    controller.sandbox(target)
    current = state['current'] if state else target
    report = controller.probe(current)
    if not report['quiescent']:
        raise Refused('staging_not_quiescent')
    if operation == 'rollback' and report['schema'] != state['rollback_schema']:
        raise Refused('schema_changed_manual_recovery_required')
    report = controller.probe(current, maintenance='enable')
    if not report['quiescent']:
        raise Refused('staging_became_busy_before_maintenance')
    # Persist intent before stopping: interrupted work cannot look successful.
    previous = (state['current'] if state['phase'] == 'healthy' else state.get('previous')) if state else None
    new_state = {'current': target, 'previous': previous,
        'release':release,'previous_release':(state.get('release') if state['phase']=='healthy' else state.get('previous_release')) if state else None,
        'rollback_schema': report['schema'], 'phase': 'switching', 'revision': revision}
    job=os.environ.get('GITHUB_RUN_ID','')
    new_state['deployment_run_id']=int(job) if re.fullmatch(r'[1-9][0-9]{0,19}',job) else None
    if state:
        # Mark previous state as switching without losing the actual current pin.
        save_state({**state, 'phase': 'switching'})
        controller.compose('stop', '--timeout', '45', 'teams-transcription')
    report = controller.probe(current, backup=uuid.uuid4().hex)
    if not report['quiescent']:
        raise Refused('post_stop_staging_not_quiescent')
    if operation == 'rollback' and report['schema'] != state['rollback_schema']:
        raise Refused('post_stop_schema_changed_manual_recovery_required')
    save_state(new_state)
    pin = STATE / 'image.tmp'
    pin.write_text('TRANSCRIPTION_IMAGE=' + target + '\n')
    pin.replace(STATE / 'image.env')
    try:
        controller.compose('up', '-d', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '90',
            'teams-transcription', timeout=120)
        health = controller.health()
        if health['status'] != 'running' or health['health'] != 'healthy' or health['image'] != target:
            raise Refused('staging_health_failed')
        # Fail if startup resumed scheduling or recovered work unexpectedly.
        if not controller.probe(target)['quiescent']:
            raise Refused('post_start_staging_not_quiescent')
    except Refused as failure:
        new_state['phase'] = 'failed'
        save_state(new_state)
        try:
            controller.compose('stop', '--timeout', '45', 'teams-transcription')
        except Refused:
            cleanup = 'helper_and_service_stop_uncertain' if len(failure.args)>1 else 'stop_uncertain'
            raise Refused(failure.args[0], cleanup, docker_error=failure.docker_error) from None
        raise
    # Release validates paused/idle atomically under the same store lock. If
    # release's transport outcome is uncertain, do not stop a service which may
    # already have accepted new work; keep the recorded phase for operator review.
    try:
        released = controller.probe(target, maintenance='disable')
    except Refused as failure:
        raise Refused(failure.args[0], 'maintenance_release_uncertain',
            docker_error=failure.docker_error) from None
    if not released['quiescent'] or released.get('maintenance', False):
        raise Refused('maintenance_release_failed')
    if operation == 'rollback' and state['phase'] != 'healthy':
        new_state['previous'] = None  # A failed candidate never enters healthy history.
        new_state['previous_release'] = None
    new_state['phase'] = 'healthy'
    save_state(new_state)
    return {'phase': 'healthy', 'operation': operation, 'image': target,
        'revision': revision, 'backup_created': report['backup_created'],
        'build_run_id':release['build_run_id'],'build_run_attempt':release['build_run_attempt'],
        'container': health, 'teams_or_model_called': False, 'audio_e2e_tested': False}


@contextlib.contextmanager
def registry_config(operation):
    if operation in {'diagnostics', 'diagnose-create', 'recover-first-deploy'}:
        with tempfile.TemporaryDirectory(prefix='teams-staging-diagnostics-') as path:
            yield path
        return
    run_id, attempt = os.environ.get('GITHUB_RUN_ID', ''), os.environ.get('GITHUB_RUN_ATTEMPT', '')
    if not re.fullmatch(r'[0-9]{1,20}', run_id) or not re.fullmatch(r'[0-9]{1,10}', attempt):
        raise Refused('trusted_job_identity_required')
    parent = STATE / 'registry'
    parent.mkdir(mode=0o700, exist_ok=True)
    path = parent / (run_id + '-' + attempt)
    path.mkdir(mode=0o700)  # Refuse a leftover directory rather than reuse credentials.
    failure = None
    try:
        yield str(path)
    except Refused as error:
        failure = error
        raise
    finally:
        shutil.rmtree(path, ignore_errors=True)
        if path.exists():
            if failure:
                raise Refused(failure.args[0], 'registry_credential_cleanup_uncertain',
                    docker_error=failure.docker_error) from None
            raise Refused('registry_credential_cleanup_failed')


def main():
    os.umask(0o077)
    def terminated(signum, frame):
        raise Refused('staging_controller_cancelled')
    signal.signal(signal.SIGTERM, terminated)
    signal.signal(signal.SIGINT, terminated)
    operation = sys.argv[1] if len(sys.argv) > 1 else ''
    digest = sys.argv[2] if len(sys.argv) > 2 else ''
    try:
        if len(sys.argv) > 3 or operation not in {'deploy', 'rollback', 'diagnostics', 'diagnose-create', 'recover-first-deploy'}:
            raise Refused('unsupported_operation')
        # Provisioning owns creation; never initialize arbitrary host paths here.
        if STATE.is_symlink() or not STATE.is_dir() or STATE.stat().st_uid != os.getuid() or STATE.stat().st_mode & 0o077:
            raise Refused('private_state_directory_required')
        with (STATE / 'controller.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Refused('staging_operation_in_progress') from None
            with registry_config(operation) as config:
                controller = Controller(config)
                result = operate(controller, operation, digest, read_state())
        print(json.dumps({'success': True, **result}))
        return 0
    except (Refused, OSError, ValueError, KeyError, TypeError, AttributeError, IndexError):
        # Fixed error codes, never subprocess output or exception body.
        code = sys.exception().args[0] if isinstance(sys.exception(), Refused) else 'staging_controller_failed'
        cleanup = sys.exception().args[1] if isinstance(sys.exception(), Refused) and len(sys.exception().args)>1 else None
        report = {'success': False, 'error_code': code, 'cleanup': cleanup}
        docker = public_docker_error(getattr(sys.exception(), 'docker_error', None))
        if docker is not None:
            report['docker'] = docker
        print(json.dumps(report))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
