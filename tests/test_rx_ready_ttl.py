"""Owner-ready coordination boundaries using actual methods and a local clock."""
import asyncio
import json
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from teams_transcription.rx_contract import RxRuntime, RxContractError, confirm_prepared, options
from teams_transcription.storage import Session


def config(formal=True):
    return {'source':'meeting', 'provider':'codex', 'rx_owner_principal':'alice',
        'rx_session_contract':formal, 'rx_test_contract':not formal}


class ReadyTTLTests(unittest.IsolatedAsyncioTestCase):
    async def prepared(self, root, clock, *, formal=True, readiness=None, ident=None):
        session = Session(root, ident).create(config(formal))
        runtime = RxRuntime(session, options(session.config()))
        meeting = SimpleNamespace(prepared_readiness=readiness or AsyncMock(return_value={'prepared':True}))
        task = asyncio.create_task(runtime.owner_ready(meeting, None, lambda:False))
        self.addAsyncCleanup(self.finish_task, task)
        for _ in range(20):
            if session.status()['state'] == 'prepared':
                return session, runtime, task
            await asyncio.sleep(0)
        self.fail('actual owner_ready did not publish prepared')

    async def finish_task(self, task):
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def test_formal_300_test_120_defaults_and_test_maximum(self):
        formal, bounded = options(config()), options(config(False))
        self.assertEqual(formal, {'capture_seconds':None, 'total_seconds':None,
            'cleanup_seconds':10., 'ready_ttl_seconds':300.})
        self.assertEqual(bounded, {'capture_seconds':300., 'total_seconds':600.,
            'cleanup_seconds':10., 'ready_ttl_seconds':120.})
        for ttl in [120, 120.001, 300]:
            value = {**config(False), 'rx_test_contract':{'ready_ttl_seconds':ttl}}
            if ttl == 120:
                self.assertEqual(options(value)['ready_ttl_seconds'], 120)
            else:
                with self.assertRaisesRegex(RxContractError, 'invalid_rx_test_contract'):
                    options(value)
        # Formal sessions expose no caller TTL override; mixed-mode overrides
        # remain invalid rather than extending the fixed coordination maximum.
        with self.assertRaisesRegex(RxContractError, 'invalid_rx_session_contract'):
            options({**config(), 'rx_test_contract':{'ready_ttl_seconds':301}})

    async def test_actual_formal_and_test_deadlines_accept_just_before_expiry(self):
        for formal, ttl in [(True,300), (False,120)]:
            with self.subTest(formal=formal), tempfile.TemporaryDirectory() as root, \
                    patch('teams_transcription.rx_contract.asyncio_clock', Mock(return_value=1000.)) as clock:
                session, runtime, task = await self.prepared(root, clock, formal=formal)
                self.assertEqual(session.status()['rx_prepared']['expires_at_monotonic'], 1000+ttl)
                clock.return_value = 1000+ttl-.001
                confirm_prepared(session, runtime.generation, 'alice')
                await task
                self.assertTrue(session.status()['owner_ready_consumed'])
                self.assertEqual(session.status()['state'], 'owner_confirmed')

    async def test_submit_and_worker_both_reject_exact_expiry(self):
        for formal, ttl in [(True,300), (False,120)]:
            with self.subTest(formal=formal), tempfile.TemporaryDirectory() as root, \
                    patch('teams_transcription.rx_contract.asyncio_clock', Mock(return_value=1000.)) as clock:
                session, runtime, task = await self.prepared(root, clock, formal=formal)
                clock.return_value = 1000+ttl
                with self.assertRaisesRegex(RxContractError, 'rx_owner_ready_expired'):
                    confirm_prepared(session, runtime.generation, 'alice')
                self.assertFalse((session.path/'owner-ready.json').exists())
                with self.assertRaisesRegex(RxContractError, 'rx_owner_ready_expired'):
                    await task
                self.assertNotIn('owner_ready_consumed', session.status())

    async def test_worker_rechecks_expiry_after_prepared_readiness(self):
        with tempfile.TemporaryDirectory() as root, \
                patch('teams_transcription.rx_contract.asyncio_clock', Mock(return_value=1000.)) as clock:
            async def crossed_boundary():
                clock.return_value = 1300.
                return {'prepared':True}
            session, runtime, task = await self.prepared(root, clock, readiness=crossed_boundary)
            clock.return_value = 1299.999
            confirm_prepared(session, runtime.generation, 'alice')
            with self.assertRaisesRegex(RxContractError, 'rx_owner_ready_expired'):
                await task
            self.assertNotIn('owner_ready_consumed', session.status())

    async def test_submit_identity_and_replay_protection_remain_single_use(self):
        with tempfile.TemporaryDirectory() as root, \
                patch('teams_transcription.rx_contract.asyncio_clock', Mock(return_value=1000.)) as clock:
            session, runtime, task = await self.prepared(root, clock)
            for owner, generation, code in [('bob',runtime.generation,'rx_owner_mismatch'),
                    ('alice','0'*32,'rx_prepared_generation_mismatch')]:
                with self.assertRaisesRegex(RxContractError, code):
                    confirm_prepared(session, generation, owner)
            ready = session.status()['rx_prepared']
            session.update(rx_prepared={**ready, 'meeting_id':'foreign-meeting'})
            with self.assertRaisesRegex(RxContractError, 'rx_prepared_generation_mismatch'):
                confirm_prepared(session, runtime.generation, 'alice')
            session.update(rx_prepared=ready)
            confirm_prepared(session, runtime.generation, 'alice')
            with self.assertRaisesRegex(RxContractError, 'rx_owner_ready_already_submitted'):
                confirm_prepared(session, runtime.generation, 'alice')
            await task
            with self.assertRaisesRegex(RxContractError, 'rx_prepared_generation_mismatch'):
                confirm_prepared(session, runtime.generation, 'alice')

    async def test_worker_rejects_foreign_marker_and_changed_prepared_state(self):
        for field in ['meeting_id', 'generation', 'principal', 'prepared']:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as root, \
                    patch('teams_transcription.rx_contract.asyncio_clock', Mock(return_value=1000.)) as clock:
                readiness = AsyncMock(return_value={'prepared':field != 'prepared'})
                session, runtime, task = await self.prepared(root, clock, readiness=readiness)
                confirm_prepared(session, runtime.generation, 'alice')
                if field != 'prepared':
                    marker = session.path/'owner-ready.json'
                    value = json.loads(marker.read_text()); value[field] = 'foreign'
                    marker.write_text(json.dumps(value))
                code = 'rx_prepared_no_longer_ready' if field == 'prepared' else 'rx_owner_ready_generation_mismatch'
                with self.assertRaisesRegex(RxContractError, code):
                    await task
                self.assertNotIn('owner_ready_consumed', session.status())

    


if __name__ == '__main__':
    unittest.main()
