"""Bind host policy callbacks and the official own-start SDP receive chain."""
import inspect
import hashlib
from dataclasses import dataclass


async def resolved(value):
    return await value if inspect.isawaitable(value) else value


@dataclass(frozen=True)
class LiveBinding:
    meeting_id: str
    thread_id: str
    provider: str = "codex-live"


@dataclass(frozen=True)
class OfficialSDPProof:
    """Host evidence for one accepted start, not a provider session-id claim."""
    binding: LiveBinding
    attempt_id: str
    sdp_sha256: str


@dataclass(frozen=True)
class OfficialSDPEnvelope:
    source: object
    attempt_id: str
    thread_id: str
    sdp: str


class ExistingLiveGuard:
    """Mandatory host callbacks, bound to one context and one Live session.

    install(context, binding) installs the host browser guard and returns its
    opaque handle. validate(handle, context, binding, sdp, kind) calls its SDP
    validator and must return literal True. The optional authorization callback
    receives only evidence from the authenticated, own-thread start/ACK chain.
    """
    def __init__(self, install, validate, *, authorize_official_answer=None):
        if not callable(install) or not callable(validate):
            raise ValueError("Existing host egress and SDP guards are required")
        self.install_callback, self.validate_callback = install, validate
        self.context = self.binding = self.handle = None
        self.authorize_callback = authorize_official_answer
        self._official = None

    async def install(self, context, binding):
        if self.context is not None or not binding.meeting_id or not binding.thread_id:
            raise RuntimeError("Live guard cannot be reused across contexts or sessions")
        self.context, self.binding = context, binding
        self.handle = await resolved(self.install_callback(context, binding))
        if self.handle is None:
            raise RuntimeError("Host egress guard did not return an installation handle")

    async def validate(self, context, binding, sdp, kind):
        if (context is not self.context or binding != self.binding or self.handle is None
                or kind not in {"offer", "answer"} or not isinstance(sdp, str) or not sdp):
            raise RuntimeError("SDP guard context/session binding mismatch")
        if await resolved(self.validate_callback(self.handle, context, binding, sdp, kind)) is not True:
            raise RuntimeError("Existing host SDP guard rejected Live description")

    def begin_official_start(self, context, binding, source, account_type, attempt_id, params):
        if (self._official is not None or context is not self.context or binding != self.binding
                or binding.provider != "codex-live" or account_type != "chatgpt" or source is None
                or not isinstance(attempt_id, str) or len(attempt_id) != 32
                or params.get("threadId") != binding.thread_id
                or params.get("model") != "gpt-live-1-codex" or params.get("version") != "v3"
                or params.get("transport", {}).get("type") != "webrtc"):
            raise RuntimeError("Official Live startup source mismatch")
        self._official = {"source":source, "attempt":attempt_id, "ack":False, "used":False}

    def acknowledge_official_start(self, context, binding, source, attempt_id):
        value = self._official
        if (not value or context is not self.context or binding != self.binding
                or source is not value["source"] or attempt_id != value["attempt"]):
            raise RuntimeError("Official Live startup acknowledgment mismatch")
        value["ack"] = True

    async def authorize_official_answer(self, context, binding, envelope):
        value = self._official
        if (not value or not value["ack"] or value["used"] or context is not self.context
                or binding != self.binding or not isinstance(envelope, OfficialSDPEnvelope)
                or envelope.source is not value["source"] or envelope.attempt_id != value["attempt"]
                or envelope.thread_id != binding.thread_id or not isinstance(envelope.sdp, str)
                or not envelope.sdp):
            raise RuntimeError("Official Live SDP source mismatch")
        proof = OfficialSDPProof(binding, value["attempt"], hashlib.sha256(envelope.sdp.encode()).hexdigest())
        if self.authorize_callback is not None:
            if await resolved(self.authorize_callback(self.handle, context, binding, proof)) is not True:
                raise RuntimeError("Official Live SDP source rejected")
        value["used"] = True
        return envelope.sdp
