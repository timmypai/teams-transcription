"""Account-only probe faults. No official app-server, model, browser or media."""
import asyncio
import errno
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from teams_transcription.provider_state import CodexProviderState
from teams_transcription.rpc import CodexRPC
from test_provider_state import provider_settings


class AccountFixture:
    def __init__(self, root, executable, *, initialize=0, account=0, cleanup=0,
                 failure=None, response=None):
        self.root = Path(root)
        self.delays = dict(initialize=initialize, account_read=account, cleanup=cleanup)
        self.failure = failure or {}
        self.response = response if response is not None else {
            'account':{'type':'chatgpt', 'email':'private@example.invalid', 'id':'private-user'},
            'requiresOpenaiAuth':True}
        self.entered = asyncio.Event()
        self.reading = asyncio.Event()
        self.cleaning = asyncio.Event()
        self.closed = False
        self.calls = []

    async def phase(self, name):
        delay = self.delays[name]
        if isinstance(delay, asyncio.Event):
            await delay.wait()
        else:
            await asyncio.sleep(delay)
        if name in self.failure:
            raise self.failure[name]

    async def __aenter__(self):
        self.entered.set()
        await self.phase('initialize')
        return self

    async def request(self, method, params):
        self.calls.append((method, params))
        if (method, params) != ('account/read', {'refreshToken':False}):
            raise AssertionError('Only the read-only account method is authorized')
        self.reading.set()
        await self.phase('account_read')
        return self.response

    async def __aexit__(self, *_):
        self.cleaning.set()
        try:
            await self.phase('cleanup')
        finally:
            self.closed = True


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.settings=provider_settings(self.directory.name)
        self.instances = []

    async def asyncTearDown(self):
        self.directory.cleanup()

    def state(self, *, budgets=None, **faults):
        def factory(root, executable):
            rpc = AccountFixture(root, executable, **faults)
            self.instances.append(rpc)
            return rpc
        return CodexProviderState(self.settings, rpc_factory=factory,
                                  **(budgets or {}))

    def assert_safe(self, state):
        snapshot = state.describe()
        for private in ('private@example.invalid', 'private-user', 'private-error',
                        'Bearer', 'refresh_token', self.directory.name):
            self.assertNotIn(private, str(snapshot))
        self.assertEqual(snapshot['auth_probe']['schema_version'], 1)
        self.assertGreaterEqual(snapshot['auth_probe']['elapsed_seconds'], 0)
        self.assertFalse(list(Path(self.settings.runtime_dir).glob('account-read-*')))

    async def test_default_budgets_accept_reported_slow_initialize_and_account(self):
        # Reproduce the reported timings with an offline fixture. This exceeds
        # the old five-second total, without using an account or official binary.
        state = self.state(initialize=8.172, account=2.930)
        self.assertTrue(await state.refresh())
        probe = state.describe()['auth_probe']
        self.assertEqual((probe['status'], probe['phase']), ('succeeded', 'complete'))
        self.assertGreaterEqual(probe['phases']['initialize']['elapsed_seconds'], 8)
        self.assertGreaterEqual(probe['phases']['account_read']['elapsed_seconds'], 2.9)
        self.assertGreaterEqual(probe['elapsed_seconds'], 11)
        self.assertTrue(state.admissible(self.settings))
        self.assertEqual(self.instances[0].calls, [('account/read', {'refreshToken':False})])
        self.assert_safe(state)

    async def test_account_has_its_own_budget_after_slow_initialize(self):
        state = self.state(initialize=.04, account=.06,
                           budgets=dict(initialize_timeout=.08, account_timeout=.08))
        self.assertTrue(await state.refresh())
        self.assert_safe(state)

    async def test_initialize_and_account_timeout_are_distinct_and_cleaned(self):
        for phase in ('initialize', 'account_read'):
            with self.subTest(phase=phase):
                faults = {'initialize' if phase == 'initialize' else 'account':asyncio.Event()}
                state = self.state(**faults, budgets=dict(initialize_timeout=.04,
                                                          account_timeout=.04))
                began = time.monotonic()
                self.assertFalse(await asyncio.wait_for(state.refresh(), 1))
                self.assertLess(time.monotonic()-began, .8)
                self.assertFalse(state.admissible(self.settings))
                self.assertEqual(state.error_code, 'provider_account_probe_timeout')
                probe = state.describe()['auth_probe']
                self.assertEqual((probe['phase'], probe['error_class']), (phase, 'TimeoutError'))
                self.assertEqual(probe['phases']['cleanup']['status'], 'succeeded')
                self.assertTrue(self.instances[-1].closed)
                self.assertEqual(len(self.instances[-1].calls), int(phase == 'account_read'))
                self.assert_safe(state)

    async def test_phase_errors_and_cleanup_error_keep_first_safe_cause(self):
        for phase in ('initialize', 'account_read', 'cleanup'):
            with self.subTest(phase=phase):
                state = self.state(failure={phase:ValueError('private-error Bearer credential')})
                self.assertFalse(await state.refresh())
                self.assertEqual(state.error_code, 'provider_account_probe_failed')
                self.assertEqual((state.probe['phase'], state.probe['error_class']), (phase, 'ValueError'))
                self.assertTrue(self.instances[-1].closed)
                self.assert_safe(state)
        state = self.state(failure={'account_read':ValueError('private-error'),
                                   'cleanup':RuntimeError('private-error')})
        self.assertFalse(await state.refresh())
        self.assertEqual((state.probe['phase'], state.probe['error_class']), ('account_read','ValueError'))
        self.assertEqual(state.probe['phases']['cleanup']['error_class'], 'RuntimeError')
        self.assert_safe(state)

    async def test_runtime_readonly_has_phase_and_no_account_probe(self):
        state = self.state()
        with patch('teams_transcription.provider_state.Path.mkdir',
                   side_effect=OSError(errno.EROFS, 'private-error')):
            self.assertFalse(await state.refresh())
        self.assertEqual(state.error_code, 'codex_runtime_readonly')
        self.assertEqual((state.probe['phase'], state.probe['error_class']), ('runtime', 'OSError'))
        self.assertEqual(self.instances, [])
        self.assert_safe(state)

    async def test_unknown_exception_class_is_not_account_data(self):
        private_kind = type('private@example.invalid', (Exception,), {})
        state = self.state(failure={'initialize':private_kind('private-error')})
        self.assertFalse(await state.refresh())
        self.assertEqual(state.probe['error_class'], 'Exception')
        self.assert_safe(state)

    async def test_auth_conditions_are_not_relaxed_and_diagnostics_are_copied(self):
        for response in ({}, {'account':None}, {'account':{'type':'apiKey'}},
                         {'account':{'type':'chatgpt-other'}}, [], 'private-error'):
            with self.subTest(response_type=type(response).__name__):
                state = self.state(response=response)
                self.assertFalse(await state.refresh())
                self.assertFalse(state.admissible(self.settings))
                self.assertEqual(state.error_code, 'chatgpt_auth_required')
                self.assertEqual(state.probe['phase'], 'account_read')
                self.assert_safe(state)
        state = self.state()
        self.assertTrue(await state.refresh())
        result = state.describe()
        result['auth_probe']['phases']['initialize']['status'] = 'private-error'
        self.assertEqual(state.probe['phases']['initialize']['status'], 'succeeded')

    async def test_cleanup_timeout_blocks_success_and_invalidates_previous_success(self):
        state = self.state()
        self.assertTrue(await state.refresh())
        state.budgets['cleanup'] = .04
        original_factory = state.rpc_factory
        def hanging_factory(*args):
            rpc = original_factory(*args)
            rpc.delays['cleanup'] = asyncio.Event()
            return rpc
        state.rpc_factory = hanging_factory
        began = time.monotonic()
        self.assertFalse(await asyncio.wait_for(state.refresh(), 1))
        await asyncio.sleep(0)  # Deliver cancellation to the cleanup coroutine.
        self.assertLess(time.monotonic()-began, .8)
        self.assertFalse(state.admissible(self.settings))
        self.assertEqual(state.error_code, 'provider_account_probe_timeout')
        self.assertEqual((state.probe['phase'], state.probe['error_class']), ('cleanup', 'TimeoutError'))
        self.assertTrue(self.instances[-1].closed)
        self.assert_safe(state)

    async def test_cancellation_during_initialize_and_account_reaps_then_propagates(self):
        for phase in ('initialize', 'account_read'):
            with self.subTest(phase=phase):
                faults = {'initialize' if phase == 'initialize' else 'account':asyncio.Event()}
                state = self.state(**faults)
                previous_count = len(self.instances)
                task = asyncio.create_task(state.refresh())
                while len(self.instances) == previous_count:
                    await asyncio.sleep(0)
                rpc = self.instances[-1]
                await asyncio.wait_for(rpc.entered.wait() if phase == 'initialize' else rpc.reading.wait(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 1)
                self.assertTrue(rpc.closed)
                self.assertFalse(state.admissible(self.settings))
                self.assertEqual(state.error_code, 'provider_account_probe_cancelled')
                self.assertEqual((state.probe['status'], state.probe['phase']), ('cancelled', phase))
                self.assert_safe(state)

    async def test_repeated_cancellation_during_cleanup_keeps_same_deadline(self):
        release = asyncio.Event()
        state = self.state(cleanup=release, budgets=dict(cleanup_timeout=.15))
        task = asyncio.create_task(state.refresh())
        while not self.instances:
            await asyncio.sleep(0)
        await self.instances[-1].cleaning.wait()
        task.cancel()
        await asyncio.sleep(.01)
        task.cancel()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)
        self.assertTrue(self.instances[-1].closed)
        self.assertFalse(state.admissible(self.settings))
        self.assertEqual((state.probe['phase'], state.probe['error_class']), ('cleanup', 'CancelledError'))
        self.assert_safe(state)

    async def test_repeated_cancellation_cannot_extend_hanging_cleanup_deadline(self):
        state = self.state(cleanup=asyncio.Event(), budgets=dict(cleanup_timeout=.07))
        task = asyncio.create_task(state.refresh())
        while not self.instances:
            await asyncio.sleep(0)
        await self.instances[-1].cleaning.wait()
        began = time.monotonic()
        for _ in range(3):
            task.cancel()
            await asyncio.sleep(.01)
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(task, .5)
        await asyncio.sleep(0)
        self.assertLess(time.monotonic()-began, .3)
        self.assertEqual(state.probe['phases']['cleanup']['error_class'], 'TimeoutError')
        self.assertFalse(state.admissible(self.settings))
        self.assert_safe(state)

    async def test_timeout_and_cleanup_error_keep_timeout_as_primary(self):
        state = self.state(account=asyncio.Event(),
                           failure={'cleanup':RuntimeError('private-error')},
                           budgets=dict(account_timeout=.03))
        self.assertFalse(await state.refresh())
        self.assertEqual(state.error_code, 'provider_account_probe_timeout')
        self.assertEqual((state.probe['phase'],state.probe['error_class']), ('account_read','TimeoutError'))
        self.assertEqual(state.probe['phases']['cleanup']['error_class'], 'RuntimeError')
        self.assert_safe(state)

    async def test_cancelled_refresh_loop_is_cleaned_by_close(self):
        state = self.state()
        state.ttl = .01
        await state.start()
        original = state.rpc_factory
        def hanging_factory(*args):
            rpc = original(*args)
            rpc.delays['account_read'] = asyncio.Event()
            return rpc
        state.rpc_factory = hanging_factory
        while len(self.instances) < 2:
            await asyncio.sleep(.001)
        await self.instances[-1].reading.wait()
        await asyncio.wait_for(state.close(), 1)
        self.assertTrue(self.instances[-1].closed)
        self.assertTrue(state.task.done())
        self.assertFalse(state.admissible(self.settings))
        self.assert_safe(state)

    async def test_budget_cannot_be_unbounded_or_extended(self):
        for kwargs in ({'initialize_timeout':float('inf')}, {'account_timeout':0},
                       {'cleanup_timeout':float('nan')}, {'cleanup_timeout':True},
                       {'initialize_timeout':31}, {'account_timeout':16}, {'cleanup_timeout':9}):
            with self.subTest(kwargs=kwargs), self.assertRaisesRegex(ValueError, 'invalid_account_probe_budget'):
                CodexProviderState(self.settings, **kwargs)


# This is a local JSON-lines protocol fixture, not the official Codex executable.
LOCAL_SERVER = '''#!/usr/bin/env python3
import json,sys,signal,time
MODE = MODE_VALUE
def send(value): print(json.dumps(value), flush=True)
if MODE == 'ignore-term': signal.signal(signal.SIGTERM, signal.SIG_IGN)
for line in sys.stdin:
    value = json.loads(line)
    method = value.get('method')
    if method == 'initialized': continue
    if method not in ('initialize','account/read'):
        raise SystemExit('An account probe must not request model or tools')
    if method == 'initialize' and MODE == 'initialize-hang': continue
    if method == 'account/read' and MODE == 'account-hang': continue
    if method == 'account/read' and MODE == 'initialize-error':
        raise SystemExit('account/read was not allowed after initialize failure')
    if method == 'initialize' and MODE == 'initialize-error':
        send({'id':value['id'],'error':{'message':'private-error'}})
    else:
        send({'id':value['id'],'result':{'account':{'type':'chatgpt'}}})
'''


class ProbeSubprocessTests(unittest.IsolatedAsyncioTestCase):
    async def run_probe(self, mode, cancel=False):
        with tempfile.TemporaryDirectory() as root:
            script = Path(root)/'local-account-fixture'
            script.write_text(LOCAL_SERVER.replace('MODE_VALUE', repr(mode)))
            script.chmod(0o700)
            instances = []
            def factory(state_dir, executable):
                rpc = CodexRPC(state_dir, str(script))
                instances.append(rpc)
                return rpc
            state = CodexProviderState(provider_settings(root), rpc_factory=factory,
                                       initialize_timeout=.3, account_timeout=.08)
            task = asyncio.create_task(state.refresh())
            if cancel:
                required_id = 2 if mode == 'account-hang' else 1
                while (not instances or not instances[0].process
                       or required_id not in instances[0].pending):
                    await asyncio.sleep(.001)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            else:
                result = await asyncio.wait_for(task, 8)
                self.assertEqual(result, mode == 'ignore-term')
            rpc = instances[0]
            self.assertIsNotNone(rpc.process.returncode)
            self.assertTrue(all(task.done() for task in rpc.tasks))
            self.assertEqual(rpc.pending, {})
            self.assertTrue(rpc._close_task.done())
            self.assertFalse(Path(rpc.state_dir).exists())
            self.assertNotIn('private-error', str(state.describe()))
            return state, rpc

    async def test_real_local_process_timeout_in_each_phase_is_reaped(self):
        for mode, phase in (('initialize-hang','initialize'), ('account-hang','account_read')):
            with self.subTest(mode=mode):
                state, rpc = await self.run_probe(mode)
                self.assertFalse(state.admissible(state.settings))
                self.assertEqual(state.probe['phase'], phase)
                self.assertEqual(state.error_code, 'provider_account_probe_timeout')
                self.assertEqual(state.probe['phases']['cleanup']['status'], 'succeeded')

    async def test_real_local_process_cancelled_initialize_is_reaped(self):
        state, rpc = await self.run_probe('initialize-hang', cancel=True)
        self.assertEqual(state.probe['status'], 'cancelled')
        self.assertFalse(state.admissible(state.settings))

    async def test_real_local_process_cancelled_account_read_is_reaped(self):
        state, rpc = await self.run_probe('account-hang', cancel=True)
        self.assertEqual((state.probe['status'],state.probe['phase']), ('cancelled','account_read'))
        self.assertFalse(state.admissible(state.settings))

    async def test_real_local_initialize_error_preserves_class_and_closes_once(self):
        state, rpc = await self.run_probe('initialize-error')
        self.assertEqual((state.probe['phase'], state.probe['error_class']), ('initialize', 'RuntimeError'))
        self.assertEqual(state.probe['phases']['cleanup']['status'], 'succeeded')
        close_task = rpc._close_task
        await rpc.close()
        self.assertIs(rpc._close_task, close_task)

    async def test_real_local_child_ignoring_terminate_is_killed_and_reaped(self):
        state, rpc = await self.run_probe('ignore-term')
        self.assertEqual(rpc.process.returncode, -9)
        self.assertTrue(state.admissible(state.settings))
        self.assertGreaterEqual(state.probe['phases']['cleanup']['elapsed_seconds'], 3)
        self.assertLess(state.probe['phases']['cleanup']['elapsed_seconds'], 8)


class RPCCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_kill_wait_is_bounded_and_pending_requests_are_discarded(self):
        release = asyncio.Event()
        rpc = CodexRPC('/tmp/unused-account-fixture')
        rpc.process = Mock(returncode=None, wait=AsyncMock(side_effect=release.wait))
        future = asyncio.get_running_loop().create_future()
        rpc.pending[1] = future
        original_wait_for = asyncio.wait_for
        budgets = []
        async def short_wait(awaitable, timeout):
            budgets.append(timeout)
            return await original_wait_for(awaitable, .02)
        began = time.monotonic()
        with patch('teams_transcription.rpc.asyncio.wait_for', short_wait):
            with self.assertRaises(TimeoutError):
                await rpc.close()
        self.assertEqual(budgets, [3, 2])
        self.assertLess(time.monotonic()-began, .5)
        rpc.process.terminate.assert_called_once()
        rpc.process.kill.assert_called_once()
        self.assertTrue(future.cancelled())
        self.assertEqual(rpc.pending, {})

    async def test_cancelling_one_close_caller_does_not_cancel_shared_reap(self):
        release = asyncio.Event()
        rpc = CodexRPC('/tmp/unused-account-fixture')
        rpc.process = Mock(returncode=None, wait=AsyncMock(side_effect=release.wait))
        owner = asyncio.create_task(rpc.close())
        while not rpc.process.terminate.called:
            await asyncio.sleep(0)
        other = asyncio.create_task(rpc.close())
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertFalse(rpc._close_task.cancelled())
        release.set()
        await asyncio.wait_for(other, 1)
        self.assertTrue(rpc._close_task.done())
        rpc.process.terminate.assert_called_once()
        rpc.process.kill.assert_not_called()
