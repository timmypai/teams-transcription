# Passive synthetic audio observation candidate

This candidate observes the existing c4090ed shared, question-stage QA. It does
not change its prompt, input, content/RX/cleanup checks, daemon defaults, PLC,
decoder state, queue policy, graph permissions or Teams. It performs no Live test
by being imported. Real meeting links/text are not accepted. The entrypoint
requires `--authorize-live` and validates hashes of the fixed QA/media modules.
It rejects the separate isolated-session QA code; do not mix the experiments.

## Source comparison and interpretation

The fixed official [Codex voice-host playout.rs](https://github.com/openai/codex/blob/3b01b36fa5eb96ba82a776bd3c2fc57f8969181f/codex-rs/voice-host/src/playout.rs)
uses a60ms GStreamer RTP jitter/reordering buffer and Opus PLC, with in-band FEC
disabled. Its bounded ingress and audible playout path differ from this project's
two no-PLC receiver segments. Copying its PLC would change evidence semantics;
this candidate does not do that. Buffering/PLC could be a later separately reviewed
tradeoff, not a proven improvement or a change made here.

This synthetic QA has no Teams sender or second relay. The tap observes only the
GPT receiver's decoded PCM and an additional zero-gain monitor clone of its no-PLC
WebAudio destination. Source callback timestamps use that AudioContext.currentTime;
render sample timestamps use currentFrame/sampleRate of the same AudioContext.
This is a common clock domain, not sample-exact source-to-playout alignment.
Source PCM is retained before the existing scratch clear; no new audio is fed into
the decoder/writer. Render monitoring never enables a physical sink or original
output track. The monitor clone is enabled only through its own zero-gain branch.

[Encoded-transform metadata](https://www.w3.org/TR/webrtc-encoded-transform/)
may expose receiveTime for the last packet of a frame. If absent, receive_ms=null;
only application reader-dequeue time is known. This is not NIC wire arrival.
[Inbound RTP stats](https://www.w3.org/TR/webrtc-stats/) supply reported loss and
jitter when available. Loss may be negative; missing values remain unavailable.
RTP timestamps alone do not distinguish packet loss from DTX or model pauses.

`decoded_silent_frames` means RMS below the declared threshold, not proof of a
natural pause. Render zero/source-stall frames are candidates, not lost-word or
confirmed-underflow measurements. Empty worklet input is counted separately.
`underflow_confirmed_ms` and `audio_quality_verified` remain null/false. Captured
phases include startup/idle; correlate indices, QA receipts and clock-exchange
bounds before interpreting an answer. No semantic speech segmentation is added.

## Bounds, overhead and cleanup

- Max120s, max64MiB PCM total (32MiB per stream), max8MiB metadata including manifest;
  the first reached limit stops/drops observation only and marks it truncated.
- Private new0700 directory; exclusive0600 files. The original QA JSON must be
  outside this directory, so its pre-existing format cannot bypass the observer cap. Interleaved48kHz f32le plus indices
  preserve channels, common-clock observations, packet linkage and byte offsets.
  Source file concatenation does not fill RTP gaps; use its indices.
- Browser PCM queues and worklet storage are bounded. Render has one in-flight
  transferable message credit, not an unbounded message stream. There is no audio
  callback await and no new playback/resampling/PLC queue.
- Source copying, render worklet timing (coarse wall time fallback if necessary),
  graph attachment, export, polling/IPC and host ingest/finalization are recorded.
  These add CPU/message/I/O load and can alter timing. Complete overhead and
  noninterference are not proved. Manifest write itself is outside finalization timing.
- Polling stops after uncertain evaluation; no retries of an unknown JS operation.
  Final drain is at most4 batches/2MiB and at most0.75s within the existing cleanup
  window. Unflushed tails are explicit. Observer task joins owned cleanup; observation
  failure is separate from the original QA result. It can still perturb that result
  through timing; no noninterference claim is made.
- Mute points are host observations after pause returns. Browser/host clock exchanges
  give timing bounds, not exact mute-to-sample attribution. The physical graph remains
  muted throughout. This is not Teams remote-tail/audibility verification.

## Fixed candidate loading for host review

The three candidate modules are `qa_audio_observation.py`, `qa_audio_tap.py` and
`qa_audio_probe.py`. Use the exact commit/hashes in the candidate evidence. Stage
only those files in a new private directory in the existing authorized QA runtime;
verify all byte hashes before executing any. Load them in the above order as
`teams_transcription.<module>` in a **new Python process**, setting __package__ and
sys.modules/package attributes. Do not edit installed media modules or daemon
memory. All other imports come from the already deployed c4090ed image. The wrapper
checks those base-module hashes again before any model start and patches only its
process-local JS constant/classes, restoring them on exit.

Use the committed offline-only `tools/check_qa_audio_candidate.py` in a new Python
process in that existing image. Stage it and the following non-secret manifest with
the three files in the same new0700 private directory. Pin the manifest bytes by
SHA256 `f496f4d9f4df36f8f09129f1f921283ff1b2f6c6c6cab70776b99a6294eaf4ad`:

```bash
python /PRIVATE_CANDIDATE/check_qa_audio_candidate.py \
  --candidate-root /PRIVATE_CANDIDATE \
  --manifest /PRIVATE_CANDIDATE/passive-audio-candidate-manifest-20261030.json \
  --manifest-sha256 f496f4d9f4df36f8f09129f1f921283ff1b2f6c6c6cab70776b99a6294eaf4ad
```

`PRIVATE_CANDIDATE` is the host's new private staging directory, not an application
config change. The checker has no Live-run option. It validates all three sources
before executing any, then only checks the base hashes; expected output has
`candidate_verified=true`, `base_verified=true`, `model_started=false`.
The exact manifest is [here](../evidence/passive-audio-candidate-manifest-20261030.json).
No installation, image replacement, OAuth call, listener or model request occurs.

Offline preflight can import the three verified modules and call
`qa_audio_probe.verify_base()` or `main(['--help'])`; neither starts Live. A later
host-authorized run uses the existing config/CODEX_HOME and a new private result:

```text
qa_audio_probe.main([
  '--authorize-live', '--qa-start-stage', 'question',
  '--config', EXISTING_PRIVATE_CONFIG,
  '--private-result', NEW_PRIVATE_QA_JSON,
  '--audio-observer-dir', NEW_PRIVATE_PCM_DIRECTORY
])
```

Host must independently verify the running source/digest is the approved c4090ed
image, keep maintenance/paused/quiescent, and account for the one shared Native
start under its existing allowance. This branch executes0 model requests. Raw PCM
stays private; share only bounded numeric summaries/hashes. Do not run the candidate
on real meetings. Actual browser/Live audio quality remains untested by fixtures.

## Cloud Actions handoff

User direction confirmed 2026-10-30: deployment, test scheduling and sanitized
result retrieval default to GitHub Actions plus the Oracle repo runner. They must
not require a personal Xiaomi/Windows/other computer to remain online. Cloud Codex
does not SSH Oracle. A failed personal SSH entrypoint does not establish that the
Oracle app or runner is offline. This policy grants no new execution or security
authority.

Read-only checks at this handoff found main still `c4090edb0e8e5db50d8950515522dba733618c51`
and the reviewed observer ref still `d5f729f6db228cc60b67674a031e99bc0545629b`.
The [last deployed job](https://github.com/paulpai0412/teams-transcription/actions/runs/41022722084)
completed successfully at 2026-10-30T07:53:34Z on runner 21,
`dot-vm-a1-teams-staging`, labels `self-hosted, Linux, ARM64, teams-transcription-staging`.
Current runner-list API returned HTTP403 `Resource not accessible by integration`;
current liveness remains unknown. No token/permission substitution or retry was made.

| Existing manual workflow | Can be reused for this candidate? |
| --- | --- |
| `meeting-functional-validation.yml` | Hosted offline source contracts at selected ref can test the candidate without OAuth; cannot inspect the deployed image or run Live. Not dispatched here. |
| `arm64-image.yml` | Main-only build/publish validation; cannot load this unmerged candidate into c4090ed. Rebuilding is unnecessary for process-local observation. |
| `oracle-diagnostics.yml` | Existing fixed host controller can return health/source/maintenance flags and sanitized diagnostics; no candidate loader, QA, ledger or PCM analysis. |
| `oracle-staging.yml` | Existing deploy/rollback only. Do not repurpose for QA or deploy just to run the observer; successful normal deploy releases maintenance. |

At the d5f/91a5 handoff there was **no `oracle-qa.yml` or installed QA bridge**.
The root-owned job-start hook accepts only the two existing Oracle
workflows from main, and a controlled local test rejects `oracle-qa.yml`. This
branch did not change that hook or install a controller. See the later prepared
implementation below; source and offline fixtures do not establish working cloud QA.

The smallest subsequent implementation is one separately reviewed, main-only
manual `oracle-qa.yml`, modes `offline` and explicitly authorized `observe`, with
the existing staging environment/labels, concurrency `oracle-staging`,
`cancel-in-progress: false`, timeout at most 5min, and a fixed root-owned host bridge.
No PR code or checkout executes on the VM. Candidate fetch uses only the exact
reviewed d5f SHA and contents hashes, not the moving branch, arbitrary URL/path or
shell input. A job-scoped `contents:read` token, if needed to fetch those public
source bytes from this private repo, stays outside the app and artifacts; no new
PAT or OAuth is required. Hook allowlisting/bridge installation is a **pending
security configuration change**, requiring its own target-specific review before
any job is dispatched. Do not hide the QA call in the allowed diagnostics workflow.

The bridge must inspect the existing rootless app container and exact digest
`sha256:7742843eb5810bb041a2e10214e61efc0da6790dba9863c2eaa53c42fb1b1828`,
source c4090ed, healthy/paused/quiescent with maintenance still set. It must reject
active meetings/leases, pending summaries or unknown cleanup. Stage only the
three fixed candidate modules, manifest and reviewed offline checker as private
files for a new process in that existing container. Use its own existing
`CODEX_HOME=/data/codex`; never read/export/copy the OAuth file. First run the
offline-only checker command above; require all hashes/base validation and
`model_started=false`. Offline mode must end here, with no quota reservation or
model call. Do not reinstall dependencies, replace image/media files, restart the
daemon, clear maintenance or enable physical output.

### One global budget and host lock

Parent/host handoff reports 7/50 used through case0007, remaining43; this task made
zero calls. That is a historical checkpoint, **not a newly inspected ledger**.
The authoritative host ledger path, existing lock and case0007 receipt hash have
not been provided to this cloud task. Do not initialize a second ledger or assume
missing files mean zero usage. Reconcile/import that checkpoint once with the
host owner under the global lock, preserve old receipts and record their hash.
`deploy/qa_cloud_contract.py` provides read-only **offline handoff contract**
validation for a proposed ledger adapter: checkpoint used 7 / limit 50 / through-case 7,
caller-pinned historical receipt SHA256, consecutive consumed slots and unique
Actions run/attempt identities. It accepts no reset/refund and blocks exhausted
or unresolved slots. It does not migrate/write a ledger or claim host validation.
`preflight(...)` always reports `live_ready=false` and lists missing integration,
even when supplied fixture data passes. Do not substitute those fixtures for
fresh host observations or proof that a lock is held.

Actual observe execution must hold the existing controller flock as well as the
same host-global QA flock across preflight, quota reservation, start, complete
owned cleanup and receipt commit. Acquire nonblocking in that order; no waiting
job can overlap deployment, diagnostics or another test. GitHub concurrency alone
does not cover CLI/legacy writers. All such writers must use the same QA ledger/
lock or be paused for the handoff; otherwise refuse. In-container probes must
also honor the app store lock/maintenance checks without holding that SQLite
store lock for the whole Live session. Keep maintenance/paused in place.

Before any possible Native start, atomically persist/fsync one consumed slot
with run/attempt + exact candidate/image identity. Never auto-refund on timeout,
crash or failed QA; uncertain start/cleanup blocks subsequent runs pending
reconciliation. Record completion in the same ledger. Duplicate/re-run of that
run/attempt must not start again. Shared question-stage observation uses at most
**one** same-model Native start for its two questions; do not use the separate
two-session isolation candidate or add retries. A subsequent test needs explicit
valid execution authorization; offline contract success is not authorization.

### Safe result retrieval

`qa_cloud_contract.public_result(result, run_id=..., run_attempt=...)` projects the
real QA return value to fixed booleans, two bounded question/RX numeric summaries,
observer counters and fixed filenames' SHA256/byte counts. Unknown strings,
exception bodies, IDs, paths, transcript/answers, URLs, OAuth and raw audio are
discarded, not masked. Quality/Teams/noninterference remain unverified. It does
not recompute or loosen the base QA acceptance logic and cannot prove input
provenance. A trusted bridge must pair it with independently verified source/run/
image/ledger linkage and separate fixed operation/cleanup status, including when
the child is killed or never returns.

The future workflow uploads **only one projected JSON and fixed bounded bridge
status**, with exact filenames, 7-day retention and an always step. Never upload
the private QA JSON, CLI stdout/stderr, `/data`, Codex home, PCM, indices/events or
an entire temp directory. Keep raw synthetic PCM private under the existing caps;
do not copy it to runner logs/summary. Only safe fixed result fields enter the
Job Summary. Cloud Codex can then retrieve the artifact through GitHub API and
verify artifact/run/source/digest/hash linkage, without personal SSH. Cancellation
must trigger bounded owned cleanup; unknown cleanup leaves the quota reservation
and interlock intact. The later prepared source below is **not yet installed or
exercised on Oracle**; the original offline helpers alone did not implement it.

## Prepared dedicated workflow and bridge

The source now includes `.github/workflows/oracle-qa.yml`,
`deploy/qa_cloud_bridge.py`, and `deploy/qa_container_entry.py`. This is prepared,
offline-tested code, not an installed/merged/dispatched workflow. Existing hook
policy remains unchanged and rejects this new workflow until its exact allowlist
entry is reviewed and installed. No VM or security configuration was changed.

The workflow is manual, main-only, staging-only, no checkout, `permissions: {}`,
same `oracle-staging` concurrency, no automatic cancellation, max5min. Default
`operation=offline` calls only the fixed installed bridge. A uses `offline` /
`observe`; B separately uses `isolated-offline` / `isolated`. Both Live operations
require explicit `confirm_live=true`; that UI input does not replace real task
authorization or the global quota. The fixed candidate is still d5f, not the
moving work branch. Neither branch push nor PR triggers this workflow.

The bridge's offline path takes the existing nonblocking controller lock, uses
the existing controller's rootless metadata/volume probe, validates the fixed
healthy/paused/maintenance/idle c409 image, and executes a **new private Python
process** as existing container UID10001. It stages only root-owned/hash-pinned
candidate bytes in `/data/qa-cloud/<run>-<attempt>-offline`, then runs the original
offline checker. No config load, OAuth access, synthesis or model start is needed.
The checker is the same hash-pinned candidate loader already reviewed. No ledger
is required, created or modified in offline mode. All Python subprocess/library
stdout/stderr is suppressed; the only output is the fixed checker receipt.

For observe, the bridge first repeats that offline check, then requires a reviewed
root-owned `qa-authority.json`, the **existing** private global lock and reconciled
persistent ledger. It re-reads runtime under both host locks, preserves ledger
history by content hash and fsyncs reservation and interlock **before** executing
one shared Native start. The container entry invokes the unchanged c409 QA with
the three observer modules, fixed question/legacy settings and existing config/
CODEX_HOME. The original QA JSON and capped source/render PCM stay private.

Only verified owned cleanup permits committing `cleaned`, writing a receipt hash,
and removing this run's interlock. A failed content assertion stays failed even
after clean cleanup. Timeout/cancellation/unknown child or cleanup retains the
consumed slot and persistent `qa-cleanup-interlock.json`; no automatic retry,
refund, audit deletion or reconciliation operation is provided. A small proposed
controller guard rejects deploy/rollback/recovery/create-diagnosis while that
marker exists; read-only diagnostics remain available. This guard must be
installed **before** observe is enabled. Killing the Docker exec CLI does not
prove the in-container QA or remote session stopped. Its original owned deadlines
still apply, but an unknown outcome needs explicit host reconciliation.

### Exact installation delta, not new blanket privileges

Already within the runner UID's authority: existing rootless Docker socket,
existing controller health/Compose/volume metadata methods, private state writes,
`docker exec` as UID10001 in this same app, and use of this deployment's existing
OAuth from that app process. Rootless Docker ownership already permits these;
the bridge is not a new sandbox or a grant of sudo/root Docker privileges.

Pending administrator-owned file installation (using an approved cloud management
path; no requirement for a personal computer to remain online):

| Source | Fixed installed target / mode |
| --- | --- |
| `deploy/qa_cloud_bridge.py` | `/opt/teams-staging/bin/qa-cloud-bridge`, root:root0755 |
| `deploy/qa_cloud_contract.py` | `/opt/teams-staging/bin/qa_cloud_contract.py`, root:root0644 |
| `deploy/qa_container_entry.py` | `/opt/teams-staging/bin/qa_container_entry.py`, root:root0644 |
| reviewed `deploy/stagingctl.py` | replace existing `/opt/teams-staging/bin/stagingctl`, root:root0755, retaining old source evidence |
| d5f's three observer modules, manifest, offline checker | `/opt/teams-staging/qa-candidate/`, root:root0755 directory, root:root0644 files |

The only proposed hook policy addition is this exact new case, alongside the two
existing allowed main workflows (no branch wildcard or arbitrary workflow):

```bash
paulpai0412/teams-transcription/.github/workflows/oracle-qa.yml@refs/heads/main) ;;
```

This is a security policy change requiring explicit review before host install;
the source hook is deliberately **not** changed here. No new user, group, sudo,
daemon socket, firewall, listener, credential, IAM or OAuth grant is requested.
The root-owned bridge/support/candidate installation is a normal trusted-code
deployment using existing administrator access, not a request to grant runner
additional persistent capabilities. None was performed by this task.

Observe authority file contract (no generated example state or fake checkpoint is
committed): `ledger_path` and `lock_path` must refer to distinct **existing private
direct children** of `/srv/teams-staging/state`; `checkpoint_sha256` must pin the
actual historical receipt through case0007; `legacy_writers_coordinated=true` is
an operator assertion after all legacy test writers use the same lock or stop.
The bridge validates this root-owned metadata but cannot prove the assertion.
Different legacy layout/format requires a reviewed preserve-history adapter;
unknown paths/hash/format stop observe, not initialize or reset usage. This is
the concrete outstanding ledger handoff, not a request for another token.

### Parallel work and single writers

Long-term engineering requirement: proactively split independent development,
review and offline test preparation into parallel work; a blocked line must not
stop unrelated progress. Report dependencies, single writers/locks and runnable
next steps at each delivery; a checkpoint is not waiting for permission to keep
doing already authorized work. Respect applicable subagent/tool restrictions and
specific action approvals. Parallel planning is not simultaneous writes to the
same branch/VM/model session or a new blanket approval.

For this handoff, A passive audio measurement and B isolated-session QA can be
reviewed/prepared independently, while model execution is serialized by the
same host locks and persistent50 allowance. `reserve(...)` supports passive d5f
with1 slot and isolated836ecbe with2 slots **reserved upfront**; a two-session
candidate's per-invocation cap is not a global ledger. Unknown results block both
paths before any rerun. The original6f bridge executed only A; the later B source
integration below is a **separate operation**, loader and projection. It is not
mixed into A, relabeled as the same experiment or given another allowance.

Current delivery dependency check:

| Work | Can progress independently | Shared resource owner / actual dependency |
| --- | --- | --- |
| A fixed observer/bridge source and offline tests | Yes | This work branch has one writer; no host/model needed |
| B836ecbe isolated-session source review/preparation | Yes | Independent review; exact candidate copied as data by this branch's single writer, no mixed A prompt/input |
| Spec/standards reviews and controlled regression | Yes | Independent readers; test fixtures have separate private temp paths |
| Oracle trusted-code/hook installation | Pending separate approval/host owner | Single VM configuration writer; source hashes and old installation evidence required |
| A/B actual model execution | No simultaneous runs | Same controller/global QA locks, reconciled persistent50 ledger, explicit valid test authorization |
| Sanitized result retrieval | After a real job creates its receipt | GitHub API artifact linkage, no personal SSH prerequisite |

## Separate B cloud operation (source preparation only)

`isolated-offline` verifies the deployed c409 physical base-module hashes, then
verifies **both** fixed836ecbe candidate byte hashes before executing either in
a new process. It loads `qa_probe` then `qa_session_isolation`, never A's
`qa_audio_*` modules. The exact source copies are under
`deploy/qa_isolated_candidate/` for reproducible offline CI and installation;
they are data for the process-local loader, not installed app/daemon modules.
Their provenance and hashes are in
`evidence/isolated-qa-candidate-manifest-20261030.json` (SHA256
`7102a643472e81407ceefdc693cdc24b58a783886b9cce7046857c204bafce99`).

After that zero-model check, separately authorized `isolated` uses the same
authority file, controller/global QA locks and persistent50 ledger as A. It
requires **two available slots before starting**, fsyncs both reservations and
the two-slot interlock, and calls the fixed B `qa_probe.run_probe` with
`session_mode=isolated`, question stage and legacy media. The candidate itself
serializes at most two same-model sessions: verified complete owned cleanup of
the first is required before the second. Shared120s work deadline and10s active
cleanup remain unchanged; no prewarm, retry, A observer or Teams is added.

The B projection uses **836ecbe identity**, bounded per-session hashes/timings,
strict per-session cleanup flags, complete-count evidence, two question/RX
summaries and fixed first/secondary failure codes. No arbitrary provider text,
credentials, raw session IDs or private QA JSON enters Actions. Session identity
and snapshot hashes remain local evidence, not a provider-consumption ACK or
proof of semantic memory erasure. A's observer projection is not reused for B.

Settlement validates all reservations owned by the same run/attempt/candidate
and updates them **together**, rather than `entries[-1]`. Two known clean sessions
settle both as `cleaned`. A known first-session failure with clean owned teardown
and a complete one-session count preserves the QA failure, settles the first as
`cleaned` and the unused second as `not_started`; **both remain consumed**, with
the same public receipt hash and no automatic refund. Any missing second receipt,
incomplete count, unclean child, uncertain outcome or mismatched ownership leaves
both reservations and interlock unresolved until explicit reconciliation. A
top-level cleanup flag cannot override an unclean/missing per-session record.
Rerunning the same GitHub run, including another attempt, cannot start again.

Additional pending host files for B (not installed by this task): create
`/opt/teams-staging/qa-isolated-candidate/` root:root0755 and install the two exact
candidate files plus its manifest root:root0644. Install the newly reviewed bridge,
entry and contract hashes from the latest installation manifest. B uses the same
proposed `oracle-qa.yml@refs/heads/main` hook case, not a new wildcard or privilege.
The existing hook still rejects this workflow. Bootstrap, actual ledger/lock/
receipt reconciliation, merge and host/runtime verification are still pending;
offline source tests do not mean either A or B is cloud-connected on Oracle.
