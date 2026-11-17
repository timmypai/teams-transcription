"""Operator-only ChatGPT device onboarding; no website cookies or API keys.

Preflight reads metadata/tool versions only. Login is an explicitly confirmed
interactive terminal operation, never CI. Auth contents/device codes are never
captured by this module. Live RX/TX requires its separate authorized probe.
"""
import asyncio
import contextlib
import fcntl
import importlib.metadata
import os
import platform
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace

PINNED='codex-cli 0.159.0-alpha.3'
PACKAGES={'playwright':'1.62.0','starlette':'1.6.0','httpx':'0.28.1','uvicorn':'0.52.1',
    'pydantic':'2.13.4','PyYAML':'6.0.3'}


class AuthError(RuntimeError):
    def __init__(self,code):
        self.code=code
        super().__init__(code)


def configured_home(settings):
    if not settings.codex_home:
        raise AuthError('codex_home_explicit_persistent_path_required')
    home=Path(settings.codex_home)
    if not home.is_absolute() or home.resolve()!=home or home==Path(settings.data_root):
        raise AuthError('codex_home_path_invalid')
    return home


def private_path(path,directory=False):
    try:
        info=path.lstat()
        return (not path.is_symlink() and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            and info.st_uid==os.getuid() and info.st_gid==os.getgid() and not info.st_mode&0o077)
    except OSError:return False


def private_auth_present(home):
    return private_path(home,True) and private_path(home/'auth.json')


def persisted(home,data_root,mounts):
    candidates=[]
    for line in mounts.splitlines():
        before,separator,after=line.partition(' - ')
        fields=before.split()
        if not separator or len(fields)<6:continue
        point=Path(fields[4].replace('\\040',' '))
        if home.is_relative_to(point):candidates.append((point,fields[5],after.split()[0],fields[3]))
    if not candidates:return False
    depth=max(len(entry[0].parts) for entry in candidates)
    effective=[entry for entry in candidates if len(entry[0].parts)==depth]
    if len(effective)!=1:return False
    point,options,filesystem,source_root=effective[0]
    # Nested Docker-in-Docker CI may store named volumes on an overlay-backed
    # host. Its separate /volumes/<name>/_data bind survives app recreation;
    # direct container overlay roots and arbitrary overlay directories do not.
    durable_filesystem=filesystem not in ('tmpfs','ramfs','overlay') or (
        filesystem=='overlay' and re.search(r'/volumes/[^/]+/_data$',source_root) is not None)
    return (point not in (Path('/'),Path('/tmp'),Path('/dev/shm'))
        and (point==home or Path(data_root).is_relative_to(point))
        and 'rw' in options.split(',') and durable_filesystem)


def software():
    checks={'python_supported':sys.version_info>=(3,11)}
    versions={'python':'.'.join(str(n) for n in sys.version_info[:3])}
    for package,expected in PACKAGES.items():
        try:value=importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:value=None
        checks['python_'+package.lower()]=value==expected
        versions[package]=value if value==expected else 'missing_or_version_mismatch'
    return checks,versions


def tool_version(executable,pattern):
    if not shutil.which(executable):return None
    try:
        value=subprocess.run([executable,'-version' if Path(executable).name=='ffmpeg' else '--version'],
            capture_output=True,text=True,timeout=5)
        match=re.search(pattern,value.stdout) if value.returncode==0 else None
        return match[1] if match else None
    except (OSError,subprocess.TimeoutExpired):return None


def preflight(settings,*,mounts=None):
    home=configured_home(settings)
    if (home/'auth.json').is_symlink():raise AuthError('codex_auth_symlink_rejected')
    if mounts is None:mounts=Path('/proc/self/mountinfo').read_text()
    checks,versions=software()
    codex=tool_version(settings.codex_bin,r'^(codex-cli 0\.159\.0-alpha\.3)\s*$')
    node=tool_version('node',r'^v([0-9]+(?:\.[0-9]+){2})')
    chromium=tool_version(settings.chromium or 'chromium',r'Chromium ([0-9]+(?:\.[0-9]+){2,3})')
    ffmpeg=tool_version('ffmpeg',r'ffmpeg version ([0-9]+(?:\.[0-9]+){1,3})')
    versions.update(codex=codex or 'missing_or_version_mismatch',node=node or 'missing',
        chromium=chromium or 'missing',ffmpeg=ffmpeg or 'missing')
    checks.update(nonroot=os.getuid()!=0,data_root_private=private_path(Path(settings.data_root),True),
        codex_pinned=codex==PINNED,node_supported=bool(node and int(node.split('.')[0])>=16),
        chromium_installed=bool(chromium),ffmpeg_installed=bool(ffmpeg),
        auth_home_private=private_path(home,True),auth_home_writable=os.access(home,os.W_OK),
        persistent_writable_mount=persisted(home,settings.data_root,mounts),
        shm_tmpfs=any(' /dev/shm ' in line and line.partition(' - ')[2].startswith('tmpfs ') for line in mounts.splitlines()))
    material=(home/'auth.json').exists()
    codes=[]
    if not checks['nonroot']:codes.append('nonroot_runtime_required')
    if not checks['data_root_private']:codes.append('private_data_root_uid_gid_required')
    if not checks['persistent_writable_mount']:codes.append('codex_home_persistent_rw_mount_required')
    if not checks['auth_home_private']:codes.append('codex_home_private_uid_gid_required')
    if not checks['auth_home_writable']:codes.append('codex_home_not_writable')
    if not checks['codex_pinned']:codes.append('codex_pinned_version_required')
    if not all(checks[name] for name in checks if name.startswith('python_') or name in ('node_supported','chromium_installed','ffmpeg_installed')):
        codes.append('runtime_dependency_missing_or_version_mismatch')
    if not checks['shm_tmpfs']:codes.append('codex_runtime_requires_tmpfs')
    if not material:codes.append('chatgpt_authorization_required')
    elif not private_auth_present(home):codes.append('codex_auth_private_uid_gid_required')
    return {'login_ready':all(checks.values()),'checks':checks,'versions':versions,
        'architecture':platform.machine(),'arm64':platform.machine()=='aarch64',
        'auth_material_present':material,'auth_material_private':private_auth_present(home),
        'error_codes':codes,'account_admission':'not_checked','live_rx_tx':'not_tested',
        'teams_audio':'not_tested','browser_login_ui_available':True}


@contextlib.contextmanager
def exclusive(path,code):
    fd=os.open(path,os.O_CREAT|os.O_RDWR,0o600)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise AuthError(code) from None
        yield
    finally:os.close(fd)


def device_login(settings,*,confirmed=False,private_terminal=False,reauthorize=False,interactive=None,environ=None):
    env=dict(os.environ if environ is None else environ)
    if not confirmed:raise AuthError('explicit_account_authorization_required')
    if not private_terminal:raise AuthError('unrecorded_terminal_and_disabled_container_logging_required')
    if env.get('CI') or env.get('GITHUB_ACTIONS'):raise AuthError('device_login_forbidden_in_ci')
    if interactive is None:interactive=all(stream.isatty() for stream in (sys.stdin,sys.stdout,sys.stderr))
    if not interactive:raise AuthError('private_interactive_terminal_required')
    if any(env.get(key) for key in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN')):
        raise AuthError('api_key_or_access_token_environment_rejected')
    home=configured_home(settings)
    if (home/'auth.json').exists() and not reauthorize:raise AuthError('explicit_reauthorization_required')
    if not private_path(Path(settings.data_root),True):raise AuthError('private_data_root_uid_gid_required')
    if (home/'auth.json').exists() and not private_auth_present(home):
        raise AuthError('codex_auth_private_uid_gid_required')
    try:home.mkdir(mode=0o700,exist_ok=True)
    except OSError:raise AuthError('codex_home_create_or_write_denied') from None
    result=preflight(settings)
    if not result['login_ready']:raise AuthError(next((c for c in result['error_codes'] if c!='chatgpt_authorization_required'),'auth_preflight_failed'))
    env['CODEX_HOME']=str(home)
    with exclusive(Path(settings.data_root)/'.assistant-daemon.lock','stop_assistant_before_device_login'), \
            exclusive(home/'.onboarding.lock','codex_auth_operation_in_progress'):
        if (home/'auth.json').is_symlink():raise AuthError('codex_auth_symlink_rejected')
        if (home/'auth.json').exists():
            if not reauthorize:raise AuthError('explicit_reauthorization_required')
            if not private_auth_present(home):raise AuthError('codex_auth_private_uid_gid_required')
        previous_signal=signal.getsignal(signal.SIGTERM)
        def terminated(signum,frame):raise AuthError('codex_device_login_cancelled_outcome_unverified')
        signal.signal(signal.SIGTERM,terminated)
        try:
            process=subprocess.run([settings.codex_bin,'login','--device-auth','-c','cli_auth_credentials_store="file"'],
                env=env,timeout=600)
        except subprocess.TimeoutExpired:raise AuthError('codex_device_login_timeout_outcome_unverified') from None
        except KeyboardInterrupt:raise AuthError('codex_device_login_cancelled_outcome_unverified') from None
        finally:signal.signal(signal.SIGTERM,previous_signal)
    if process.returncode in (130,-2,-15):raise AuthError('codex_device_login_cancelled_outcome_unverified')
    if process.returncode:raise AuthError('codex_device_login_denied_or_failed')
    if not private_auth_present(home):raise AuthError('codex_auth_private_file_not_created')
    return {'login_process_completed':True,'auth_material_private':True,
        'account_admission':'not_checked','live_rx_tx':'not_tested','teams_audio':'not_tested'}


async def check_account(settings,*,provider_factory=None):
    from .provider_state import CodexProviderState
    result=preflight(settings)
    if not result['login_ready'] or not result['auth_material_private']:
        raise AuthError(next(iter(result['error_codes']),'auth_preflight_failed'))
    home=configured_home(settings)
    prior=os.environ.get('CODEX_HOME')
    try:
        os.environ['CODEX_HOME']=str(home)
        with exclusive(home/'.onboarding.lock','codex_auth_operation_in_progress'), \
                tempfile.TemporaryDirectory(prefix='codex-account-',dir='/dev/shm') as directory:
            policy=SimpleNamespace(runtime_dir=directory,codex_bin=settings.codex_bin,
                live_provider=settings.live_provider,live_model=settings.live_model,live_cost_policy=settings.live_cost_policy)
            provider=(provider_factory or CodexProviderState)(policy)
            admitted=await provider.refresh()
            description=provider.describe()
            return {'account_admitted':admitted,'provider':description,'live_rx_tx':'not_tested','teams_audio':'not_tested',
                'next_action':None if admitted else 'reauthorize_in_private_terminal' if description['auth_error_code']=='chatgpt_auth_required'
                else 'inspect_fixed_provider_probe_phase_and_network_before_reauthorization'}
    finally:
        if prior is None:os.environ.pop('CODEX_HOME',None)
        else:os.environ['CODEX_HOME']=prior
