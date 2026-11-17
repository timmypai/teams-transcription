# Reviewed empty first-deploy recovery

Related #3 and #4. This proposal is source code only; no host update or new
create/start was executed by this Cloud task. It is separate from the original
once-only `diagnose-create`, whose audit must remain untouched.

## Confirmed configuration defect

Both installed staging/onboarding templates rendered the flow-style YAML
`tmpfs: [/tmp:size=512m,mode=1777]` as two mounts: `/tmp:size=512m` and
`mode=1777`. The latter is not an absolute mount destination. Host read-only
render independently confirmed this. The first diagnostic returned only
`compose_create / unclassified / exit 1`; its original stderr was discarded,
so it cannot retrospectively prove that particular daemon error.

Quote the complete tmpfs entry in both templates. The semantic regression
executes real Compose `config` with synthetic empty runtime.env and requires
exactly `['/tmp:size=512m,mode=1777']`; both old templates failed before the fix.
It needs Docker CLI/Compose but no daemon. The classification regression drives
the public controller pipe boundary with the Moby nonabsolute-mount error and
now exports only `mount_path_not_absolute`, never the path or raw stderr.

Seccomp remains enabled. Compose v5.6.1 resolves its profile through project
directory, which the controller supplies explicitly. Capabilities, non-root,
readonly filesystem, network/port bindings and resource limits are unchanged.

## Concrete operator package after approval

The original VM task uses its existing administrator access to install only the
reviewed controller and two Compose files, verifying hashes before atomic
replacement. No new credential, sudo entitlement, daemon, group or firewall
setting is needed. Keep root:root; controller 0755, Compose files 0644.

| Repository file | Installed target | SHA256 |
| --- | --- | --- |
| `deploy/stagingctl.py` | `/opt/teams-staging/bin/stagingctl` | `b43166c1b5526752f5526a2cce8bbbbb0b51fc3fc637ebdc109c7a809aa89384` |
| `deploy/compose.staging.yml` | `/opt/teams-staging/compose/compose.yml` | `c9c7fda103df0eb9945abeb472ce7e5e02204b1e72159a62c8546f01dfa19b31` |
| `deploy/compose.onboarding.yml` | `/opt/teams-staging/compose/compose.onboarding.yml` | `acf8e42e79cb33dee3a4671bc6d16c7e79094920709a8ee568e36c0214ee8d9d` |

The fixed reviewed commit accompanies the delivery report.
The controller validates administrator ownership/non-symlinks and both exact
Compose hashes, rather than accepting whatever configuration happens to exist.
Before execution the host task rechecks no application container, no DB,
maintenance present, failed/previous-null state, and the existing original
`create-diagnostic-40880287493.json` audit. Do not delete/edit audit or state.

Run **once** as the existing rootless owner, only after the package is approved:

```sh
sudo -u teams-staging -- /opt/teams-staging/bin/stagingctl recover-first-deploy
```

The operation accepts no digest, path, shell or arbitrary option. It is host-only;
no workflow/hook change exposes it. Existing deploy/rollback gates remain intact.
It uses the cached image already admitted by successful build run 40866819722,
source c70250a09b5dbe64e5118bf86dae4f9f52000b10, digest
`sha256:9a8ef70cb0c1c39a8edc544d1fbd337c967d909d6a8563901a64e16a2c644b4f`.
No rebuild/pull/login/new release receipt is performed. This candidate predates
the proposed full staging lifecycle CI gate; it must not be relabeled as having
passed that future gate.

## Guards, lifecycle and retained evidence

The shared read-only preconditions require failed first-deployment state,
previous=null, original run/revision/release identity, exact image pin and cached
ARM64 image, existing project bridge, maintenance/quiescence/no DB and no app
container. Existing read-only volume metadata helper containers still execute.
The old audit must be regular, owner-private 0600, contain the expected original
operation/image/revision/run and `outcome=attempted`, and remain unchanged.

Before any create/start the controller exclusively creates private
`first-recovery-40880287493.json`, containing the original state text verbatim,
its SHA256, the old audit SHA256 and both reviewed Compose hashes. It fsyncs the
file and directory. This is an **intent**, not a completion claim; even failed
or uncertain execution consumes the fence. It is never overwritten/deleted.

The only forward mutations are fixed Compose `create --no-build --pull never`
and `start --wait --wait-timeout 90`. Before start, maintenance/no-DB/quiescence
are checked again. Success requires running/healthy on the pinned image and
actual SQLite metadata showing DB present, scheduler paused, no active work and
maintenance still present. Only then is deployment phase committed healthy,
with the private history intent linked, synchronizing the state directory after
the atomic state replacement; original run/source identity and null
previous image remain. **Maintenance is never cleared** and no scheduling is
enabled. On startup the app can initialize its normal SQLite DB/session files;
that is an intended recovery mutation, unlike the earlier stopped-create probe.

Lifecycle error or handled cancellation triggers a best-effort fixed service
stop; stop failure adds `recovery_stop_uncertain` without replacing the original
error. Failed lifecycle leaves original deployment bytes/audit intact. A state
commit transport/IO outcome can be uncertain; the controller does not blindly
stop after a possibly committed healthy result. SIGKILL/VM crash cannot promise
stop: keep the fence/state/maintenance and independently inspect, never retry.

The host task records the exit code and small allowlisted JSON, then reads back
state/history hash, original audit hash, effective Compose tmpfs, health,
maintenance and paused/idle booleans. Do not publish private state/config/DB,
raw logs, environment, credentials or account data. A failed/unknown result
requires a new concrete review; do not modify fences to run this operation again.
No OAuth, Calendar authorization, Live/Teams session or audio acceptance is
performed. No previous healthy image exists, so rollback remains untested.

## Separate CI follow-up

The urgent host fix does not wait for CI platform changes. A separate manual ARM64
integration must exercise these actual templates through create/start/health/
restart persistence/finally cleanup with isolated project/volume/synthetic config.
It must preserve security/resources, record permitted isolation overrides and
actual rootless/engine/Compose/cgroup/limits, and report `not-equivalent` if the
hosted backend differs from Oracle. Existing rootful `compose_smoke.py` is not
Oracle-equivalent. No CI workflow or release-gate migration is part of this fix.
