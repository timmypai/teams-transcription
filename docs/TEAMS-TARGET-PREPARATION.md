# Source-bound immediate owner control candidate

The initial11385889 candidate incorrectly retained scheduled start/end and manual
lateness for an immediate owner-controlled task. Those were application limits,
not missing instructions from the user and not requirements of the Teams URL
adapter. It must not be activated as the final solution. Its evidence remains
historical; no expired occurrence, failed run or timestamp is rewritten.

This corrected candidate prepares one **immediate manual** target from the exact
root-authorized stored source link/consent. It needs no planned start or end.
The new row explicitly stores `start_at:null`, `end_at:null`,
`join_early_seconds:null`, `max_lateness_seconds:null`, and a strictly validated
`local_preparation.mode=owner_immediate` binding. `prepared_at` is only the actual
record-creation metadata; it is never participant presence or an invented
meeting start. A completed/failed original manual source may be reused; skipped,
canceled, stale, unauthorized or calendar sources cannot gain authorization.

## Authority and installation delta (not applied)

The same root-owned0644 authority path is used:
`/opt/teams-staging/qa-candidate/teams-acceptance-authority.json`. The sole host
administrator must explicitly approve/install exactly this six-field contract,
using the actual private original ID/revision and existing allowlist hash:

```json
{
  "version": "teams-source-v2",
  "source_occurrence_id": "<existing private original occurrence ID>",
  "source_revision": 3,
  "mode": "owner_immediate",
  "maximum_seconds": null,
  "egress_policy_sha256": "<existing approved allowlist SHA256>"
}
```

Revision3 is a previously reported value, not a runtime constant. No scheduled
window or participant timestamp belongs in this static source authority. Legacy
source-v1 preparation documents do not silently become immediate authority.
The new local **prepare** capability still requires approval; it is additional
to PR33's Join/view/owned Stop. Same-UID access is not isolation from that UID.
The trusted root-owned host helper remains the canonical authority/budget gate.

Deployment needs a reviewed/verified ARM image and the fixed existing
`teams-acceptance` helper with four new release pins. Keep PR33's existing
`--local-teams-control` flag; no new serve flag, port, egress, account, password,
token, sudo, Docker group or public HTTP route is added. The same manual
`oracle-teams.yml` adds `prepare` and a string `participant_confirmed_at` input.
Main-only condition, permissions={}, installed command, runner and common
concurrency remain. No new hook filename is requested; actual hook compatibility
must still be verified, not inferred from this source file.

The helper creates `state/teams-prepared.json` (runner UID0600) with authority
hash and prepared target binding. Source identity/hash makes prepare idempotent;
used/conflicting receipts fail closed. Never delete a receipt/interlock or roll
back a ledger to retry a started case. Old history remains intact.

## Complete cloud control sequence

1. Host owner installs the approved image/helper/workflow/authority, verifies
   health, maintenance/paused/quiescent, unchanged canonical ledger, free locks
   and no interlock. Technical setup does not require a participant to wait.
2. `prepare`, confirm_meeting=false: under the existing locks, validate source,
   release and canonical budget; atomically create one new pending immediate
   target. No Join, model reservation, maintenance release or login occurs.
   An explicit retry after a lost prepare response returns the same pending
   record; it cannot create a second target or reuse an already-started one.
3. `offline`, confirm_meeting=false: validate prepared binding/nullable immediate
   mode and technical readiness without participant presence or side effects.
   Preparing does not start an admission timer: the target remains usable until
   its one actual run or an explicit authority/consent/cleanup change.
4. Once ready, obtain one actual participant confirmation. `run` requires
   confirm_meeting=true and that actual timezone-aware message timestamp in
   participant_confirmed_at. Missing/future/older-than300-second confirmation
   refuses before reserving two canonical starts. This retained start-admission
   fence is not a meeting lifetime; execution time must not stand in for the
   message. Timestamp provenance is trusted operator input, not a cryptographic
   human-presence claim. No root file must be rewritten for this confirmation.
5. Run reserves2/50, creates the existing owned interlock, releases maintenance
   and dispatches one Join. The daemon owns the meeting after short handoff.
   Prepared rows require a null-duration acceptance at the claim transaction;
   ordinary UI/scheduler calls cannot start them without that policy. The same
   A/B per-role durable start limits, source/revision/lease fences remain.
6. Immediate mode has **no scheduled-end or lateness gate**. Global duration
   configuration must be null; a configured finite cap is refused, not silently
   applied or overwritten. Continue until exact owned Stop, confirmed Teams end,
   authorization/lease loss, service shutdown or existing safety failure.
   Calendar and ordinary scheduled/manual admission/end rules remain unchanged.
   Existing per-operation read/Join/ICE/drain/cleanup bounds remain failure
   bounds. In particular the existing120-second browser Join/admission timeout
   does not end an already-joined meeting; the five-minute Actions timeout only
   bounds a control command, not the daemon-owned meeting.
7. `stop` uses original durable run/session/request/release ownership, without
   new presence or another Join/reservation. It mutes/stops or finalizes a naturally
   completed run, verifies cleanup, restores maintenance and settles both slots
   without refund. Unknown outcomes retain original reservations/interlock.

Automation never reads admin cookies, CSRF, Windows Credential Manager or the
admin password. Existing admin hash/signing-key files remain startup config for
the co-hosted UI, not a manual session dependency. Existing ChatGPT OAuth and any
Teams-required guest/account admission remain separate prerequisites. No actual
Join, Live/audio or remote listening is proved by offline/handoff/job success.

## Nullable data and recovery

SQL time columns already permit NULL; no schema migration is added. Direct
meeting projections preserve NULL and label immediate mode; the calendar counts
undated records rather than inventing grid times. Prepared UI Join remains off;
active Stop/status and history remain readable. Frozen prior context uses actual
claim time when there is no schedule; it is not participant evidence. Historical
immediate runs use actual run time when reading prior published history, without
re-enabling the canceled summary/outbox service.

The fixture snapshot contains all64 exact Python modules from deployed
1b8ffa64, with archive/per-module hashes. It uses no current application fallback.
Offline tests verify pending/terminal NULL rows, old read APIs and admin app
health/unauthenticated401, unchanged SQL dump, no due scheduling, and explicit
old Join refusal before runner/provider. This is old-source compatibility with
unchanged external libraries, **not an old ARM container deployment**.

A coordinated rollback may use the currently verified PR33 release as a
maintenance/paused recovery candidate after owned Stop/cleanup. Preserve old
helper, workflow/authority contracts and data backups; restore compatible root
files before invoking the old helper. Preserve ledger/receipt/interlock history.
The old application cannot run immediate NULL-time targets; do not repin or
force it to rejoin them. Actual old-image readback and host recovery remain
unexecuted here. Do not substitute older080 image or claim model/audio recovery.

All code, fixture, review and test preparation is source-only. No new machine
capability, root authority or workflow was activated; real usage remains the
last host-reported16/50. Longer actual meetings consume account resources even
with only two starts; no unattended unlimited-service/cost permission is implied.
