"""Controlled production lifecycle tests: no browser, model, meeting or audio device."""
import asyncio
import base64
import contextlib
import json
import os
import tempfile
import time
import wave
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch


from teams_transcription.browser import MeetingBrowser

from teams_transcription.meeting_store import MeetingStore
from teams_transcription.rx_contract import RxContractError, confirm_prepared, options, supervise
from teams_transcription.live import LiveProvider

from teams_transcription.storage import Session
from teams_transcription.worker import _run_session, run_session

from lifecycle_probe import session_events


LIMITS = dict(capture_seconds=.06, total_seconds=.8, cleanup_seconds=.2, ready_ttl_seconds=.3)
# Real SQLite work must not turn behavior checks into cleanup speed tests.
# Dedicated deadline, expiry, repeated-cancellation and hung-cleanup tests
# keep LIMITS; the owner-stop behavior test retains its separate 500ms wait.
# Capture durations/sample caps are unchanged in both fixture profiles.
BEHAVIOR_LIMITS = {**LIMITS, 'total_seconds':3., 'cleanup_seconds':1.,
    'ready_ttl_seconds':1.}
URL = 'https://teams.microsoft.com/l/meetup-join/synthetic-fixture'


def config(**changes):
    value = dict(source='meeting', provider='codex', meeting_url=URL, name='fixture',
        duration=300, join_timeout=.1, audio_timeout=.1, drain_seconds=.001,
        codex_bin='never-called', save_audio=False, response_enabled=False,
        response_output_enabled=False, rx_test_contract=LIMITS.copy(),
        rx_owner_principal='local:' + str(os.getuid()))
    value.update(changes)
    return value


def native_transport(*, ready=True, failed=False, stopped=False):
    return {'input_mode':'native_media', 'connection':'connected', 'data_channel':'open',
        'native_source':{'available':True, 'ready':ready, 'failed':failed, 'stopped':stopped, 'capture_enabled':ready,
            'failure_code':'native_media_source_lost' if failed else 'none',
            'frame_count':1, 'connected_frames':1, 'live_tracks':1, 'cleanup_status':'idle',
            'packets_received':4, 'bytes_received':128},
        'reports':[{'packetsSent':2, 'bytesSent':128}]}


class Fixture:
    def __init__(self, *, admission_delay=0, live_delay=0, hang_close=False, prepared=True,
                 source=None, stats_delay=0, admitted_delay=0, final_stats_delay=0, start_delay=0,
                 disconnect_after=None, start_failure=False, revoke_on_live_start=False, media_ready=True,
                 initial_pcm=None, stats_report=None, emit_final=True, observe_pcm=True, native_failed=False):
        self.admission_delay, self.live_delay = admission_delay, live_delay
        self.hang_close, self.prepared = hang_close, prepared
        self.joins = self.captures = self.live_starts = self.pushes = 0
        self.closed = []
        self.process = None
        self.source = source
        self.source_task = None
        self.stats_delay, self.admitted_delay = stats_delay, admitted_delay
        self.final_stats_delay = final_stats_delay
        self.start_delay = start_delay
        self.disconnect_after = disconnect_after
        self.start_failure, self.revoke_on_live_start = start_failure, revoke_on_live_start
        self.media_ready = media_ready
        self.initial_pcm = initial_pcm if initial_pcm is not None else b'\x01\x00' * 32
        self.stats_report, self.emit_final = stats_report, emit_final
        self.observe_pcm, self.native_failed = observe_pcm, native_failed
        self.media_sources = []
        self.push_samples = []
        self.capture_started_at = None
        self.capture_stopped_at = None
        self.stats_calls = self.admitted_calls = 0
        self.late_pcm_attempts = 0

    @contextlib.contextmanager
    def install(self):
        owner = self
        class Browser:
            def __init__(self, *args): self.browser = object()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): owner.closed.append('browser')
        class Meeting:
            def __init__(self, browser, on_audio, event, **kwargs):
                async def bound_audio(packet):
                    await on_audio({'source_document':self, **packet})
                self.on_audio = bound_audio
                owner.meeting = self
            @property
            def native_audio_failed(self):
                return owner.native_failed
            async def open(self, *args): pass
            async def join(self, *args):
                owner.joins += 1
                await asyncio.sleep(owner.admission_delay)
            async def prepared_readiness(self): return {'prepared':owner.prepared}
            async def start_capture(self, **kwargs):
                owner.captures += 1
                owner.capture_started_at = time.monotonic()
                if owner.observe_pcm:
                    ended = time.monotonic()
                    await self.on_audio(dict(sample_rate=24000, frames=len(owner.initial_pcm)//2, start_frame=0,
                        data=base64.b64encode(owner.initial_pcm).decode(), source_clock='monotonic',
                        source_started_at=ended-len(owner.initial_pcm)/48000, source_ended_at=ended))
                if owner.source:
                    owner.source_task = asyncio.create_task(owner.source(self.on_audio,owner))
                await asyncio.sleep(owner.start_delay)
                return {'ready':True}
            async def stats(self):
                owner.stats_calls += 1
                if 'capture' in owner.closed:
                    # Simulate a stale browser callback during slow final stats.
                    owner.late_pcm_attempts += 1
                    await self.on_audio(dict(sample_rate=24000,frames=32,start_frame=32,
                        data=base64.b64encode(b'\x01\x00'*32).decode()))
                    await asyncio.sleep(owner.final_stats_delay)
                else:
                    await asyncio.sleep(owner.stats_delay)
                return owner.stats_report if owner.stats_report is not None else []
            async def admitted(self):
                owner.admitted_calls += 1
                await asyncio.sleep(owner.admitted_delay)
                return owner.disconnect_after is None or owner.admitted_calls < owner.disconnect_after
            async def capture_lifecycle(self):
                return 'joined' if await self.admitted() else 'confirmed_end'
            async def stop_capture(self):
                owner.closed.append('capture')
                if owner.capture_stopped_at is None:
                    owner.capture_stopped_at = time.monotonic()
                if owner.source_task:
                    owner.source_task.cancel()
                    await asyncio.gather(owner.source_task,return_exceptions=True)
            async def close(self):
                if owner.source_task and not owner.source_task.done(): await self.stop_capture()
                owner.closed.append('meeting')
        class Live:
            def __init__(self, *args, on_final, media_source=None, **kwargs):
                owner.provider = self
                owner.media_sources.append(media_source)
                self.media_source = media_source
                self.on_final, self.error = on_final, None
                self.final_emitted = False
                async def stats():
                    return native_transport(ready=owner.media_ready and owner.captures > 0 and 'capture' not in owner.closed,
                        failed=owner.native_failed, stopped='capture' in owner.closed)
                self.peer = SimpleNamespace(stats=AsyncMock(side_effect=stats))
                self.rpc = SimpleNamespace(process=None)
            async def start(self):
                owner.live_starts += 1
                if owner.start_failure:
                    raise RuntimeError('controlled startup failure')
                if owner.hang_close:
                    owner.process = await asyncio.create_subprocess_exec('sleep', '60')
                    self.rpc.process = owner.process
                await asyncio.sleep(owner.live_delay)
                if owner.revoke_on_live_start:
                    owner.prepared = False
            async def push(self, data):
                owner.pushes += 1
                owner.push_samples.append(len(data)//2)
                if self.media_source is not None:
                    raise AssertionError('native meeting input cannot push Python PCM')
                if owner.pushes == 1 and owner.emit_final:
                    await self.on_final(dict(event_id='fixture-final', revision=0,
                        text='受控來源逐字稿', timestamp='2026-10-10T01:00:00Z', sequence=1))
            async def drain(self, seconds): pass
            async def ready(self):
                ready = owner.media_ready and not owner.native_failed and not self.error
                if ready and owner.emit_final and not self.final_emitted:
                    await self.on_final(dict(event_id='fixture-final', revision=0,
                        text='受控來源逐字稿', timestamp='2026-10-10T01:00:00Z', sequence=1))
                    self.final_emitted = True
                return ready
            async def close(self):
                if owner.hang_close: await asyncio.Event().wait()
                owner.closed.append('live')
        with patch('teams_transcription.worker.Browser', Browser), patch('teams_transcription.worker.MeetingBrowser', Meeting), patch('teams_transcription.worker.LiveProvider', Live), patch('teams_transcription.worker.summary.generate', new_callable=AsyncMock) as summary:
            yield summary


class RxContractTests(unittest.IsolatedAsyncioTestCase):
    async def run_confirmed(self, session, fixture, *, runtime_limits=None):
        # Explicit recording-adapter tests enter the real owned worker seam;
        # they do not claim authorization from the transcription-only CLI gate.
        operation = (run_session(session, install_signals=False) if runtime_limits is None else
            supervise(session, runtime_limits, _run_session, install_signals=False))
        task = asyncio.create_task(operation)
        try:
            generation = await self.ready(session, task)
            confirm_prepared(session, generation, session.config()['rx_owner_principal'])
            return await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def continuous_source(self,callback,owner):
        offset = 32
        while True:
            await callback(dict(sample_rate=24000,frames=16,start_frame=offset,
                data=base64.b64encode(b'\x01\x00'*16).decode()))
            offset += 16
            await asyncio.sleep(.004)

    async def test_slow_periodic_stats_cannot_extend_capture_window(self):
        with tempfile.TemporaryDirectory() as root:
            limits={**LIMITS,'capture_seconds':.15}
            session = Session(root).create(config(rx_test_contract=limits))
            fixture = Fixture(source=self.continuous_source,stats_delay=.3,start_delay=.03)
            with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL',.005):
                self.assertEqual(await self.run_confirmed(session,fixture),0)
            self.assertGreaterEqual(fixture.stats_calls,1)
            self.assertTrue(fixture.source_task.done())
            self.assertLess(fixture.capture_stopped_at - fixture.capture_started_at, .2)
            self.assertEqual(fixture.pushes, 0)
            self.assertEqual(session.status()['rx_audio']['python']['queued_frames'], 0)

    async def test_slow_periodic_admitted_cannot_extend_capture_window(self):
        with tempfile.TemporaryDirectory() as root:
            limits={**LIMITS,'capture_seconds':.15}
            session = Session(root).create(config(rx_test_contract=limits))
            fixture = Fixture(source=self.continuous_source,admitted_delay=.3,start_delay=.03)
            with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL',.005):
                self.assertEqual(await self.run_confirmed(session,fixture),0)
            self.assertGreaterEqual(fixture.admitted_calls,1)
            self.assertLess(fixture.capture_stopped_at - fixture.capture_started_at, .2)
            self.assertEqual(fixture.pushes, 0)

    async def test_formal_unlimited_waits_for_owner_then_runs_until_owner_stop(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(duration=None, rx_test_contract=False, rx_session_contract=True))
            limits = options(session.config())
            self.assertIsNone(limits['capture_seconds'])
            self.assertIsNone(limits['total_seconds'])
            fixture = Fixture(source=self.continuous_source)
            with fixture.install() as summary:
                task = asyncio.create_task(run_session(session, install_signals=False))
                try:
                    generation = await self.ready(session, task)
                    self.assertEqual(fixture.captures, 0)
                    self.assertEqual(fixture.live_starts, 0)
                    self.assertEqual(session.status()['rx_prepared']['live_state'], 'not_started')
                    confirm_prepared(session, generation, session.config()['rx_owner_principal'])
                    receiving_deadline = time.monotonic() + 1
                    while not session.status().get('rx_ready'):
                        self.assertFalse(task.done())
                        self.assertLess(time.monotonic(), receiving_deadline)
                        await asyncio.sleep(.003)
                    await asyncio.sleep(.18)
                    self.assertFalse(task.done())
                    self.assertTrue(session.status()['rx_ready'])
                    self.assertIsNone(fixture.capture_stopped_at)
                    self.assertEqual(fixture.pushes, 0)
                    session.stop()
                    self.assertEqual(await asyncio.wait_for(task, .5), 0)
                    summary.assert_not_called()
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(fixture.live_starts, 1)
            self.assertEqual(session.status()['state'], 'stopped')
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')
            self.assertFalse(session.status()['rx_ready'])
            # Stop persists the final observer ledger; a pre-poll snapshot
            # during capture is not evidence about native delivery.
            self.assertGreater(session.status()['rx_audio']['python']['incoming_frames'], 32)

    

    

    async def test_formal_live_disconnect_interrupts_hung_capture_await_and_cleans(self):
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(config(duration=None,rx_test_contract=False,rx_session_contract=True))
            fixture=Fixture(source=self.continuous_source,stats_delay=3,start_delay=.03)
            with fixture.install(),patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL',.005):
                task=asyncio.create_task(run_session(session,install_signals=False))
                generation=await self.ready(session,task)
                confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                for _ in range(100):
                    if fixture.stats_calls: break
                    await asyncio.sleep(.005)
                self.assertGreater(fixture.stats_calls,0)
                fixture.provider.error='private provider error that must not be persisted'
                self.assertEqual(await asyncio.wait_for(task,.5),1)
            self.assertEqual(session.status()['error'],'live_error_unknown')
            self.assertEqual(session.status()['cleanup_status'],'cleaned')
            self.assertTrue(fixture.source_task.done())
            self.assertNotIn('private provider error',json.dumps(session.status(),ensure_ascii=False))

    async def test_meeting_lobby_and_unconfirmed_stop_never_start_live(self):
        for contract in ('ordinary', 'bounded', 'formal'):
            for waiting in ('lobby', 'owner'):
                if contract == 'ordinary' and waiting == 'owner':
                    continue
                with self.subTest(contract=contract, waiting=waiting), tempfile.TemporaryDirectory() as root:
                    changes = {}
                    if contract != 'bounded':
                        changes.update(rx_test_contract=False, rx_session_contract=contract == 'formal')
                    if contract == 'formal':
                        changes['duration'] = None
                    session = Session(root).create(config(**changes))
                    fixture = Fixture(admission_delay=3 if waiting == 'lobby' else 0)
                    with fixture.install():
                        task = asyncio.create_task(run_session(session, install_signals=False))
                        try:
                            if waiting == 'owner':
                                await self.ready(session, task)
                            else:
                                while not fixture.joins:
                                    self.assertFalse(task.done())
                                    await asyncio.sleep(.003)
                            self.assertEqual(fixture.live_starts, 0)
                            self.assertEqual(fixture.captures, 0)
                        finally:
                            session.stop()
                            if contract == 'ordinary':
                                task.cancel()
                                with self.assertRaises(asyncio.CancelledError):
                                    await task
                            else:
                                self.assertEqual(await asyncio.wait_for(task, .5), 0)
                    self.assertEqual(fixture.live_starts, 0)

    async def test_formal_start_failure_and_lost_admission_do_not_retry(self):
        for mode in ('start_failed','admission_lost','media_lost'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as root:
                session=Session(root).create(config(duration=None,rx_test_contract=False,rx_session_contract=True))
                fixture=Fixture(start_failure=mode=='start_failed',revoke_on_live_start=mode=='admission_lost',
                    media_ready=mode!='media_lost')
                with fixture.install(): self.assertEqual(await self.run_confirmed(session,fixture),1)
                self.assertEqual((fixture.joins,fixture.live_starts,fixture.captures),(1,1,1 if mode=='media_lost' else 0))
                self.assertFalse(session.status().get('rx_ready',False))
                self.assertFalse(any(event['type'] == 'receiving_ready' for event in session_events(session)))

    async def test_formal_ready_notification_waits_for_actual_rx_handshake(self):
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(config(duration=None,rx_test_contract=False,rx_session_contract=True))
            fixture=Fixture(start_delay=.12)
            with fixture.install():
                task=asyncio.create_task(run_session(session,install_signals=False))
                generation=await self.ready(session,task)
                self.assertEqual(session.status()['rx_prepared']['live_state'],'not_started')
                confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                while not fixture.captures: await asyncio.sleep(.003)
                self.assertEqual(session.status()['state'],'rx_starting')
                self.assertFalse(session.status()['rx_ready'])
                while session.status()['state']!='recording':
                    self.assertFalse(task.done())
                    await asyncio.sleep(.003)
                self.assertTrue(session.status()['rx_ready'])
                self.assertEqual(fixture.live_starts,1)
                session.stop()
                self.assertEqual(await asyncio.wait_for(task,.5),0)

    async def test_formal_supervisor_preserves_expired_phase_and_safe_fields(self):
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create(config(duration=None,rx_test_contract=False,rx_session_contract=True))
            fixture=Fixture(stats_delay=3,start_delay=.03)
            with fixture.install(),patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL',.005):
                task=asyncio.create_task(run_session(session,install_signals=False))
                generation=await self.ready(session,task)
                confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                while not fixture.stats_calls: await asyncio.sleep(.003)
                fixture.provider.failure=dict(error_code='live_session_expired',channel='rpc',
                    type='thread/realtime/closed',reason='expired',stage='connected',raw_payload='private')
                fixture.provider.error='private'
                self.assertEqual(await asyncio.wait_for(task,.5),1)
            self.assertEqual(session.status()['error'],'live_session_expired')
            diagnostic=session.status()['diagnostic']
            self.assertEqual((diagnostic['phase'],diagnostic['channel'],diagnostic['reason']),('rx_capture','rpc','expired'))
            self.assertNotIn('private',json.dumps(session.status(),ensure_ascii=False))

    async def test_slow_final_stats_closes_ingress_before_await(self):
        # This verifies ingress closure during final stats, not whether SQLite
        # and first PCM can finish inside a 60ms capture window. Dedicated
        # deadline/sample-cap tests retain their original limits.
        for setup_stall in (0, .08):
            with self.subTest(setup_stall=setup_stall), tempfile.TemporaryDirectory() as root:
                session = Session(root).create(config(duration=None, save_audio=True,
                    rx_test_contract=False, rx_session_contract=True))
                fixture = Fixture(final_stats_delay=.1, disconnect_after=1)
                original_update = Session.update
                def update(target, **values):
                    if values.get('state') == 'rx_starting':
                        time.sleep(setup_stall)  # Controlled synchronous setup stall.
                    return original_update(target, **values)
                limits = {**BEHAVIOR_LIMITS, 'capture_seconds':None}
                # A confirmed fixture end enters the normal drain/final-stats
                # path after readiness; an owner stop can cancel that path.
                with fixture.install(), patch.object(Session, 'update', update), \
                        patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005):
                    self.assertEqual(await self.run_confirmed(session, fixture,
                        runtime_limits=limits), 0)
                self.assertEqual(fixture.push_samples, [])
                self.assertEqual(fixture.late_pcm_attempts, 1)
                self.assertEqual(session.status()['end_reason'], 'confirmed_end')
                self.assertTrue(any(event['type'] == 'receiving_ready'
                    for event in session_events(session)))
                self.assertEqual(session.status()['cleanup_status'], 'cleaned')
                self.assertEqual(session.status()['rx_audio']['python']['incoming_frames'], 32)
                with wave.open(str(session.path / 'audio.wav')) as saved:
                    self.assertEqual(saved.getnframes(), 32)


    async def test_source_failure_is_not_replaced_by_session_close_timeout(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(duration=None,
                rx_test_contract=False, rx_session_contract=True))
            fixture = Fixture()
            with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005):
                from teams_transcription import worker
                with patch.object(worker.MeetingBrowser, 'capture_lifecycle',
                        AsyncMock(return_value='meeting_removed')):
                    task = asyncio.create_task(run_session(session, install_signals=False))
                    try:
                        generation = await self.ready(session, task)
                        confirm_prepared(session, generation, session.config()['rx_owner_principal'])
                        while not fixture.live_starts:
                            self.assertFalse(task.done())
                            await asyncio.sleep(.003)
                        async def failed_close():
                            fixture.provider.error = 'live_realtime_error'
                            fixture.provider.failure = dict(error_code='live_realtime_error',
                                channel='datachannel', type='error', stage='closing')
                            raise TimeoutError('synthetic sender drain')
                        fixture.provider.close = AsyncMock(side_effect=failed_close)
                        self.assertEqual(await asyncio.wait_for(task, 1), 1)
                    finally:
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(session.status()['error'], 'meeting_removed')
            self.assertEqual(session.status()['diagnostic']['exception_type'], 'RxContractError')
            self.assertEqual(session.status()['diagnostic']['phase'], 'rx_capture')
            self.assertEqual(session.status()['end_reason'], 'meeting_removed')
            self.assertEqual(session.status()['state'], 'failed')

    async def test_provider_error_during_media_close_cannot_complete_capture(self):
        calls = []
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract=BEHAVIOR_LIMITS.copy()))
            fixture = Fixture()

            def live(browser, state_dir, on_text, on_event, *args, **kwargs):
                provider = LiveProvider(browser, state_dir, on_text, on_event, **kwargs)
                fixture.provider = provider
                deliver = provider.peer.callback

                async def finish_input(**kwargs):
                    calls.append('input_finished')
                    return False

                async def stats():
                    return native_transport()

                async def close_peer():
                    calls.append('peer_closed')
                    calls.append('provider_error')
                    await deliver({'type':'error', 'error':{'message':'synthetic failure'}})

                provider.peer = SimpleNamespace(push=AsyncMock(), stats=stats,
                    request_session_close=finish_input, close=close_peer)
                provider.rpc = SimpleNamespace(process=None, close=AsyncMock())
                async def start():
                    provider.thread_id = 'synthetic-thread'
                    provider.stage = 'connected'
                    await kwargs['on_final'](dict(event_id='post-drain-fixture', revision=0,
                        text='Synthetic shutdown source.', timestamp='2026-10-10T01:00:00Z', sequence=1))
                provider.start = start
                return provider

            with fixture.install(), patch('teams_transcription.worker.LiveProvider', live):
                self.assertEqual(await self.run_confirmed(session, fixture), 1)
            self.assertEqual(calls, ['input_finished', 'peer_closed', 'provider_error'])
            fixture.provider.rpc.close.assert_awaited_once()
            self.assertIn('browser', fixture.closed)
            self.assertEqual(session.status()['state'], 'failed')
            self.assertEqual(session.status()['error'], 'live_realtime_error')
            self.assertEqual(session.status()['end_reason'], 'provider_error')
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')





    async def test_confirmed_end_drains_accepted_pcm_and_keeps_reason(self):
        async def burst(callback, owner):
            for offset in (32, 48):
                await callback(dict(sample_rate=24000, frames=16, start_frame=offset,
                    data=base64.b64encode(b'\x02\x00' * 16).decode()))
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(duration=None, provider='record-only', save_audio=True,
                rx_test_contract=False, rx_session_contract=False))
            fixture = Fixture(source=burst, disconnect_after=1)
            with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005):
                self.assertEqual(await self.run_confirmed(session, fixture,
                    runtime_limits={**BEHAVIOR_LIMITS, 'capture_seconds':None, 'total_seconds':None}), 0)
            self.assertEqual(fixture.push_samples, [])
            with wave.open(str(session.path / 'audio.wav')) as saved:
                self.assertEqual(saved.getnframes(), 64)
            self.assertEqual(session.status()['end_reason'], 'confirmed_end')
            self.assertEqual(session.status()['state'], 'recorded')
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')
            self.assertTrue(fixture.source_task.done())



    async def test_capture_deadline_drains_accepted_pcm_without_reopening_ingress(self):
        async def burst(callback, owner):
            for offset in (32, 48):
                await callback(dict(sample_rate=24000, frames=16, start_frame=offset,
                    data=base64.b64encode(b'\x02\x00' * 16).decode()))
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as root:
            limits = {**LIMITS, 'capture_seconds':.02, 'cleanup_seconds':.3}
            session = Session(root).create(config(rx_test_contract=False, provider='record-only', save_audio=True))
            fixture = Fixture(source=burst)
            with fixture.install():
                self.assertEqual(await self.run_confirmed(session, fixture, runtime_limits=limits), 0)
            self.assertEqual(fixture.push_samples, [])
            with wave.open(str(session.path / 'audio.wav')) as saved:
                self.assertEqual(saved.getnframes(), 64)
            self.assertEqual(session.status()['end_reason'], 'duration_limit')
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')
            self.assertTrue(fixture.source_task.done())



    async def test_new_cancellation_keeps_existing_provider_failure(self):
        source_stopped, release_stop = asyncio.Event(), asyncio.Event()
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(duration=None,
                rx_test_contract=False, rx_session_contract=True))
            fixture = Fixture(stats_delay=1)
            with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005):
                from teams_transcription import worker
                original_stop = worker.MeetingBrowser.stop_capture
                async def stop_capture(meeting):
                    await original_stop(meeting)
                    source_stopped.set()
                    await release_stop.wait()
                with patch.object(worker.MeetingBrowser, 'stop_capture', stop_capture):
                    task = asyncio.create_task(run_session(session, install_signals=False))
                    try:
                        generation = await self.ready(session, task)
                        confirm_prepared(session, generation, session.config()['rx_owner_principal'])
                        while not fixture.stats_calls:
                            self.assertFalse(task.done())
                            await asyncio.sleep(.003)
                        fixture.provider.failure = dict(error_code='live_session_expired',
                            channel='rpc', type='thread/realtime/closed', reason='expired', stage='connected')
                        fixture.provider.error = 'live_session_expired'
                        await asyncio.wait_for(source_stopped.wait(), 1)
                        task.cancel()
                        release_stop.set()
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 1)
                    finally:
                        release_stop.set()
                        if not task.done():
                            task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(session.status()['state'], 'failed')
            self.assertEqual(session.status()['error'], 'live_session_expired')
            self.assertEqual(session.status()['diagnostic']['reason'], 'expired')
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')



    async def test_requested_stop_preserves_a_real_drain_failure(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(duration=None,
                rx_test_contract=False, rx_session_contract=True))
            fixture = Fixture()
            with fixture.install():
                task = asyncio.create_task(run_session(session, install_signals=False))
                try:
                    generation = await self.ready(session, task)
                    confirm_prepared(session, generation, session.config()['rx_owner_principal'])
                    while not fixture.captures:
                        self.assertFalse(task.done())
                        await asyncio.sleep(.003)
                    fixture.provider.drain = AsyncMock(side_effect=TimeoutError('synthetic real drain expiry'))
                    session.stop()
                    self.assertEqual(await asyncio.wait_for(task, 1), 1)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            fixture.provider.drain.assert_awaited_once()
            self.assertEqual(session.status()['state'], 'failed')
            self.assertEqual(session.status()['end_reason'], 'operator_stop')
            self.assertEqual(session.status()['error'], 'live_drain_failed')
            self.assertEqual(session.status()['diagnostic']['phase'], 'live_drain')
            self.assertEqual(session.status()['diagnostic']['exception_type'], 'TimeoutError')
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')






    async def test_native_A_does_not_require_or_push_slow_or_unavailable_observer(self):
        async def slow_observer(callback, owner):
            await asyncio.sleep(1)
        for source in (None, slow_observer):
            with self.subTest(slow=source is not None), tempfile.TemporaryDirectory() as root:
                session = Session(root).create(config(rx_test_contract={**BEHAVIOR_LIMITS, 'capture_seconds':.15}))
                fixture = Fixture(observe_pcm=False, source=source)
                with fixture.install():
                    self.assertEqual(await self.run_confirmed(session, fixture), 0)
                status = session.status()
                self.assertEqual(fixture.pushes, 0)
                self.assertIs(fixture.media_sources[0], fixture.meeting)
                self.assertEqual(status['state'], 'completed')
                self.assertEqual(status['cleanup_status'], 'cleaned')
                self.assertEqual(status['rx_audio']['input_mode'], 'native_media')
                self.assertEqual(status['rx_audio']['python']['incoming_frames'], 0)
                self.assertEqual(status['rx_audio']['python']['queued_frames'], 0)
                self.assertIsNone(status['rx_audio']['python']['queue_depth'])
                self.assertIsNone(status['rx_audio']['live_push'])
                self.assertFalse(list(session.path.glob('*.wav')))

    async def test_native_observer_burst_cannot_create_A_queue_or_sample_limit(self):
        async def burst(callback, owner):
            while not owner.provider.final_emitted:
                await asyncio.sleep(0)
            payload = base64.b64encode(b'\x01\x00' * 16).decode()
            for offset in range(32, 8032, 16):
                await callback(dict(sample_rate=24000, frames=16, start_frame=offset, data=payload))
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as root:
            # This checks native queue/sample isolation, not cleanup speed.
            # Real SQLite cleanup needs its own budget on hosted ARM runners.
            limits = {**BEHAVIOR_LIMITS, 'capture_seconds':.15}
            session = Session(root).create(config(rx_test_contract=limits))
            fixture = Fixture(source=burst)
            with fixture.install():
                result = await self.run_confirmed(session, fixture)
                self.assertEqual(result, 0, {key:session.status().get(key)
                    for key in ('state', 'error', 'end_reason', 'cleanup_status')})
            self.assertEqual(session.status()['state'], 'completed')
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')
            snapshot = session.status()['rx_audio']
            self.assertEqual(snapshot['python']['incoming_frames'], 8032)
            self.assertEqual(snapshot['python']['queued_frames'], 0)
            self.assertEqual(snapshot['python']['queue_overflows'], 0)
            self.assertIsNone(snapshot['live_push'])
            self.assertEqual(fixture.pushes, 0)
            limits = [event for event in session_events(session) if event['type'] == 'capture_limit']
            for event in limits:
                self.assertEqual(event['pcm_accepted_samples'], 0)
                self.assertEqual(event['pcm_consumed_samples'], 0)
                self.assertIsNone(event['pcm_sample_limit'])

    async def test_recording_only_overflow_is_a_real_recording_failure_with_drained_tail(self):
        async def burst(callback, owner):
            payload = base64.b64encode(b'\x01\x00' * 16).decode()
            for offset in range(32, 32 + 257 * 16, 16):
                await callback(dict(sample_rate=24000, frames=16, start_frame=offset, data=payload))
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as root:
            limits = {**BEHAVIOR_LIMITS, 'capture_seconds':1.}
            session = Session(root).create(config(provider='record-only', save_audio=True, rx_test_contract=False))
            fixture = Fixture(source=burst)
            with fixture.install():
                self.assertEqual(await self.run_confirmed(session, fixture, runtime_limits=limits), 1)
            status = session.status()
            self.assertEqual(status['error'], 'pcm_queue_overflow')
            self.assertEqual(status['diagnostic']['exception_type'], 'QueueFull')
            self.assertEqual(status['rx_audio']['python']['queue_overflows'], 1)
            self.assertEqual(status['rx_audio']['python']['queued_frames'],
                status['rx_audio']['python']['consumed_frames'])
            with wave.open(str(session.path / 'audio.wav')) as saved:
                self.assertEqual(saved.getnframes(), status['rx_audio']['python']['consumed_frames'])
            self.assertEqual(fixture.live_starts, 0)
            self.assertEqual(status['cleanup_status'], 'cleaned')

    async def test_unavailable_PCM_stats_do_not_stop_native_A(self):
        with tempfile.TemporaryDirectory() as root:
            # This verifies optional observer failure, not a cleanup deadline.
            # Real SQLite writes on hosted ARM runners can exceed the shared
            # 200ms cleanup fixture budget. Keep the capture window unchanged;
            # dedicated deadline tests retain their tight limits.
            limits = {**BEHAVIOR_LIMITS, 'capture_seconds':.15}
            session = Session(root).create(config(rx_test_contract=limits))
            fixture = Fixture(observe_pcm=False)
            with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005):
                from teams_transcription import worker
                with patch.object(worker.MeetingBrowser, 'stats',
                        AsyncMock(side_effect=RuntimeError('private observer failure'))) as observer:
                    result = await self.run_confirmed(session, fixture)
                    self.assertEqual(result, 0, {key:session.status().get(key)
                        for key in ('state', 'error', 'end_reason', 'cleanup_status')})
                    observer.assert_awaited()
            self.assertEqual(session.status()['state'], 'completed')
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')
            self.assertEqual(fixture.pushes, 0)
            self.assertEqual(session.status()['rx_audio']['live_transport']['outbound_packets'], 2)
            self.assertNotIn('private observer failure', json.dumps(session.status()))

    async def test_native_A_survives_optional_transport_timeout_with_last_real_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(duration=None, rx_test_contract=False, rx_session_contract=True))
            fixture = Fixture()
            polling = asyncio.Event()
            calls = 0
            async def poll():
                nonlocal calls
                calls += 1
                if calls == 1:
                    return native_transport()
                polling.set()
                await asyncio.sleep(.7)
                return native_transport()
            with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005):
                task = asyncio.create_task(run_session(session, install_signals=False))
                try:
                    generation = await self.ready(session, task)
                    confirm_prepared(session, generation, session.config()['rx_owner_principal'])
                    while not session.status().get('rx_ready'):
                        self.assertFalse(task.done())
                        await asyncio.sleep(.003)
                    fixture.provider.peer.stats.side_effect = poll
                    await asyncio.wait_for(polling.wait(), .6)
                    previous = session.status()['rx_audio']['live_transport']
                    self.assertIsNotNone(previous)
                    await asyncio.sleep(.55)
                    self.assertFalse(task.done())
                    self.assertEqual(session.status()['state'], 'recording')
                    self.assertEqual(session.status()['rx_audio']['live_transport'], previous)
                    session.stop()
                    self.assertEqual(await asyncio.wait_for(task, .8), 0)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')
            self.assertEqual(fixture.pushes, 0)

    async def test_unavailable_or_transient_native_diagnostics_are_not_source_failure(self):
        for probe in ('timeout', 'not_ready', 'unavailable', 'malformed', 'empty'):
            with self.subTest(probe=probe), tempfile.TemporaryDirectory() as root:
                session = Session(root).create(config(rx_test_contract={**BEHAVIOR_LIMITS, 'capture_seconds':.2}))
                async def source(callback, owner):
                    peer = owner.provider.peer
                    if probe == 'timeout':
                        peer.stats.side_effect = TimeoutError('private diagnostic failure')
                    else:
                        peer.stats.side_effect = None
                        report = native_transport(ready=False)
                        if probe == 'unavailable':
                            report['native_source']['available'] = False
                        elif probe == 'malformed':
                            report['input_mode'] = ['private diagnostic value']
                        elif probe == 'empty':
                            report = {}
                        peer.stats.return_value = report
                    await asyncio.Event().wait()
                fixture = Fixture(source=source)
                with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005):
                    self.assertEqual(await self.run_confirmed(session, fixture), 0)
                self.assertEqual(session.status()['state'], 'completed')
                self.assertEqual(session.status()['cleanup_status'], 'cleaned')
                self.assertEqual(fixture.pushes, 0)
                self.assertFalse(fixture.native_failed)
                self.assertNotIn('private diagnostic', json.dumps(session.status()))

    async def test_definite_native_failure_is_not_hidden_by_stop_during_optional_poll(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(duration=None, rx_test_contract=False, rx_session_contract=True))
            fixture = Fixture()
            polling = asyncio.Event()
            async def poll():
                polling.set()
                await asyncio.sleep(.7)
                return native_transport()
            with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005):
                task = asyncio.create_task(run_session(session, install_signals=False))
                try:
                    generation = await self.ready(session, task)
                    confirm_prepared(session, generation, session.config()['rx_owner_principal'])
                    while not session.status().get('rx_ready'):
                        self.assertFalse(task.done())
                        await asyncio.sleep(.003)
                    fixture.provider.peer.stats.side_effect = poll
                    await asyncio.wait_for(polling.wait(), .5)
                    fixture.native_failed = True
                    session.stop()
                    self.assertEqual(await asyncio.wait_for(task, .8), 1)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(session.status()['state'], 'failed')
            self.assertEqual(session.status()['error'], 'rx_native_source_failed')
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')
            self.assertEqual(fixture.pushes, 0)

    async def test_native_source_runtime_failure_is_not_observer_success(self):
        async def lose_source(callback, owner):
            await asyncio.sleep(.02)
            owner.native_failed = True
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract={**BEHAVIOR_LIMITS, 'capture_seconds':.2}))
            fixture = Fixture(source=lose_source)
            with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005):
                self.assertEqual(await self.run_confirmed(session, fixture), 1)
            self.assertEqual(session.status()['error'], 'rx_native_source_failed')
            self.assertEqual(session.status()['state'], 'failed')
            self.assertEqual(fixture.pushes, 0)
            self.assertEqual(session.status()['diagnostic']['phase'], 'rx_capture')

    async def test_unsafe_observer_packets_do_not_permanently_gate_B_or_native_A(self):
        def packet(offset):
            ended = time.monotonic()
            return dict(sample_rate=24000, frames=8, start_frame=offset,
                data=base64.b64encode(b'\x01\x00' * 8).decode(), source_clock='monotonic',
                source_started_at=ended-8/24000, source_ended_at=ended)
        for fault in ('malformed', 'gap', 'clock', 'push_error', 'browser_drop'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as root:
                session = Session(root).create(config(response_enabled=True, duration=None,
                    rx_test_contract=False, rx_session_contract=True))
                controller = SimpleNamespace(state='sleeping', accepted=0, fail_next=False)
                controller.capture_ready = lambda: controller.state == 'sleeping'
                controller.start = AsyncMock(return_value=True)
                async def stop():
                    controller.state = 'stopped'
                    return 'stopped'
                controller.stop = AsyncMock(side_effect=stop)
                controller.status = lambda: {'state':controller.state,
                    'cleanup_status':'cleaned' if controller.state == 'stopped' else 'idle'}
                def push(pcm, **kwargs):
                    if controller.fail_next:
                        controller.fail_next = False
                        raise RuntimeError('private observer error')
                    if controller.state != 'sleeping':
                        return False
                    controller.accepted += 1
                    return True
                controller.push_rx = push
                async def source(callback, owner):
                    while not owner.provider.final_emitted:
                        await asyncio.sleep(0)
                    if fault == 'malformed':
                        await callback({'sample_rate':24000, 'frames':8, 'start_frame':32, 'data':'private-invalid'})
                    elif fault == 'gap':
                        await callback(packet(999))
                    elif fault == 'clock':
                        value = packet(32)
                        value.pop('source_clock')
                        await callback(value)
                    elif fault == 'push_error':
                        controller.fail_next = True
                        await callback(packet(32))
                    offset = 40 if fault in {'clock', 'push_error'} else 32
                    await callback(packet(offset))
                    await asyncio.sleep(.11)
                    owner.b_stops_before_recovery = controller.stop.await_count
                    await callback(packet(offset+8))
                    owner.b_recovered_packets = controller.accepted
                    session.stop()
                    await asyncio.Event().wait()
                report = [{'dropped':3, 'playout_errors':1}] if fault == 'browser_drop' else None
                fixture = Fixture(source=source, stats_report=report)
                with fixture.install(), patch('teams_transcription.worker.CAPTURE_STATUS_INTERVAL', .005), patch(
                        'teams_transcription.response_runtime.ProductionResponseController', return_value=controller):
                    self.assertEqual(await asyncio.wait_for(self.run_confirmed(session, fixture), 2), 0)
                self.assertEqual(session.status()['state'], 'stopped')
                self.assertEqual(session.status()['cleanup_status'], 'cleaned')
                self.assertEqual(fixture.b_stops_before_recovery, 0)
                self.assertEqual(fixture.b_recovered_packets, 3)
                self.assertEqual(controller.state, 'stopped')
                controller.stop.assert_awaited_once()
                self.assertEqual(fixture.pushes, 0)
                self.assertEqual(session.status()['rx_audio']['python']['invalid_packets'],
                    1 if fault in {'malformed', 'gap'} else 0)
                events = session_events(session)
                self.assertFalse(any(event['type'] in {'response_rx_unavailable', 'capture_error'} for event in events))
                self.assertNotIn('private-invalid', json.dumps(events))
                self.assertNotIn('private observer error', json.dumps(events))

    async def test_native_observer_continuity_is_independent_per_host_document(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(response_enabled=True, duration=None,
                rx_test_contract=False, rx_session_contract=True))
            received = []
            controller = SimpleNamespace(state='sleeping')
            controller.capture_ready = lambda: controller.state == 'sleeping'
            controller.start = AsyncMock(return_value=True)
            async def stop():
                controller.state = 'stopped'
                return 'stopped'
            controller.stop = AsyncMock(side_effect=stop)
            controller.status = lambda: {'state':controller.state,
                'cleanup_status':'cleaned' if controller.state == 'stopped' else 'idle'}
            def push(pcm, **kwargs):
                received.append((pcm, kwargs))
                return True
            controller.push_rx = push
            async def source(callback, owner):
                while not owner.provider.final_emitted:
                    await asyncio.sleep(0)
                async def packet(document, offset):
                    ended = time.monotonic()
                    await callback(dict(source_document=document, sample_rate=24000, frames=8,
                        start_frame=offset, data=base64.b64encode(b'\x01\x00'*8).decode(),
                        source_clock='monotonic', source_started_at=ended-8/24000,
                        source_ended_at=ended))
                main, child = owner.meeting, owner
                await packet(main, 32)
                await packet(child, 0)
                await packet(main, 40)
                await packet(child, 8)
                await packet(child, 999)  # An actual child gap is still rejected.
                await packet(main, 48)  # A child fault cannot reset main continuity.
                await packet(child, 16)
                await packet('page-forged-identity', 0)
                await packet(child, 24)
                session.stop()
                await asyncio.Event().wait()
            fixture = Fixture(source=source)
            with fixture.install(), patch('teams_transcription.response_runtime.ProductionResponseController',
                    return_value=controller):
                self.assertEqual(await asyncio.wait_for(self.run_confirmed(session, fixture), 2), 0)
            self.assertEqual(len(received), 8)
            self.assertEqual(session.status()['rx_audio']['python']['invalid_packets'], 2)
            self.assertEqual(fixture.pushes, 0)
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')

    async def test_native_saved_WAV_uses_finite_recording_adapter_not_live_push(self):
        async def burst(callback, owner):
            while not owner.provider.final_emitted:
                await asyncio.sleep(0)
            await callback(dict(sample_rate=24000, frames=2048, start_frame=32,
                data=base64.b64encode(b'\x01\x00' * 2048).decode()))
            await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(save_audio=True, rx_test_contract=False))
            fixture = Fixture(source=burst)
            with fixture.install():
                self.assertEqual(await self.run_confirmed(session, fixture, runtime_limits=BEHAVIOR_LIMITS.copy()), 0)
            self.assertEqual(fixture.pushes, 0)
            with wave.open(str(session.path / 'audio.wav')) as saved:
                self.assertEqual(saved.getnframes(), 1440)
            snapshot = session.status()['rx_audio']
            self.assertEqual(snapshot['python']['queued_frames'], 1440)
            self.assertEqual(snapshot['python']['consumed_frames'], 1440)
            self.assertIsNone(snapshot['live_push'])
            self.assertEqual(session.status()['cleanup_status'], 'cleaned')

    async def test_saved_WAV_failure_still_fails_native_capture(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(save_audio=True, rx_test_contract=False))
            fixture = Fixture()
            with fixture.install(), patch.object(wave.Wave_write, 'writeframesraw',
                    side_effect=OSError('private recording failure')):
                self.assertEqual(await self.run_confirmed(session, fixture, runtime_limits=BEHAVIOR_LIMITS.copy()), 1)
            self.assertEqual(session.status()['state'], 'failed')
            self.assertEqual(session.status()['diagnostic']['exception_type'], 'OSError')
            self.assertEqual(session.status()['diagnostic']['phase'], 'rx_capture')
            self.assertEqual(fixture.pushes, 0)
            self.assertNotIn('private recording failure', json.dumps(session.status()))

    async def test_file_input_keeps_real_PCM_push_adapter_and_drains(self):
        def prepare(config, target):
            with wave.open(str(target), 'wb') as saved:
                saved.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
                saved.writeframes(b'\x01\x00' * 64)
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(source='file', wav='unused', duration=.02, rx_test_contract=False))
            fixture = Fixture()
            with fixture.install(), patch('teams_transcription.worker.prepare_wav', prepare):
                self.assertEqual(await run_session(session, install_signals=False), 0)
            self.assertEqual(fixture.media_sources, [None])
            self.assertEqual(fixture.push_samples, [64])
            self.assertEqual(session.status()['state'], 'completed')
            with wave.open(str(session.path / 'audio.wav')) as saved:
                self.assertEqual(saved.getnframes(), 64)

    async def test_stop_during_ordinary_live_start_never_opens_capture(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract=False))
            fixture = Fixture(live_delay=.04)
            with fixture.install():
                task = asyncio.create_task(run_session(session, install_signals=False))
                while not fixture.live_starts:
                    self.assertFalse(task.done())
                    await asyncio.sleep(.003)
                session.stop()
                self.assertEqual(await asyncio.wait_for(task, .5), 0)
            self.assertEqual(fixture.captures, 0)
            self.assertEqual(fixture.pushes, 0)
            self.assertEqual(session.status()['state'], 'stopped')

    async def test_unconfirmed_native_source_stop_is_cleanup_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            fixture = Fixture()
            with fixture.install():
                from teams_transcription import worker
                with patch.object(worker.MeetingBrowser, 'stop_capture',
                        AsyncMock(side_effect=RuntimeError('private gate failure'))):
                    self.assertEqual(await self.run_confirmed(session, fixture), 1)
            self.assertEqual(session.status()['state'], 'failed')
            self.assertEqual(session.status()['cleanup_status'], 'unknown')
            self.assertEqual(session.status()['error'], 'rx_cleanup_unknown')
            self.assertNotIn('private gate failure', json.dumps(session.status()))

    async def test_public_bounded_contract_still_rejects_recording_and_response_modes(self):
        for changes in ({'provider':'record-only', 'save_audio':True},
                {'save_audio':True}, {'response_enabled':True}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as root:
                session = Session(root).create(config(**changes))
                fixture = Fixture()
                with fixture.install(), self.assertRaisesRegex(RxContractError, 'rx_test_requires_transcription_only'):
                    await run_session(session, install_signals=False)
                self.assertEqual(fixture.live_starts, 0)
                self.assertEqual(fixture.captures, 0)
                self.assertFalse((session.path / 'audio.wav').exists())

    async def ready(self, session, task):
        try:
            for _ in range(300):
                if session.status()['state'] == 'prepared':
                    return session.status()['rx_prepared']['generation']
                if task.done():
                    await task  # Retrieve the actual startup exception.
                    self.fail('runner terminated before prepared: ' + str(session.status()))
                await asyncio.sleep(.003)
            self.fail('prepared not published')
        except BaseException:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def test_capture_requires_current_owner_confirmation_and_single_live(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract=BEHAVIOR_LIMITS.copy()))
            fixture = Fixture(admission_delay=.03)
            with fixture.install() as summary:
                task = asyncio.create_task(run_session(session, install_signals=False))
                generation = await self.ready(session, task)
                await asyncio.sleep(.04)
                self.assertEqual((fixture.captures, fixture.pushes), (0, 0))
                for owner, nonce, code in [('other',generation,'rx_owner_mismatch'),
                        (session.config()['rx_owner_principal'],'0'*32,'rx_prepared_generation_mismatch')]:
                    with self.assertRaisesRegex(RxContractError,code): confirm_prepared(session,nonce,owner)
                confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                with self.assertRaisesRegex(RxContractError,'already_submitted'):
                    confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                self.assertEqual(await task, 0)
                summary.assert_not_called()
            self.assertEqual((fixture.live_starts,fixture.joins,fixture.captures,fixture.pushes),(1,1,1,0))
            self.assertIs(fixture.media_sources[0], fixture.meeting)
            self.assertEqual(session.status()['cleanup_status'],'cleaned')
            result = MeetingStore(root).read(session.ident)
            self.assertEqual(result['status'],'completed')
            self.assertIn('受控來源逐字稿',''.join(row['text'] for row in result['sources']))
            self.assertFalse(list(session.path.glob('*.wav')))
            with fixture.install(), self.assertRaisesRegex(RxContractError,'already_started'):
                await run_session(session,install_signals=False)
            self.assertEqual(fixture.joins,1)

    async def test_expired_owner_ready_never_starts_capture(self):
        with tempfile.TemporaryDirectory() as root:
            limits = {**LIMITS,'ready_ttl_seconds':.04}
            session = Session(root).create(config(rx_test_contract=limits))
            fixture = Fixture()
            with fixture.install(): self.assertEqual(await run_session(session,install_signals=False),1)
            self.assertEqual(fixture.captures,0)
            self.assertEqual(session.status()['error'],'rx_owner_ready_expired')
            self.assertTrue(MeetingStore(root).read(session.ident)['terminal'])

    async def test_delayed_admission_uses_shared_deadline_and_no_second_join(self):
        with tempfile.TemporaryDirectory() as root:
            limits = dict(capture_seconds=.05,total_seconds=.25,cleanup_seconds=.08,ready_ttl_seconds=.05)
            session = Session(root).create(config(rx_test_contract=limits))
            fixture = Fixture(admission_delay=3)
            started = time.monotonic()
            with fixture.install(): self.assertEqual(await run_session(session,install_signals=False),1)
            self.assertLess(time.monotonic()-started,.4)
            self.assertEqual((fixture.joins,fixture.captures),(1,0))
            self.assertEqual(session.status()['error'],'rx_total_deadline_exceeded')

    async def test_owner_stop_cancels_pending_live_start_without_capture(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract=BEHAVIOR_LIMITS.copy()))
            fixture = Fixture(live_delay=3)
            with fixture.install():
                task = asyncio.create_task(run_session(session,install_signals=False))
                generation = await self.ready(session,task)
                confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                while not fixture.live_starts:
                    self.assertFalse(task.done())
                    await asyncio.sleep(.003)
                session.stop()
                self.assertEqual(await asyncio.wait_for(task,.5),0)
            self.assertEqual((fixture.captures,fixture.pushes),(0,0))
            self.assertEqual(session.status()['state'],'stopped')
            self.assertEqual(session.status()['cleanup_status'],'cleaned')

    async def test_hung_cleanup_kills_and_reaps_only_owned_subprocess(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            fixture = Fixture(hang_close=True)
            with fixture.install():
                task = asyncio.create_task(run_session(session,install_signals=False))
                generation = await self.ready(session,task)
                confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                started = time.monotonic()
                self.assertEqual(await task,1)
            self.assertLess(time.monotonic()-started,.4)
            self.assertIsNotNone(fixture.process.returncode)
            self.assertEqual(session.status()['error'],'rx_cleanup_unknown')

    async def test_cleanup_does_not_kill_unrelated_process(self):
        unrelated = await asyncio.create_subprocess_exec('sleep','60')
        try:
            with tempfile.TemporaryDirectory() as root:
                session = Session(root).create(config())
                fixture = Fixture(hang_close=True)
                with fixture.install():
                    task = asyncio.create_task(run_session(session,install_signals=False))
                    generation = await self.ready(session,task)
                    confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                    self.assertEqual(await task,1)
                self.assertIsNone(unrelated.returncode)
                self.assertIsNotNone(fixture.process.returncode)
        finally:
            unrelated.kill()
            await unrelated.wait()

    async def test_fifo_confirmation_is_rejected_without_blocking(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            fixture = Fixture()
            with fixture.install():
                task = asyncio.create_task(run_session(session,install_signals=False))
                await self.ready(session,task)
                os.mkfifo(session.path/'owner-ready.json',0o600)
                self.assertEqual(await asyncio.wait_for(task,.4),1)
            self.assertEqual(fixture.captures,0)
            self.assertEqual(session.status()['error'],'rx_owner_ready_invalid')

    async def test_cancel_twice_is_bounded_and_reaps_owned_process(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            fixture = Fixture(hang_close=True)
            with fixture.install():
                task = asyncio.create_task(run_session(session,install_signals=False))
                generation = await self.ready(session,task)
                confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                while fixture.process is None:
                    self.assertFalse(task.done())
                    await asyncio.sleep(.003)
                task.cancel()
                await asyncio.sleep(.04)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertIsNotNone(fixture.process.returncode)
            self.assertTrue(MeetingStore(root).read(session.ident)['terminal'])

    async def test_unprepared_or_replaced_generation_cannot_capture(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            fixture = Fixture(prepared=False)
            with fixture.install(): self.assertEqual(await run_session(session,install_signals=False),1)
            self.assertEqual(fixture.captures,0)
            self.assertEqual(session.status()['error'],'rx_prepared_not_ready')

    async def test_foreign_meeting_marker_fails_closed(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            fixture = Fixture()
            with fixture.install():
                task = asyncio.create_task(run_session(session,install_signals=False))
                generation = await self.ready(session,task)
                (session.path/'owner-ready.json').write_text(json.dumps(dict(schema_version=1,
                    meeting_id='0'*32,generation=generation,principal=session.config()['rx_owner_principal'])))
                self.assertEqual(await task,1)
            self.assertEqual(fixture.captures,0)
            self.assertEqual(session.status()['error'],'rx_owner_ready_generation_mismatch')

    async def test_ready_rechecks_prepared_before_capture(self):
        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config())
            fixture = Fixture()
            with fixture.install():
                task = asyncio.create_task(run_session(session,install_signals=False))
                generation = await self.ready(session,task)
                fixture.prepared = False
                confirm_prepared(session,generation,session.config()['rx_owner_principal'])
                self.assertEqual(await task,1)
            self.assertEqual(fixture.captures,0)
            self.assertEqual(session.status()['error'],'rx_prepared_no_longer_ready')

    async def test_browser_prepared_requires_admission_deferred_capture_and_muted_senders(self):
        meeting = MeetingBrowser(object(),AsyncMock(),AsyncMock(),defer_capture=True)
        meeting.admitted = AsyncMock(return_value=True)
        frame = SimpleNamespace(evaluate=AsyncMock(return_value=dict(hook_present=True,
            capture_deferred=True,tx_muted=True)))
        meeting.page = SimpleNamespace(frames=[frame])
        self.assertTrue((await meeting.prepared_readiness())['prepared'])
        for field in ('hook_present','capture_deferred','tx_muted'):
            frame.evaluate.return_value = dict(hook_present=True,capture_deferred=True,tx_muted=True)
            frame.evaluate.return_value[field] = False
            self.assertFalse((await meeting.prepared_readiness())['prepared'])
        frame.evaluate.return_value = dict(hook_present=True,capture_deferred=True,tx_muted=True)
        meeting.admitted.return_value = False
        self.assertFalse((await meeting.prepared_readiness())['prepared'])
        self.assertFalse(meeting.capture_started)

    

    

    
