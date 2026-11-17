"""Deterministic native observation -> actual worker -> B admission, no browser/model."""
import asyncio
import base64
import tempfile
import time
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import AsyncMock

from teams_transcription.b_dual_policy import OwnedSenderDualPolicy
from teams_transcription.browser import MeetingBrowser
from teams_transcription.native_media import NativeMeetingMedia
from teams_transcription.response_runtime import ProductionResponseController
from teams_transcription.storage import Session
from teams_transcription.worker import run_session
from test_rx_contract import Fixture, config


class _Frame:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = 0

    async def evaluate(self, expression, argument=None):
        self.calls += 1
        if self.failure is not None:
            error, self.failure = self.failure, None
            if isinstance(error, BaseException):
                raise error
            return {'accepted': error, 'reason': 'foreign_sender'}
        return {'accepted': True, 'reason': 'none'} if isinstance(argument,dict) and argument.get('receipt') else True


class CaptureFanoutTests(unittest.IsolatedAsyncioTestCase):
    async def sequence(self, failure=None, *, pending_scope=False, save_audio=False):
        started = asyncio.Event()
        complete = asyncio.Event()
        received = []
        policy_frame = _Frame(failure)
        result = {}

        class Controller(ProductionResponseController):
            async def start(self):
                await started.wait()
                self.native = SimpleNamespace(
                    error=None,
                    cleanup_timeout=5, fail_input_policy=AsyncMock(),
                    status=lambda: {'state': 'sleeping', 'output_muted': True},
                    push_rx=lambda ident, data, **clock: received.append((ident, data)) or True)
                self.state = 'sleeping'

            async def stop(self):
                if self._policy_failure_task is not None:
                    await self._policy_failure_task
                self.state = 'stopped'
                return 'stopped'

        async def source(on_audio, fixture):
            try:
                controller = fixture.session.response_controller
                meeting = fixture.meeting
                policy = meeting.dual_policy = OwnedSenderDualPolicy(policy_frame, fixture.session.ident,
                    on_failure=controller._policy_failed)
                controller.policy = policy
                meeting._map_audio_packet = MeetingBrowser._map_audio_packet
                media = NativeMeetingMedia(meeting)
                target = _Frame()
                target.context, target.main_frame = object(), object()
                media.target = target
                media._observation_running = True
                media.documents = {object(): 'synthetic-document'}
                media._connected_keys.add('synthetic-link')
                media._observation_clock['offset'] = 0
                binding = dict(page=target, context=target.context, frame=target.main_frame)
                index = 0

                async def emit():
                    nonlocal index
                    end = time.monotonic()
                    await media._observe(binding, dict(
                        data=base64.b64encode(b'\x01\x00' * 32).decode(),
                        sample_rate=24000, frames=32, start_frame=index * 32,
                        capture_clock_id=media._target_owner,
                        capture_start_ms=(end - 32 / 24000) * 1000,
                        capture_end_ms=end * 1000))
                    index += 1

                for _ in range(10):
                    await emit()
                result['startup'] = dict(fixture.session.response_rx_counts)
                started.set()
                while controller.state != 'sleeping':
                    await asyncio.sleep(0)
                if pending_scope:
                    await emit()  # Establish continuity before the skipped source interval.
                    media._scope_checks['synthetic-check'] = object()
                    for _ in range(10):
                        await emit()
                    media._scope_checks.clear()
                for _ in range(40):
                    await emit()
                result.update(final=dict(fixture.session.response_rx_counts),
                    policy_faulted=policy.faulted, controller=controller.status(),
                    receiver_calls=len(received), policy_calls=policy_frame.calls,
                    observation=media.observation_snapshot())
            except BaseException as error:
                result['error'] = error
            finally:
                complete.set()

        with tempfile.TemporaryDirectory() as root:
            session = Session(root).create(config(rx_test_contract=None, duration=None,
                response_enabled=True, allowed_egress_hosts=[], save_audio=save_audio))
            fixture = Fixture(source=source, observe_pcm=False)
            fixture.session = session
            with fixture.install(), patch(
                    'teams_transcription.response_runtime.ProductionResponseController', Controller):
                task = asyncio.create_task(run_session(session, install_signals=False))
                try:
                    await asyncio.wait_for(complete.wait(), 3)
                    if 'error' in result:
                        raise result['error']
                finally:
                    session.stop()
                    await asyncio.wait_for(task, 3)
                if save_audio:
                    with wave.open(str(session.path / 'audio.wav')) as audio:
                        result['saved_frames'] = audio.getnframes()
        return result

    async def test_bound_policy_receives_no_B_only_startup_callbacks_or_replay(self):
        result = await self.sequence()
        self.assertEqual(result['startup']['offered_packets'], 0)
        self.assertEqual(result['startup']['rejected_packets'], 0)
        self.assertEqual(result['policy_calls'], 40)
        self.assertEqual(result['receiver_calls'], 40)
        self.assertEqual(result['observation']['consumer_unavailable_packets'], 10)

    async def test_startup_gate_keeps_later_input_continuous_without_replaying_old_audio(self):
        result = await self.sequence()
        self.assertEqual(result['startup']['offered_packets'], 0)
        self.assertEqual(result['startup']['rejected_packets'], 0)
        self.assertEqual(result['final']['offered_packets'], 40)
        self.assertEqual(result['final']['accepted_packets'], 40)
        self.assertEqual(result['receiver_calls'], 40)

        self.assertFalse(result['policy_faulted'])

    async def test_pending_scope_drops_only_its_packets_and_requires_a_fresh_check(self):
        result=await self.sequence(pending_scope=True)
        # Keep the worker's existing discontinuity guard: the first packet
        # after ten skipped source packets is rejected, then input resumes.
        self.assertEqual(result['final']['offered_packets'],40)
        self.assertEqual(result['final']['accepted_packets'],40)
        self.assertEqual(result['observation']['received_packets'],61)
        self.assertEqual(result['observation']['scope_pending_packets'],10)
        self.assertEqual(result['observation']['delivered_packets'],41)
        self.assertFalse(result['policy_faulted'])

    async def test_policy_rejection_produces_the_same_counts_as_timeout(self):
        rejected = await self.sequence(False)
        timeout = await self.sequence(TimeoutError())
        self.assertEqual(rejected['final'], timeout['final'])
        self.assertTrue(rejected['policy_faulted'])
        self.assertEqual(rejected['receiver_calls'], 0)
        self.assertEqual(rejected['controller']['state'], 'unavailable')
        self.assertEqual(rejected['controller']['capture_policy_failure']['reason'], 'foreign_sender')

    async def test_one_policy_exception_blocks_fanout_and_is_no_longer_silent(self):
        # Injected causal counterexample, not proof this exception occurred live.
        result = await self.sequence(TimeoutError())
        self.assertEqual(result['final']['offered_packets'], 0)
        self.assertEqual(result['final']['rejected_packets'], 0)
        self.assertEqual(result['final']['accepted_packets'], 0)
        self.assertEqual(result['receiver_calls'], 0)
        self.assertTrue(result['policy_faulted'])
        self.assertEqual(result['policy_calls'], 1)
        self.assertEqual(result['controller']['state'], 'unavailable')
        self.assertEqual(result['controller']['capture_policy_failure']['reason'], 'evaluation_timeout')
        self.assertEqual(result['observation']['received_packets'], 50)
        self.assertEqual(result['observation']['delivered_packets'], 0)
        self.assertEqual(result['observation']['policy_rejected_packets'], 1)
        self.assertEqual(result['observation']['consumer_unavailable_packets'], 49)

    async def test_WAV_required_PCM_keeps_A_samples_while_B_initializes(self):
        result = await self.sequence(save_audio=True)
        self.assertEqual(result['startup']['offered_packets'], 10)
        self.assertEqual(result['startup']['rejected_packets'], 10)
        self.assertEqual(result['saved_frames'], 50 * 32)
        self.assertEqual(result['observation']['delivered_packets'], 50)
        self.assertEqual(result['observation']['consumer_unavailable_packets'], 0)
        self.assertEqual(result['policy_calls'], 50)
        self.assertEqual(result['receiver_calls'], 40)


class ObservationAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_admission_is_rechecked_after_owner_and_policy_awaits(self):
        for stage in ('owner', 'policy'):
            with self.subTest(stage=stage):
                entered, release = asyncio.Event(), asyncio.Event()
                ready = [True]
                async def owner_evaluate(*args):
                    if stage == 'owner':
                        entered.set()
                        await release.wait()
                    return True
                async def policy_evaluate(*args):
                    if stage == 'policy':
                        entered.set()
                        await release.wait()
                    return {'accepted': True, 'reason': 'none'}
                policy_frame = SimpleNamespace(evaluate=AsyncMock(side_effect=policy_evaluate))
                meeting = SimpleNamespace(response_capture_ready=lambda: ready[0],
                    on_audio=AsyncMock(), dual_policy=OwnedSenderDualPolicy(policy_frame, 'synthetic'),
                    _map_audio_packet=lambda *args: {'source_clock': 'monotonic'})
                media = NativeMeetingMedia(meeting)
                target = SimpleNamespace(context=object(), main_frame=object(),
                    evaluate=AsyncMock(side_effect=owner_evaluate))
                media.target = target
                media._observation_running = True
                media.documents = {object(): 'synthetic'}
                media._connected_keys.add('synthetic')
                source = dict(page=target, context=target.context, frame=target.main_frame)
                first = asyncio.create_task(media._observe(source, {}))
                await asyncio.wait_for(entered.wait(), 1)
                waiting = asyncio.create_task(media._observe(source, {}))
                await asyncio.sleep(0)
                ready[0] = False
                release.set()
                await asyncio.gather(first, waiting)
                meeting.on_audio.assert_not_awaited()
                self.assertEqual(target.evaluate.await_count, 1)
                self.assertEqual(policy_frame.evaluate.await_count, int(stage == 'policy'))
                self.assertEqual(media.observation_snapshot()['consumer_unavailable_packets'], 2)


if __name__ == '__main__':
    unittest.main()
