# Native revision isolation: proposal and evidence contract

Status: **design for isolated synthetic QA, not an implemented runtime switch**.
Current main keeps one B Native conversation. This branch adds bounded observations
and retains paired Native turns that arrive during the current snapshot dispatch
before its output lease opens. It does not change prompts, acceptance requirements,
threads, media routing or permissions; matching assistant IDs and real media quiet
remain required for completion.

## What the fixed SDK establishes

Codex source is pinned to `3b01b36fa5eb96ba82a776bd3c2fc57f8969181f`.
[Frameless append/update](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/codex-api/src/endpoint/realtime_websocket/methods_frameless_bidi.rs)
serializes `session.context.append` as input text. Application `full_snapshot`
JSON is not a native item deletion. `session.update` exposes instructions/voice
and optional initial items; it does not establish a history-erasure guarantee.
Empty initial items are omitted. Do not invent clear/reset/delete events or use
v2 operations as v3 substitutes.

The existing `thread/start` and `thread/realtime/start` path can create a new
conversation using the **same installed model and same private OAuth home**.
Keep `includeStartupContext=false` and independently scoped runtime state; do
not resume an old answer thread or copy authentication material into a new home.
An isolated QA experiment needs no new API key, grant, account permission, port,
firewall or model selection. Actual concurrent admission/capacity still needs
measurement; account admission alone does not prove that concurrent Live works.

## Smallest candidate to test

```text
meeting capture A -------------------------- continuous and unchanged
B listener L: ASR / wake / question framing -- continuous; no SQLite fact snapshots
    admitted question + authorized current snapshot
        unchanged snapshot -> existing answer session, if still healthy/scoped
        changed snapshot   -> fresh answer session R, current authorized data only
    R audio -> existing meeting-owned output lease / echo exclusion
```

Use the authorized source-record digest, not the largest provider revision across
different sources, to detect changes. Retain legitimate historical context from
the current authorized snapshot; omit previous answer-session chat/output.
Do not replay old source snapshots or assistant answers into a fresh R.
This isolates prior answer context; it does not guarantee answer format/correctness.

L must remain live while R starts/stops. Its existing wake/greeting behavior and
question admission stay authoritative. An already admitted question is passed as
host application input; it is not a fabricated provider ASR/wake event. No source
facts or answer marker are delivered to L. A new coordinator would be needed;
this is not safely achieved by replacing the running Native peer in place.

Each R needs independently owned browser/RPC resources and guard binding. Forced
cleanup must target only R's process lineage, never a browser shared with A/L.
The meeting output lease and echo exclusion must cover R audio, so L cannot wake
on the assistant's own speech. At most one R may own output. Unknown old-session
cleanup keeps the output interlock; do not fall back to a contaminated session.
After startup awaits and before marker/output, revalidate meeting/thread/lease,
question generation and current snapshot. Changed/revoked scope fails closed,
closes R and waits for a new admission; no automatic retry/channel loop.

## Latency and model-channel accounting

Fresh startup includes account/read, thread creation, browser/ICE/SDP and media
attachment. Existing steps include CLI version wait3s, SDP answer wait35s and media
connect wait20s; these are component bounds, not a promised end-to-end latency.
Record startup separately from question dispatch, first text/audio, completion
and cleanup. Do not silently extend the current45s QA answer wait or120s work
budget. These are QA bounds; no new total answer deadline is imposed on production Native speech. If the isolated experiment cannot fit its declared budgets, report it.

Count every actual realtime start conservatively, including failed startups.
For two changed revisions, answer-only synthetic QA with pre-admitted questions
uses2 answer starts but proves neither wake nor continuous listening. Full QA
with one persistent listener plus two fresh answer sessions may use3 starts;
it cannot be counted as the old one-channel case. Stable snapshots can reuse R;
Those3 starts describe B only: if an experiment also starts a new A transcription
channel, count that additional start too; an already running A is not a new start.
changed snapshots retire it and consume another start. No prewarming or hidden
retries. Keep a per-start ledger against the existing50 allowance. This branch
uses0 model starts. Resource cost of simultaneous A/L/R must be measured under
the current limits; do not increase resources or relax sandbox settings.

## Offline prototype and later live acceptance

Before runtime integration, use owned mock SDK/browser boundaries to test:

| Scenario | Required evidence |
| --- | --- |
| Same snapshot across questions | One healthy R reused, new request per question |
| Same source revision changes | New thread/session identity, new snapshot only |
| Different source rows/history | Full currently authorized records retained |
| Source/auth changes during startup | No stale marker/output; new R cleaned |
| Old R cleanup unknown | Interlock; A/L continue, no fallback or auto retry |
| Cancel/stop while R starts | Only R owned resources closed; output muted |
| Synthetic answer-only run | Wake explicitly unverified; each start counted |

Then, in a separately executed authorized synthetic QA experiment, retain strict
current-fact/quote checks, real same-answer RX evidence and ACK/closed/reaped
cleanup. Record each model-channel start, startup latency, immutable snapshot,
payload/marker receipts and content result. Fixtures cannot establish model
memory isolation, voice quality, real wake continuity or Teams audibility.

## B: bounded lifecycle observations implemented here

Native keeps at most64 lifecycle/transition events, at most8 open-turn hashes
per observation, and saturating counters. It records callback-relative time,
fixed event/role/reason labels, ID presence/SHA, request-scope SHA, current/
previous/open-turn hashes and done set/clear reasons. No raw ID, text, token,
SDP, provider error body or arbitrary event fields are retained. RPC ingress
and own-thread transcript counts remain separate from DC ingress. Existing
browser delivery counters (message/JSON/binding outcomes) are projected at
the bounded QA peer snapshots. Observations do not feed acceptance decisions.

QA retains each completed answer's trace, a timeout's pre-cleanup trace through
the existing completion snapshot, and the post-cleanup trace. Missing observation
is unavailable, not success. Dropped/saturated counters and truncated open-hash
lists are explicit. Case0005 has no DC lifecycle trace; its RPC transcript times
cannot be substituted for DC identity or reconstructed into a fake event order.

## case0006 scoped early-turn correction

Unlike case0005's missing DC fields, the parent-supplied case0006 trace contains
`created(thinking) -> new_output_lease -> matching done(unmatched) -> RPC final`.
The new regression replays that ordering at the actual snapshot-send seam. Turn
tracking now starts immediately before this admitted request's first snapshot
chunk dispatch, after scope checks. Opening the output lease no longer discards
those IDs. Playback still waits for complete context delivery, normal output
admission and marker commit; early done alone cannot complete without real media.
Done-before-created, unknown IDs, wrong roles, previously admitted IDs (including
nonfinal turns from an older question) and revoked scopes do not satisfy completion.
A per-responder identity history retains at most4096 SHA256 hashes without eviction.
At capacity a new ID fails the channel closed with `response_turn_identity_capacity`,
muting output without retry or automatic session creation. This is an explicit
local memory protection, not a claimed provider or content limit. The history
count/limit are observable; raw IDs are not exposed. IDs and current host scope are local
correlation, not provider request IDs or proof that every delayed new ID belongs
to a particular source revision. No model-consumption ACK is invented.

QA additionally saves `speech_completion_before_stop` synchronously before the
supervisor cancels/stops its operation. This includes the whole-work120s deadline,
so cleanup cannot replace the pending state with `closed`. The existing per-phase
timeout snapshot remains separate. These additions do not fix the case0006 answer
content or prove that case0005 had the same race.
