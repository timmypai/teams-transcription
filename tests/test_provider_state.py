"""Read-only official account admission and cleanup; no model or network."""
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from teams_transcription.provider_state import CodexProviderState


def provider_settings(root):
    return SimpleNamespace(runtime_dir=str(Path(root)/'runtime'),codex_bin='never-called',
        live_provider='codex_live',live_model='gpt-live-1-codex',live_cost_policy='no_new_cost')


class ReadOnlyAccountRPC:
    calls = []
    account_type = 'chatgpt'
    def __init__(self, runtime, executable):
        self.runtime, self.executable = runtime, executable
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        pass
    async def request(self, method, params):
        self.calls.append((method, params))
        if method != 'account/read' or params != {'refreshToken':False}:
            raise AssertionError('A preflight must never make a model, purchase or login request')
        return {'account':{'type':self.account_type}, 'requiresOpenaiAuth':True}


class ProviderStateTests(unittest.IsolatedAsyncioTestCase):
    async def test_fresh_chatgpt_account_allows_only_existing_scoped_route(self):
        with tempfile.TemporaryDirectory() as root:
            settings = provider_settings(root)
            ticks = [10]
            ReadOnlyAccountRPC.calls.clear()
            state = CodexProviderState(settings, rpc_factory=ReadOnlyAccountRPC, clock=lambda:ticks[0])
            self.assertFalse(state.admissible(settings))
            self.assertTrue(await state.refresh())
            self.assertEqual(ReadOnlyAccountRPC.calls, [('account/read',{'refreshToken':False})])
            self.assertTrue(state.admissible(settings))
            self.assertFalse(state.admissible(SimpleNamespace(**vars(settings))))
            self.assertEqual(state.describe()['model_access'], 'checked_on_start')
            self.assertEqual(state.describe()['cost_policy_satisfied'], 'existing_chatgpt_allocation_only')
            ticks[0] += 301
            self.assertFalse(state.admissible(settings))
            self.assertEqual(state.describe()['provider_status'], 'blocked')

    async def test_api_key_missing_account_and_readonly_failure_do_not_admit(self):
        class ApiKeyRPC(ReadOnlyAccountRPC):
            account_type = 'apiKey'
        class ReadOnlyFailure(ReadOnlyAccountRPC):
            async def __aenter__(self):
                raise RuntimeError('Read-only file system: private token must not be logged')
        with tempfile.TemporaryDirectory() as root:
            settings = provider_settings(root)
            for factory, code in ((ApiKeyRPC,'chatgpt_auth_required'),
                                  (ReadOnlyFailure,'codex_runtime_readonly')):
                state = CodexProviderState(settings, rpc_factory=factory)
                self.assertFalse(await state.refresh())
                self.assertFalse(state.admissible(settings))
                self.assertEqual(state.describe()['auth_error_code'], code)
                self.assertNotIn('private', str(state.describe()))

    async def test_probe_cleanup_failure_cannot_leave_stale_success(self):
        class CloseFailure(ReadOnlyAccountRPC):
            async def __aexit__(self, *args):
                raise RuntimeError('fixture close failure')
        with tempfile.TemporaryDirectory() as root:
            settings = provider_settings(root)
            state = CodexProviderState(settings, rpc_factory=CloseFailure)
            self.assertFalse(await state.refresh())
            self.assertFalse(state.admissible(settings))

    async def test_readonly_failure_keeps_existing_home_and_discards_private_runtime(self):
        seen = []
        inherited_home = os.environ.get('CODEX_HOME')
        class RuntimeFailure(ReadOnlyAccountRPC):
            async def __aenter__(self):
                seen.append(Path(self.runtime))
                self.assert_home = os.environ.get('CODEX_HOME')
                if self.assert_home != inherited_home:
                    raise AssertionError('The probe must preserve its existing auth home')
                raise RuntimeError('Read-only file system (os error 30)')
        with tempfile.TemporaryDirectory() as root:
            settings = provider_settings(root)
            state = CodexProviderState(settings,rpc_factory=RuntimeFailure)
            self.assertFalse(await state.refresh())
            self.assertEqual(state.error_code,'codex_runtime_readonly')
            self.assertEqual(len(seen),1)
            self.assertTrue(seen[0].is_relative_to(Path(settings.runtime_dir)))
            self.assertFalse(seen[0].exists())
            self.assertEqual(os.environ.get('CODEX_HOME'),inherited_home)
            self.assertFalse(state.admissible(settings))

    



