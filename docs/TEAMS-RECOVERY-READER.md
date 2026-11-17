# Recovery reader candidate

This candidate is based on `080ea4e8f8deaa6ec4a5ebbc4f1e29616dbdbe66`.
It changes only stored acceptance-policy reads to accept a strictly validated
`maximum_seconds:null` record. Existing admission validation remains byte-for-byte
unchanged and rejects that policy for a new start. No database, transcript,
ledger, history, cleanup evidence or quota is rewritten or refunded.

This is a reader patch, not an inherently read-only service image. A recovery
deployment must remain in maintenance with scheduling paused, no active meeting
or lease and no new Join/model calls. The current installed Teams helper must
not be repinned to enable meetings on this recovery target. Its release mismatch
is a refusal, not permission to bypass it. This candidate does not cancel the
old acceptance timer or restart the new owner-controlled meeting on old runtime.

## Normal release path, not executed

1. Review/approve this candidate separately. The existing image workflow runs
   only on main, so merge it normally before the owner-controlled feature and
   output-diagnostics changes. Do not build a different branch with a false
   main/source identity. Existing fixed feature heads remain unchanged.
2. Run the existing native ARM validation/publish workflow once. Its controlled
   suite now includes the four recovery-reader fixtures. Verify actual ARM
   image checks, source/run/attempt/digest, artifact integrity and release gate.
   Store that immutable recovery release; there is no such digest yet.
3. Merge the two separately approved feature heads through normal GitHub rules,
   run the combined tests and normal ARM release, then follow the specifically
   approved image/helper/authority deployment. Three-way source and CI-suite
   simulation found no conflict; it is not a full combined test or actual merge.

This requires one additional recovery build/publication and its existing
compute/storage costs. No build, purchase, billing change, PR, merge or
deployment is authorized or performed by this source preparation.

## Smallest safe recovery using the existing controller

Keep the current 080 image as a historical pin, but do not use it after new
null-duration records exist. Its reader rejects those records even though the
SQL schema hash is unchanged.

After the new release has been used, explicit owner Stop must first complete
on that release. Verify model counts, cleanup, original two-slot settlement,
maintenance/paused state, no active meeting/lease and lock availability. Unknown
Stop or cleanup must keep its interlock and reservations for the existing
authorized host reconciliation; do not clear a marker or retry Join to recover.

Then use normal `oracle-staging.yml operation=deploy` with the verified recovery
image's exact digest, source SHA and build run ID. This is a **recovery deploy**;
it is not permission to select the incompatible previous 080 image through
`operation=rollback`. Keep all current data, let the controller create its normal
backup, and restore maintenance immediately after its normal deployment path.
Read back health, exact image/source, paused/quiescent state, history and ledger.
The target provides compatible reads and a place to prepare a forward fix; it
does not certify B attachment, remote audibility or restored meeting operation.

If a directly usable `operation=rollback` target is required, deploy and admit
the compatible recovery release in maintenance before deploying the feature
release. It then becomes the controller's verified previous target. That adds
one deployment and needs explicit approval; publishing an image alone does not
make it the controller's previous healthy release.

If the running app cannot prove Stop/cleanup or the controller refuses a safe
deployment, remain in maintenance/interlock and use a reviewed compatible
forward fix. Do not force an old image, change safety gates, restore an older
database to discard new history, or infer a runnable recovery digest.

## Evidence boundaries

Pure SQLite/validator fixtures verify null and finite history, unchanged stored
bytes, unknown cleanup/counts, identity/schema rejection and duplicate bindings.
The reader candidate itself still rejects a new null-duration admission. The
combined owner feature deliberately changes that admission in its separately
reviewed runtime. No current real meeting, model, image or Oracle was tested.
Model usage remains 16/50. Longer owner-controlled meetings still need an actual
approved meeting/end/stop scope and applicable account-resource cost coverage;
two model starts are not a model-minutes or unlimited-spending allowance.

Chromium's local SUID helper/capability launch failure remains an independent
environment blocker from the output-diagnostics record, not the Oracle attach
root cause. This candidate does not retry launch or change sandbox settings.
