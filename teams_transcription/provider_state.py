"""Read-only official account state for the previously authorized Live route.

Authentication is an admission condition, not proof of model/media success.
No model call, token extraction, credits purchase, key or API fallback occurs.
"""
import asyncio
import contextlib
import errno
import math
import tempfile
import time
from pathlib import Path

from .rpc import CodexRPC
from .storage import stamp


class CodexProviderState:
    provider = 'codex_live'
    model = 'gpt-live-1-codex'

    # Separate budgets: account/read never spends initialize or cleanup time.
    INITIALIZE_TIMEOUT = 30
    ACCOUNT_TIMEOUT = 15
    CLEANUP_TIMEOUT = 8

    def __init__(self, settings, *, rpc_factory=CodexRPC, clock=time.monotonic, ttl=300,
                 initialize_timeout=INITIALIZE_TIMEOUT, account_timeout=ACCOUNT_TIMEOUT,
                 cleanup_timeout=CLEANUP_TIMEOUT):
        self.settings, self.rpc_factory, self.clock, self.ttl = settings, rpc_factory, clock, ttl
        self.budgets = dict(initialize=initialize_timeout, account_read=account_timeout,
                            cleanup=cleanup_timeout)
        for phase, maximum in (('initialize',self.INITIALIZE_TIMEOUT),
                               ('account_read',self.ACCOUNT_TIMEOUT),
                               ('cleanup',self.CLEANUP_TIMEOUT)):
            value = self.budgets[phase]
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError('invalid_account_probe_budget')
        self.probe = {'schema_version':1, 'status':'unchecked', 'phase':None,
                      'error_class':None, 'elapsed_seconds':0, 'phases':{}}
        self.account_type = None
        self.checked_at = None
        self.checked_clock = None
        self.error_code = 'provider_account_unchecked'
        self.closing = asyncio.Event()
        self.task = None

    def admissible(self, settings):
        return (settings is self.settings and settings.live_provider == self.provider
            and settings.live_model == self.model and settings.live_cost_policy == 'no_new_cost'
            and self.account_type == 'chatgpt' and self.checked_clock is not None
            and 0 <= self.clock()-self.checked_clock < self.ttl)

    def describe(self):
        return {'provider_status':'admissible_unverified' if self.admissible(self.settings) else 'blocked',
                'auth_present':'verified' if self.admissible(self.settings) else 'unknown',
                'auth_checked_at':self.checked_at, 'auth_error_code':self.error_code,
                'auth_probe':{**self.probe, 'phases':{k:dict(v) for k,v in self.probe['phases'].items()}},
                'model_access':'checked_on_start', 'media_probe_passed':'unknown',
                'zh_live_probe_passed':'unknown', 'cost_policy_satisfied':'existing_chatgpt_allocation_only'}

    @staticmethod
    def _exception_class(exc):
        # Exception names/bodies may contain account data: only fixed labels escape.
        for kind in (asyncio.CancelledError, TimeoutError, OSError, RuntimeError,
                     ValueError, TypeError, KeyError):
            if isinstance(exc, kind):
                return kind.__name__
        return 'Exception'

    @staticmethod
    def _failure_code(exc):
        if isinstance(exc, asyncio.CancelledError):
            return 'provider_account_probe_cancelled'
        if isinstance(exc, TimeoutError):
            return 'provider_account_probe_timeout'
        if (isinstance(exc, OSError) and exc.errno == errno.EROFS
                or 'read-only file system' in str(exc).lower()):
            return 'codex_runtime_readonly'
        return 'provider_account_probe_failed'

    @staticmethod
    def _consume(task):
        if not task.cancelled():
            task.exception()

    async def _bounded(self, awaitable, seconds):
        # wait_for waits for cancellation handling, which can overrun its timeout.
        # Leave partial initialization for the separate, bounded cleanup phase.
        task = asyncio.create_task(awaitable)
        task.add_done_callback(self._consume)
        try:
            done, _ = await asyncio.wait({task}, timeout=seconds)
            if not done:
                raise TimeoutError()
            return task.result()
        finally:
            if not task.done():
                task.cancel()

    async def _cleanup(self, rpc, temporary):
        async def finish():
            try:
                if rpc is not None:
                    await rpc.__aexit__(None, None, None)
            finally:
                if temporary is not None:
                    temporary.cleanup()
        task = asyncio.create_task(finish())
        deadline = asyncio.get_running_loop().time() + self.budgets['cleanup']
        cancelled = False
        while True:
            remaining = max(0, deadline - asyncio.get_running_loop().time())
            try:
                done, _ = await asyncio.wait({task}, timeout=remaining)
            except asyncio.CancelledError:
                # Finish reaping the child within the same deadline, then propagate.
                cancelled = True
                continue
            if not done:
                task.cancel()
                task.add_done_callback(self._consume)
                return TimeoutError(), cancelled
            try:
                task.result()
            except (Exception, asyncio.CancelledError) as exc:
                return exc, cancelled
            return None, cancelled

    async def refresh(self):
        # Erase a previous success before any awaited operation can fail/cancel.
        self.account_type = self.checked_clock = None
        self.error_code = 'provider_account_probe_failed'
        self.checked_at = stamp()
        began = time.monotonic()
        self.probe = {'schema_version':1, 'status':'running', 'phase':'runtime',
                      'error_class':None, 'elapsed_seconds':0, 'phases':{}}
        rpc = temporary = None
        failure = cancellation = None
        phase = 'runtime'
        phase_began = began

        def record(name, started, exc=None, status='succeeded'):
            self.probe['phases'][name] = {
                'status':status, 'elapsed_seconds':round(time.monotonic()-started, 3),
                'error_class':self._exception_class(exc) if exc else None}

        try:
            runtime = Path(self.settings.runtime_dir)
            runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
            temporary = tempfile.TemporaryDirectory(prefix='account-read-', dir=runtime)
            record(phase, phase_began)
            phase = self.probe['phase'] = 'initialize'
            phase_began = time.monotonic()
            rpc = self.rpc_factory(temporary.name, self.settings.codex_bin)
            entered = await self._bounded(rpc.__aenter__(), self.budgets[phase])
            record(phase, phase_began)
            phase = self.probe['phase'] = 'account_read'
            phase_began = time.monotonic()
            response = await self._bounded(
                entered.request('account/read', {'refreshToken':False}), self.budgets[phase])
            account = response.get('account') if isinstance(response, dict) else None
            if not isinstance(account, dict) or account.get('type') != 'chatgpt':
                self.error_code = 'chatgpt_auth_required'
                failure = (phase, None)
                record(phase, phase_began, status='failed')
            else:
                record(phase, phase_began)
        except (Exception, asyncio.CancelledError) as exc:
            failure = (phase, exc)
            record(phase, phase_began, exc, 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'failed')
            if isinstance(exc, asyncio.CancelledError):
                cancellation = exc
            self.error_code = self._failure_code(exc)
        finally:
            self.probe['phase'] = 'cleanup'
            cleanup_began = time.monotonic()
            cleanup_error, cleanup_cancelled = await self._cleanup(rpc, temporary)
            record('cleanup', cleanup_began, cleanup_error,
                   'failed' if cleanup_error else 'succeeded')
            if cleanup_cancelled:
                cancellation = cancellation or asyncio.CancelledError()
                if failure is None:
                    failure = ('cleanup', cancellation)
                    self.error_code = 'provider_account_probe_cancelled'
            if cleanup_error is not None and failure is None:
                failure = ('cleanup', cleanup_error)
                self.error_code = self._failure_code(cleanup_error)
            self.probe.update(status='cancelled' if cancellation else 'failed' if failure else 'succeeded',
                              phase=failure[0] if failure else 'complete',
                              error_class=self._exception_class(failure[1]) if failure and failure[1] else None,
                              elapsed_seconds=round(time.monotonic()-began, 3))
        if cancellation is not None:
            raise cancellation
        if failure is not None:
            return False
        self.account_type = 'chatgpt'
        self.checked_clock = self.clock()
        self.error_code = None
        return True

    async def start(self):
        await self.refresh()
        self.task = asyncio.create_task(self._refresh_loop())

    async def _refresh_loop(self):
        while not self.closing.is_set():
            try:
                await asyncio.wait_for(self.closing.wait(), self.ttl / 2)
            except asyncio.TimeoutError:
                await self.refresh()

    async def close(self):
        self.closing.set()
        if self.task:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task
