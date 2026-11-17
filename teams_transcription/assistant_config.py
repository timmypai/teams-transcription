"""Standalone daemon/admin configuration; no caller OAuth or MCP settings."""
import os
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class AssistantSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    data_root: str = '/data'
    host: str = '127.0.0.1'
    port: int = Field(default=8080, ge=1024, le=65535)
    public_base_url: str | None = None
    admin_password_file: str = '/data/config/admin-password.json'
    admin_session_secret_file: str = '/data/config/admin-session.key'
    admin_session_seconds: int = Field(default=3600, ge=60, le=86400)
    cookie_secure: bool = False
    google_credentials_file: str = '/data/config/google-credentials.json'
    google_client_config_file: str | None = None
    calendar_poll_seconds: int = Field(default=60, ge=15, le=3600)
    lookahead_hours: int = Field(default=48, ge=1, le=168)
    lookback_seconds: int = Field(default=300, ge=0, le=3600)
    join_timeout_seconds: int = Field(default=120, ge=10, le=900)
    max_duration_seconds: int | None = Field(default=None, gt=0, strict=True)
    audio_timeout_seconds: int = Field(default=30, ge=5, le=120)
    drain_seconds: int = Field(default=5, ge=1, le=10)
    shutdown_seconds: int = Field(default=30, ge=15, le=120)
    lease_seconds: int = Field(default=30, ge=15, le=120)
    summary_lease_seconds: int = Field(default=90, ge=70, le=300)
    codex_bin: str = 'codex'
    codex_home: str | None = None
    chromium: str | None = None
    storage_state: str | None = None
    allowed_egress_hosts: list[str] = Field(default_factory=list)
    allowed_meeting_hosts: list[str] = Field(default_factory=lambda: [
        'teams.microsoft.com', 'teams.cloud.microsoft', 'teams.live.com'])
    summary_model: str | None = None
    display_name: str = Field(default='小白會議助理', min_length=1, max_length=100)
    timezone: str = 'UTC'
    live_provider: str = 'codex_live'
    live_model: str = 'gpt-live-1-codex'
    live_cost_policy: str = 'no_new_cost'
    log_level: str = 'info'

    @model_validator(mode='before')
    @classmethod
    def strict_policy_booleans(cls, value):
        if isinstance(value, dict) and 'cookie_secure' in value and type(value['cookie_secure']) is not bool:
            raise ValueError('invalid_cookie_policy')
        return value

    @model_validator(mode='after')
    def validate_runtime(self):
        root = Path(self.data_root)
        if not root.is_absolute() or root.is_symlink():
            raise ValueError('invalid_data_root')
        for key in ('admin_password_file', 'admin_session_secret_file', 'google_credentials_file'):
            path = Path(getattr(self, key))
            if not path.is_absolute() or not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('private_config_outside_data_root')
        for key in ('google_client_config_file', 'codex_home', 'chromium', 'storage_state'):
            value = getattr(self, key)
            if value is not None and (not value or not Path(value).is_absolute()):
                raise ValueError('invalid_runtime_path')
        if self.public_base_url:
            parsed = urlsplit(self.public_base_url)
            if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                    or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
                raise ValueError('invalid_public_base_url')
            if parsed.scheme == 'http' and parsed.hostname not in {'localhost', '127.0.0.1', '::1'}:
                raise ValueError('https_required_for_remote_admin')
            if parsed.scheme == 'https' and not self.cookie_secure:
                raise ValueError('secure_cookie_required')
        if self.host not in {'127.0.0.1', '::1', '0.0.0.0'}:
            raise ValueError('invalid_listen_host')
        if self.shutdown_seconds < self.drain_seconds + 10:
            raise ValueError('shutdown_window_too_short')
        if (self.live_provider, self.live_model, self.live_cost_policy) != (
                'codex_live', 'gpt-live-1-codex', 'no_new_cost'):
            raise ValueError('unsupported_live_runtime')
        if not self.codex_bin or self.log_level not in {'debug', 'info', 'warning', 'error'}:
            raise ValueError('invalid_runtime_setting')
        if self.summary_model is not None:
            from .summary import validate_codex_summary_model
            validate_codex_summary_model(self.summary_model)
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError('invalid_timezone') from None
        return self


def load_assistant_settings(path=None, overrides=None, environ=None):
    values = {}
    if path:
        try:
            with Path(path).open('rb') as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            document = yaml.safe_load(raw) or {}
            if not isinstance(document, dict):
                raise ValueError()
            values.update(document)
        except (OSError, ValueError, yaml.YAMLError):
            raise ValueError('invalid_assistant_configuration') from None
    environment = os.environ if environ is None else environ
    known = {'ASSISTANT_' + field.upper():field for field in AssistantSettings.model_fields}
    if any(name.startswith('ASSISTANT_') and name not in known for name in environment):
        raise ValueError('unknown_assistant_environment_setting')
    for name, field in known.items():
        if environment.get(name):
            raw = environment[name]
            try:
                value = yaml.safe_load(raw)
            except yaml.YAMLError:
                raise ValueError('invalid_assistant_environment_setting') from None
            values[field] = value
    if overrides:
        values.update({key:value for key,value in overrides.items() if value is not None})
    try:
        return AssistantSettings.model_validate(values)
    except (ValueError, TypeError):
        raise ValueError('invalid_assistant_configuration') from None


def doctor(settings):
    """Inspect local tools/file metadata only; never read tokens or contact providers."""
    import shutil
    import sqlite3
    import stat
    import subprocess
    from .runtime_guard import RuntimeLiveGuard

    checks = {'non_root':os.getuid()!=0,
        'data_writable':Path(settings.data_root).is_dir() and os.access(settings.data_root,os.W_OK),
        'egress_policy_configured':False}
    try:
        # Constructor validation is inert: [] is valid deny-all, and neither
        # prepare() nor destination resolution is authorized by local doctor.
        RuntimeLiveGuard('doctor', settings.allowed_egress_hosts)
        checks['egress_policy_configured'] = True
    except ValueError:
        pass
    for name,value in (
            ('admin_password_private',settings.admin_password_file),
            ('admin_session_secret_private',settings.admin_session_secret_file),
            ('google_credentials_private',settings.google_credentials_file),
            ('google_client_config_private',settings.google_client_config_file)):
        try:
            info = Path(value).lstat() if value else None
            checks[name] = bool(info and stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid()
                and not info.st_mode & 0o077)
        except OSError:
            checks[name] = False
    home = Path(settings.codex_home or os.environ.get('CODEX_HOME') or Path.home()/'.codex')
    try:
        info = (home/'auth.json').lstat()
        checks['codex_oauth_private'] = stat.S_ISREG(info.st_mode) and info.st_uid==os.getuid() and not info.st_mode & 0o077
    except OSError:
        checks['codex_oauth_private'] = False
    executable = shutil.which(settings.codex_bin)
    checks['codex_pinned'] = False
    if executable:
        try:
            version = subprocess.run([executable,'--version'],capture_output=True,text=True,timeout=5)
            checks['codex_pinned'] = version.returncode==0 and version.stdout.strip()=='codex-cli 0.159.0-alpha.3'
        except (OSError,subprocess.TimeoutExpired):
            pass
    checks['chromium_installed'] = bool(shutil.which(settings.chromium or 'chromium'))
    checks['ffmpeg_installed'] = bool(shutil.which('ffmpeg'))
    return {'schema_version':1,'mode':'standalone','port':settings.port,'checks':checks,
        'local_ready':all(checks.values()),'sqlite_version':sqlite3.sqlite_version,
        'gates':{'google_authorization':'unverified','teams_admission':'unverified',
            'live_transcription':'unverified','native_audibility':'unverified',
            'codex_summary':'unverified','glossary_asr_effect':'unverified',
            'chromium_sandbox':'unverified'}}
