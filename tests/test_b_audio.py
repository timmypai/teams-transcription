"""Synthetic bytes only; no system audio devices or stored raw audio."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from teams_transcription.b_audio import GatedPCMOutput, PulseTXSink


class OutputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sink = AsyncMock()
        self.output = GatedPCMOutput("one", self.sink)

    async def asyncTearDown(self):
        await self.output.close()

    async def test_default_mute_explicit_enable_and_stop_discards(self):
        await self.output.begin_speech("one")
        self.assertFalse(await self.output.write("one", b"\1\0" * 20))
        self.sink.start.assert_not_awaited()
        self.sink.write.assert_not_awaited()
        await self.output.end_speech()
        self.output.start_output()
        await self.output.begin_speech("one")
        self.assertTrue(await self.output.write("one", b"\2\0" * 20))
        self.sink.set_muted.assert_awaited_once_with(False)
        await self.output.stop_output()
        self.assertFalse(await self.output.write("one", b"\3\0"))
        self.output.start_output()
        await self.output.begin_speech("one")
        self.sink.start.assert_awaited()
        self.assertEqual(self.output.frames, 0)

    async def test_wrong_scope_format_and_lease_fail_closed(self):
        for scope, pcm, options in [("two", b"\0\0", {}), ("one", b"x", {}),
                ("one", b"\0\0", {"sample_rate":24000}), ("one", b"\0\0", {"channels":2}),
                ("one", b"\0"*48002, {})]:
            with self.assertRaises(ValueError):
                await self.output.write(scope, pcm, **options)
        with self.assertRaises(ValueError):
            await self.output.begin_speech("two")
        self.output.start_output()
        await self.output.begin_speech("one", max_seconds=.02)
        await asyncio.sleep(.04)
        self.assertFalse(self.output.active)

    async def test_default_speech_has_no_deadline_and_crosses_old_duration_limits(self):
        now = [100.]
        self.output.clock = lambda:now[0]
        self.output.start_output()
        await self.output.begin_speech("one")
        self.assertIsNone(self.output.deadline)
        self.assertIsNone(self.output._expiry)
        now[0] += 61
        self.assertTrue(await self.output.write("one", b"\x20\x00" * 20))
        self.sink.drain.return_value = {"status":"drained","truncated":False,"reason":None}
        self.assertEqual((await self.output.drain())["status"], "drained")
        self.sink.drain.assert_awaited_once_with(timeout=3)
        self.assertEqual(json.loads(json.dumps({"deadline":self.output.deadline},
            allow_nan=False)), {"deadline":None})

    async def test_explicit_duration_above_sixty_seconds_is_honored_without_extension(self):
        now = [100.]
        self.output.clock = lambda:now[0]
        self.output.start_output()
        await self.output.begin_speech("one", max_seconds=120)
        self.assertEqual(self.output.deadline, 220)
        self.assertIsNotNone(self.output._expiry)
        now[0] = 219
        self.assertTrue(await self.output.write("one", b"\x20\x00"))
        now[0] = 220
        self.assertFalse(await self.output.write("one", b"\x20\x00"))
        self.assertEqual((await self.output.drain())["reason"], "lease_expired")
        self.sink.drain.assert_not_awaited()

    async def test_invalid_explicit_speech_duration_refuses_before_start(self):
        for seconds in (0, -1, float("inf"), float("nan"), True, "120"):
            with self.assertRaises(ValueError):
                await self.output.begin_speech("one", max_seconds=seconds)
        self.assertFalse(self.output.active)
        self.sink.start.assert_not_awaited()

    async def test_write_failure_mutes_and_disables(self):
        self.output.start_output()
        await self.output.begin_speech("one")
        self.sink.write.side_effect = RuntimeError("fixture failure")
        with self.assertRaises(RuntimeError):
            await self.output.write("one", b"\1\0")
        self.assertFalse(self.output.enabled)
        self.assertFalse(self.output.active)

    async def test_gated_drain_requires_adapter_completion_and_closes_speech(self):
        self.output.start_output()
        await self.output.begin_speech("one")
        await self.output.write("one", b"\1\0" * 20)
        release = asyncio.Event()
        async def drain(*, timeout):
            await release.wait()
            return {"status": "drained", "truncated": False, "reason": None}
        self.sink.drain.side_effect = drain
        pending = asyncio.create_task(self.output.drain(timeout=.5))
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        release.set()
        self.assertEqual((await pending)["status"], "drained")
        self.assertFalse(await self.output.write("one", b"\1\0"))
        self.assertEqual((await self.output.end_speech())["status"], "drained")

    async def test_gated_drain_timeout_stops_and_reports_truncation(self):
        self.output.start_output()
        await self.output.begin_speech("one")
        await self.output.write("one", b"\1\0")
        async def stalled(*, timeout):
            await asyncio.Event().wait()
        self.sink.drain.side_effect = stalled
        result = await self.output.drain(timeout=.01)
        self.assertEqual(result, {"status": "truncated", "truncated": True, "reason": "drain_timeout"})
        self.assertFalse(self.output.active)

    async def test_default_muted_drain_and_explicit_stop_report_delivery(self):
        await self.output.begin_speech("one")
        await self.output.write("one", b"\1\0")
        self.assertEqual((await self.output.drain())["status"], "muted")
        self.sink.drain.assert_not_awaited()
        self.output.start_output()
        await self.output.begin_speech("one")
        await self.output.write("one", b"\1\0")
        self.assertTrue((await self.output.end_speech())["truncated"])

    async def test_gated_drain_cancel_stops_and_preserves_failure_result(self):
        self.output.start_output()
        await self.output.begin_speech("one")
        await self.output.write("one", b"\1\0")
        entered = asyncio.Event()
        async def stalled(*, timeout):
            entered.set()
            await asyncio.Event().wait()
        self.sink.drain.side_effect = stalled
        pending = asyncio.create_task(self.output.drain(timeout=1))
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(self.output.last_delivery["reason"], "drain_cancelled")
        self.assertFalse(self.output.active)
        self.assertFalse(self.output.enabled)

    async def test_gated_drain_cannot_extend_expired_speech_lease(self):
        self.output.start_output()
        await self.output.begin_speech("one")
        await self.output.write("one", b"\1\0")
        self.output.deadline = self.output.clock() - .01
        self.assertEqual((await self.output.drain())["reason"], "lease_expired")
        self.sink.drain.assert_not_awaited()

    async def test_invalid_drain_timeout_refuses(self):
        for timeout in (0, -1, 61, float("inf"), float("nan"), True):
            with self.assertRaises(ValueError):
                await self.output.drain(timeout)
        self.sink.drain.assert_not_awaited()

    async def test_enable_mid_muted_speech_is_refused(self):
        await self.output.begin_speech("one")
        with self.assertRaises(RuntimeError):
            self.output.start_output()

    async def test_close_finally_disposes_idle_adapter_after_successful_drain(self):
        self.output.start_output()
        await self.output.begin_speech("one")
        self.sink.drain.return_value = {"status":"drained","truncated":False,"reason":None}
        await self.output.drain()
        self.assertFalse(self.output.active)
        self.sink.stop.assert_not_awaited()
        await self.output.close()
        self.sink.stop.assert_awaited_once()
        self.assertTrue(self.output.closed)


class PulsePipeTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_subprocess_pipe_with_fake_audio_executables(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            control = root / "fake-pactl"
            player = root / "fake-pacat"
            log = root / "control.jsonl"
            count = root / "count.txt"
            control.write_text("#!/usr/bin/env python3\nimport json,sys\n"
                + f"with open({str(log)!r},'a') as f: f.write(json.dumps(sys.argv[1:])+'\\n')\n"
                + "if 'list' in sys.argv: print(json.dumps([{'name': 'agent_tx' if sys.argv[-1]=='sinks' else 'agent_mic', 'mute':True}]))\n")
            player.write_text("#!/usr/bin/env python3\nimport sys\nn=0\n"
                + "while True:\n data=sys.stdin.buffer.read1(4096)\n if not data: break\n n+=len(data)\n"
                + f" with open({str(count)!r},'w') as f: f.write(str(n))\n")
            control.chmod(0o700)
            player.chmod(0o700)
            sink = PulseTXSink(pacat=str(player), pactl=str(control))
            await sink.start()
            try:
                await sink.set_muted(False)
                await sink.write(b"\1\0" * 2048)
                for _ in range(100):
                    if count.exists() and count.read_text() == "4096":
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(count.read_text(), "4096")
            finally:
                await sink.pause()
            messages = [json.loads(line) for line in log.read_text().splitlines()]
            self.assertEqual(messages[2:], [
                ["set-source-mute", "agent_mic", "1"], ["set-sink-mute", "agent_tx", "1"],
                ["set-sink-mute", "agent_tx", "0"], ["set-source-mute", "agent_mic", "0"],
                ["set-source-mute", "agent_mic", "1"], ["set-sink-mute", "agent_tx", "1"]])
            self.assertIsNone(sink.process)
            # A second synthetic player exits cleanly on EOF, unlike the forced
            # termination above. No silence heuristic or device is involved.
            await sink.start()
            await sink.set_muted(False)
            await sink.write(b"\2\0" * 10)
            delivery = await sink.drain(timeout=2)
            self.assertEqual(delivery, {"status": "drained", "truncated": False, "reason": None})
            self.assertEqual(await sink.pause(), delivery)
            self.assertEqual(count.read_text(), "20")
            self.assertIsNone(sink.process)
            self.assertEqual(set(p.name for p in root.iterdir()),
                {"fake-pactl", "fake-pacat", "control.jsonl", "count.txt"})

    async def test_absent_route_refuses_before_starting_player(self):
        sink = PulseTXSink()
        sink._control = AsyncMock(return_value=b'[]')
        with self.assertRaisesRegex(RuntimeError, "not provisioned"):
            await sink.start()
        self.assertIsNone(sink.process)


class PulseGateLifecycleTests(unittest.IsolatedAsyncioTestCase):
    """Stateful control/player fakes: never run PulseAudio or physical devices."""
    def fixture(self, *, sink_muted=True, source_muted=True):
        calls = []
        states = {"sink": sink_muted, "source": source_muted}
        sink = PulseTXSink()
        async def control(*args):
            calls.append(tuple(args))
            if "list" in args:
                kind = "sink" if args[-1] == "sinks" else "source"
                return json.dumps([{"name": "agent_tx" if kind == "sink" else "agent_mic",
                                    "mute": states[kind]}]).encode()
            kind = "sink" if args[0] == "set-sink-mute" else "source"
            states[kind] = args[-1] == "1"
            return b""
        sink._control = AsyncMock(side_effect=control)
        player = SimpleNamespace(returncode=None,
            stdin=SimpleNamespace(transport=SimpleNamespace(abort=Mock(side_effect=lambda: calls.append(("abort",))),
                is_closing=Mock(return_value=False), get_write_buffer_size=Mock(return_value=0)),
                                  write=Mock(), drain=AsyncMock()),
            terminate=Mock(side_effect=lambda: calls.append(("terminate",))), kill=Mock())
        async def wait():
            player.returncode = -15
            return -15
        player.wait = AsyncMock(side_effect=wait)
        return sink, player, states, calls, control

    async def test_normal_stop_double_mutes_and_aborts_queue_before_termination(self):
        sink, player, states, calls, _ = self.fixture()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        self.assertEqual(states, {"sink": True, "source": True})
        await sink.set_muted(False)
        self.assertEqual(states, {"sink": False, "source": False})
        await sink.write(b"\1\0" * 20)
        await sink.stop()
        self.assertEqual(calls[-4:], [("set-source-mute", "agent_mic", "1"),
            ("set-sink-mute", "agent_tx", "1"), ("abort",), ("terminate",)])
        self.assertEqual(states, {"sink": True, "source": True})
        self.assertIsNone(sink.process)

    async def test_explicit_restore_preserves_each_original_gate_across_speeches(self):
        for sink_muted, source_muted in ((False, True), (True, False), (False, False), (True, True)):
            with self.subTest(sink=sink_muted, source=source_muted):
                sink, player, states, calls, _ = self.fixture(
                    sink_muted=sink_muted, source_muted=source_muted)
                for _ in range(2):
                    player.returncode = None
                    with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
                        await sink.start()
                    await sink.set_muted(False)
                    await sink.stop()
                    self.assertEqual(states, {"sink": True, "source": True})
                await sink.stop(restore_original=True)
                self.assertEqual(states, {"sink": sink_muted, "source": source_muted})
                # Restoring is explicitly requested only after all player bytes
                # were discarded; later start captures no replacement snapshot.
                self.assertLess(calls.index(("terminate",)), len(calls) - 2)
                self.assertEqual(calls[-2:], [("set-sink-mute", "agent_tx", "1" if sink_muted else "0"),
                    ("set-source-mute", "agent_mic", "1" if source_muted else "0")])

    async def test_unknown_initial_mute_refuses_without_mutating_route(self):
        sink = PulseTXSink()
        sink._control = AsyncMock(return_value=b'[{"name":"agent_tx"}]')
        with patch("asyncio.create_subprocess_exec", AsyncMock()) as spawn:
            with self.assertRaisesRegex(RuntimeError, "mute state is unknown"):
                await sink.start()
        spawn.assert_not_awaited()
        self.assertIsNone(sink.process)

    async def test_unmute_failure_closes_both_gates_and_discards_player(self):
        sink, player, states, calls, control = self.fixture()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        async def fail_source(*args):
            if args == ("set-source-mute", "agent_mic", "0"):
                raise RuntimeError("source fixture failure")
            return await control(*args)
        sink._control.side_effect = fail_source
        with self.assertRaisesRegex(RuntimeError, "source fixture failure"):
            await sink.set_muted(False)
        self.assertEqual(states, {"sink": True, "source": True})
        player.terminate.assert_called_once()
        self.assertIsNone(sink.process)

    async def test_mute_failure_still_attempts_other_gate_and_kills_without_restore(self):
        sink, player, states, calls, control = self.fixture(sink_muted=False, source_muted=False)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        await sink.set_muted(False)
        async def fail_source(*args):
            if args == ("set-source-mute", "agent_mic", "1"):
                calls.append(tuple(args))
                raise RuntimeError("cannot mute source")
            return await control(*args)
        sink._control.side_effect = fail_source
        with self.assertRaisesRegex(RuntimeError, "cannot mute source"):
            await sink.stop(restore_original=True)
        self.assertTrue(states["sink"])
        self.assertEqual(calls[-4:], [("set-source-mute", "agent_mic", "1"),
            ("set-sink-mute", "agent_tx", "1"), ("abort",), ("terminate",)])
        self.assertIsNone(sink.process)

    async def test_cancel_after_unmute_effect_finishes_fail_closed_cleanup(self):
        sink, player, states, calls, control = self.fixture()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        applied = asyncio.Event()
        async def delayed_source(*args):
            result = await control(*args)
            if args == ("set-source-mute", "agent_mic", "0"):
                applied.set()
                await asyncio.Event().wait()
            return result
        sink._control.side_effect = delayed_source
        pending = asyncio.create_task(sink.set_muted(False))
        await applied.wait()
        self.assertEqual(states, {"sink": False, "source": False})
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(states, {"sink": True, "source": True})
        self.assertIsNone(sink.process)
        player.stdin.transport.abort.assert_called_once()

    async def test_cancel_stop_waits_for_both_gates_and_player_cleanup(self):
        sink, player, states, calls, control = self.fixture()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        await sink.set_muted(False)
        entered, release = asyncio.Event(), asyncio.Event()
        async def slow_mute(*args):
            if args == ("set-source-mute", "agent_mic", "1"):
                entered.set()
                await release.wait()
            return await control(*args)
        sink._control.side_effect = slow_mute
        pending = asyncio.create_task(sink.stop())
        await entered.wait()
        pending.cancel()
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        pending.cancel()  # repeated caller cancellation must not cancel cleanup
        await asyncio.sleep(0)
        self.assertFalse(pending.done())
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(states, {"sink": True, "source": True})
        self.assertIsNone(sink.process)
        player.terminate.assert_called_once()

    async def test_cancel_spawn_after_player_exists_retains_handle_for_cleanup(self):
        sink, player, states, calls, _ = self.fixture()
        created, release = asyncio.Event(), asyncio.Event()
        async def delayed_spawn(*args, **kwargs):
            created.set()
            await release.wait()
            return player
        with patch("asyncio.create_subprocess_exec", delayed_spawn):
            pending = asyncio.create_task(sink.start())
            await created.wait()
            pending.cancel()
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        self.assertEqual(states, {"sink": True, "source": True})
        self.assertIsNone(sink.process)
        player.terminate.assert_called_once()

    async def test_restore_failure_remutes_both_instead_of_leaving_partial_restore(self):
        sink, player, states, calls, control = self.fixture(sink_muted=False, source_muted=False)
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        async def fail_restore_source(*args):
            if args == ("set-source-mute", "agent_mic", "0"):
                raise RuntimeError("restore fixture failure")
            return await control(*args)
        sink._control.side_effect = fail_restore_source
        with self.assertRaisesRegex(RuntimeError, "restore fixture failure"):
            await sink.stop(restore_original=True)
        self.assertEqual(states, {"sink": True, "source": True})
        self.assertIsNone(sink.process)

    async def test_failed_player_spawn_leaves_both_gates_muted(self):
        sink, player, states, calls, _ = self.fixture(sink_muted=False, source_muted=False)
        with patch("asyncio.create_subprocess_exec", AsyncMock(side_effect=OSError("missing fixture player"))):
            with self.assertRaisesRegex(OSError, "missing fixture player"):
                await sink.start()
        self.assertEqual(states, {"sink": True, "source": True})
        self.assertIsNone(sink.process)

    async def test_cancel_pactl_spawn_reaps_mutator_before_returning_to_cleanup(self):
        sink = PulseTXSink()
        created, release = asyncio.Event(), asyncio.Event()
        process = SimpleNamespace(returncode=None, kill=Mock(), wait=AsyncMock())
        async def delayed_spawn(*args, **kwargs):
            created.set()
            await release.wait()
            return process
        with patch("asyncio.create_subprocess_exec", delayed_spawn):
            pending = asyncio.create_task(sink._control("set-source-mute", "agent_mic", "0"))
            await created.wait()
            pending.cancel()
            await asyncio.sleep(0)
            self.assertFalse(pending.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await pending
        process.kill.assert_called_once()
        process.wait.assert_awaited_once()

    async def test_pulse_drain_waits_for_eof_clean_exit_then_mutes(self):
        sink, player, states, calls, _ = self.fixture()
        entered, release = asyncio.Event(), asyncio.Event()
        player.stdin.close = Mock(side_effect=lambda: calls.append(("eof",)))
        player.stdin.wait_closed = AsyncMock()
        async def clean_wait():
            entered.set()
            await release.wait()
            player.returncode = 0
            return 0
        player.wait.side_effect = clean_wait
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        await sink.set_muted(False)
        await sink.write(b"\1\0")
        pending = asyncio.create_task(sink.drain(timeout=.5))
        await entered.wait()
        self.assertFalse(pending.done())
        self.assertEqual(states, {"sink": False, "source": False})
        release.set()
        self.assertEqual((await pending)["status"], "drained")
        self.assertEqual(states, {"sink": True, "source": True})
        player.terminate.assert_not_called()
        self.assertIsNone(sink.process)
        self.assertEqual((await sink.drain())["status"], "drained")

    async def test_pulse_drain_timeout_discards_and_reports_truncation(self):
        sink, player, states, calls, _ = self.fixture()
        player.stdin.close = Mock()
        player.stdin.wait_closed = AsyncMock()
        async def stalled_wait():
            if player.terminate.called:
                player.returncode = -15
                return -15
            await asyncio.Event().wait()
        player.wait.side_effect = stalled_wait
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        await sink.set_muted(False)
        await sink.write(b"\1\0")
        result = await sink.drain(timeout=.01)
        self.assertEqual(result, {"status": "truncated", "truncated": True, "reason": "drain_timeout"})
        self.assertEqual(states, {"sink": True, "source": True})
        player.terminate.assert_called_once()
        self.assertIsNone(sink.process)

    async def test_pulse_cancel_drain_closes_both_gates_and_kills_player(self):
        sink, player, states, calls, _ = self.fixture()
        player.stdin.close = Mock()
        player.stdin.wait_closed = AsyncMock()
        entered = asyncio.Event()
        async def stalled_wait():
            if player.terminate.called:
                player.returncode = -15
                return -15
            entered.set()
            await asyncio.Event().wait()
        player.wait.side_effect = stalled_wait
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        await sink.set_muted(False)
        await sink.write(b"\1\0")
        pending = asyncio.create_task(sink.drain(timeout=1))
        await entered.wait()
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        self.assertEqual(states, {"sink": True, "source": True})
        player.terminate.assert_called_once()
        self.assertEqual(sink.last_delivery["reason"], "drain_cancelled")
        self.assertTrue(sink.last_delivery["truncated"])
        self.assertIsNone(sink.process)

    async def test_pulse_nonzero_exit_is_not_drain_success(self):
        sink, player, states, calls, _ = self.fixture()
        player.stdin.close = Mock()
        player.stdin.wait_closed = AsyncMock()
        async def failed_wait():
            player.returncode = 7
            return 7
        player.wait.side_effect = failed_wait
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        await sink.set_muted(False)
        await sink.write(b"\1\0")
        self.assertEqual((await sink.drain())["reason"], "player_exit_nonzero")
        self.assertTrue(sink.last_delivery["truncated"])
        self.assertEqual(states, {"sink": True, "source": True})

    async def test_stop_with_queued_pcm_reports_truncation(self):
        sink, player, states, calls, _ = self.fixture()
        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=player)):
            await sink.start()
        await sink.set_muted(False)
        await sink.write(b"\1\0")
        self.assertEqual(await sink.stop(), {"status": "truncated", "truncated": True, "reason": "explicit_stop"})

    async def test_unstarted_adapter_has_no_control_side_effects(self):
        sink = PulseTXSink()
        sink._control = AsyncMock()
        await sink.stop()
        await sink.set_muted(True)
        with self.assertRaisesRegex(RuntimeError, "not running"):
            await sink.set_muted(False)
        sink._control.assert_not_awaited()
