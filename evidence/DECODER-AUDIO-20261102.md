# Native meter audio experiment: reject production substitution

The candidate genuinely decodes synthetic Opus and emits detectable native audio,
but is **not a valid replacement for trusted render quiet**. No runtime/app source,
Oracle setting, permission, sandbox or CSP was changed. No meeting/model was used.

[All four raw JSON experiments](decoder-audio-20261102.json) are preserved,
including failed/missing matches and provisional reporting mistakes, alongside
[35 passing focused tests](decoder-audio-focused-20261102.txt). The previous
constructor-only evidence is unchanged.

The existing local sandboxed Chromium 156.0.8078.47 / amd64 image was used with
network none, permissions empty, read-only root, non-root UID, dropped caps,
existing seccomp and no credential/private mounts. All twelve final contexts and
the owned container were cleaned. ARM64 was not exercised; no Oracle runner or
new execution environment was configured.

## Actual signal path

AudioEncoder encodes a fixed non-repeating 48kHz chirp-plus-tone fixture as 20ms
Opus packets. A synthetic encoded-stream receiver supplies these **real encoded
bytes** to the actual candidate receiver, which uses real AudioDecoder, generator,
WebAudio and native TrackProcessor. An independent decoder processes the same
packet sequence/OpusHead as the reference, including the intentionally dropped
packet. Synthetic frame timestamps are RTP ticks; encoder timestamps are
microseconds. These are not confused or treated as real transport timestamps.

The observer copies bounded PCM in-page and compares it after playback; only JSON
summaries leave the page. Buffers are zeroed and AudioData closed. No real RTP,
ICE, Teams sender, microphone or remote audibility is exercised. The native
encoded receiver writable receives **zero writes** in every codec case, so this
component experiment does not send decoded input into the native PLC path or A.

The optional observation hook, reference decoding and fixture pacing add workload;
the JSON records copy costs and source enqueue intervals, not a claim of zero
interference. Native timestamps have gaps/regressions and are not assumed to be a
perfect sample clock. Sample duplication is not exhaustively verified. Anchor
matching and complete 960-sample, all-channel comparison are separate metrics.

## Final observed outcomes

| Case | Complete packet comparisons | Other result |
| --- | --- | --- |
| Mono | 30/30 | all payload samples matched; this run had no anchor discontinuity |
| Stereo | 30/30 | 2 anchor discontinuities; continuity not passed |
| Delayed packet | 30/30 | captured 7680-frame zero gap between packet11/12; full render coverage unknown |
| Dropped packet | 29/29 supplied | omitted packet not invented; 3 discontinuities |
| Duplicate timestamp | 30/30 | duplicate filtered; 2 discontinuities |
| Timestamp wrap | 29/30 | incomplete capture, preserved as failure/unknown |
| Invalid timestamp | 2 captured complete packets | first error `b_no_plc_rtp_timestamp` preserved |
| Stop early | 2 captured complete packets | queued playback truncated; cleanup passed |
| Observer fault | no payload output | first error `b_no_plc_meter_failed`; startup failed; cleanup passed |
| Stop during checkpoint | no payload output | startup failed; repeated close safe; cleanup passed |

Every successful startup observed a real 480-frame capture after the silent write.
This proves capture activity, **not the identity/playout of the silent primer** or
remote drain. Empty payload comparisons are null, not success. Earlier raw runs
include mono29/30 and stereo28/30 cases; the later 30/30 result does not erase them.

## Decisive quiet counterexample

A separate native WebAudio oscillator schedules sound at context seconds
0.08–0.16. Continuous observation sees nonzero samples. Delaying just the native
meter read from approximately 0.0427 to 0.232 seconds covers that entire sound:
the observer then reports **zero nonzero samples and energy=0**, without an error.
Both paths clean up successfully. The final control observed 3360 nonzero frames;
the prior independently reviewed run observed 3840. Both stalled controls observed 0.

The one-buffer observer can therefore discard sound and later appear quiet. This
violates the production completion contract under a slow observer. It is not
repaired by changing thresholds, adding silence or weakening quiet assertions.
The candidate remains tool-only and is rejected for production selection. Solving
that contract would require a separately verified loss-aware observation design;
it is not a deployable fix in this branch.

The tool's exit 0 / `experiment_completed=true` means the experiment and safety
cleanup completed. Its explicit `candidate_runtime_eligible=false` means the
candidate was rejected; neither field is Oracle/Teams audio acceptance.

## Reproduce

Inside the same existing sandboxed image/security settings recorded in the JSON:

```sh
python tools/decoder_offline_check.py --audio --output /tmp/decoder-audio.json
PYTHONPATH=tests:. python -m unittest test_decoder_offline_check test_b_no_plc_startup test_no_plc_setup_failure test_native_output_attach_diagnostics test_b_document_policy -v
```

Independent Cloud spec review confirmed the quiet rejection. Standards review
identified and verified fixes to anchor/full-packet labels and starvation scope;
the regression corrupts sample 900 outside the true 512:768 anchor window. No native
GitHub review or application release is claimed. Oracle's actual decoder_setup
cause still requires the PR36 target-frame policy/API/first-stage evidence; these
amd64 experiments do not identify that cause. No model budget was consumed.
