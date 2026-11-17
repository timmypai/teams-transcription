# Policy-compatible Teams target decoder candidate

The parent-relayed PR36 observation identifies `worklet_module / AbortError`,
available APIs, a running 48k context, and script-src self/nonce without Blob,
plus Trusted Types script requirements. Cloud did not retrieve the private host
evidence file. Equivalent synthetic CSP reproduces the old loader failure; this
does not prove it is the only possible Oracle failure.

The target now uses native per-channel Analyser history; the source Live receiver
is unchanged. No CSP, Trusted Types, sandbox, permission, network, credential,
quota, authority or meeting stop policy is relaxed. The rejected TrackProcessor
meter is not selected.

The actual Opus/WebCodecs path remains. No native encoded writable/PLC fallback,
Python PCM path, playback FIFO or application scheduling replaces it. A splitter
feeds two mono histories; a 128-frame maximum RMS retains the existing 8-PCM16
noise floor without antiphase cancellation or short-burst dilution. The 32768-frame
history spans about 683ms at 48k. Clock brackets and 512-frame margins only credit
advancing overlapping coverage. Initial/stale/suspended/missing coverage cannot
be backfilled as quiet. The consumer separately requires real source quiet and
clips evidence to the current speech/turn scope.

`target_decoded_input_v2` counters explicitly describe real target decoded input.
Render evidence is a separate versioned interval/boolean. Overlapping window
energy and context clocks are not fabricated as cumulative render PCM;
`activity_frames` is null in this mode. History retention may conservatively
delay completion. It is not remote drain. Design references: [Web Audio
Analyser](https://www.w3.org/TR/webaudio-1.1/#dom-analysernode-getfloattimedomaindata),
[Chromium native history](https://chromium.googlesource.com/chromium/src/+/refs/heads/main/third_party/blink/renderer/modules/webaudio/realtime_analyser.cc).

For startup, a source-side gain starts at zero. Same-format primes/probes stay
behind this gate. A pre-gate native analyser must observe the exact, unique
generator marker and quiet tail before opening. At most three probes share the
existing 500ms startup bound; this is not a model retry or meeting deadline.
Stop first closes the gate. Real timestamps remain unchanged. The new receipt
`native-history-gated-v2` does not claim old worklet ACK, exact primer consumption
or remote drain.

[Summary/checks/hashes](decoder-history-20261102.json) ·
[Exact synthetic logs, gzip JSON](decoder-history-raw-20261102.json.gz).

Executed locally: amd64 Chromium 156.0.8078.47, sandbox enabled, network none,
non-root UID10001, read-only filesystem, existing seccomp, drop-all caps, 2 CPUs,
4GiB. No private mounts, credentials, microphone permission, meeting or model.
The current source archive overlays the existing local test image; this is not
a patched installed-image or ARM result. Owned contexts/containers were cleaned.

| Final fixture | Result |
| --- | --- |
| Old worklet / strict equivalent CSP | expected module AbortError |
| Old worklet / synthetic Blob-allowed control | constructs |
| New mono / stereo / delayed packet | 16/16 complete packets each; no extra probe signal |
| Voice then decoded silence | actual NativeTrackOutput trace replay completes; quiet 0.810667s; activity frames null |
| 220ms stall | historical short sound remains active |
| 1000ms stall | unknown coverage, quiet zero; later verified quiet recovers |
| Antiphase stereo | active, not false quiet |
| Invalid timestamp / early Stop | first error retained / cleanup verified |

The fixture uses real codecs/native graphs and a synthetic encoded-stream
receiver, not real RTP/Teams. Reference decoding follows the same packets. PCM
stays in-page and is not exported. Capture passively waits for actual tail identity
instead of assuming a fixed wall-time cutoff; this cannot create missing samples.
The full voiced range still compares independently. Consumer replay uses actual
browser render/target counters and independent reference source data; its sink
is synthetic.

**499 controlled offline, 71 focused and 41 full Native regression tests passed**
(overlapping suites, not a summed total). Independent Cloud spec/safety reviews
found no unresolved confirmed defect after corrections. Actual stats double-poll,
negative frame bounds and suspended stale quiet have red/green regressions.
Input activity cannot substitute for render activity.

Failed intermediate results remain in the archive: rejected sideband startup,
first-packet loss, initial marker failure and premature tail capture. Intermediate
source versions were not all committed; their hashes/raw results are retained
without pretending to reconstruct old commits. Final receiver/consumer/fixture
bytes match the exercised source archive.

The existing manual image/Compose smoke now runs the same policy check against
the image's installed package, with public tool/fixture bytes in tmpfs and no
host-source override. An unpatched-image negative control correctly fails.
The positive patched-image / ARM check still requires the next approved build.
Workflow triggers and permissions are unchanged.

No merge, build/publication, Oracle update, Join, Stop or extra model request was
performed during repair. Applying the candidate requires normal review/merge,
ARM checks, immutable release evidence, and the single host owner's ordinary
helper release-pin update/deployment. No database migration or new host authority
is needed. Current live PR36 remains untouched.

Actual Oracle B readiness, sender/RTP, human hearing, A transcripts and wake
recognition remain separate acceptance. Issue20 playback continuity is not closed;
observed gaps are retained, not converted to a pass.

## Native review follow-up

The native GitHub Codex review of `e50bcbb` reported [P2 short-turn activity
loss](https://github.com/paulpai0412/teams-transcription/pull/37#discussion_r4439108286).
The public output regression fails against those original consumer bytes. The
fix keeps activity inside the current output lease, while a new turn still
invalidates both quiet tails. It does not require each turn to have a separate
audio ACK. A generation fence prevents an in-flight pre-notification observation
or the first cumulative source delta from restoring old quiet.

New leases still discard activity. Their activity floor now comes from the native
clock in the existing target track-enable action, rather than an earlier poll or
the later host ACK. Missing/invalid clock receipts pause and fail closed. This is
a local action boundary, never proof of remote hearing or drain. No additional
IPC, permissions, CSP changes or playback scheduling were introduced.

This follow-up passed **507 controlled offline**, **120 focused/Native** tests,
and all **11 amd64 sandboxed synthetic browser** cases. The latter still covers
real codecs/native histories without RTC; the scalar consumer replay keeps
output disabled and does not invent an actual sender-enable receipt. New Node
and public sink tests cover the enable clock contract. The new installed image,
ARM, real target sender and Oracle remain untested. An outer RTC suite could not
launch Chromium because of the existing sandbox helper condition; its six launch
errors are not claimed as passed. The first local container call also failed
before browser startup because its public tar was not readable by UID10001;
that temporary input mode was corrected, with the original failure retained.

Independent Cloud spec/standards reviews found no confirmed defect in this delta.
Final native GitHub review must be requested against the new commit; the prior
finding is retained and not self-resolved. [Follow-up evidence and exact source
hashes](decoder-history-review-20261102.json), including its linked raw archive,
preserve the red baseline, green results and limits. No merge/build/deployment,
meeting control or model request was performed by this follow-up.
