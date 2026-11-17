# Stopped create diagnostic proposal

Related #3 and #4. This source change is not installed or executed on Oracle.
It adds no workflow, hook permission, registry credential, network/security
setting, application recovery, or service start operation.

The first deploy [40880287493](https://github.com/paulpai0412/teams-transcription/actions/runs/40880287493)
failed with `docker_command_failed`; the bounded diagnostic
[40880788344](https://github.com/paulpai0412/teams-transcription/actions/runs/40880788344)
reported `staging_container_missing`. The parent VM task subsequently confirmed
failed state, no previous image, maintenance present, no DB, the correct cached
ARM64 image, and an existing project bridge with no app container. Its dry-run
create succeeded; no engine error was found. **The actual create failure cause
is still unknown.** Previously discarded stderr cannot be recovered from the
existing allowlisted artifact. A successful dry-run does not validate real create.

## Bounded error evidence

The fixed `Controller.docker()` command wrapper retains at most 64 KiB stdout
and 16 KiB stderr in memory, with no raw output temporary file. The existing
diagnostics recent-log path remains separate and still uses its private
`TemporaryFile`; its 64 KiB read/projection bound is not a disk hard cap.
Excess command stderr is drained and dropped;
stdout overflow or command timeout terminates only the invocation's CLI/plugin
process group, not the Docker daemon or app containers. The owned CLI is waited
on for at most two additional seconds; uncertain cleanup preserves the original
failure and is reported separately. Existing disposable-container cleanup remains.

Failure JSON preserves the original `error_code` and `cleanup`, and may add:

```json
{"docker":{"operation":"compose_create","category":"network_create_failed","exit_code":1,"stderr_truncated":false,"stdout_truncated":false}}
```

All values come from fixed enums, bounded exit codes and strict booleans. There
is no raw or partially redacted stderr fallback. Unknown content remains
`unclassified`; a category is an observation, not a proven root cause. Tokens,
addresses, paths, environment, configuration, transcripts and exception bodies
are never copied into this object. Registry cleanup wrappers preserve the first
Docker category even if cleanup also fails.

## Proposed one-shot host operation

After source review/merge and a separately confirmed installation of this exact
updated controller as root-owned `/opt/teams-staging/bin/stagingctl` (0755), the
existing VM administrator can execute the following as the existing rootless
owner. This is a proposed operation, not an instruction already executed here:

```sh
sudo -u teams-staging -- /opt/teams-staging/bin/stagingctl diagnose-create
```

No digest, path, shell, Docker option, token or arbitrary command can be supplied.
The command uses the existing controller lock and an empty temporary Docker
config; it does not require a GitHub job token. The existing Actions diagnostics
workflow continues to run only the original read-only `diagnostics` operation.

Before any real create, the new operation requires:

- `phase=failed`, `previous=null`, a valid original deployment run ID, and a
  revision matching both the recorded release and locally inspected ARM64 image.
- The unchanged state and exact digest in the regular, non-symlink `image.env`.
- The already-existing `teams-staging_default` bridge with the expected project
  label. It does not provision a missing network.
- Maintenance present, no DB and quiescent metadata, plus no Compose app container.

It exclusively creates a 0600 `create-diagnostic-<original-deploy-run>.json`
intent record in the private state directory and fsyncs both the file and its
directory **before** the Compose create command. The record contains only operation/image/revision/run metadata and
`outcome=attempted`. It fences repetition even after failure, timeout or an
uncertain result; it is not a completion receipt and is never auto-deleted.

The sole Compose mutation is fixed:

```text
docker compose [installed project/config/pin] create --no-build --pull never teams-transcription
```

It does not execute app `up`, `start`, `stop`, removal, build, pull or login. The
app service's entrypoint is not run. The existing metadata `probe()` still uses
short-lived, no-network helpers with a read-only volume and Python entrypoint;
their existing owned-helper cleanup remains. This is not a claim that no helper
container executes. Success requires the app container to be `created`,
`running=false`, PID 0 and pinned to the same image, with maintenance/no-DB and
the exact deployment state still intact. The stopped container is retained as
evidence. A failed or uncertain result does not trigger a second create or cleanup
that could hide evidence.

This is **not recovery**: it never marks a deployment healthy, rewrites deployment
history, clears maintenance, restores a DB, or enables scheduling. The ordinary
deploy gate still refuses failed/switching state, and rollback still refuses a
first deployment with no previous healthy image. Further recovery requires its
own reviewed, concrete plan; do not delete the audit/state/marker to retry.

The lock serializes approved controller operations; it does not sandbox other
commands run as the same UID. The VM operator must keep other manual mutations
quiescent while probing. No service, OAuth, provider, Live or Teams operation is
authorized by this proposal.

## Local verification

`test_staging_docker_diagnostics` exercises the real pipe capture against local
synthetic children, including oversized output, token-input backpressure,
unknown/malformed metadata and timeout ownership. Synthetic controller adapters
verify the public operation's single-create fence and preservation of failed
state/maintenance, plus refusal of healthy/previous/DB/missing-marker/existing
container/wrong-pin cases. Existing deploy and rollback gates remain tested.
No local test is an actual Oracle create or recovery result.
