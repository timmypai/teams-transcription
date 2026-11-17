"""Owner-confirmed RX scopes shared by the daemon and bounded local fixtures.

The supervisor owns meeting deadlines; this scope bounds cleanup and source
writes. Native B starts muted, and the independent outbox owns postmeeting work.
"""
import asyncio
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from .owned_processes import OwnedProcesses
from .diagnostics import live_failure


class RxContractError(RuntimeError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class RxCaptureComplete(RxContractError):
    def __init__(self):
        super().__init__('rx_capture_limit_reached')


def options(config):
    formal = config.get('rx_session_contract', False)
    if type(formal) is not bool or (formal and config.get('rx_test_contract', False)):
        raise RxContractError('invalid_rx_session_contract')
    value = config.get('rx_test_contract', False)
    if not formal and (value is False or value is None):
        return None
    if value is True or formal:
        value = {}
    if not isinstance(value, dict) or set(value) - {'capture_seconds', 'total_seconds', 'cleanup_seconds', 'ready_ttl_seconds'}:
        raise RxContractError('invalid_rx_test_contract')
    result = dict(capture_seconds=None if formal else 300., total_seconds=None if formal else 600.,
        cleanup_seconds=10., ready_ttl_seconds=300. if formal else 120.)
    result.update(value)
    for name, maximum in [('capture_seconds', 300), ('total_seconds', 600), ('cleanup_seconds', 10), ('ready_ttl_seconds', 300 if formal else 120)]:
        number = result[name]
        if formal and name in {'capture_seconds','total_seconds'} and number is None:
            continue
        if type(number) not in (float, int) or not math.isfinite(number) or not 0 < number <= maximum:
            raise RxContractError('invalid_rx_test_contract')
    if not formal and result['capture_seconds'] + result['cleanup_seconds'] >= result['total_seconds']:
        raise RxContractError('invalid_rx_test_contract')
    if (config.get('source') != 'meeting' or config.get('provider') != 'codex'
            or config.get('save_audio', False) or (not formal and config.get('response_enabled', False))
            or config.get('response_output_enabled', False)):
        raise RxContractError('rx_test_requires_transcription_only')
    return result


def confirm_prepared(session, generation, principal):
    """Caller must authenticate the current daemon/session owner first."""
    status = session.status()
    ready = status.get('rx_prepared') or {}
    if (status.get('state') != 'prepared' or not isinstance(generation, str)
            or not re.fullmatch('[0-9a-f]{32}', generation)
            or ready.get('generation') != generation or ready.get('meeting_id') != session.ident):
        raise RxContractError('rx_prepared_generation_mismatch')
    if principal != session.config().get('rx_owner_principal', 'local:' + str(os.getuid())):
        raise RxContractError('rx_owner_mismatch')
    if asyncio_clock() >= ready['expires_at_monotonic']:
        raise RxContractError('rx_owner_ready_expired')
    marker = session.path / 'owner-ready.json'
    # Publish a complete private file atomically without replacing an old marker.
    payload = {'schema_version': 1, 'meeting_id': session.ident, 'generation': generation,
               'principal': principal}
    temporary = marker.with_name('.owner-ready-' + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(payload, stream)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, marker)
        except FileExistsError:
            raise RxContractError('rx_owner_ready_already_submitted') from None
    finally:
        temporary.unlink(missing_ok=True)
    return {'meeting_id': session.ident, 'generation': generation, 'owner_ready_submitted': True}


def asyncio_clock():
    # The host monotonic clock is shared by the CLI and worker processes.
    import time
    return time.monotonic()


class ScopedSession:
    """Preserve underlying lease fencing and reject late worker writes."""
    mutations = {'update', 'event', 'append_text', 'append_final', 'save_provisional', 'record_increment', 'end_capture', 'bind_final_alias',
                 'finalize_minutes'}

    def __init__(self, session, runtime):
        self.base, self.runtime = session, runtime

    def __getattr__(self, name):
        value = getattr(self.base, name)
        if name not in self.mutations:
            return value
        def effect(*args, **kwargs):
            if not self.runtime.writes_open:
                raise RxContractError('rx_attempt_closed')
            return value(*args, **kwargs)
        return effect

    @property
    def response_controller(self):
        # The daemon holds the underlying fenced session, while run_session
        # writes through this scope. Both expose the same owned controller.
        return getattr(self.base, 'response_controller', None)

    @response_controller.setter
    def response_controller(self, controller):
        if not self.runtime.writes_open:
            raise RxContractError('rx_attempt_closed')
        self.base.response_controller = controller


class RxRuntime:
    def __init__(self, session, limits):
        self.session, self.limits = session, limits
        self.generation = uuid.uuid4().hex
        self.started = asyncio_clock()
        self.deadline = math.inf if limits['total_seconds'] is None else self.started + limits['total_seconds']
        self.work_deadline = self.deadline - limits['cleanup_seconds']
        self.cleanup_deadline = None
        self.soft_cleanup_deadline = None
        self.tasks = set()
        self.browser = self.provider = self.response = None
        self.owned = OwnedProcesses()
        self.writes_open = True
        self.cleanup_unknown = False
        self.failure_code = None
        self.phase = 'prepare'
        self.failure_diagnostic = None
        self.audio_snapshot_writer = None
        self.audio_snapshot_missing = False
        self.capture_deadline = None
        config = session.config()
        self.input_mode = 'native_media' if config['source'] == 'meeting' and config['provider'] == 'codex' else 'pcm'
        self.pcm_capture_required = self.input_mode == 'pcm' or config.get('save_audio', False)
        self.capture_sample_limit = (min(7_200_000, int(limits['capture_seconds'] * 24000))
            if self.pcm_capture_required and limits['capture_seconds'] is not None else None)
        self.pcm_accepted_samples = 0
        self.pcm_consumed_samples = 0
        self.capture_full = None
        self.capture_closed = None
        if session.status().get('state') != 'starting':
            raise RxContractError('rx_attempt_already_started')
        try:
            fd = os.open(session.path / 'rx-attempt.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise RxContractError('rx_attempt_already_started') from None
        with os.fdopen(fd, 'w') as stream:
            json.dump({'generation': self.generation, 'meeting_id': session.ident}, stream)

    def track(self, task):
        if task in self.tasks:
            return task
        self.tasks.add(task)
        def finished(value):
            self.tasks.discard(value)
            if not value.cancelled():
                value.exception()
        task.add_done_callback(finished)
        return task

    def capture_processes(self):
        if self.provider is not None:
            self.owned.add(getattr(getattr(self.provider, 'rpc', None), 'process', None))
        if self.response is not None:
            native = getattr(self.response, 'native', None)
            rpc = getattr(native, 'rpc', None)
            self.owned.add(getattr(rpc, 'process', None))
            if self.cleanup_deadline is not None:
                # Include shielded native/RPC close tasks and receiver tasks;
                # cancellation of their callers must not hide owned cleanup.
                candidates = [getattr(native, name, None) for name in (
                    '_close_task', 'receiver', '_rx_task', '_tool_task',
                    '_speech_task', '_phase_timeout')]
                candidates += [getattr(rpc, '_close_task', None)]
                candidates += list(getattr(rpc, 'tasks', ()))
                for task in candidates:
                    if isinstance(task, asyncio.Task) and not task.done() and task is not asyncio.current_task():
                        self.track(task)
        if self.browser is not None:
            # Verified Playwright PipeTransport fields, including partial start.
            for owner in (getattr(self.browser, 'playwright', None), getattr(self.browser, '_starter', None)):
                impl = getattr(owner, '_impl_obj', owner)
                connection = getattr(impl, '_connection', None)
                self.owned.add(getattr(getattr(connection, '_transport', None), '_proc', None))
        self.owned.snapshot()

    def begin_cleanup(self):
        if self.cleanup_deadline is None:
            now = asyncio_clock()
            self.cleanup_deadline = min(self.deadline, now + self.limits['cleanup_seconds'])
            self.soft_cleanup_deadline = min(self.deadline, now + .8 * self.limits['cleanup_seconds'])
            self.capture_processes()

    def begin_capture(self):
        duration = self.limits['capture_seconds']
        self.capture_deadline = min(self.work_deadline, math.inf if duration is None else asyncio_clock() + duration)
        self.capture_full = asyncio.get_running_loop().create_future()
        self.capture_closed = asyncio.get_running_loop().create_future()

    def end_capture(self):
        if self.capture_deadline is not None:
            self.capture_deadline = min(self.capture_deadline, asyncio_clock())
            if not self.capture_closed.done():
                self.capture_closed.set_result(None)

    def capture_open(self):
        return self.capture_deadline is not None and asyncio_clock() < self.capture_deadline

    async def call(self, awaitable, *, cleanup=False, capture=False, interrupt_on_samples=True):
        if cleanup:
            self.begin_cleanup()
        end = self.soft_cleanup_deadline if cleanup else self.work_deadline
        if capture:
            end = min(end, self.capture_deadline)
        remaining = end - asyncio_clock()
        sample_stop = capture and interrupt_on_samples and self.capture_full.done()
        if remaining <= 0 or sample_stop:
            if asyncio.isfuture(awaitable):
                self.track(awaitable)
                awaitable.cancel()
            elif hasattr(awaitable, 'close'):
                awaitable.close()
            if cleanup:
                self.cleanup_unknown = True
                return None
            if capture:
                raise RxCaptureComplete()
            raise RxContractError('rx_total_deadline_exceeded')
        task = self.track(asyncio.ensure_future(awaitable))
        try:
            wakeups = {task}
            if capture:
                wakeups.add(self.capture_closed)
            if capture and interrupt_on_samples:
                wakeups.add(self.capture_full)
            done, _ = await asyncio.wait(wakeups, timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if task not in done or (capture and self.capture_closed in done) or (capture and interrupt_on_samples and self.capture_full in done):
                task.cancel()
                if cleanup:
                    self.cleanup_unknown = True
                    return None
                if capture:
                    raise RxCaptureComplete()
                raise RxContractError('rx_total_deadline_exceeded')
            return await task
        except BaseException:
            if not task.done():
                task.cancel()
            raise

    async def owner_ready(self, meeting, provider, stopped):
        expires = min(self.work_deadline - (self.limits['capture_seconds'] or 0),
            asyncio_clock() + self.limits['ready_ttl_seconds'])
        if expires <= asyncio_clock():
            raise RxContractError('rx_capture_budget_unavailable')
        self.session.update(state='prepared', rx_prepared={'schema_version': 1,
            'meeting_id': self.session.ident, 'generation': self.generation,
            'expires_at_monotonic': expires, 'capture_seconds': self.limits['capture_seconds'],
            'total_seconds': self.limits['total_seconds'], 'cleanup_seconds': self.limits['cleanup_seconds'],
            'live_state': 'not_started' if provider is None else 'connected',
            'rx_ready': False,
            'owner_confirmation_required': True})
        while True:
            if stopped():
                raise InterruptedError('Stopped before owner confirmation')
            if asyncio_clock() >= expires:
                raise RxContractError('rx_owner_ready_expired')
            marker = self.session.path / 'owner-ready.json'
            if marker.exists():
                try:
                    fd = os.open(marker, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                    with os.fdopen(fd, 'r') as stream:
                        info = os.fstat(stream.fileno())
                        if not stat.S_ISREG(info.st_mode) or info.st_size > 2048:
                            raise ValueError()
                        value = json.loads(stream.read(2049))
                except (OSError, ValueError):
                    raise RxContractError('rx_owner_ready_invalid') from None
                expected = {'schema_version': 1, 'meeting_id': self.session.ident,
                    'generation': self.generation, 'principal': self.session.config().get('rx_owner_principal', 'local:' + str(os.getuid()))}
                if value != expected:
                    raise RxContractError('rx_owner_ready_generation_mismatch')
                if (provider is not None and provider.error) or not (await self.call(meeting.prepared_readiness()))['prepared']:
                    raise RxContractError('rx_prepared_no_longer_ready')
                if stopped() or asyncio_clock() >= expires:
                    raise RxContractError('rx_owner_ready_expired')
                self.session.update(state='owner_confirmed', owner_ready_consumed=True)
                return
            await self.call(asyncio.sleep(.02))

    async def finish(self):
        self.begin_cleanup()
        self.capture_processes()
        if self.response is not None:
            try:
                status = self.response.status()
                if status.get('state') != 'stopped' or status.get('cleanup_status') == 'unknown':
                    self.cleanup_unknown = True
            except Exception:
                self.cleanup_unknown = True
        reaped, quiet = False, 0
        # Cancelling native close can create a shielded RPC close in its
        # finally. Rediscover owned tasks/processes until stable or deadline.
        while asyncio_clock() < self.cleanup_deadline:
            self.capture_processes()
            for task in tuple(self.tasks):
                if not task.done(): task.cancel()
            if self.owned.live(): self.owned.kill()
            await asyncio.sleep(0)
            self.capture_processes()
            reaped = (not self.owned.live()) if all(p.returncode is not None for p in self.owned.processes) else \
                await self.owned.reap(min(self.cleanup_deadline, asyncio_clock() + .02))
            pending = any(not task.done() for task in self.tasks)
            quiet = quiet + 1 if reaped and not pending and not self.owned.live() else 0
            if quiet >= 2: break
            await asyncio.sleep(min(.001, max(0, self.cleanup_deadline - asyncio_clock())))
        self.capture_processes()
        pending = [task for task in self.tasks if not task.done()]
        for task in pending: task.cancel()
        if self.owned.live(): self.owned.kill()
        self.cleanup_unknown = self.cleanup_unknown or not reaped or bool(pending) or bool(self.owned.live())
        return not self.cleanup_unknown



async def supervise(session, limits, runner, *, install_signals):
    import signal
    runtime = RxRuntime(session, limits)
    wrapped = ScopedSession(session, runtime)
    task = asyncio.create_task(runner(wrapped, install_signals=False, rx_runtime=runtime))
    loop = asyncio.get_running_loop()
    cancelled = False
    def stop():
        session.stop()
    if install_signals:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop)
    try:
        while not task.done():
            if runtime.cleanup_deadline is None and runtime.provider is not None and runtime.provider.error:
                runtime.failure_diagnostic = {**live_failure(getattr(runtime.provider,'failure',None)),
                    'phase':runtime.phase}
                runtime.failure_code = runtime.failure_diagnostic['error_code']
                task.cancel()
                break
            browser = getattr(runtime.browser, 'browser', None)
            connected = getattr(browser, 'is_connected', None)
            if runtime.cleanup_deadline is None and callable(connected) and not connected():
                runtime.failure_code = 'rx_browser_disconnected'
                task.cancel()
                break
            if session.stopped() or asyncio_clock() >= runtime.work_deadline:
                runtime.failure_code = 'rx_stopped' if session.stopped() else 'rx_total_deadline_exceeded'
                if runtime.cleanup_deadline is None:
                    task.cancel()
                break
            await asyncio.wait({task}, timeout=min(.02, max(0, runtime.work_deadline - asyncio_clock())))
    except asyncio.CancelledError:
        cancelled = True
        session.stop()
        if runtime.failure_code in {None, 'rx_stopped', 'rx_cancelled'}:
            runtime.failure_code = 'rx_cancelled'
        if runtime.cleanup_deadline is None:
            task.cancel()
    finally:
        runtime.begin_cleanup()
        async def settle():
            if not task.done():
                await asyncio.wait({task}, timeout=max(0, runtime.soft_cleanup_deadline - asyncio_clock()))
            await runtime.finish()
        cleanup_task = asyncio.create_task(settle())
        while not cleanup_task.done() and runtime.cleanup_deadline > asyncio_clock():
            try:
                await asyncio.wait({cleanup_task}, timeout=runtime.cleanup_deadline - asyncio_clock())
            except asyncio.CancelledError:
                cancelled = True
                if runtime.failure_code in {None, 'rx_stopped', 'rx_cancelled'}:
                    runtime.failure_code = 'rx_cancelled'
                # This caller is cancellable; already-owned cleanup is not.
                # Keep its original deadline and let the final reaper fence
                # any unreturned accepted PCM rather than aborting it here.
                session.stop()
        if not cleanup_task.done():
            cleanup_task.cancel()
            runtime.capture_processes()
            runtime.owned.kill()
            runtime.cleanup_unknown = True
        elif cleanup_task.exception() is not None:
            runtime.cleanup_unknown = True
        try:
            if runtime.audio_snapshot_writer:
                runtime.audio_snapshot_writer(session)
        except Exception:
            runtime.audio_snapshot_missing = True
        finally:
            runtime.writes_open = False
        if not task.done():
            task.cancel()
            runtime.cleanup_unknown = True
        snapshot_status = {'rx_audio_snapshot_status':'missing'} if runtime.audio_snapshot_missing else {}
        existing = session.status()
        existing_failure = existing.get('state') in {
            'failed', 'recorded_without_transcript', 'completed_with_summary_error'}
        benign_stop = runtime.failure_code in {None, 'rx_stopped', 'rx_cancelled'}
        # The PCM adapter owns only accepted file/recording samples, never
        # native WebRTC delivery. A native observer cannot create drain debt.
        if (benign_stop and not existing_failure and runtime.pcm_capture_required
                and runtime.pcm_consumed_samples != runtime.pcm_accepted_samples):
            runtime.cleanup_unknown = True
        if runtime.cleanup_unknown or runtime.failure_code:
            failed = runtime.cleanup_unknown or existing_failure or not benign_stop
            code = ('rx_cleanup_unknown' if runtime.cleanup_unknown else
                existing.get('error') if existing_failure and benign_stop else runtime.failure_code)
            if runtime.failure_diagnostic:
                session.update(end_reason='provider_error')
            elif runtime.failure_code=='rx_browser_disconnected':
                session.update(end_reason='transport_loss')
            session.update(state='failed' if failed else 'stopped', error=code,
                cleanup_status='unknown' if runtime.cleanup_unknown else 'cleaned', **snapshot_status)
            if runtime.failure_diagnostic:
                session.update(diagnostic=runtime.failure_diagnostic)
        else:
            session.update(cleanup_status='cleaned', **snapshot_status)
        state = session.status()['state']
        failed = state in {'failed', 'recorded_without_transcript', 'completed_with_summary_error'}
        session.finalize_minutes('failed' if failed else 'stopped' if state == 'stopped' else 'completed',
            'transcript_unavailable' if state == 'recorded_without_transcript' else
            session.status().get('error') if failed or runtime.failure_code else None)
        if install_signals:
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    if cancelled:
        raise asyncio.CancelledError
    if task.done() and not task.cancelled():
        result = task.result()
        return 1 if failed else result
    return 1 if failed else 0
