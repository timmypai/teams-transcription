"""B-only, opt-in PCM output. No device provisioning and no audio files."""
import asyncio
import contextlib
import json
import math
import struct
import time

# PCM16 amplitude units; engineering noise floor, not a voice detector.
PCM_ACTIVITY_RMS_THRESHOLD = 8


def _pcm16le_amplitudes(pcm):
    frames = len(pcm) // 2
    nonzero, peak, squares = 0, 0, 0
    for (sample,) in struct.iter_unpack("<h", pcm):
        nonzero += sample != 0
        peak = max(peak, abs(sample))
        squares += sample * sample
    return frames, nonzero, peak, math.sqrt(squares / frames) if frames else 0.0


class PulseTXSink:
    """Use the already provisioned agent_tx -> agent_mic route, gated twice.

    Construction has no device side effects. Each player starts with both gates
    muted. Normal stop leaves both muted; restoring the original states requires
    an explicit ``stop(restore_original=True)`` after playback is discarded.
    """
    def __init__(self, *, sink="agent_tx", source="agent_mic", pacat="pacat", pactl="pactl"):
        self.sink, self.source = sink, source
        self.pacat, self.pactl = pacat, pactl
        self.process = None
        self._original_mutes = None
        self._route_verified = False
        self._written_bytes = 0
        self.last_delivery = None
        self._lock = asyncio.Lock()

    async def _control(self, *args):
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            self.pactl, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL))
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            # Do not leave a delayed mutating pactl command alive after cleanup.
            process = await spawn
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            raise
        try:
            out, _ = await asyncio.wait_for(process.communicate(), 3)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if process.returncode:
            raise RuntimeError("Dedicated TX device control failed")
        return out

    async def _both_muted(self):
        # Close the downstream gate first. Attempt both even when one fails.
        failure = None
        for kind, name in (("source", self.source), ("sink", self.sink)):
            try:
                await self._control("set-" + kind + "-mute", name, "1")
            except BaseException as error:
                if failure is None:
                    failure = error
        if failure is not None:
            raise failure

    async def _discard_player(self):
        process, self.process = self.process, None
        if process is None:
            return
        # Abort the pipe transport instead of flushing pending speech on close.
        if process.stdin is not None:
            transport = process.stdin.transport
            # A completed EOF may already have closed its transport. Abort a
            # still-open pipe or closing pipe with queued bytes, never re-close
            # a fully closed asyncio transport (whose loop has been released).
            if not transport.is_closing() or transport.get_write_buffer_size():
                transport.abort()
        if process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 2)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    process.kill()
                await process.wait()

    async def _stop_locked(self, restore_original=False):
        had_player = self.process is not None
        if had_player or self.last_delivery is None:
            self.last_delivery = {"status": "truncated" if had_player and self._written_bytes else "stopped",
                                  "truncated": bool(had_player and self._written_bytes),
                                  "reason": "explicit_stop" if had_player and self._written_bytes else None}
        try:
            if self._route_verified:
                await self._both_muted()
        finally:
            await self._discard_player()
        # A failed mute/cleanup must never reopen either gate.
        if restore_original and self._original_mutes is not None:
            try:
                for kind, name, muted in (("sink", self.sink, self._original_mutes[0]),
                                          ("source", self.source, self._original_mutes[1])):
                    await self._control("set-" + kind + "-mute", name, "1" if muted else "0")
            except BaseException:
                await self._both_muted()
                raise
        return dict(self.last_delivery)

    @staticmethod
    async def _finish_cleanup(operation):
        # Cancellation cannot interrupt closing the gates or discarding bytes.
        task = asyncio.create_task(operation)
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
        result = task.result()
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def start(self):
        async with self._lock:
            if self.process is not None:
                raise RuntimeError("TX sink already started")
            states = []
            for kind, name in (("sinks", self.sink), ("sources", self.source)):
                items = json.loads(await self._control("--format=json", "list", kind))
                matches = [item for item in items if item.get("name") == name]
                if len(matches) != 1:
                    raise RuntimeError("Dedicated TX route is not provisioned")
                if type(matches[0].get("mute")) is not bool:
                    raise RuntimeError("Dedicated TX route mute state is unknown")
                states.append(matches[0]["mute"])
            if self._original_mutes is None:
                self._original_mutes = tuple(states)
            self._route_verified = True
            self._written_bytes = 0
            self.last_delivery = None
            try:
                await self._both_muted()
                # If spawn has taken effect when its caller is cancelled, retain
                # its handle so cleanup can still terminate the owned player.
                spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                    self.pacat, "--playback", "--raw", "--format=s16le", "--rate=48000",
                    "--channels=1", "--latency-msec=40", "--device=" + self.sink,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL))
                try:
                    self.process = await asyncio.shield(spawn)
                except asyncio.CancelledError:
                    self.process = await spawn
                    raise
            except BaseException:
                await self._finish_cleanup(self._stop_locked())
                raise

    async def set_muted(self, muted):
        async with self._lock:
            if muted:
                if self._route_verified:
                    await self._finish_cleanup(self._both_muted())
                return
            if not self._route_verified or self.process is None or self.process.returncode is not None:
                raise RuntimeError("TX player is not running")
            try:
                # Open upstream first, then downstream only when both succeed.
                await self._control("set-sink-mute", self.sink, "0")
                await self._control("set-source-mute", self.source, "0")
            except BaseException:
                await self._finish_cleanup(self._stop_locked())
                raise

    async def write(self, pcm):
        if not self.process or self.process.returncode is not None:
            raise RuntimeError("TX player is not running")
        self._written_bytes += len(pcm)
        self.process.stdin.write(pcm)
        await asyncio.wait_for(self.process.stdin.drain(), .5)

    async def drain(self, timeout=3):
        """Send EOF and await pacat's stream drain / clean process exit.

        A pipe write/drain only means bytes reached pacat, not Pulse playback.
        pacat playback handles EOF by draining its Pulse stream before exit;
        only a zero exit after EOF is accepted here. Cleanup remains muted.
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Invalid audio drain timeout")
        async with self._lock:
            process = self.process
            if process is None:
                return dict(self.last_delivery or {"status": "idle", "truncated": False, "reason": None})
            async def finish():
                process.stdin.close()
                await process.stdin.wait_closed()
                return await process.wait()
            try:
                returncode = await asyncio.wait_for(finish(), timeout)
                if returncode == 0:
                    result = {"status": "drained", "truncated": False, "reason": None}
                else:
                    result = {"status": "truncated", "truncated": True, "reason": "player_exit_nonzero"}
            except asyncio.TimeoutError:
                result = {"status": "truncated", "truncated": True, "reason": "drain_timeout"}
            except BaseException as error:
                self.last_delivery = {"status": "truncated", "truncated": True,
                                      "reason": "drain_cancelled" if isinstance(error, asyncio.CancelledError) else "drain_failed"}
                await self._finish_cleanup(self._stop_locked())
                # _stop_locked reports an interrupt, retain the drain failure.
                self.last_delivery["reason"] = "drain_cancelled" if isinstance(error, asyncio.CancelledError) else "drain_failed"
                raise
            await self._finish_cleanup(self._stop_locked())
            self.last_delivery = result
            return dict(result)

    async def pause(self):
        """Mute and discard the Pulse player; a later speech starts a fresh one."""
        async def cleanup():
            async with self._lock:
                return await self._stop_locked()
        return await self._finish_cleanup(cleanup())

    async def stop(self, *, restore_original=False):
        async def cleanup():
            async with self._lock:
                return await self._stop_locked(restore_original)
        return await self._finish_cleanup(cleanup())


class GatedPCMOutput:
    """48kHz mono s16le, session bound, default mute, revocable speech lease.

    Speech completion pauses and discards pending bytes, retaining a browser's
    owned track. Only start_output() can permit physical output; close is final.
    """
    def __init__(self, meeting_id, sink, *, clock=time.monotonic):
        self.meeting_id, self.sink, self.clock = meeting_id, sink, clock
        self.enabled = False
        self.active = False
        self.closed = False
        self.deadline = None
        self.frames = 0
        self.nonzero_frames = 0
        self.activity_frames = 0
        self.last_active = 0
        self.last_nonzero = 0
        self.last_packet_frames = self.last_packet_nonzero_frames = self.last_packet_peak = 0
        self.last_packet_rms = 0.0
        self.last_delivery = None
        self._lock = asyncio.Lock()
        self._expiry = None

    def start_output(self):
        if self.closed or self.active:
            raise RuntimeError("Output is closed or a speech is already active")
        self.enabled = True

    async def begin_speech(self, meeting_id, *, max_seconds=None):
        if (meeting_id != self.meeting_id or (max_seconds is not None
                and (isinstance(max_seconds, bool) or not isinstance(max_seconds, (int, float))
                    or not math.isfinite(max_seconds) or max_seconds <= 0))):
            raise ValueError("Invalid output scope or lease")
        async with self._lock:
            if self.closed or self.active:
                raise RuntimeError("Output unavailable or busy")
            self.active = True
            self.frames, self.last_active = 0, 0
            self.nonzero_frames = 0
            self.activity_frames = 0
            self.last_nonzero = 0
            self.last_packet_frames = self.last_packet_nonzero_frames = self.last_packet_peak = 0
            self.last_packet_rms = 0.0
            self.last_delivery = None
            self.deadline = None if max_seconds is None else self.clock() + max_seconds
            if self.enabled:
                try:
                    await self.sink.start()
                    await self.sink.set_muted(False)
                except BaseException:
                    self.active = False
                    self.enabled = False
                    with contextlib.suppress(Exception):
                        await self.sink.stop()
                    raise
            if max_seconds is not None:
                self._expiry = asyncio.create_task(self._expire(max_seconds))

    async def _expire(self, seconds):
        await asyncio.sleep(seconds)
        try:
            await self.end_speech()
        except Exception:
            # Adapter pause discards pending bytes even if mute control fails.
            # A failed control also revokes permission for subsequent speech.
            self.enabled = False

    async def write(self, meeting_id, pcm, *, sample_rate=48000, channels=1):
        if (meeting_id != self.meeting_id or sample_rate != 48000 or channels != 1
                or not isinstance(pcm, bytes) or len(pcm) % 2 or len(pcm) > 48000):
            raise ValueError("Invalid TX packet")
        async with self._lock:
            if (not self.active or self.closed
                    or (self.deadline is not None and self.clock() >= self.deadline)):
                return False
            # Track playout even in mute mode, without retaining source bytes.
            frames, nonzero, peak, rms = _pcm16le_amplitudes(pcm)
            self.frames += frames
            self.nonzero_frames += nonzero  # Exact mathematical nonzero samples.
            self.last_packet_frames, self.last_packet_nonzero_frames = frames, nonzero
            self.last_packet_peak, self.last_packet_rms = peak, rms
            if nonzero:
                self.last_nonzero = self.clock()
            if rms >= PCM_ACTIVITY_RMS_THRESHOLD:
                # Count frames of qualifying packets, including their zero
                # crossings. Low LSB tails never restart effective quiet time.
                self.activity_frames += frames
                self.last_active = self.last_nonzero
            if not self.enabled:
                return False
            try:
                await self.sink.write(pcm)
            except BaseException:
                self.enabled = self.active = False
                if self._expiry:
                    self._expiry.cancel()
                    self._expiry = None
                with contextlib.suppress(Exception):
                    await self.sink.pause()
                raise
            return True

    async def drain(self, timeout=3):
        """Bounded completion proof supplied by the output adapter.

        Closes the speech to new packets. Silence / elapsed frame duration never
        counts as proof; completion is local playback, not remote audibility.
        """
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
            raise ValueError("Invalid audio drain timeout")
        async with self._lock:
            expiry, self._expiry = self._expiry, None
            if expiry and expiry is not asyncio.current_task():
                expiry.cancel()
            was_active, self.active = self.active, False
            if not was_active:
                return dict(self.last_delivery or {"status": "idle", "truncated": False, "reason": None})
            if not self.enabled:
                self.last_delivery = {"status": "muted", "truncated": False, "reason": "output_disabled"}
                return dict(self.last_delivery)
            if self.deadline is not None:
                remaining = self.deadline - self.clock()
                if remaining <= 0:
                    self.last_delivery = {"status": "truncated", "truncated": True, "reason": "lease_expired"}
                    await PulseTXSink._finish_cleanup(self.sink.pause())
                    return dict(self.last_delivery)
                timeout = min(timeout, remaining)
            try:
                result = await asyncio.wait_for(self.sink.drain(timeout=timeout), timeout)
                if (not isinstance(result, dict) or result.get("status") not in {"drained", "truncated"}
                        or type(result.get("truncated")) is not bool
                        or (result["status"] == "drained") == result["truncated"]):
                    raise RuntimeError("Output adapter returned invalid drain result")
                # Discard/mute the lease, not the browser's stable owned player.
                await self.sink.pause()
                self.last_delivery = dict(result)
            except asyncio.TimeoutError:
                self.last_delivery = {"status": "truncated", "truncated": True, "reason": "drain_timeout"}
                await PulseTXSink._finish_cleanup(self.sink.pause())
            except BaseException as error:
                self.enabled = False
                self.last_delivery = {"status": "truncated", "truncated": True,
                                      "reason": "drain_cancelled" if isinstance(error, asyncio.CancelledError) else "drain_failed"}
                await PulseTXSink._finish_cleanup(self.sink.pause())
                raise
            return dict(self.last_delivery)

    async def end_speech(self):
        async with self._lock:
            expiry, self._expiry = self._expiry, None
            if expiry and expiry is not asyncio.current_task():
                expiry.cancel()
            was_active, self.active = self.active, False
            if was_active and self.enabled:
                try:
                    result = await self.sink.pause()
                    self.last_delivery = result if isinstance(result, dict) else {
                        "status": "truncated" if self.frames else "stopped",
                        "truncated": bool(self.frames), "reason": "explicit_stop" if self.frames else None}
                except BaseException:
                    self.enabled = False
                    self.last_delivery = {"status": "unknown", "truncated": bool(self.frames), "reason": "stop_failed"}
                    raise
            elif was_active:
                self.last_delivery = {"status": "muted", "truncated": False, "reason": "output_disabled"}
            return dict(self.last_delivery or {"status": "idle", "truncated": False, "reason": None})

    async def stop_output(self):
        try:
            return await self.end_speech()
        finally:
            self.enabled = False

    async def close(self):
        self.closed = True
        try:
            await self.stop_output()
        except BaseException:
            # Retain the original failure while still disposing idle or
            # partially-started owned resources.
            with contextlib.suppress(Exception):
                await PulseTXSink._finish_cleanup(self.sink.stop())
            raise
        else:
            await PulseTXSink._finish_cleanup(self.sink.stop())
