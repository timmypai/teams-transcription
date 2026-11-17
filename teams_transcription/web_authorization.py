"""One session-owned, short-lived native ChatGPT device authorization.

No password/token input, credential copying, model calls, or login retries.
The daemon reserves its idle meeting slot while the native process owns login.
"""
import asyncio
import contextlib
import os
import re
import time

from .codex_auth import AuthError, configured_home, exclusive, preflight, private_auth_present
from .codex_device_rpc import CodexDeviceRPC


class WebChatGPTAuthorization:
    def __init__(self, daemon, settings, sessions):
        self.daemon, self.settings, self.sessions = daemon, settings, sessions
        self.owner = None
        self.state = 'idle'
        self.code = self.url = self.login_id = None
        self.task = self.rpc = None
        self.error_code = None
        self.cleanup_error_code = None
        self.cleanup_failed = False
        self.cleaning = False
        self.entered = asyncio.Event()
        self.close_task = None

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def owner_valid(self):
        return bool(self.owner and self.owner['expires_at'] > self.sessions.clock()
            and self.sessions.sessions.get(self.owner['nonce']) is self.owner)

    def view(self, session):
        if session is not self.owner:
            return {'state':'busy' if self.busy or self.cleanup_failed else 'idle'}
        value = {'state':self.state, 'error_code':self.error_code,
            'cleanup_error_code':self.cleanup_error_code}
        if self.state == 'pending':
            if self.owner_valid():
                value.update(user_code=self.code, verification_url=self.url)
            else:
                value.update(state='expired',error_code='authorization_session_expired')
        return value

    async def begin(self, session, *, confirmed=False, reauthorize=False):
        if not confirmed:
            raise AuthError('explicit_account_authorization_required')
        async with self.daemon._join_lock:
            if self.busy or self.cleanup_failed:
                raise AuthError('codex_auth_operation_in_progress')
            status = self.daemon.status()
            policy = await asyncio.to_thread(self.daemon.store.settings)
            if (not status['started'] or status['closing'] or status['active_session_id']
                    or status['capture_interlock'] or self.daemon._capture is not None
                    or self.daemon._lease is not None or not policy['scheduler_paused']):
                raise AuthError('pause_scheduler_and_finish_meeting_before_login')
            if self.sessions.sessions.get(session['nonce']) is not session or session['expires_at'] <= self.sessions.clock():
                raise AuthError('authorization_session_expired')
            self.owner, self.state, self.error_code = session, 'starting', None
            self.cleanup_error_code = None
            self.code = self.url = self.login_id = None
            self.entered = asyncio.Event()
            self.close_task = None
            self.cleaning = False
            self.daemon.auth_operation_in_progress = True
            # The app lifecycle owns this task, including request disconnects.
            self.task = asyncio.create_task(self._run(reauthorize))

    def _check_home(self, reauthorize):
        home = configured_home(self.settings)
        auth = home/'auth.json'
        if auth.is_symlink():
            raise AuthError('codex_auth_symlink_rejected')
        if auth.exists():
            if not reauthorize:
                raise AuthError('explicit_reauthorization_required')
            if not private_auth_present(home):
                raise AuthError('codex_auth_private_uid_gid_required')
        return home

    async def _run(self, reauthorize):
        self.entered.set()
        self.rpc = None
        locks = contextlib.ExitStack()
        try:
            if os.environ.get('CI') or os.environ.get('GITHUB_ACTIONS'):
                raise AuthError('device_login_forbidden_in_ci')
            if any(os.environ.get(key) for key in ('OPENAI_API_KEY','CODEX_API_KEY','CODEX_ACCESS_TOKEN')):
                raise AuthError('api_key_or_access_token_environment_rejected')
            home = self._check_home(reauthorize)
            readiness = await asyncio.to_thread(preflight, self.settings)
            if not readiness['login_ready']:
                raise AuthError(next((code for code in readiness['error_codes']
                    if code != 'chatgpt_authorization_required'), 'auth_preflight_failed'))
            locks.enter_context(exclusive(home/'.onboarding.lock', 'codex_auth_operation_in_progress'))
            self._check_home(reauthorize)
            if not self.owner_valid():
                raise AuthError('authorization_session_expired')
            if self.daemon._provider is not None:
                try:
                    await self.daemon._provider.close()
                except BaseException:
                    self.cleanup_failed = True
                    self.cleanup_error_code = 'codex_device_login_cleanup_unverified'
                    raise AuthError('codex_device_login_cleanup_unverified') from None
                self.daemon._provider = None
            self.rpc = CodexDeviceRPC(self.settings)
            await self.rpc.start()
            if not self.owner_valid() or self.daemon.closing.is_set():
                raise AuthError('authorization_session_expired')
            result = await self.rpc.request('account/login/start', {'type':'chatgptDeviceCode'})
            if (not isinstance(result, dict) or result.get('type') != 'chatgptDeviceCode'
                    or result.get('verificationUrl') != 'https://auth.openai.com/codex/device'
                    or not isinstance(result.get('loginId'), str)
                    or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', result['loginId'])
                    or not isinstance(result.get('userCode'), str)
                    or not re.fullmatch(r'[A-Z0-9-]{6,32}', result['userCode'])):
                raise AuthError('invalid_device_authorization_response')
            self.login_id = result['loginId']
            self.url, self.code = result['verificationUrl'], result['userCode']
            self.state = 'pending'
            deadline = time.monotonic() + 600
            while True:
                if not self.owner_valid():
                    raise AuthError('authorization_session_expired')
                if time.monotonic() >= deadline:
                    raise AuthError('codex_device_login_timeout_outcome_unverified')
                try:
                    event = await asyncio.wait_for(self.rpc.notifications.get(), .5)
                except TimeoutError:
                    continue
                if event.get('method') == 'client/exit':
                    raise AuthError('codex_device_login_failed_outcome_unverified')
                if event.get('method') != 'account/login/completed':
                    continue
                params = event.get('params') or {}
                if params.get('loginId') != self.login_id:
                    continue
                if not self.owner_valid():
                    raise AuthError('authorization_session_expired')
                if params.get('success') is not True:
                    raise AuthError('codex_device_login_denied_or_failed')
                if not private_auth_present(home):
                    raise AuthError('codex_auth_private_file_not_created')
                self.state = 'completed'
                break
        except asyncio.CancelledError:
            self.state = 'cancelled'
            self.error_code = 'codex_device_login_cancelled_outcome_unverified'
        except AuthError as error:
            self.state, self.error_code = 'failed', error.code
        except Exception:
            self.state, self.error_code = 'failed', 'codex_device_login_failed_outcome_unverified'
        finally:
            self.cleaning = True
            self.code = self.url = None
            # Keep the reservation and file lock until cancel and process reap.
            if self.rpc is not None:
                if self.state != 'completed' and self.login_id:
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(5):
                            await self.rpc.request('account/login/cancel', {'loginId':self.login_id})
                try:
                    await self.rpc.close()
                except Exception:
                    self.cleanup_failed = True
                    self.state = 'failed'
                    self.cleanup_error_code = 'codex_device_login_cleanup_unverified'
            self.login_id = None
            self.daemon.auth_operation_in_progress = self.cleanup_failed
            locks.close()

    async def cancel(self, session):
        if session is not self.owner:
            raise AuthError('authorization_owner_required')
        await self.close()

    async def close(self):
        if self.busy:
            if self.close_task is None:
                self.close_task = asyncio.create_task(self._cancel())
            await asyncio.shield(self.close_task)

    async def _cancel(self):
        await self.entered.wait()
        if not self.cleaning:
            self.task.cancel()
        await self.task
