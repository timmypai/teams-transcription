# Owner-controlled single Teams acceptance preparation

The corrected immediate owner-controlled candidate is described separately in
[TEAMS-TARGET-PREPARATION.md](TEAMS-TARGET-PREPARATION.md). It supersedes the
scheduled-window requirements below only for explicitly prepared immediate manual
targets. Requiring the user to supply an end for the original-link immediate task
was an application defect. Calendar rules remain; the new capability is not enabled.

This branch cancels the fixed 120-second meeting lifetime. It is source
preparation only: the deployed helper, workflow, authority and image are not
changed by editing these files. The historical 120-second failure remains
recorded; it included lobby waiting and must not be rewritten as successful.

The candidate reuses the running daemon and existing SQLite acceptance/lease
seams through an **opt-in same-UID Unix socket**, not management HTTP actions.
The web UI retains its password/session, Origin and CSRF checks. Automation no
longer reads an admin cookie/CSRF file or requires a browser session. This is
source preparation: no listener or host machine authorization is enabled here.

The candidate host helper accepts only `offline`, `run` or `stop`; the occurrence ID/revision,
approved egress-policy hash and fresh participant confirmation come from one
root-owned authority file. No meeting URL, shell, model or credential is a
workflow input. A missing/disabled Unix listener is refused; there is no fallback to web login.
The helper never reads a password hash, session signing key or browser cookie. Creating a
manual occurrence uses the existing private management page and its explicit
recording/speaking consent. Google authorization is unnecessary for this path.

## Execution and evidence

`deploy/teams_acceptance.py` holds the existing controller lock then the original
canonical QA lock, validates fresh exact-release healthy/maintenance/paused/idle
metadata and the exact authorized target, and reserves **two** slots in the existing
50-start ledger before releasing maintenance or dispatching one Join. Checkpoint
7 and all historical entries are preserved. Last host-reported usage is 16/50; a later
authorized invocation would conservatively reserve through 18/50, subject to a
fresh read. Offline neither reserves nor changes maintenance. Locks are acquired
once; controller methods are called directly without a nested stagingctl CLI.

Join binds the acceptance request to its occurrence/session in the same SQLite
claim transaction, before the runner starts. A lost local control response is reconciled
by reading this binding, never by retrying Join. Output/stop use that exact
session and request binding.

New admissions require `maximum_seconds: null` and the explicit bound
`acceptance_stop_policy=owner_or_meeting_end` field. The daemon rejects old
finite acceptance durations for new Join, while existing finite policies remain
readable as historical evidence. `max_duration_seconds` must be null as well;
an independently configured cap is refused, never silently substituted or
overwritten. The worker's formal RX contract already supports null capture and
total durations. No replacement fixed meeting deadline is introduced.

`run` returns after it verifies the original durable Join binding. The existing
daemon in the existing app container owns the meeting; no background shell,
runner child, new service or orphan process is created. A successful handoff is
not proof of actual admission, transport, content or cleanup. The original two
reservations and private interlock remain, bound to the starting run/attempt,
source/image/release and occurrence/revision/request. The controller and global
locks are released when the short command returns. Other QA starts are blocked;
active lease/quiescence checks continue to refuse deployment.

`stop` reads only that owned interlock, revalidates the release and owned
SQLite binding, mutes/stops the exact session once, waits for existing bounded
cleanup, restores maintenance and settles the original two slots. It does not
Join, reserve again, refund unused slots or depend on a new presence window.
After natural meeting end, the same operation can finalize without another Stop.
Browser session expiry or loss on container restart does not affect this local
transport. Restart still does not automatically rejoin an interrupted meeting.
Unknown counts/cleanup, lost binding or changed ledger retain reservations and
interlock for explicit reconciliation. A different candidate's interlock cannot
be retired by this command. Scheduler remains paused.

The five-minute Actions timeout now bounds only a start/stop control command,
not the meeting lifetime. The daemon still stops on an explicit owner Stop, the
actual authorized occurrence end or confirmed meeting end, authorization/lease
loss, service shutdown and existing source/crash failures. The occurrence end
must be a real approved meeting boundary, not another fabricated short task
deadline. This branch never extends an occurrence or authorizes an indefinite
meeting. The existing 300-second presence check is start admission only and is
not changed here.

Immediately before A and B's actual `thread/start` and `thread/realtime/start`
RPCs, a capture-lease-protected transaction persists that role/stage attempt.
Each role is limited to one thread and one native start. Persistence failure
prevents the RPC; an ambiguous RPC remains consumed. Re-instantiating a limiter
cannot erase durable attempts. No reconnect/prewarm/extra model is introduced.

Only a bounded scalar acceptance projection is returned: protocol/binding,
counts, joined/cleanup evidence, source RX and B relay TX metrics. The host
artifact drops session/occurrence IDs, API bodies, title, URL, transcript, OAuth,
cookie, CSRF and SDP. Positive local RX/TX is not remote audibility or continuity.
The observer from Issue #20 is not mixed into this case. Human remote listening,
wake behavior and content assessment remain separate acceptance observations.

Server-side automatic summary is canceled globally: no daemon summary worker,
new outbox enqueue or retry service. Transcript and prior published history/jobs
remain intact for dot; no replacement summary service or schema migration.

## Exact remaining installation/authority deltas

None of these changes is applied by this branch:

1. A reviewed image containing the owner-controlled admission/runtime changes
   must be built and released. The helper's four `RELEASE_*`/image
   pins are intentionally unset until that exact release is verified. Source
   preparation refuses execution with unset pins and does not claim a new image
   has been tested.
2. Install the fixed root-owned helper as
   `/opt/teams-staging/bin/teams-acceptance` (0755). Reuse the existing root-owned
   qa bridge/contract/controller; preserve their authority, ledger, locks,
   interlock and all old bundles. No sudo grant or Docker group is added.
3. Approve one root-owned 0644 file
   `/opt/teams-staging/qa-candidate/teams-acceptance-authority.json` containing only
   `version`, `occurrence_id`, `occurrence_revision`, `maximum_seconds`,
   `participant_confirmed_until` (fresh at start, at most five minutes ahead) and
   `egress_policy_sha256`. These come from the actual private target and approved
   runtime allowlist; no real target is in source. For this candidate,
   `maximum_seconds` must be null. No old authority is silently rewritten.
4. Explicitly approve enabling the local machine control boundary with the
   **single** added serve argument `--local-teams-control` in the existing
   container command. Default remains off. The daemon owns
   `/data/.teams-control` (0700), `owner.lock` (0600) and `control.sock` (0600).
   Linux `SO_PEERCRED` must match the daemon UID; a lifetime flock prevents a
   second listener from replacing a live socket. Stale owned socket recovery is
   allowed only after acquiring that lock; symlinks/unsafe owners/modes refuse.
   No host port, new mount, password, token, account or external service is added.
   The existing rootless Docker owner can execute one fixed command as container
   UID/GID 10001:10001: `teams-transcription local-teams-control --config
   /data/config/runtime.yml`. Input is bounded JSON over stdin, never a URL or
   shell command. Only view, owner-controlled Join, mute, and exact request-bound
   Stop are accepted. Speaking enable, scheduler, arbitrary actions, target
   creation and transcript access are absent. Both peers must have the same UID.
   **This grants that local UID a narrow control capability; it is not a sandbox
   against that UID.** That UID already owns the application SQLite/config and
   media process. The root-owned helper still supplies fresh authority, release
   verification, canonical two-slot reservation, controller/global locks and
   unknown-cleanup interlock. Do not expose/mount the socket for other users or
   treat its permissions as protection from a compromised same-UID process.
   Replacing the fixed root-owned helper and enabling the listener require
   explicit fixed-version approval; neither is applied by this branch. Preserve
   the old session file privately if rollback needs it; this candidate never
   reads it, and does not delete credentials or copy the admin password to VM.
5. The existing manual `oracle-teams.yml` and its main-only hook allowlist need
   **no change** for this candidate. They already expose `offline`, `run`, `stop`.
   No new workflow operation or runner permission is added. Same concurrency,
   runner, permissions={}, no checkout, installed command and five-minute control
   timeout. Current workflow SHA256 is
   `b8a29f23a0f1f7f674976e6172a60222110f6b31491d2b1fe5dfdfb762831ee9`.
   The source test preserves that pin; it does not certify host installation.
   The same main-only workflow hook case is reused, not broadened. Installing a
   replacement root-owned helper and enabling local machine control need
   explicit fixed-payload approval; this branch does neither on the host.
6. A current valid meeting/time window and participant presence/recording/speech
   consent are still required. Supply target privately; notify before Join and
   wait for the user's fresh "already in the meeting" confirmation. Resolve any
   new Teams/auth/CDN/media egress or login requirement explicitly; no permission
   grant, token transfer, public port or external network setting is made here.

After specific approval and installation, run `offline` first, then exactly one
authorized `run`; later use `stop` for explicit Stop or natural-end finalization.
Stop/cleanup and maintenance evidence must be read back even
when transport/content fails. A technical error has an owner/diagnostic next step;
only the concrete target, presence or installation approvals are waiting-user.
Dispatch and projected result retrieval use GitHub Actions/Oracle runner and
must not depend on a personal computer staying online.

The two-start/50-start ledger counts starts, not model minutes. Longer actual
meetings can consume more existing model/account resources even without extra
channels; no new provider, subscription, credentials, network or billed service
is enabled here. A future real run needs the actual meeting/end/owner-stop scope
and any newly applicable cost approval. Source completion is not that approval.

Independent Cloud Codex review is reported separately from local tests and the
official GitHub review of a future PR. The official mechanism completed PR #29;
that is not a review or merge approval for this candidate.

## Local-control candidate verification and activation

`serve` without the explicit flag creates no local socket. Public management HTTP
remains authenticated even when the Unix listener is enabled. The client sends
one bounded request (4 KiB; 64 KiB response), with OS peer credentials and fixed
redacted errors. The server's ten-second bound applies only to request input; client/Docker
response waits are 15/20 seconds. They are transport failure bounds, never
meeting lifetimes. An admitted mutation continues to daemon ownership even if
its client times out or disconnects; do not cancel it midway through a committed
SQLite claim. Listener shutdown waits admitted handlers before daemon cleanup.
A lost Join result keeps original reconciliation/no-retry behavior. No fixed meeting lifetime is added.

Activation after approval requires: merge/review and build a new ARM image;
verify release/artifact/digest; pin the helper's four release fields; install
that fixed helper root-owned; append the opt-in serve argument; deploy with
compatible recovery ready; verify maintenance/paused/idle and run `offline`.
No new helper executable, hook case, sudo, Docker group, network rule or secret
is required. Existing root-owned controller/QA bridge/contract/authority and
canonical ledger are reused without replacement. Do not claim this machine
capability was enabled merely because code/tests/PR exist. Reverting the flag
removes the socket on clean shutdown; reverting helper/image restores the old
transport, which again needs its private browser session. Remove the new serve
flag before starting a recovery image that predates this CLI option. Preserve ledger and
interlock across rollback; active/unknown runs require owned cleanup first.

Synthetic Unix/SQLite/daemon tests cover Join/Stop with public API 401, lost
browser sessions, restart without sessions, wrong binding/revision, no speaking
enable, invalid/oversized frames, UID rejection, unsafe paths and singleton
socket cleanup. Docker execution is a mocked process boundary in host tests;
these are **not** actual ARM image, Oracle rootless, Teams admission or live audio
evidence. No Teams/Microsoft login state is created or substituted here.
