# B canonical user input repair and WSL baseline comparison

PR37 reached real B decoder/ready without its earlier worklet failure. Parent
host observations then showed no host question, with user done and unassociated
assistant observations. Those assistant journal entries are not user input or
proof that a tool-backed question was admitted. The actual case's complete B
canonical event trace was not available; A text is not substituted for B.

## Confirmed missing supported path

The pinned Codex source emits canonical history before flat transcript mirrors.
Final-only input is supported: core creates a user history delta before emitting
the flat done. Previously B ignored all canonical item deltas and accepted only
flat user deltas. The new public receive-loop regression fails against PR37 and
passes after repair; local PCM activity/quiet still controls question boundaries.

- [Ordered delivery](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/core/src/session/mod.rs#L2522)
- [Final-only history delta](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/core/src/realtime_history.rs#L372)
- [Canonical method definitions](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/app-server-protocol/src/protocol/common.rs#L2012)
- [WebRTC startup session ID](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/core/src/realtime_conversation.rs#L1587)

The router binds the same thread's exact-v3 session from the current issued
startup notification, even before media-ready. A transcript item cannot supply
its own session authority. Only known open user items contribute their deltas;
assistant, foreign, unknown, completed and replayed bindings cannot authorize a
question. Canonical mode ignores flat mirrors. A valid flat-first legacy stream
remains explicit and is never combined with a second stream. Done/completed text
is never inserted as a substitute or a framing boundary. New IDs with identical
text are not globally deduplicated: repeated spoken questions remain valid.

Identity/tombstone memory is bounded at4096; exhaustion uses the existing B
notification failure/cleanup route. Diagnostics retain at most16 fixed events,
character counts and relative timing, with no text, provider IDs or session IDs.
The previously omitted local activity/freshness/quiet snapshot is now projected.
Wake phrases, PCM freshness/RMS/quiet, authorization, source fences, A capture,
output policy, model quotas and owned Stop are unchanged.

## Validation and retained failures

- **528 controlled offline tests passed**, no skips; not full original discovery.
- **21 targeted input tests passed**, including final-only canonical input,
  actual RPC receive-loop routing, mirror isolation, stale-first session,
  startup-before-ready, wrong version, tombstones, malformed metadata, privacy,
  PCM freshness, repeated questions and capacity failure.
- **11 RPC shapes validated** against schema exported by the actual installed
  `codex-cli 0.159.0-alpha.3`, in an amd64 network-none container without account
  mounts. This validates shapes, not a live model exchange.
- Expanded142-test run:134 passed,7 failures/1 error in the8
  `test_response_runtime` browser-dependent cases, which stopped at outer
  `browser_open / TargetClosedError`. They did not reach the candidate router;
  they remain failed rather than being counted as passed. No sandbox change.
- Independent Spec review found two issues, both reproduced and fixed: an item
  could self-anchor the session, and invalid flat text could select fallback.
  Final independent Spec/Standards reviews found no confirmed remaining defect;
  this is not native GitHub review or actual Oracle validation.
- Earlier test-only races/schema-fixture errors are retained in the machine
  evidence. Admission count preceded asynchronous context preparation; the test
  now waits for actual answering state without increasing its deadline.

[Machine evidence, exact hashes and WSL comparison](b-question-input-20261102.json)
links the compressed raw red-baseline and regression logs. The source remains
unmerged; no new image, host update, Join or model request was performed.

## Successful WSL baselines are distinct

| Evidence | What it supports | What remains unknown |
| --- | --- | --- |
| HANDOFF61 / RESULTS138, seventh round | Human heard wake responses and no response without wake; provider handoff/tool admission recorded | Exact successful source snapshot; cannot equate with today's host-question controller |
| HANDOFF110 / RESULTS671, Oct28 native-track | Positive human hearing report and normal Stop/cleanup; cached image `a22d154c…3252` | No revision label/source hashes linking that image to a public commit |
| Oct29 no-PLC receipt | Installed module hashes match `bf0a108`;11 synthetic media tests, Chromium155/host network | No human remote-hearing acceptance for that newer path |
| Oct17 controlled Native receipt | Same model/CLI,13 questions,197 `input_transcript.added` events | No Teams/physical audio; does not cover final-only input |

Oct29 no-PLC and Oracle PR37 have byte-identical `b_question.py`. Public
`push_rx`, `_pump_rx`, `_poll_questions` and `start` are unchanged across the
three inspected refs. No evidence supports a newly raised Oracle wake/quiet
threshold. New output/no-PLC/history/CSP paths must be distinguished from input
ASR routing. amd64/host-network versus ARM64/rootless-bridge is a real environment
difference, not by itself an established cause.

Remaining precise baseline needs: source hashes from the successful native-track
image, same-run B delta/done/canonical metadata and local PCM activity/freshness/
quiet timing. Do not reconstruct missing events, read credentials, or treat the
later no-PLC image as if the earlier human test had validated it.
