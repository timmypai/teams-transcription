# B output continuity: synthetic counterexamples, not a live quality verdict

Base: PR39 merge `b9a9f375e4b94e5e7b2f3372f2c4a511bce8f289`.
The user heard a reply but reported uneven audio. The first commits added
measurements only. The latest source now integrates the shared write pacer
described below; PLC, quiet/authorization rules, provider and the running Oracle
meeting remain unchanged. Nothing in this branch has been deployed.

## Established facts and limits

- Deployed configuration is Codex CLI `0.159.0-alpha.3`, Live
  `gpt-live-1-codex`, WebRTC, with two project-owned no-PLC decode/render stages.
- Host reported `save_audio=false`. There is no waveform available to listen
  back to. A decoded 306240-frame / 6.38-second positive-energy window is not
  proof of continuous audible speech. No new recording was enabled.
- Official Codex [pinned playout source](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/voice-host/src/playout.rs)
  uses a 60ms jitter buffer and PLC, plus conversion/resampling. Our path differs.
  This reference is not a complete description of private GPT Live internals,
  nor proof that the installed CLI binary uses that exact commit.
- In [fixed WebRTC source](https://webrtc.googlesource.com/src/+/6d433affd7889e8e242dc59d8106b78c0c4062fd/audio/channel_receive.cc),
  transformed encoded audio enters NetEq afterward. Our encoded receiver uses
  WebCodecs/generator rather than returning packets to the native encoded
  writable. We cannot assume native jitter reordering/PLC has already occurred.
- Chromium's [WebAudio stream sink implementation](https://chromium.googlesource.com/chromium/src/+/400660a8b09d9613712a81db523f2424c631bebf/third_party/blink/renderer/modules/mediastream/webaudio_media_stream_audio_sink.cc)
  has a finite FIFO and discards incoming audio when full. This supports an
  overrun mechanism, but this source revision has not been mapped to the tested
  Chromium build. The synthetic evidence below locates a discontinuity between
  accepted decoded writes and render; it does not identify the precise internal
  FIFO or establish the cause of the live Oracle complaint.

## Measured synthetic cases

Sandboxed local **amd64** Chromium `156.0.8078.47`, cached immutable image
`sha256:08020a8a9da4dde8e2495cd57868e575dd2ad722de29de910c32c2a3be000076`,
with current production Python/JS mounted read-only. This is not a newly built
image, ARM validation, real RTP/ICE, or an end-to-end Teams test.

Each case contains 16 independently reference-decoded synthetic Opus packets,
20ms / 960 frames each, at 48kHz mono. PCM stays in bounded page arrays and is
zeroed in `finally`; only JSON and compressed JSON/log evidence are retained.
The push fixture uses fixed timers independent of reader demand, but on the
same JS thread. It is not a model of real network arrival. Scalar timing and
analyser snapshots can perturb timing. No observer overhead is subtracted from
latency or assumed independent of other measurements.

Final block-alignment run:

| Case | Decoded packets | Exact rendered 128-frame blocks | Interpretation |
| --- | ---: | ---: | --- |
| paced | 16/16 | 90/120 | Interior source frames 5760..9599 were not found; flanking source blocks 5632 and 9600 are adjacent in render (128-frame delta). |
| burst, 3 packets/60ms | 16/16 | 59/120 | More interior blocks absent; first and last payload identity found. |
| reordered 0,2,1,3… | 15/16 | unknown | Production clock discards late packet 1. Different Opus history invalidates comparison against the all-packets reference. |
| intentional 120ms writer stall | 16/16 | unknown | No tail identity found in the retained render window; this is not evidence that all audio was lost. |

All four cases completed owned close/decoder invalidation/track stop/context
close and repeat-close checks. The paced and burst cases demonstrate that
decoded counters and resolved writes are insufficient continuity evidence.
They do not prove that real Oracle packets arrived in these patterns. Results
varied between retained runs, so these counts are examples, not a universal
loss rate. The pre-existing demand-paced mono/starvation controls retained
16/16 complete packets; the injected starvation gap remains a separate case.

## Measurement corrections and preserved evidence

Independent Spec review found two problems in the initial fixture: comparing
the reordered case to a different Opus history, and calling timer-enqueued
reference prefixes consumed source evidence. Both were corrected: reordered
render identity is explicitly unknown, and quality cases do not emit that
source trace. A no-tail capture also emits no missing-block claims.

An intermediate capture took a snapshot before the tail played. Final aligned
cases wait for full tail identity. Exact packet search supplements coarse chirp
correlation, which can choose the wrong period. Unit tests reject partial
samples and wrong-channel matches. Per-packet matches do not prove an exhaustive
absence of duplicates or all kinds of distortion.

Original/intermediate failures are preserved under `audio-continuity-20261102/`,
with hashes in `raw-manifest.json`. Do not replace those reports with the latest
result. The writer-stall unknown and reordered reference limitation remain.

## Reproduction and validation

Use `tools/decoder_continuity_check.py --output /tmp/continuity.json` in the
existing approved sandboxed browser environment. Optional `--case` selects one
fixed experiment. Exit zero means measurement/cleanup completed, **not** sound
quality passed. `tools/decoder_history_check.py` remains the separate unchanged
11-case policy/history qualification gate.

For the local cached image, stage only this public package and the three files
`decoder_continuity_check.py`, `decoder_history_check.py`, `decoder_audio_check.js`
under a read-only `/candidate` (directories0755/files0644). Set
`PYTHONPATH=/candidate:/candidate/tools`. The image has an editable installed
package pointing at `/opt/meeting`; in the isolated fixture process set
`teams_transcription.__path__=['/candidate/teams_transcription']` before importing
the tool. This explicit source override is recorded, not installed-image proof.

Container constraints used: network none, read-only root, UID10001:10001,
cap-drop ALL, no-new-privileges, unchanged `docker/chromium-seccomp.json`, 512
pids, 2 CPUs, 2GiB RAM, 256MiB shm, 512MiB private tmpfs. No auth/home/data mounts,
host ports, microphone permissions, model requests, or Oracle connection.

Validated: 24 focused tests, 11 actual browser history/policy cases, and the
552-test controlled offline aggregate (zero failures/errors/skips; see machine receipt). The latter is not full
discovery. Independent Standards review found no confirmed defect; Spec
findings above were corrected and rereviewed. Native GitHub review is separate. A final real-browser writer-stall regression
confirmed no-tail means zero anchors and null overall sample comparison, rather
than claiming 16 packets of mismatches.

## Initial diagnostic remaining work (before production source integration)

At that initial stage no playback fix was claimed. A safe candidate must address burst admission versus
native playout consumption without inventing source quiet, adding an unbounded
answer queue, or replacing real missing samples with PLC. Test its startup,
steady burst, interruption and cleanup against these preserved counterexamples
before a new release. Live quality still needs correlated downlink timing or
explicitly authorized isolated synthetic capture; current idle counters cannot
serve as remote listening evidence. No additional session or recording was
started. Existing live model budget remains 28/50, independent of these local
offline cases.

## Subsequent isolated clock-lead candidate (historical measurement stage)

The same fixed encoded audio was rerun in one sandboxed image as a baseline
versus the opt-in `--candidate clock_lead_40ms` fixture transform. This limits
estimated scheduled-ahead duration using `AudioContext.currentTime`, waiting
in 5ms increments before writing. It adds no PCM FIFO or PLC and does not
participate in source/render quiet, completion, drain or authorization.

| Input | Baseline full packets / exact blocks | Candidate full packets / exact blocks |
| --- | --- | --- |
| paced | 13/16; 97/120 | 16/16; 120/120 |
| three-packet burst | 8/16; 59/120 | 16/16; 120/120 |
| writer stall | 13/16; 97/120 | 16/16; 120/120; measured 4800-frame all-zero gap after packet7 |
| reordered | 15/16 decoded; render unknown | 15/16 decoded; render unknown |

All eight contexts/decoders/owned tracks completed cleanup. The intentional
stall remains audible missing-time output (100ms zeros), not invented speech;
the candidate does not recover genuinely late packets. Precise sample matches
show an improvement for this synthetic source and these arrival patterns, not
a live Oracle quality or ARM qualification. No additional model/meeting call.

Independent reviews confirm this is **application-level clock pacing**, not
native consumption ACK or a hard bound on the hidden native FIFO. Only the
currently pending decoded packet is owned by this loop; this does not bound
the entire upstream browser/codec pipeline. Clock advancement may include
underflow zeros. Consequently the candidate is kept exclusively in the test
tool, not wired into production. Its added write-admission wait, cancellation,
both receiver stages and ARM need review before integration. The earlier
description of a blanket B no-custom-pacing prohibition was too broad; see the
precise contract review below.
The remaining-work requirements above still apply.

## Precise contract review and minimal integration design

Both independent reviewers corrected their initial overbroad interpretation:

- `AGENTS.md:29` prohibits clock estimation for **A**, not B.
- `AGENTS.md:31` prohibits B Python PCM, application FIFO and BufferSource
  scheduling. This candidate adds none of these.
- `AGENTS.md:86` requires native media/WebRTC pacing and transparent resource
  bounds; an advisory native-clock write-admission wait must be described
  honestly, but the text does not ban every JS wait.
- `docs/B-LIVE-NATIVE.md:114` retains one packet decode/write/close before the
  next read. `:116` describes the current version's lack of buffering settings;
  it is not a permanent security prohibition. No new public buffer setting or
  startup prefill threshold is proposed.

This is a source/architecture implementation difference, **not new security
authority** requiring another account/credential/network approval. Existing
source-fix authorization suffices to prepare and review it. The candidate must
not be marketed as a native consumption ACK or hard FIFO cap.

Minimal integration design (subsequently implemented in the source section below):

1. One shared helper in `b_no_plc.py`, used by source and target generated
   receivers after their existing startup handshake. Keep received RTP
   timestamps, packet codec/format validation and decode sequence unchanged.
2. Before a real write, estimate submitted duration ahead of the running
   `AudioContext.currentTime`. Wait when it exceeds `max(40ms, this packet's
   duration)`. Recheck ownership/open state after every await. No PCM copy,
   additional packet queue, per-packet flush, replay or fabricated samples.
3. Tie the single owned wait to `close()` so cleanup wakes it immediately;
   preserve the first failure and prevent late writer completion reopening the
   receiver. Test both generated paths and clock/writer cancellation.
4. Leave source/render energy, quiet, native turn completion, mute/withdrawal,
   meeting/context fences and `drained=false` unchanged. Update only B's pacing
   description and the local-only backpressure comment. No changes to
   `b_response.py`, `b_context.py`, `response_runtime._AsyncStore`, model setup,
   A, UI, authority, hooks or network configuration are needed.

User-visible tradeoff: burst submission becomes slower, so latency can increase;
timer descheduling can still produce missing-time zeros. No first-answer or
end-to-end latency maximum is promised. Clock lead does not prove consumption.
If a strict acknowledged, loss-free handoff is required, existing JS APIs do
not provide one. Application PCM pull/FIFO or PLC would change the explicit B
contract and needs a separate decision; neither is part of this first proposal.

## 10-second, slow-consumer and cancellation measurements

Seven additional sandboxed amd64 browser cases ran: baseline and candidate
10-second paced, three-packet jitter, and deliberately slow30ms writes, plus
candidate cancellation during its clock wait. Same CSP/permissions/sandbox;
no model, meeting, recording or external connection.

| Case | Baseline ingress-to-write maximum | Candidate maximum | Read-sampled offers-minus-reads peak, baseline/candidate |
| --- | ---: | ---: | ---: |
| 500 packets paced | 133.7ms | 168.4ms | 5 / 7 |
| 500 packets jitter | 138.8ms | 161.8ms | 7 / 7 |
| 500 packets,30ms consumer | 5569.2ms | 5596.9ms | 179 / 180 |

These are observations, not causal estimates of added latency or ear-end
latency. Source delivery and actual timer times are preserved. The startup
checkpoint contributes to initial delay. All six long cases decoded/wrote
500 packets /480000 frames; final16 packets /120 blocks matched exactly.
Earlier rendered audio was not retained: whole-sentence matching remains
`null`, not passed. Reordered recovery remains unknown/unimplemented.

The slow case shows the candidate does not repair sustained throughput below
ingress rate. The peak above is sampled **after read**, not an exact fixture
queue peak or browser queue cap. Its original raw field `max_queued_packets`
was subsequently renamed `offered_minus_read_sampled_peak`. The finite fixture
offers only500 packets. It does not implement Chromium's encoded-source drop
behavior or demonstrate a bound on the entire real upstream pipeline.

Cancellation caught a clock wait after2 writes; subsequent write calls were0.
Owned track/context/decoder/reader/repeated-close cleanup passed in all7 cases.
Maximum container cgroup peak was387567616 bytes (~370MiB) under2147483648 bytes.
It includes browser/codec/fixture and earlier cases in that container, not JS
heap size or proof of long-run leak freedom. Explicit long-case arrays were
1920000 reference bytes,768000 capture bytes,80660 encoded payload bytes and
262144 history-copy bytes, plus runtime objects/native allocation. Only scalar
JSON was retained; PCM was zeroed and the container removed.

## Production source integration and full-sentence verification

Current `b_no_plc_startup.NO_PLC_RECEIVER_JS` applies `with_clock_pacing()` after
the existing startup transform. The target history receiver inherits that same
shared helper; no shared context files were changed. The default source path
now uses cancellable `ready()` and `write()` calls. `close()` wakes the one owned
wait, retains first failure, and prevents late completion from writing again.
The original receiver still closes each AudioData in `finally` and retains its
original input/render counters. The helper never rewrites audio timestamps or
uses its estimate for quiet/drain. This is a source fix, not an Oracle release.

The full-sentence verifier uses the existing analyser history, no new graph
node. It stitches only unique, sample-exact overlapping windows into an18-second
bounded page-local array. Raced read windows spanning more than256 clock frames
are skipped, then the next window must still prove exact overlap. Long gaps,
ambiguous/nonfinite samples or missing overlap are unknown, never invented data.
One initial slow-case overlap unknown and the initial file-permission failure
are preserved. Independent reviewers also caught a tolerance-prefilter mismatch
and NaN comparison; both have explicit counterexample tests and were corrected.

Final actual sandboxed amd64 browser comparison uses500 packets /480000 samples
per long case, independent reference decode, unchanged received timestamps and
the **real integrated helper**, not the earlier fixture-only candidate:

| Input | Old complete packets | Integrated complete packets | Whole contiguous reference / extra signal |
| --- | ---: | ---: | --- |
| paced | 496/500 | 500/500 | integrated exact; outside payload zero |
| three-packet burst | 496/500 | 500/500 | integrated exact; outside payload zero |
| two speech regions with300ms encoded silence | 498/500 | 500/500 | integrated exact including silence; outside zero |
| deliberate30ms consumer | 500/500 | 500/500 | both contain extra all-zero gaps; continuity **not passed** |

The two-speech-region case reuses the same receiver; cancelled receivers remain
closed, while a new owned helper starts without the previous schedule (unit
regression). Actual browser cancellation during a clock wait had zero subsequent
write calls. All9 matrix cases completed owned/repeated cleanup. All11 existing
policy/history browser qualification cases also passed with current source.

Integrated encoded-arrival-to-first-observed-full-render-packet delays were
198.8–251.4ms paced,188.0–273.6ms burst and186.3–228.6ms for two regions. These
include observation polling and are not Teams/network/device/ear-end latency.
The slow case reached5693.4ms versus5692.7ms baseline; its275 zero gaps
(264960 frames) versus273 (262080) are preserved, not called an improvement.
No packet loss, PLC or PCM flush was used to conceal the slow-consumer limit.

The active timeline observer itself had a21.7ms maximum operation and241ms
cumulative cost in the paced run; costs can overlap other work and are not
subtracted. Maximum observed container cgroup peak was419123200 bytes (~400MiB)
under2GiB. Timeline PCM storage plus previous window is3587072 bytes, additional
to the earlier fixture arrays. Raw PCM never leaves the page and is zeroed.
These finite runs are not leak-freedom or general real-time scheduling proofs.

Resource/failure boundaries: at most one packet's decoded output remains in the
receiver's existing pending list (maximum5760 frames per Opus packet), plus one
owned clock wait; no answer-sized application queue was added. The browser's
upstream encoded queue is not exposed by JS; no hard capacity/latency claim is
made for it. Sustained overload can still lose native ingress audio or insert
underflow zeros. Reader/writer/clock/format/graph failure stops the owned B
receiver through existing fail/cleanup; unknown cleanup remains fail-closed.
There is no new answer/meeting timeout, automatic reconnect or decoder reset.

Validation:16 new public-pacer seam tests and6 measurement tests; final controlled
aggregate571 tests, zero failures/errors/skips. The initial aggregate's3 errors
were historical startup fixtures importing current source. They now restore
exact `c9d54b7` startup bytes whose hash matches the unchanged sealed manifest;
all original per-file gates remain. The old QA profiles still reject a changed
physical runtime. Independent Spec/Standards rereview found no remaining
confirmed issue. Official GitHub review, ARM/new-image and live quality remain
pending. Prior Draft PR creation was denied and has not been retried.
