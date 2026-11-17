# Native response runtime integration

`worker.run_session` constructs `ProductionResponseController` only for a Codex
meeting with `response_enabled=True`. Each session/lease uses its existing unique
`runtime_dir`; B creates its independent `response-native` app-server state there.
The worker exposes `session.response_controller` for the authenticated supervisor.
Its `status()` is synchronous; `enable_output()`, `disable_output()` and `stop()`
are async. A private config flag cannot enable output: startup always calls
`native.start(enable_output=False)` and output requires a subsequent control call.

The response channel is B's `build_native_meeting_responder` v3 client-delegation
path. Its one read tool uses `MeetingStoreMinutesReader` over the scoped SQLite
snapshot on a worker thread, bound to this meeting ID. No legacy Codex answerer or
appendSpeech substitute is used. Missing policy, sender or response-start support
produces fixed response error codes and leaves A transcription running.

For `response_mode=True`, `OWNED_AUDIO_INPUT_JS` installs before document code on
the three exact approved Teams hosts. It exposes a labelled, audio-only WebAudio
input with fresh disabled tracks, preserves native audio-output enumeration, and
rejects camera or unsatisfied mandatory device identities. Permissions are not
spoofed. The document owns the source until pagehide/context destruction; RX stop
does not end B's restoration target. Teams must still negotiate sending normally:
input availability alone is not evidence of a sending direction or remote audio.

The virtual input has no microphone DSP: auto gain, echo cancellation and noise
suppression are fixed off, as exposed by its settings/capabilities. Negotiate
those properties at the owned-source boundary, not with Chromium's capture-device
`applyConstraints()` on a WebAudio track. Optional preferences may differ, but an
unsupported `exact` processing requirement is rejected with its constraint name.
Other processing/format constraints still use the native track API. See
[optional versus required constraints](https://developer.mozilla.org/en-US/docs/Web/API/MediaTrackConstraints/echoCancellation).

`MeetingBrowser` finds exactly one existing live muted audio sender and binds the
B BrowserTXSink and OwnedSenderDualPolicy to its frame/document/peer/sender.
It does not acquire a physical microphone or provision Pulse devices. Every RX
binding invocation checks the exact dual policy before forwarding; RX-only mode
requires every audio/video sender disabled. A foreign enabled sender fails closed.
Normal owned stop/replaceTrack restoration remains permitted by B's reviewed
stopping transition. B startup runs concurrently with A capture, and shutdown
finishes/cancels B before closing the meeting context.

Capture packets include the JS frame capture clock identity and start/end times
from `performance.now()` at the audio callback, with start derived from the PCM
frame duration. Before starting capture, a bounded round-trip calibration maps
that document clock to Python monotonic; a calibration taking over 100 ms is not
used for B. The source interval is not final receipt time. Missing/mismatched clock
metadata skips only B fanout; A still receives PCM. Page-supplied source_clock,
source_started_at and source_ended_at are always removed before mapping: only a
matching document clock, finite ordered times, valid frame duration and bounded
future endpoint can create those host fields. Native push_rx independently
rejects expired or owned-output-overlapping intervals. ScriptProcessor timing and
physical/acoustic echo behavior still need real service validation.

`RuntimeLiveGuard` is a **new local SDP validator** because this checkout has no
previous formal SDP guard. Its callbacks are installed through B's
ExistingLiveGuard adapter. B's browser-context HTTP/WebSocket destinations use
`security.validate_destination` and `allowed_egress_hosts`. An explicit empty
list is a valid deny-all HTTP/WebSocket policy; only a missing policy is rejected
at controller startup. It grants no media access: the own-start SDP proof below
is still mandatory. Teams/A contexts and Codex child-process service traffic are
not governed by this B-context guard; it is not a global network firewall.
Configured ICE hosts are checked before a response peer can gather candidates.
The user-approved official Live negotiation may supply dynamic public media IPs;
they are not required to equal DNS records of HTTP endpoint hosts. An answer
requires bounded audio/data media, a SHA-256 fingerprint, public unicast candidate
IPs, valid UDP/TCP candidate type/component/priority and ports 1–65535. No forward
or reverse DNS lookup is performed for an SDP candidate. Offer-local host IP/mDNS
candidates remain local sources. HTTP/WS and preconfigured ICE server destination
rules still use the existing explicit endpoint policy. Browser routing is not a
network firewall and does not pin the browser's later DNS/connect result.

Dynamic media authorization is a single-use in-memory capability from the actual
production receive chain: existing ChatGPT account check → fresh own app-server
thread → own `gpt-live-1-codex` v3 WebRTC start attempt → successful start RPC ACK →
SDP notification received from the same RPC's notification queue with that exact
thread ID. Notifications before the own attempt, foreign thread/source/context,
missing ACK, altered SDP and capability replay cannot authorize an answer. The
capability binds meeting/provider/thread/context, host attempt nonce and SDP hash.
The host nonce identifies one local negotiation attempt, **not a provider session
ID**. The installed 0.159.0-alpha.3 schema's SDP notification has only threadId and
sdp; realtimeSessionId is optional/nullable on the separate started notification,
and the start response is an empty object. This implementation does not invent a
session field or claim the optional provider ID was received. Fresh thread and
single attempt prevent reuse of an earlier negotiation's SDP. Native startup still
checks the pinned Codex version. Source-chain validation does not prove real Live
media or speech interoperability.

This baseline contains no previous `egress.py`/`source_session.py` or official SDP
validator implementation to reuse. The new local validator and host-policy adapter
are explicitly supplied by this integration; no missing earlier implementation is
claimed to have been imported.

Controlled local tests:

```sh
PYTHONPATH=tests:. python -m unittest test_response_runtime test_runtime_guard test_worker_minutes test_browser -v
```

The response runtime tests exercise the actual worker and Chromium RX,
with an injected native provider and local generated, inaudible media. They check
scoped minutes, source-time fanout, default mute, explicit owned TX while RX
continues, restore while RX continues, missing policy without B RPC, and immediate
stop with no PCM, partially installed output cleanup, and unknown cleanup status.
The dual exception is released only after the exact original owned sender is
restored and all outgoing audio/video tracks are verified disabled; unknown
cleanup remains unknown. Status preserves the native provider's output_enabled
and output_muted flags even in stop_unknown; it never converts cleanup failure
into a successful mute assertion. Enabling output is rejected in that state.
These are provider-reported flags, not physical Teams playout confirmation.
Guard tests cover HTTP/WS denial, binding, official source proof, dynamic public
media, private/multicast candidates, bad ports/types, media shape and ICE preflight.
The capture binding test rejects forged source assertions on wrong/missing clocks
and invalid timing while still forwarding the original PCM to A.
They do not contact Teams, GPT Live or
any human microphone, play to a human, save original audio, or establish that Teams
participants hear generated speech. #35's real RX-ready failure root cause remains
unproven; daytime native delegation/quiet-context response and physical output
acceptance remain required.

## B progress and failure evidence

Authenticated `/api/status.response` and the dashboard expose the controller's
bounded `progress` snapshot. It separates missing source-clock fanout, accepted/
rejected B packets, local peer enqueue, scoped user/assistant delta/done counts,
pending wake markers, admitted/rejected handoffs, tool calls and speech completion.
`has_question` means non-punctuation text follows a wake match, not verified ASR
accuracy or question semantics. Local enqueue, PCM activity and sink drain do not
prove provider consumption or remote audibility.

The existing worker snapshot cadence saves `response_status` and host
`response_snapshot_at` in SQLite runtime state. The first shutdown snapshot is
preserved before B cleanup changes its state; history shows its observation time.
Snapshot failure emits only `response_snapshot_failed` and cannot interrupt A or
abandon cleanup. No transcript, question, URL, SDP, raw error or media is included.
No partial-transcript wake, synthetic-question admission or provider-final
fallback is enabled by this diagnostic path.
