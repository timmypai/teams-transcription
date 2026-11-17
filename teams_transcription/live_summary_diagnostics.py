"""Bounded probe evidence: fixed labels, timing and counts, never payloads."""
import copy
import time

PHASES = (
    "binary_verify", "rpc_initialize", "account_read", "thread_start", "peer_bind",
    "ice_offer", "realtime_start_ack", "sdp_answer", "sdp_authorize", "sdp_apply",
    "media_connected", "append_text_ack", "voice_stats_before", "voice_input",
    "voice_stats_after", "wait_output", "voice_stats_final", "cleanup_output",
    "stop_ack", "stop_closed", "cleanup_peer", "cleanup_rpc",
)
RPC_EVENTS = {
    "thread/realtime/sdp": "sdp", "thread/realtime/started": "started", "thread/realtime/closed": "closed",
    "thread/realtime/error": "error", "client/exit": "client_exit",
    "thread/realtime/transcript/delta": "transcript_delta",
    "thread/realtime/transcript/done": "transcript_done",
    "thread/realtime/itemAdded": "item_added",
    "thread/realtime/handoff": "handoff", "thread/realtime/handoffRequested": "handoff_requested",
    "delegation.created": "delegation_created",
    "thread/realtime/item/completed": "realtime_item_completed",
    "item/started": "ordinary_item_started", "item/completed": "ordinary_item_completed",
}
DATA_EVENTS = (
    "session.started", "session.updated", "output_audio.delta", "input_transcript.added",
    "output_transcript.added", "turn.done", "delegation.created", "error", "unknown",
)


class LiveSummaryDiagnostics:
    def __init__(self, timeout, *, clock=time.monotonic):
        self.clock, self.timeout = clock, timeout
        self.origin = None
        self.phases = {name: {"state": "not_entered", "entered_ms": None,
            "duration_ms": None, "remaining_ms": None} for name in PHASES}
        self.failure_phase = None
        self.failure_kind = None
        self.timeout_scope = None
        self.rpc_events = {name: 0 for name in (*RPC_EVENTS.values(), "ordinary_turn", "unknown")}
        self.scope_counts = {name: 0 for name in ("own", "foreign", "unscoped")}
        self.roles = {name: 0 for name in ("assistant", "user", "other")}
        self.items = {name: 0 for name in ("handoff_request", "delegation", "other")}
        self.data_events = {name: 0 for name in DATA_EVENTS}
        self.first_assistant_ms = None
        self.first_transport_event = None
        self.closed_before_stop = None
        self.closed_after_stop = None
        self._active_phase = None

    def begin(self):
        self.origin = self.clock()

    def elapsed(self):
        return max(0, self.clock() - self.origin) if self.origin is not None else 0

    @staticmethod
    def _increment(counts, key):
        counts[key] = min(1000000, counts[key] + 1)

    def phase(self, name, state, failure_kind=None):
        if name not in self.phases or state not in {"entered", "returned", "cancelled", "failed"}:
            return
        row, elapsed = self.phases[name], self.elapsed()
        if state == "entered":
            if not name.startswith("cleanup_") and not name.startswith("stop_"):
                self._active_phase = name
            row.update(state=state, entered_ms=round(elapsed * 1000),
                       remaining_ms=round(max(0, self.timeout - elapsed) * 1000))
        else:
            if self._active_phase == name:
                self._active_phase = None
            row.update(state=state, duration_ms=max(0, round(elapsed * 1000) - row["entered_ms"]))
            if state in {"cancelled", "failed"} and self.failure_phase is None:
                self.failure_phase = name
                self.failure_kind = failure_kind if isinstance(failure_kind, str) and failure_kind in {"timeout", "cancelled", "exception"} else None

    def notification(self, method, params, own_thread):
        thread = params.get("threadId")
        scope = "own" if own_thread is not None and thread == own_thread else "foreign" if thread else "unscoped"
        self._increment(self.scope_counts, scope)
        # Foreign content contributes one fixed scope count and nothing else.
        if scope != "own" and method != "client/exit":
            return
        name = RPC_EVENTS.get(method if isinstance(method, str) else None, "ordinary_turn" if isinstance(method, str) and method.startswith("turn/") else "unknown")
        self._increment(self.rpc_events, name)
        if name in {"transcript_delta", "transcript_done"}:
            role = params.get("role")
            self._increment(self.roles, role if isinstance(role, str) and role in {"assistant", "user"} else "other")
            if role == "assistant" and self.first_assistant_ms is None:
                self.first_assistant_ms = round(self.elapsed() * 1000)
        if name == "item_added":
            item = params.get("item")
            kind = item.get("type") if isinstance(item, dict) else None
            self._increment(self.items, kind if isinstance(kind, str) and kind in {"handoff_request", "delegation"} else "other")

    def data_event(self, event):
        kind = event.get("type") if isinstance(event, dict) else None
        self._increment(self.data_events, kind if isinstance(kind, str) and kind in self.data_events else "unknown")

    def closed(self, reason, before_stop):
        category = reason if isinstance(reason, str) and reason in {"requested", "transport_closed", "error"} else "missing" if reason is None else "unknown"
        field = "closed_before_stop" if before_stop else "closed_after_stop"
        if getattr(self, field) is None:
            setattr(self, field, {"reason": category, "elapsed_ms": round(self.elapsed() * 1000)})

    def transport(self, reason):
        causes = {"Response Live connection closed": "rpc_closed",
            "Response Live RPC error": "rpc_error", "Response Live app-server exited": "rpc_exit",
            "GPT response data channel error": "dc_error", "remote_decoder_failed": "peer_decoder",
            "output_backpressure": "peer_backpressure", "output_delivery_failed": "peer_delivery",
            "Live response document navigated": "peer_navigation",
            "Invalid or undeliverable GPT output audio": "peer_pcm"}
        if self.first_transport_event is None:
            self.first_transport_event = {"cause": causes.get(reason if isinstance(reason, str) else None, "unknown"),
                "active_phase": self._active_phase, "elapsed_ms": round(self.elapsed() * 1000)}

    def snapshot(self):
        return copy.deepcopy({"version": 2, "budget_ms": round(self.timeout * 1000),
            "elapsed_ms": round(self.elapsed() * 1000), "failure_phase": self.failure_phase,
            "failure_kind": self.failure_kind, "timeout_scope": self.timeout_scope, "phases": self.phases,
            "rpc_ack_scope": "host_operation_return_only",
            "stop_confirmation_scope": "own_receipt_after_stop_dispatch",
            "rpc_returns": {"realtime_start": self.phases["realtime_start_ack"]["state"] == "returned",
                "append_text": self.phases["append_text_ack"]["state"] == "returned",
                "stop": self.phases["stop_ack"]["state"] == "returned"},
            "notification_scope_counts": self.scope_counts, "rpc_event_counts": self.rpc_events,
            "transcript_role_counts": self.roles, "item_type_counts": self.items,
            "data_channel_event_counts": self.data_events, "first_assistant_ms": self.first_assistant_ms,
            "first_transport_event": self.first_transport_event,
            "closed_before_stop": self.closed_before_stop, "closed_after_stop": self.closed_after_stop})
