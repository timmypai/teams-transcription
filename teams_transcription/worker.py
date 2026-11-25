"""Recording lifecycle owned by the standalone daemon or controlled fixtures."""
import asyncio
import base64
import contextlib
import json
import os
import math
import signal
import subprocess
import time
import wave
import weakref
from datetime import datetime, timezone
from pathlib import Path
from .browser import Browser, MeetingBrowser, meeting_platform
from .live import LiveProvider
from .media import load_ice_servers
from .rpc import safe_error
from . import summary
from .storage import process_signature
from .meeting_store import MeetingStore
from .diagnostics import failure_diagnostic, live_failure, teams_join_diagnostic
from .rx_contract import RxCaptureComplete, RxContractError
from .rx_audio_stats import RxAudioSnapshot, nonzero_samples

CAPTURE_STATUS_INTERVAL = 2.0


def prepare_wav(config, target):
    args = ["ffmpeg", "-nostdin", "-y", "-hide_banner", "-loglevel", "error"]
    if config["source"] == "synthetic":
        args += ["-f", "lavfi", "-i", "flite=text=This is a synthetic test. The meeting starts at ten. The next step is to review the report.:voice=slt", "-af", "apad", "-t", str(config["duration"])]
    else:
        args += ["-i", config["wav"]]
    args += ["-ar", "24000", "-ac", "1", "-c:a", "pcm_s16le", str(target)]
    result = subprocess.run(args, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError("Audio conversion failed: " + safe_error(result.stderr))


async def run_session(session, *, install_signals=True):
    from .rx_contract import options, supervise
    policy=session.config().get('teams_acceptance')
    if policy is not None:
        from .teams_acceptance import session_budget
        session.acceptance_budget=session_budget(session,policy)
    limits = options(session.config())
    try:
        if limits is not None:
            return await supervise(session, limits, _run_session, install_signals=install_signals)
        return await _run_session(session, install_signals=install_signals)
    finally:
        if policy is not None and session.status().get('cleanup_status')=='cleaned':
            from .teams_acceptance import FINISHED_EVENT
            session.event(FINISHED_EVENT,{})


async def _run_session(session, *, install_signals=True, rx_runtime=None):
    config = session.config()
    defer_live = config['source'] == 'meeting' and config['provider'] == 'codex'
    native_input = defer_live
    pcm_required = not native_input or config.get('save_audio', False)
    rx_ready_handshake = defer_live and rx_runtime is not None
    if rx_runtime:
        config = {**config, 'duration': rx_runtime.limits['capture_seconds']}
    async def perform(awaitable, *, cleanup=False, capture=False):
        return await rx_runtime.call(awaitable, cleanup=cleanup, capture=capture) if rx_runtime else await awaitable
    started = time.monotonic()
    recording_at = started
    interrupted = False
    loop = asyncio.get_running_loop()
    def request_stop():
        nonlocal interrupted
        interrupted = True
    if install_signals:
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, request_stop)
    def stopped():
        return interrupted or session.stopped()

    session.update(state="preparing", pid=os.getpid(), process_signature=process_signature(os.getpid()), summary_state="not_requested")
    provider = None
    meeting = None
    response = None
    screen_controller = None
    screen_close = None
    response_start = response_close = None
    audio = None
    frames = 0
    capture_error = None
    capture_failure = None
    queue = asyncio.Queue(maxsize=256) if config['source'] == 'meeting' and pcm_required else None
    consumer = None
    consumer_stop = None
    consumer_failure = None
    source_drained = config['source'] != 'meeting'
    provider_drain_requested = False
    primary_failure = None
    primary_provider_failure = None
    recording_enabled = False
    expected_frames = weakref.WeakKeyDictionary()
    stats = []
    phase = "prepare"
    nonzero_frames = 0
    pending_finals = set()
    persistence_failed = False
    response_rx_failed = False
    audio_stats = RxAudioSnapshot(session, input_mode='native_media' if native_input else 'pcm') if config['source'] == 'meeting' else None
    response_rx_counts = dict.fromkeys(('clock_missing_packets', 'offered_packets',
        'accepted_packets', 'rejected_packets', 'error_packets'), 0)
    session.response_rx_counts = response_rx_counts
    audio_snapshot_missing = False

    def persist_response_snapshot(target=None):
        if response is not None and response_close is None:
            destination = target or session
            try:
                destination.update(response_status=response.status(),
                    response_snapshot_at=datetime.now(timezone.utc).isoformat())
            except Exception:
                # Diagnostics cannot interrupt A or abandon B cleanup.
                with contextlib.suppress(Exception):
                    destination.event('response_snapshot_error', {'error_code':'response_snapshot_failed'})

    def persist_audio_snapshot(target=None):
        nonlocal audio_snapshot_missing
        persist_response_snapshot(target)
        if audio_stats:
            result = None
            try:
                result = audio_stats.persist(phase=phase, queue_depth=queue.qsize() if queue is not None else None,
                    provider=provider, session=target)
            except Exception:
                audio_snapshot_missing = True
                if rx_runtime:
                    rx_runtime.audio_snapshot_missing = True
            if audio_snapshot_missing:
                with contextlib.suppress(Exception):
                    (target or session).update(rx_audio_snapshot_status='missing')
            return result

    if rx_runtime:
        rx_runtime.audio_snapshot_writer = persist_audio_snapshot

    def set_phase(value):
        nonlocal phase
        phase = value
        if rx_runtime:
            rx_runtime.phase = value
        session.event("phase", {"phase": value})

    async def stop_screen():
        nonlocal screen_close
        if screen_controller is None:
            return
        if screen_close is None:
            screen_close = asyncio.create_task(screen_controller.close())
            if rx_runtime:
                rx_runtime.track(screen_close)
        try:
            result = await perform(asyncio.shield(screen_close), cleanup=True)
            clean = result == 'cleaned'
        except Exception:
            clean = False
        if not clean:
            if rx_runtime:
                rx_runtime.cleanup_unknown = True
            session.update(cleanup_status='unknown')
            session.event('screen_cleanup', {'error_code': 'screen_cleanup_unknown'})

    async def stop_response():
        nonlocal response_close
        if response_start:
            if not response_start.done():
                response_start.cancel()
            await perform(asyncio.gather(response_start, return_exceptions=True), cleanup=True)
        if response:
            if rx_runtime:
                rx_runtime.capture_processes()
            if response_close is None:
                persist_response_snapshot()
                response_close = asyncio.create_task(response.stop())
                if rx_runtime:
                    rx_runtime.track(response_close)
            try:
                result = await perform(asyncio.shield(response_close), cleanup=True)
                status = response.status()
                clean = (result in {'stopped', 'not_started'}
                    and status.get('state') == 'stopped'
                    and status.get('cleanup_status') != 'unknown')
            except Exception:
                clean = False
            if not clean:
                if rx_runtime:
                    rx_runtime.cleanup_unknown = True
                session.update(response_cleanup_status='unknown', cleanup_status='unknown')
                session.event('response_cleanup', {'error_code':'response_cleanup_unknown'})
            else:
                session.update(response_cleanup_status='cleaned')
            if rx_runtime:
                rx_runtime.capture_processes()
            if not clean and not rx_runtime:
                raise RxContractError('rx_cleanup_unknown')


    async def capture_stats(*, cleanup=False):
        try:
            request = meeting.stats()
            if native_input and not pcm_required:
                request = asyncio.wait_for(request, .5)
            return (await perform(request, cleanup=cleanup, capture=not cleanup)) or []
        except RxCaptureComplete:
            raise
        except Exception:
            if pcm_required:
                raise
            return []

    async def event(kind, data):
        if 'join' in data:
            data = {**data, 'join': teams_join_diagnostic(data['join'])}
        if kind in {"live_error", "live_connection_failed"}:
            # Older providers may include arbitrary exception text. Keep only
            # their bounded stage/count diagnostics at the persistence boundary.
            data = {key: value for key, value in data.items() if key not in {"error", "diagnostic_error"}}
            if kind == 'live_error':
                data = {**live_failure(data), 'phase':phase}
            else:
                data["error_code"] = "live_connection_failed"
        session.event(kind, data)
        if audio_stats and kind == 'rx_ready' and isinstance(data.get('frames'), list):
            audio_stats.observe_browser([{'capture_readiness':value} for value in data['frames']])
            persist_audio_snapshot()
        if kind in {"joining", "connecting", "waiting_for_admission", "joined"}:
            session.update(state=kind)

    def join_diagnostic():
        if phase not in {'browser_open', 'meeting_join'}:
            return None
        getter = getattr(meeting, 'join_diagnostic', None)
        if not callable(getter):
            return None
        try:
            value = getter()
        except Exception:
            value = {}
        return teams_join_diagnostic(value) if value is not None else None

    async def text(value):
        entry = session.append_text(value, (time.monotonic() - recording_at) * 1000)
        session.event("transcript", {"segment": entry["id"]})

    async def final(event):
        def finished(task):
            nonlocal persistence_failed
            pending_finals.discard(task)
            if not task.cancelled() and task.exception() is not None:
                persistence_failed = True
            elif not task.cancelled() and audio_stats:
                audio_stats.final_callbacks_completed += 1
        task = asyncio.create_task(asyncio.to_thread(session.append_final, event))
        if rx_runtime:
            rx_runtime.track(task)
        pending_finals.add(task)
        task.add_done_callback(finished)
        result = await asyncio.shield(task)
        session.event("transcript_final", {"version": result["version"], "revision": event["revision"]})

    async def provisional(value):
        def finished(task):
            nonlocal persistence_failed
            pending_finals.discard(task)
            if not task.cancelled() and task.exception() is not None:
                persistence_failed = True
        task = asyncio.create_task(asyncio.to_thread(session.save_provisional,value))
        if rx_runtime:
            rx_runtime.track(task)
        pending_finals.add(task)
        task.add_done_callback(finished)
        # Cancellation cannot strand a running SQLite commit outside the
        # worker's existing persistence drain and lease fence.
        await asyncio.shield(task)

    async def persist_increment(value):
        def finished(task):
            nonlocal persistence_failed
            pending_finals.discard(task)
            if not task.cancelled() and task.exception() is not None:
                persistence_failed = True
        task = asyncio.create_task(asyncio.to_thread(session.record_increment, value, publish=False))
        if rx_runtime:
            rx_runtime.track(task)
        pending_finals.add(task)
        task.add_done_callback(finished)
        await asyncio.shield(task)

    async def publish_source_view():
        def refresh():
            state = session.store.transcript_state(session.ident)
            session.update(transcript_state=state)
        task = asyncio.create_task(asyncio.to_thread(refresh))
        if rx_runtime:
            rx_runtime.track(task)
        pending_finals.add(task)
        def finished(task):
            nonlocal persistence_failed
            pending_finals.discard(task)
            if not task.cancelled() and task.exception() is not None:
                persistence_failed = True
        task.add_done_callback(finished)
        await asyncio.shield(task)

    async def end_text_capture(value):
        task = asyncio.create_task(asyncio.to_thread(session.end_capture, value, publish=False))
        if rx_runtime:
            rx_runtime.track(task)
        pending_finals.add(task)
        def finished(task):
            nonlocal persistence_failed
            pending_finals.discard(task)
            if not task.cancelled() and task.exception() is not None:
                persistence_failed = True
        task.add_done_callback(finished)
        await asyncio.shield(task)

    async def write_chunk(data):
        nonlocal frames, nonzero_frames
        # Admission owns the capture deadline/sample cap. Once accepted, PCM
        # belongs to this attempt until its bounded source drain completes.
        if audio:
            audio.writeframesraw(data)
        frames += len(data) // 2
        nonzero = audio_stats.consumed(data) if audio_stats else nonzero_samples(data)
        nonzero_frames += nonzero
        if provider and not native_input:
            if audio_stats:
                audio_stats.push_attempted(data)
            await provider.push(data)
            if audio_stats:
                audio_stats.push_returned(data, nonzero)
        if rx_runtime:
            rx_runtime.pcm_consumed_samples += len(data) // 2

    async def on_audio(packet):
        nonlocal capture_error, capture_failure, response_rx_failed
        if not recording_enabled or capture_error:
            return
        if rx_runtime and (not rx_runtime.capture_open() or (rx_runtime.capture_sample_limit is not None
                and rx_runtime.pcm_accepted_samples >= rx_runtime.capture_sample_limit)):
            return
        document = None
        try:
            if (not isinstance(packet, dict) or packet.get("sample_rate") != 24000
                    or type(packet.get("frames")) is not int or not 1 <= packet["frames"] <= 2048
                    or type(packet.get("start_frame")) is not int or packet["start_frame"] < 0
                    or not isinstance(packet.get("data"), str) or len(packet["data"]) > 5464):
                raise RuntimeError("invalid_pcm_packet")
            data = base64.b64decode(packet["data"], validate=True)
            if len(data) != packet["frames"] * 2:
                raise RuntimeError("invalid_pcm_length")
            # Only MeetingBrowser's document-bound host assertion reaches this
            # seam. Page-supplied capture IDs never key continuity state.
            document = packet.get("source_document")
            if document is None:
                if response:
                    response_rx_counts['clock_missing_packets'] += 1
                raise RuntimeError("pcm_document_unavailable")
            expected_frame = expected_frames.get(document)
            gap = (packet["start_frame"] != (expected_frame or 0) if pcm_required else
                expected_frame is not None and packet["start_frame"] != expected_frame)
            expected_frames[document] = packet["start_frame"] + packet["frames"]
            if gap:
                raise RuntimeError("pcm_frame_gap")
            audio_stats.incoming(data)
            if pcm_required:
                if rx_runtime and rx_runtime.capture_sample_limit is not None:
                    remaining = rx_runtime.capture_sample_limit - rx_runtime.pcm_accepted_samples
                    data = data[:remaining * 2]
                queue.put_nowait(data)
                audio_stats.queued(data)
                if rx_runtime:
                    rx_runtime.pcm_accepted_samples += len(data) // 2
                    if rx_runtime.capture_sample_limit is not None and rx_runtime.pcm_accepted_samples == rx_runtime.capture_sample_limit:
                        rx_runtime.capture_full.set_result(None)
            if response:
                begin, end = packet.get("source_started_at"), packet.get("source_ended_at")
                valid_time = (packet.get("source_clock") == "monotonic"
                    and type(begin) in (int, float) and type(end) in (int, float)
                    and math.isfinite(begin) and math.isfinite(end) and 0 <= begin < end)
                if not valid_time:
                    response_rx_counts['clock_missing_packets'] += 1
                else:
                    response_rx_counts['offered_packets'] += 1
                    try:
                        accepted = response.push_rx(data, started_at=begin, ended_at=end)
                        response_rx_counts['accepted_packets' if accepted is True else 'rejected_packets'] += 1
                    except Exception:
                        response_rx_counts['error_packets'] += 1
                        if not response_rx_failed:
                            response_rx_failed = True
                            session.event('response_rx_error', {'error_code':'response_rx_failed'})
        except Exception as exc:
            overflow = isinstance(exc, asyncio.QueueFull)
            audio_stats.python['queue_overflows' if overflow else 'invalid_packets'] += 1
            if pcm_required:
                capture_error = "pcm_queue_overflow" if overflow else "invalid_pcm_packet"
                capture_failure = failure_diagnostic(exc, "rx_capture")
                session.event("capture_error", capture_failure)
            else:
                # Observation loss never becomes A drain debt or a permanent
                # B gate. The next valid packet retains its real timestamps.
                if document is not None:
                    with contextlib.suppress(TypeError):
                        expected_frames.pop(document, None)

    async def consume():
        nonlocal consumer_failure
        while True:
            data = await queue.get()
            try:
                if data is None:
                    return
                try:
                    await write_chunk(data)
                except Exception as exc:
                    # Awaiting a failed task again replaces its traceback.
                    # Freeze the first push-site receipt before drain awaits.
                    consumer_failure = (exc, failure_diagnostic(exc, 'rx_capture'))
                    raise
            finally:
                queue.task_done()

    async def drain_source():
        nonlocal recording_enabled, source_drained, consumer_stop
        if source_drained:
            return
        recording_enabled = False
        if rx_runtime:
            rx_runtime.end_capture()
            rx_runtime.begin_cleanup()
        set_phase('rx_drain')
        if meeting:
            try:
                await perform(meeting.stop_capture(), cleanup=True)
            except BaseException:
                if rx_runtime:
                    rx_runtime.cleanup_unknown = True
                session.update(cleanup_status='unknown')
                raise
        await stop_screen()
        await stop_response()
        if consumer:
            if not consumer.done():
                if consumer_stop is None:
                    consumer_stop = asyncio.create_task(queue.put(None))
                    if rx_runtime:
                        rx_runtime.track(consumer_stop)
                await perform(asyncio.shield(consumer_stop) if rx_runtime else
                    asyncio.wait_for(asyncio.shield(consumer_stop), 2), cleanup=True)
                await perform(asyncio.shield(consumer) if rx_runtime else
                    asyncio.wait_for(asyncio.shield(consumer), 15), cleanup=True)
            if not consumer.done():
                raise RxContractError('rx_source_drain_incomplete')
            await consumer
            if rx_runtime and rx_runtime.pcm_consumed_samples != rx_runtime.pcm_accepted_samples:
                raise RxContractError('rx_source_drain_incomplete')
        source_drained = True

    async def drain_provider():
        nonlocal provider_drain_requested
        if provider and not provider_drain_requested:
            provider_drain_requested = True
            set_phase('live_drain')
            session.update(state='draining')
            await perform(provider.drain(config['drain_seconds']), cleanup=True)

    async def start_provider(browser):
        nonlocal provider
        set_phase('live_start')
        provider = LiveProvider(browser.browser, Path(config.get('runtime_dir', session.path)) / 'codex-runtime',
            text, event, config['codex_bin'], ice_servers=load_ice_servers(config.get('ice_servers_file')),
            on_final=final, lookup_final=lambda event_id: session.store.get_final(session.ident,event_id),
            bind_alias=session.bind_final_alias, glossary_snapshot=config.get('glossary_snapshot'),
            **({'before_native_start':lambda stage:session.acceptance_budget.claim('A',stage)}
                if getattr(session,'acceptance_budget',None) is not None else {}),
            **({'media_source':meeting} if native_input else {}))
        provider.on_provisional = provisional  # Legacy preview compatibility only.
        provider.on_increment = persist_increment
        provider.on_source_view = publish_source_view
        provider.on_capture_end = end_text_capture
        if rx_runtime:
            rx_runtime.provider = provider
        try:
            await perform(provider.start())
        except BaseException:
            if not defer_live:
                await perform(provider.close(), cleanup=True)
            raise

    try:
        persist_audio_snapshot()
        if stopped():
            raise InterruptedError("Stopped before preparation")
        if config["source"] == "meeting" and config["provider"] == "record-only" and not config.get("save_audio", False):
            raise ValueError("record_only_requires_explicit_save_audio")
        # Source preparation is local and does not contact any model.
        source_file = session.path / "source.wav"
        if config["source"] != "meeting":
            await asyncio.to_thread(prepare_wav, config, source_file)
        if config["source"] != "meeting" or config.get("save_audio", False):
            audio = wave.open(str(session.path / "audio.wav"), "wb")
            audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        needs_browser = config["provider"] == "codex" or config["source"] == "meeting"
        async with contextlib.AsyncExitStack() as stack:
            if needs_browser:
                set_phase("browser_open")
            browser = None
            if needs_browser:
                browser = Browser(config.get('chromium'))
                if rx_runtime:
                    rx_runtime.browser = browser
                    await perform(browser.__aenter__())
                    stack.push_async_callback(lambda: perform(browser.__aexit__(None, None, None), cleanup=True))
                else:
                    browser = await stack.enter_async_context(browser)
            if config['provider'] == 'codex' and not defer_live:
                await start_provider(browser)
            try:
                if stopped():
                    raise InterruptedError("Stopped before recording")
                if config["source"] == "meeting":
                    platform = meeting_platform(config["meeting_url"])
                    meeting = MeetingBrowser(browser.browser, on_audio, event, defer_capture=True,
                        response_mode=bool(config.get('rx_session_contract', False) and config.get('response_enabled', False)))
                    set_phase("browser_open")
                    await perform(meeting.open(config["meeting_url"], config.get("storage_state")))
                    set_phase("meeting_join")
                    await perform(meeting.join(platform, config["name"], config["join_timeout"], stopped))
                    if rx_runtime:
                        set_phase('prepared')
                        if not (await perform(meeting.prepared_readiness()))['prepared']:
                            raise RxContractError('rx_prepared_not_ready')
                        set_phase('owner_wait')
                        await perform(rx_runtime.owner_ready(meeting, provider, stopped))
                    if defer_live:
                        if stopped():
                            raise InterruptedError('Stopped before Live startup')
                        await start_provider(browser)
                        if not (await perform(meeting.prepared_readiness()))['prepared']:
                            raise RxContractError('rx_prepared_no_longer_ready')
                    if stopped():
                        raise InterruptedError('Stopped before capture')
                    if config.get("response_enabled", False) and config["provider"] == "codex":
                        screen_mode = config.get("screen_context_mode", "off")
                        if screen_mode in {"on_demand", "watch_and_cache"}:
                            from .screen_capture import ScreenShareCapturer
                            from .screen_vision import ScreenVisionAnalyzer
                            from .screen_watcher import ScreenContextController
                            async def _persist_screen_obs(obs):
                                await asyncio.to_thread(session.record_screen_observation, obs)
                            screen_controller = ScreenContextController(
                                session.ident,
                                mode=screen_mode,
                                capturer=ScreenShareCapturer(meeting),
                                analyzer=ScreenVisionAnalyzer(
                                    Path(config.get("runtime_dir", session.path)) / "screen-vision",
                                    executable=config["codex_bin"],
                                    model=config.get("vision_model"),
                                ),
                                persist_observation=_persist_screen_obs,
                                poll_seconds=float(config.get("screen_poll_seconds", 2.5)),
                                min_analyze_interval_seconds=float(config.get("screen_min_analyze_interval_seconds", 6.0)),
                                max_frames=int(config.get("screen_frame_cache_limit", 16)),
                            )
                            await screen_controller.start()
                        from .response_runtime import ProductionResponseController
                        response = ProductionResponseController(session, meeting, browser.browser, config,
                            load_ice_servers(config.get("ice_servers_file")),
                            screen_controller=screen_controller)
                        if native_input and not pcm_required:
                            # A owns the independent native track. Admit its
                            # optional B-only PCM consumer only after B is ready.
                            meeting.response_capture_ready = response.capture_ready
                        session.response_controller = response
                        if rx_runtime:
                            rx_runtime.response = response
                        response_start = asyncio.create_task(response.start())
                        if rx_runtime:
                            rx_runtime.track(response_start)
                    recording_at = time.monotonic()
                    if rx_runtime:
                        rx_runtime.begin_capture()
                    recording_enabled = True
                    session.update(state='rx_starting' if rx_ready_handshake else 'recording',
                        recording_started_at=datetime.now(timezone.utc).isoformat(), **({'rx_ready':False} if rx_ready_handshake else {}))
                    if queue is not None:
                        consumer = asyncio.create_task(consume())
                        if rx_runtime:
                            rx_runtime.track(consumer)
                    set_phase("rx_start")
                    try:
                        capture = meeting.start_capture(timeout=config['audio_timeout'], stopped=stopped)
                        report = await perform(capture if rx_runtime else asyncio.wait_for(capture, config['audio_timeout'] + 1), capture=True)
                        if stopped():
                            raise InterruptedError('Stopped before media readiness')
                        if native_input:
                            if meeting.native_audio_failed:
                                raise RxContractError('rx_native_source_failed')
                            if (not isinstance(report,dict) or report.get('ready') is not True
                                    or not await perform(provider.ready(),capture=True)):
                                raise RxContractError('rx_live_not_ready')
                            if rx_ready_handshake:
                                session.update(state='recording', rx_ready=True, live_state='connected')
                                session.event('receiving_ready', {'live_state':'connected','rx_ready':True})
                        set_phase("rx_capture")
                        last_status = recording_at
                        missing_since = None
                        while not stopped() and (config['duration'] is None or time.monotonic() - recording_at < config['duration']):
                            if capture_error:
                                raise RxContractError(capture_error)
                            if consumer and consumer.done():
                                await consumer
                            if provider and provider.error:
                                raise RuntimeError(provider.error)
                            if native_input and meeting.native_audio_failed:
                                raise RxContractError('rx_native_source_failed')
                            if not native_input and time.monotonic() - recording_at > config["audio_timeout"] and frames == 0:
                                raise RuntimeError("Joined meeting but no incoming audio track was captured")
                            if time.monotonic() - last_status >= CAPTURE_STATUS_INTERVAL:
                                persist_audio_snapshot()
                                stats = await capture_stats()
                                audio_stats.observe_browser(stats)
                                persist_audio_snapshot()
                                if provider:
                                    try:
                                        value = await perform(asyncio.wait_for(provider.peer.stats(), .5), capture=True)
                                    except RxCaptureComplete:
                                        raise
                                    except Exception:
                                        pass  # Optional diagnostics cannot certify source failure.
                                    else:
                                        audio_stats.observe_transport(value)
                                        source = value.get('native_source') if isinstance(value, dict) else None
                                        if native_input and isinstance(source, dict) and source.get('failed') is True:
                                            raise RxContractError('rx_native_source_failed')
                                    persist_audio_snapshot()
                                if pcm_required and any(x.get("dropped", 0) or x.get("playout_errors", 0) for x in stats):
                                    raise RuntimeError("Browser dropped audio packets")
                                lifecycle = await perform(meeting.capture_lifecycle(),capture=True)
                                if lifecycle=='confirmed_end':
                                    session.update(end_reason='confirmed_end')
                                    session.event('meeting_ended',{'reason':'confirmed_end'})
                                    break
                                if lifecycle=='meeting_removed':
                                    session.update(end_reason='meeting_removed')
                                    raise RxContractError('meeting_removed')
                                if lifecycle=='joined':
                                    missing_since = None
                                else:
                                    missing_since = missing_since or time.monotonic()
                                    if time.monotonic()-missing_since>=10:
                                        session.update(end_reason='transport_loss')
                                        raise RxContractError('meeting_transport_lost')
                                last_status = time.monotonic()
                            pause = min(.1, max(0, config['duration'] - (time.monotonic() - recording_at))) if rx_runtime and config['duration'] is not None else .1
                            await perform(asyncio.sleep(pause), capture=True)
                        if stopped():
                            session.update(end_reason='operator_stop')
                        elif session.status().get('end_reason')!='confirmed_end':
                            session.update(end_reason='duration_limit')
                    except RxCaptureComplete:
                        session.event('capture_limit', {'input_mode':'native_media' if native_input else 'pcm',
                            'pcm_accepted_samples':rx_runtime.pcm_accepted_samples,
                            'pcm_consumed_samples':rx_runtime.pcm_consumed_samples,
                            'pcm_sample_limit':rx_runtime.capture_sample_limit})
                        session.update(end_reason='operator_stop' if stopped() else 'duration_limit')
                    finally:
                        if rx_runtime:
                            recording_enabled = False  # Close ingress before any final stats/cleanup await.
                            if config['duration'] is None or stopped():
                                rx_runtime.end_capture()
                            rx_runtime.begin_cleanup()
                    await drain_source()
                    if pcm_required:
                        stats = await capture_stats(cleanup=True)
                        audio_stats.observe_browser(stats)
                    persist_audio_snapshot()
                    if (pcm_required and any(x.get("dropped", 0) or x.get("playout_errors", 0) for x in stats)) or capture_error:
                        raise RxContractError(capture_error) if capture_error else RuntimeError("Browser dropped audio packets")
                    if native_input and meeting.native_audio_failed:
                        raise RxContractError('rx_native_source_failed')
                    recording_enabled = False
                    if rx_runtime:
                        rx_runtime.begin_cleanup()
                    await stop_screen()
                    await stop_response()
                    await perform(meeting.close(), cleanup=True)
                    meeting = None
                    if pcm_required and frames == 0 and not stopped():
                        raise RuntimeError("No incoming meeting audio was recorded")
                else:
                    recording_at = time.monotonic()
                    session.update(state="recording", recording_started_at=datetime.now(timezone.utc).isoformat())
                    with wave.open(str(source_file)) as src:
                        while not stopped():
                            if time.monotonic() - recording_at >= config["duration"]:
                                break
                            data = src.readframes(2048)
                            if not data:
                                break
                            await write_chunk(data)
                            due = recording_at + frames / 24000
                            await asyncio.sleep(max(0, due - time.monotonic()))
                    if config["provider"] == "mock":
                        values = json.loads(Path(config["mock_fixture"]).read_text())
                        if not isinstance(values, list):
                            raise ValueError("Mock fixture must be a JSON list of transcript strings")
                        for value in values:
                            if not isinstance(value, str):
                                raise ValueError("Mock transcript must be text")
                            await text(value)
                if provider:
                    await drain_provider()
                    if not native_input:
                        value = await perform(provider.peer.stats(), cleanup=True)
                        if value is not None:
                            session.event("webrtc_stats", value)
                if config['source'] == 'meeting' and stopped():
                    raise InterruptedError('Stopped after source drain')
            except BaseException as exc:
                primary_failure = (exc, phase)
                primary_provider_failure = (live_failure(getattr(provider, 'failure', None))
                    if provider and provider.error else None)
                raise
            finally:
                recording_enabled = False
                persist_audio_snapshot()
                if rx_ready_handshake:
                    session.update(rx_ready=False)
                if rx_runtime:
                    rx_runtime.end_capture()
                    rx_runtime.begin_cleanup()
                if stopped() and not session.status().get('end_reason'):
                    session.update(end_reason='operator_stop')
                cleanup_failure = None
                async def cleanup_step(awaitable):
                    nonlocal cleanup_failure
                    try:
                        return await awaitable
                    except Exception as exc:
                        if cleanup_failure is None:
                            cleanup_failure = (exc, phase)
                        diagnostic = (consumer_failure[1] if consumer_failure
                            and exc is consumer_failure[0] else failure_diagnostic(exc, phase))
                        session.event('capture_cleanup_error', diagnostic)

                await cleanup_step(drain_source())
                if native_input and meeting and meeting.native_audio_failed and cleanup_failure is None:
                    cleanup_failure = (RxContractError('rx_native_source_failed'),
                        primary_failure[1] if primary_failure else phase)
                await cleanup_step(stop_screen())
                await cleanup_step(stop_response())
                if meeting:
                    await cleanup_step(perform(meeting.close(), cleanup=True))
                if consumer and not consumer.done():
                    if rx_runtime:
                        rx_runtime.cleanup_unknown = True
                    consumer.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await perform(consumer, cleanup=True)
                if consumer_stop and not consumer_stop.done():
                    consumer_stop.cancel()
                    await perform(asyncio.gather(consumer_stop, return_exceptions=True), cleanup=True)
                if provider:
                    if source_drained:
                        await cleanup_step(drain_provider())
                    set_phase('cleanup')
                    await cleanup_step(perform(provider.close(), cleanup=True))
                if cleanup_failure and (primary_failure is None or
                        isinstance(primary_failure[0], (asyncio.CancelledError, InterruptedError))):
                    exc, phase = cleanup_failure
                    raise exc
                if primary_failure is not None:
                    _, phase = primary_failure
            if audio:
                audio.close()
                audio = None
        session.event("capture_complete", {"input_mode":'native_media' if native_input else 'pcm',
            "frames": frames, "sample_rate": 24000, "nonzero_frames": nonzero_frames,
            "all_zero": frames > 0 and nonzero_frames == 0,
            "audio_saved": config["source"] != "meeting" or config.get("save_audio", False), "browser": stats})
        if config["provider"] == "codex" and not session.entries():
            session.update(state="recorded_without_transcript", summary_state="not_requested",
                audio_seconds=frames / 24000 if pcm_required else None)
            return 1
        if config["provider"] == "record-only":
            session.update(state="recorded", summary_state="not_requested", audio_seconds=frames / 24000)
            return 0
        # Capture cleanup owns no model summary work. The daemon consumes the
        # durable terminal-source outbox on a separate summary lease.
        session.update(state='stopped' if stopped() else 'completed',
            summary_state='not_requested', audio_seconds=frames / 24000 if pcm_required else None)
        return 0
    except InterruptedError as exc:
        join = join_diagnostic()
        session.update(state="stopped", audio_seconds=frames / 24000 if pcm_required else None, summary_state="not_requested",
            **({'diagnostic': {**failure_diagnostic(exc, phase), 'join': join}} if join is not None else {}))
        return 0
    except asyncio.CancelledError as exc:
        join = join_diagnostic()
        session.update(state="stopped", audio_seconds=frames / 24000 if pcm_required else None, summary_state="not_requested",
            **({'diagnostic': {**failure_diagnostic(exc, phase), 'join': join}} if join is not None else {}))
        raise
    except Exception as exc:
        preserve_primary = primary_failure is not None and not isinstance(
            primary_failure[0], (asyncio.CancelledError, InterruptedError))
        if preserve_primary:
            exc, phase = primary_failure
        if consumer_failure and exc is consumer_failure[0]:
            diagnostic = dict(consumer_failure[1])
            phase = diagnostic['phase']
        else:
            diagnostic = (capture_failure if capture_failure and isinstance(exc, RxContractError)
                and exc.code == capture_error else failure_diagnostic(exc, phase))
        join = join_diagnostic()
        if join is not None:
            diagnostic['join'] = join
        rx_step = getattr(meeting, "rx_step", None)
        if phase == "rx_start" and rx_step in {"admission_check", "receiver_scan", "readiness_check", "ready"}:
            diagnostic["rx_step"] = rx_step
        error = exc.code if isinstance(exc, RxContractError) else phase + "_failed"
        provider_failure = (primary_provider_failure if preserve_primary else
            live_failure(getattr(provider, 'failure', None))
            if provider and provider.error else None)
        if provider_failure:
            error = provider_failure['error_code']
            diagnostic.update(provider_failure)
        session.update(end_reason='provider_error' if provider_failure else
            session.status().get('end_reason','capture_error' if phase=='rx_capture' else 'preparation_error'))
        session.event("error", {"error": error, **diagnostic})
        session.update(state="failed", error=error, diagnostic=diagnostic,
            audio_seconds=frames / 24000 if pcm_required else None, summary_state="not_requested")
        return 1
    finally:
        persist_audio_snapshot()
        if audio:
            audio.close()
        try:
            if pending_finals:
                await perform(asyncio.gather(*pending_finals, return_exceptions=True), cleanup=True)
            persist_audio_snapshot()
            if persistence_failed:
                session.update(state="failed", error="transcript_persistence_failed")
            state = session.status()["state"]
            failed = state in {"failed", "recorded_without_transcript", "completed_with_summary_error"}
            supervisor_failure = getattr(session, "failure_code", None)
            if not rx_runtime:
                session.finalize_minutes("failed" if supervisor_failure or failed else "stopped" if stopped() or state == "stopped" else "completed",
                    supervisor_failure or ("transcript_unavailable" if state == "recorded_without_transcript" else "summary_failed" if state == "completed_with_summary_error" else session.status().get("error") if failed else None))
        finally:
            if install_signals:
                for sig in (signal.SIGINT, signal.SIGTERM):
                    loop.remove_signal_handler(sig)
