# PR38 B capture stall: causal counterexamples, not a live root-cause verdict

Base: `73f44ad5eda47abb97c16a969f2d7489934c3470`.
Live reference: Actions `41435156198/1`. No additional meeting/model request was made.

The sections through “Validation and limitations” record the test-only
checkpoint `5487b9015461734de79ca251898b50fcf81ab095`. The follow-up correction
and newer host evidence are recorded separately below; original receipts remain.

The host reported B ready, fanout offered=10/rejected=10/accepted=0,
peer-push=0 and no B transcript notifications, while A continued receiving.
This establishes missing B input, not an ARM-specific or provider transcript failure.

## Reproducible distinctions

`PYTHONPATH=tests:. python -m unittest test_response_capture_fanout -v`
passed three deterministic, browser-free tests. They traverse the actual
`NativeMeetingMedia._observe` -> packet mapping -> worker `on_audio` ->
`ProductionResponseController.push_rx`, with controlled external interfaces.

1. Ten packets while the controller is starting are rejected; once it is
   sleeping, forty more are accepted. Startup rejection alone does not latch.
2. One injected policy `TimeoutError`, followed by successful evaluations,
   produces exactly 10 offers, 10 rejections and zero accepted packets.
   The policy remains faulted; the controller still reports sleeping.
3. One explicit policy `False` produces the same counters. These counters alone
   cannot distinguish capability rejection from an evaluation failure.

The receiving native implementation and controller startup are fixtures. These
tests do not validate browser JS permissions, real PCM freshness/quiet, ARM,
remote audibility or the actual cause of the live policy/capture loss.

The existing fail-closed policy is retained. Removing its foreign-sender,
identity or timeout checks would not be a valid repair. The confirmed program
defect is that a permanently blocked observation can remain invisible while B
still reports sleeping; no production fix is claimed in this diagnostic change.

The relevant worker/browser/native-media/response-controller/dual-policy files
are unchanged between PR37 `5eae43e24fe3b572d2a2d7ca44c2d78298ef59ce` and this base.
Before the first output authorization, policy.binding is null, so the
ownedIntact decoder/replaceTrack validation is not the direct rejecting branch.
No reversed observation-lock/policy-lock dependency was found.

## Required existing live evidence

Two fresh snapshots, a few seconds apart, of only:

- `rx_audio.updated_at`
- `rx_audio.browser.packets`, `dropped`, `context_states`
- `rx_audio.python.incoming_packets`, `invalid_packets`
- `rx_audio.live_transport.native_source.observation_ready`, `failed`, `failure_code`
- response fanout `clock_missing_packets`, `error_packets` and snapshot time

These distinguish browser emission loss, a gate before the worker, and worker
packet/clock rejection. The current persistent projection does not store the
policy's first failure/time or its faulted bit; unavailable evidence must stay
unknown. No existing private text, raw PCM, URL, SDP or credentials are requested.

## Validation and limitations

- New causal tests plus RX statistics and transcript routing: 40 passed,
  zero failures/errors (1.756 s).
- Independent Spec and Standards reviews found no confirmed defect in these
  tests; neither reviewer reran them.
- Existing real-browser dual-policy baseline could not launch Chromium in this
  Cloud executor: TargetClosedError; Chromium reported a misconfigured SUID
  sandbox helper. No sandbox workaround, privilege change or security setting
  change was attempted. This environment failure is not the Oracle root cause.
- No runtime change, deployment, merge, new model/meeting request or quota change.
- Original live and prior failed receipts remain unchanged.

## Follow-up host evidence and fail-closed correction

Host snapshots at 11:36:01 and 11:36:22 UTC show browser packets
14834 -> 15083 with dropped=63 unchanged, Python incoming=10 unchanged,
invalid=0, and native-source packets 64513 -> 65586. Native observation remained
ready, not failed. Fanout remained 10/10/0 with clock-missing/error=0.
This narrows the loss to the observation bridge before worker admission.
Different counter domains are not subtracted to infer loss. The precise first
trigger remains unknown; no historical timeout/rejection trace can be recreated.

The new runtime change addresses the independently demonstrated silent-failure
defect, not a claim that the original input has been restored:

- Policy JS returns a fixed boolean/reason receipt in the existing evaluation;
  Python strictly validates it. The first reason/type and capped host-relative
  time survive later failures. No URL, binding ID, SDP or exception body is saved.
- A policy failure immediately makes B unavailable. One bounded revoke operation
  invalidates the question/scope, attempts owned mute before waiting for task
  cleanup, and prevents new input/questions/output authorization.
- A delayed cancellation regression failed against the pre-order-fix method:
  owned pause was never awaited before the blocked join. It passes with the fix.
- Stop joins revoke outside the controller lock. Starting/stopping races cannot
  announce ready or start a late second revoke. Unknown revoke remains
  `stop_unknown`, with `output_muted=null`; physical mute is not invented.
- Eight saturated scalar callback counters distinguish scope, owner, policy,
  clock and callback rejection before worker fanout. These add no browser IPC.
  A callback return is not provider/model receipt or PCM admission.
- A timeout remains fail-closed, distinct from explicit capability rejection.
  There is no automatic unlatch, reconnection, new channel, retry or permission
  grant. A new owner-admitted lifecycle must revalidate the original conditions.
  Original wake, PCM quiet/freshness, output identity and quota rules remain.

Final local validation: 114 focused tests and 549 controlled offline aggregate
tests passed, zero failures/errors/skips. The aggregate now includes the capture
and policy lifecycle regressions. Independent Spec/Standards reviews found the
mute-before-join issue; it was corrected and rereviewed without remaining
confirmed findings. Reviewers did not rerun tests.

An expanded progress suite also exposed two older whole-dict assertions (12
subtest failures) omitting existing TX `counter_domain`/`render_evidence` fields.
The same failures were reproduced on pristine PR38. They remain preserved and
unmodified; neither their assertions nor the runtime TX evidence were weakened.
The initial aggregate's nine attach-receipt fixture failures were corrected by
updating only the synthetic Playwright return contract, not the safety assertions.

Machine summary and preserved green/red/baseline logs:
`b-capture-policy-20261102.json`, `b-capture-policy-raw-20261102.json.gz`.
GitHub native review, ARM/new-image/browser-policy integration and live recovery
remain separate gates. No Oracle deployment, new model request or meeting
operation was performed for this correction.
