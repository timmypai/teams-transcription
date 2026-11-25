"""Meeting-scoped, default-muted native Live production wiring.

B failure affects only its response branch. A still owns the complete ledger.
No legacy answerer/appendSpeech path is imported or used here.
"""
import asyncio
from pathlib import Path

from .b_browser_tx import BrowserTXSink
from .b_dual_policy import OwnedSenderDualPolicy
from .b_native import build_native_meeting_responder
from .b_response import MeetingStoreMinutesReader
from .assistant_store import AssistantStore
from .qa_activity import QuestionActivityRecorder
from .runtime_guard import RuntimeLiveGuard
from .diagnostics import RESPONSE_START_PHASES, response_start_failure, response_start_receipt, sender_selection_diagnostic, response_progress_diagnostic, response_runtime_failure_receipt, response_policy_failure_receipt, response_capture_delivery_receipt, screen_context_diagnostic


class _AsyncStore:
    def __init__(self, root):
        self.store = AssistantStore(root)

    async def read(self, meeting_id):
        return await asyncio.to_thread(self.store.read_question_snapshot,meeting_id)


class ProductionResponseController:
    def __init__(self, session, meeting, browser, config, ice_servers, *, screen_controller=None):
        self.session, self.meeting, self.browser = session, meeting, browser
        self.meeting_id, self.config, self.ice_servers = session.ident, config, ice_servers
        self.screen_controller = screen_controller
        self.native = None
        self.state = "starting"
        self.error_code = None
        self._lock = asyncio.Lock()
        self.sink = self.policy = self.frame = None
        self._policy_released = False
        self.startup_phase = 'not_started'
        self.startup_error = None
        self.cleanup_errors = []
        self._policy_failure_task = None
        self._native_stopping = False
        self._stop_requested = False
        self._policy_revoke_unknown = False
        self._startup_task = None

    def _policy_failed(self):
        starting = self.state == 'starting'
        if self.state in {'stopped','stop_unknown'} or self._native_stopping:
            return
        self.error_code = self.error_code or 'response_input_policy_failed'
        self.state = 'unavailable'
        # Startup owns its existing cleanup path. A running controller owns one
        # revoke operation, which stop() must join before touching the same sink.
        if not starting and self.native is not None and self._policy_failure_task is None:
            self._policy_failure_task = asyncio.create_task(self._revoke_policy_output())

    async def _revoke_policy_output(self):
        async with self._lock:
            if self._native_stopping or self.state == 'stopped':
                return  # Owned shutdown has already taken responsibility for mute.
            try:
                await asyncio.wait_for(self.native.fail_input_policy(), self.native.cleanup_timeout)
            except asyncio.CancelledError:
                self._policy_revoke_unknown = True
                self._cleanup_error('policy_revoke')
                self.error_code = 'response_cleanup_unknown'
                self.state = 'stop_unknown'
                raise
            except Exception as exc:
                self._policy_revoke_unknown = True
                self._cleanup_error('policy_revoke',exc)
                self.error_code = 'response_cleanup_unknown'
                self.state = 'stop_unknown'

    def _cleanup_error(self, phase, exc=None):
        # Keep the first receipt for each of the two fixed cleanup operations.
        if not any(e['phase'] == phase for e in self.cleanup_errors):
            self.cleanup_errors.append(response_start_failure(exc, phase))

    async def _release_policy(self):
        if self.policy is None or self._policy_released:
            return True
        if self.sink._pending or self.sink.started:
            self._cleanup_error('policy_release')
            return False
        try:
            restored = await self.frame.evaluate("""owner => {
                const b=window.__bTXBindings?.get(owner);
                return !!b && b.document===document && b.capture===window.__meeting &&
                  b.meetingId===window.__bMeetingId && b.href===location.href &&
                  b.pc.getSenders().includes(b.sender) && b.sender.track===b.original &&
                  !b.original?.enabled && (b.original!==null ||
                    (b.pc.getTransceivers().includes(b.transceiver) && b.transceiver.sender===b.sender));
            }""", self.sink.owner)
            if restored is not True:
                self._cleanup_error('policy_release')
                return False
            # Already released on a previous idempotent shutdown.
            if self.meeting.dual_policy is None:
                self._policy_released = True
                return True
            self._policy_released = await self.meeting.release_dual_policy(self.frame, self.policy)
            if not self._policy_released:
                self._cleanup_error('policy_release')
            return self._policy_released
        except Exception as exc:
            self._cleanup_error('policy_release', exc)
            return False

    async def start(self):
        if self._startup_task is not None or self._stop_requested or self.state != 'starting':
            return False
        self._startup_task = asyncio.current_task()
        try:
            return await self._start()
        finally:
            self._startup_task = None

    async def _start(self):
        try:
            self.startup_phase = 'guard_construct'
            if "allowed_egress_hosts" not in self.config:
                raise ValueError("existing_egress_policy_required")
            host = RuntimeLiveGuard(self.meeting_id, self.config.get("allowed_egress_hosts", []), ice_servers=self.ice_servers)
            self.startup_phase = 'guard_prepare'
            await host.prepare()
            self.startup_phase = 'sender_select'
            frame, peer, sender = await self.meeting.response_sender(self.meeting_id)
            self.startup_phase = 'policy_construct'
            policy = OwnedSenderDualPolicy(frame, self.meeting_id, timeout=5, on_failure=self._policy_failed)
            self.startup_phase = 'sink_construct'
            sink = BrowserTXSink(frame, meeting_id=self.meeting_id, peer_index=peer,
                sender_index=sender, dual_channel_policy=policy.authorize,
                selection_id=getattr(self.meeting, '_response_selection_id', None),
                document_policy=(lambda:self.meeting.document_policy(frame))
                    if callable(getattr(self.meeting,'document_policy',None)) else None)
            self.frame, self.policy, self.sink = frame, policy, sink
            self.startup_phase = 'sink_bind'
            await sink.bind()
            self.startup_phase = 'policy_check'
            if not await policy.check():
                raise RuntimeError("initial_sender_policy_failed")
            self.startup_phase = 'policy_bind'
            self.meeting.bind_dual_policy(frame, policy)
            self.startup_phase = 'native_construct'
            runtime = Path(self.config.get("runtime_dir", self.session.path))
            reader = MeetingStoreMinutesReader(_AsyncStore(self.session.root))
            self.native = build_native_meeting_responder(self.browser, runtime,
                self.meeting_id, reader, guard=host.adapter(), sink=sink,
                executable=self.config["codex_bin"], ice_servers=self.ice_servers,
                wake_words=("小白小白","xiaobai xiaobai"),prior_context=self.config.get('prior_context'),
                **({'before_native_start':lambda stage:self.session.acceptance_budget.claim('B',stage)}
                    if getattr(self.session,'acceptance_budget',None) is not None else {}),
                activity_recorder=QuestionActivityRecorder(self.session.store, self.meeting_id),
                screen_controller=self.screen_controller)
            # Output authorization is exclusively an authenticated control action.
            self.startup_phase = 'native_start'
            await self.native.start(enable_output=False)
            self.startup_phase = 'input_policy_check'
            if policy.faulted or not await policy.check():
                raise RuntimeError('input_policy_failed_during_start')
            self.startup_phase = 'ready'
            self.state = "sleeping"
            self.session.event("response_ready", {"output_enabled":False,
                "sender_selection":sender_selection_diagnostic(getattr(self.meeting, '_response_sender_diagnostic', None))})
        except asyncio.CancelledError:
            await self.stop()
            raise
        except Exception as exc:
            if self.startup_error is None:
                native_error = getattr(self.native, 'startup_error', None)
                self.startup_error = (response_start_receipt(native_error) if isinstance(native_error, dict)
                    else response_start_failure(exc, self.startup_phase))
            self.error_code = "response_start_failed"
            self.state = "unavailable"
            if self.native:
                try:
                    await self.native.stop()
                except Exception as cleanup_exc:
                    self._cleanup_error('native_stop', cleanup_exc)
                    self.error_code = "response_cleanup_unknown"
            if not await self._release_policy():
                self.error_code = "response_cleanup_unknown"
            if self.error_code == "response_cleanup_unknown":
                self.state = "stop_unknown"
            self.session.event("response_unavailable", {"error_code":self.error_code,
                "startup_error":dict(self.startup_error),
                "sender_selection":sender_selection_diagnostic(getattr(self.meeting, '_response_sender_diagnostic', None)),
                "cleanup_errors":[dict(e) for e in self.cleanup_errors]})
            # No exception body, provider text, URL, token or SDP reaches logs.
            return False
        return True

    def status(self):
        value = self.native.status() if self.native else {}
        phase = (self.startup_error.get('phase', 'unknown') if self.startup_error
            else getattr(self.native, 'startup_phase', self.startup_phase))
        faulted = self.policy is not None and self.policy.faulted
        state = ('unavailable' if faulted and self.state not in {'stopped','stop_unknown'} else self.state)
        ready = state not in {"unavailable", "starting", "stopped", "stop_unknown"}
        media = getattr(self.meeting,'_native_media',None)
        return {"meeting_id":self.meeting_id,
                "state":value.get("state", state) if ready else state,
                # A failed cleanup/state transition cannot prove physical mute.
                # Keep the provider's actual flags even in stop_unknown. Missing
                # provider evidence is not treated as a successful mute either.
                "output_enabled":value.get("output_enabled", False),
                "output_muted":None if self._policy_revoke_unknown or
                    (self._policy_failure_task is not None and not self._policy_failure_task.done())
                    else value.get("output_muted", self.native is None),
                "error_code":self.error_code or ("response_failed" if value.get("error") else None),
                "capture_policy_failure":response_policy_failure_receipt(
                    self.policy.failure_snapshot()) if self.policy is not None else None,
                "capture_delivery":response_capture_delivery_receipt(
                    media.observation_snapshot()) if media is not None else None,
                "startup_phase":phase if phase in RESPONSE_START_PHASES else 'unknown',
                "startup_error":dict(self.startup_error) if self.startup_error else None,
                "runtime_failure":response_runtime_failure_receipt(value.get('runtime_failure')),
                "sender_selection":sender_selection_diagnostic(getattr(self.meeting, '_response_sender_diagnostic', None)),
                "progress": response_progress_diagnostic({**value,
                    'fanout': getattr(self.session, 'response_rx_counts', {})}) if self.native else None,
                "cleanup_errors":[dict(e) for e in self.cleanup_errors],
                "question_activity": value.get("question_activity",
                    {"state":"unavailable", "error_code":"question_activity_not_configured"}),
                "screen_context": screen_context_diagnostic(
                    self.screen_controller.status() if self.screen_controller is not None else value.get("screen_context")),
                "native_contract":"v3-host-question",
                "stop_status":value.get("stop_status", "not_started"),
                "cleanup_status":value.get("cleanup_status", "idle")}

    async def enable_output(self):
        async with self._lock:
            if (not self.native or self.state != "sleeping" or self.native.error
                    or self.policy is not None and self.policy.faulted):
                raise RuntimeError("native_response_unavailable")
            self.native.enable_output()
            return self.status()

    async def disable_output(self):
        async with self._lock:
            if self.native:
                await self.native.disable_output()
            return self.status()

    def capture_ready(self):
        return bool(self.native and self.state == "sleeping" and not self._stop_requested
            and not self._native_stopping
            and not self.native.error
            and not getattr(getattr(self.native, 'output', None), 'closed', False)
            and not (self.policy is not None and self.policy.faulted))

    def push_rx(self, pcm, *, started_at, ended_at):
        if self.capture_ready():
            try:
                return self.native.push_rx(self.meeting_id, pcm, started_at=started_at, ended_at=ended_at)
            except Exception:
                self.error_code = "response_rx_failed"
        return False

    async def stop(self):
        self._stop_requested = True
        startup = self._startup_task
        if startup is not None and startup is not asyncio.current_task() and not startup.done():
            if not startup.cancelling():
                startup.cancel()
            await asyncio.shield(asyncio.gather(startup, return_exceptions=True))
        if self._policy_failure_task is not None:
            await asyncio.shield(self._policy_failure_task)
        async with self._lock:
            result = await self._stop_locked()
        if self._policy_failure_task is not None:
            await asyncio.shield(self._policy_failure_task)
        return result

    async def _stop_locked(self):
        if not self.native:
            if not await self._release_policy():
                self.error_code = "response_cleanup_unknown"
            if self.error_code == "response_cleanup_unknown":
                self.state = "stop_unknown"
                return "unknown"
            self.state = "stopped"
            return "not_started"
        try:
            self._native_stopping = True
            result = await self.native.stop()
        except BaseException as exc:
            self._cleanup_error('native_stop', exc)
            self.state = "stop_unknown"
            self.error_code = "response_cleanup_unknown"
            raise
        if not await self._release_policy():
            self.state = "stop_unknown"
            self.error_code = "response_cleanup_unknown"
            return "unknown"
        self.state = "stopped" if result in {"stopped", "not_started"} else "stop_unknown"
        if self.state == 'stopped':
            self._policy_revoke_unknown = False
        return result
