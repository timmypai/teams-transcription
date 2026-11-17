# Isolated semantic QA runtime candidate v1

This source prepares a new fixed candidate; it does not install or run it on
Oracle. No model request is made by importing or offline-checking the bundle.
Historical A/B candidates, receipts and quota entries remain unchanged.

- Runtime bundle commit: `1849e60b98e5cc93a5aef7bda5ecaf432dede1fc`.
- Runtime manifest SHA256:
  `9432a42aff31b1b75d2e86ede18f921cb1a12496aaa204a9a9dd861254000aa5`.
- Criterion: `synthetic_current_fact_semantic_v1`, child QA receipt schema3,
  semantic coordinator/public projection schema2.
- The earlier `9036aaf…` manifest pins source preparation only. It is not this
  three-file runtime manifest. Candidate SHA remains a real Git commit, with a
  separate manifest hash; no content digest is presented as a commit.

`deploy/qa_semantic_candidate/` contains the reviewed criterion plus a separate
copy of B's probe and coordinator. It preserves `_run_probe_session`, current
revision snapshots, serial sessions, the total work deadline and owned cleanup.
The helper is loaded before the probe/coordinator after every hash is checked.
Physical installed c409 base hashes and the existing c409 immutable image are
still required; the daemon/image/auth are not replaced by this candidate.

The existing manual main-only `oracle-qa.yml` gains two explicit operations:
`semantic-v1-offline` and `semantic-v1-isolated`. Old operations retain their old
candidate identities. Offline checks do not read/reserve quota or start models.
Live still requires explicit selected-operation authorization and the same
runtime/authority/controller/global-lock/ledger checks. Two slots are reserved
before a start; complete verified cleanup settles both, including an unused
second slot without refund. Unknown outcomes retain reservations/interlock.
The canonical historical checkpoint7 plus existing entries is not reset.

Semantic pass requires complete supported current facts, not an expected
substring. Unsupported forms remain unknown. Strict quotation is diagnostic.
Each public check is bound to one same-session delivery: matching current source
version, question1, valid source/payload hashes, returned marker, typed positive
payload/sent bytes and completed transfer. This is local delivery evidence, not
a provider-consumption ACK. RX, physical mute, scoped tool, no stale facts,
distinct session identities and verified cleanup remain required. The bounded
two-stage projection retains criterion/status/reason/type, revision/version and
hashes but drops raw answer/source/audio/OAuth fields. It rejects mixed versions.

## Source hook and host payload boundary

The repo hook now includes only the exact main `oracle-qa.yml` case that the user
already approved and the host owner installed. PRs, branches and other workflows
remain denied. This reconciles source drift; this task grants no new host case,
sudo, credential, account, network port or token scope. There is no host operation
in this source delivery. Workflow permissions{}, runner labels, staging environment
and shared `oracle-staging` concurrency are unchanged.

`evidence/semantic-runtime-root-payload-v1.json` lists the seven proposed root-owned
files and exact hashes/modes. The existing installed hook is verification-only;
do not overwrite it from source without reconciling its approved installed hash.
Any root payload update is a separately reviewed fixed-version installation by
the existing admin owner. Verify old installed hashes against approved source,
hold existing locks, preserve maintenance/paused state and make atomic backups.
Unknown existing files/hashes or active/uncertain QA must stop installation.

Rollback may restore only known program/bundle bytes after no active or unknown
attempt remains and the old reader accepts all settled ledger entries. Never
rewind quota/history, clear interlock, remove old candidates or copy OAuth. The
existing controller interlock guard remains in place. This manifest is a handoff
inventory, not an installer or installation approval.

## Executed offline evidence

Tests load the actual hash-verified bundle in a private physical c409 fixture
package. Model starts, browser/media and Docker are external controlled seams;
the tested coroutine route does not invoke Live. Loaded-probe fixtures verify
neutral-prefix acceptance, wrong/stale/unsupported rejection and RX gating.
Coordinator fixtures verify serial cleanup and mismatched-child-version stop.
Bridge fixtures cover both reservations, checkpoint/history preservation,
no refund, insufficient budget, rerun denial and unknown cleanup.

```bash
PYTHONPATH=tests:. python -m unittest test_cloud_qa_semantic.SemanticRuntimeTests -v
python tools/ci_contracts.py /tmp/NEW-contract-results
```

Actual ARM/browser/Oracle installation and semantic Live acceptance remain
unverified. Existing B manual semantic pass does not need another model call.
Actual historical prefix bytes are still not local fixtures. No hosted replay
dispatch is part of this delivery.
