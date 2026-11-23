#!/usr/bin/env bash
# Reviewed scripts only; disposable nonroot sandboxed browser, no app data mount.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
case ${DEVFLOW_OP:-} in look|calendar-occurrences|calendar-settings|issue-binding) ;; *) echo 'unsupported_operation' >&2; exit 2;; esac
if [[ ${DEVFLOW_OP} == issue-binding && -z ${DEVFLOW_PRIVATE_RESULT:-} ]]; then
  echo 'binding_private_only' >&2
  exit 2
fi
# Private calls are nested under the installed target controller and its lock.
# Their temp directory is outside all Actions workspace upload globs.
if [[ -n ${DEVFLOW_PRIVATE_RESULT:-} ]]; then
  E=${DEVFLOW_PRIVATE_RESULT%/*}/projection
else
  E=.devflow/evidence/operate/${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}
fi
mkdir -p "$(dirname "$E")"
# A reused local/run directory must not supply stale success evidence.
mkdir "$E"
chmod 700 "$E"
export DEVFLOW_OPERATION_EVIDENCE="$E"
private_helper=${DEVFLOW_PRIVATE_HELPER:-}
helper_labels=()
if [[ -n $private_helper ]]; then
  [[ $private_helper =~ ^[a-f0-9]{32}$ && -n ${DEVFLOW_PRIVATE_RESULT:-} ]]
  helper=devflow-teams-transcription-private-browser-$private_helper
  [[ ${DEVFLOW_PRIVATE_RESULT%/*} == */"$helper" ]]
  helper_labels=(--label io.devflow.owner=timmypai/teams-transcription
                 --label "io.devflow.teams.private-browser=$private_helper")
  envf=$(mktemp "${DEVFLOW_PRIVATE_RESULT%/*}/password.XXXXXX")
else
  envf=$(mktemp)
fi
chmod 600 "$envf"
created=0
cleanup() {
  rm -f "$envf"
  # Managed helpers are reconciled by the controller against its durable slot.
  if [[ -z $private_helper && $created == 1 ]] && ! docker "${docker_args[@]}" rm -f "$helper" >/dev/null 2>&1; then
    rm -f "$E/look.png"
    if [[ -n ${DEVFLOW_PRIVATE_RESULT:-} ]]; then rm -f "$DEVFLOW_PRIVATE_RESULT"; fi
    printf '%s\n' '{"ok":false,"code":"operation_cleanup_unverified"}' > "$E/result.json"
    exit 1
  fi
}
trap cleanup EXIT
export DEVFLOW_OP_ARGS=${DEVFLOW_OP_ARGS:-\{\}}
python3 - "$envf" <<'VALIDATE'
import json,os,sys
from pathlib import Path
result=Path(os.environ['DEVFLOW_OPERATION_EVIDENCE'])/'result.json'
result.write_text(json.dumps(dict(ok=False,code='operation_setup_failed')))
result.chmod(0o600)
try:
    args=json.loads(os.environ['DEVFLOW_OP_ARGS'])
    if not isinstance(args,dict):raise ValueError
    if not os.environ.get('DEVFLOW_PRIVATE_RESULT'):
        op=os.environ['DEVFLOW_OP']
        if op=='look' and args not in ({},{'path':'/login'}):raise ValueError
        if op=='calendar-occurrences' and args:raise ValueError
        if op=='calendar-settings' and args and not (set(args)=={'calendar_ref','enabled','revision'}
                and args['calendar_ref']=='primary' and type(args['enabled']) is bool
                and type(args['revision']) is int and 0<=args['revision']<2**53):raise ValueError
except (ValueError,TypeError):
    result.write_text(json.dumps(dict(ok=False,code='invalid_DEVFLOW_OP_ARGS')))
    raise SystemExit('invalid_DEVFLOW_OP_ARGS')
password=None
for line in os.environ.get('DEVFLOW_OPS_ENV','').splitlines():
    key,sep,value=line.partition('=')
    if key=='ADMIN_PASSWORD' and sep:password=value
if os.environ['DEVFLOW_OP']!='look' and not password:
    result.write_text(json.dumps(dict(ok=False,code='missing_ADMIN_PASSWORD')))
    raise SystemExit('missing ADMIN_PASSWORD in DEVFLOW_OPS_ENV')
Path(sys.argv[1]).write_text('ADMIN_PASSWORD='+password+'\n' if password else '')
VALIDATE
test "$(id -u)" != 0
docker_args=(--host "unix:///run/user/$(id -u)/docker.sock")
app=devflow-teams-transcription-app
# Fixed target; same installed 8080 Origin, no foreign discovery or mutation.
test "$(docker "${docker_args[@]}" inspect "$app" --format '{{.State.Running}}' 2>/dev/null)" = true
image=$(docker "${docker_args[@]}" inspect "$app" --format '{{.Image}}' 2>/dev/null)
installed=$(docker "${docker_args[@]}" image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' 2>/dev/null)
test "$installed" = "${GITHUB_SHA:?reviewed deployed SHA required}"
v=$(python3 -c 'import json;print(json.load(open(".devflow/ui/package.json"))["devDependencies"]["@playwright/test"])')
if [[ -n $private_helper ]]; then
  # Distinguish never-attempted from a possibly accepted Docker create whose
  # response was lost. Persist before submission, under the inherited lock.
  mkdir "${DEVFLOW_PRIVATE_RESULT%/*}/creating"
  sync -f "${DEVFLOW_PRIVATE_RESULT%/*}"
else
  helper=devflow-teams-transcription-operate-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}
fi
# Container UID != host UID under rootless Docker. Keep private evidence inside
# its namespace, then stream from that live namespace to host-owned private files.
docker "${docker_args[@]}" create --name "$helper" "${helper_labels[@]}" --user 10001:10001 --read-only --log-driver none \
  --cap-drop ALL --security-opt no-new-privileges --security-opt "seccomp=$PWD/docker/chromium-seccomp.json" \
  --network "container:$app" --shm-size=256m --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  --tmpfs /work:rw,nosuid,nodev,size=512m,mode=700,uid=10001,gid=10001 \
  --tmpfs /evidence:rw,nosuid,nodev,size=32m,mode=700,uid=10001,gid=10001 --env-file "$envf" \
  -e BASE_URL=http://127.0.0.1:8080 -e HOME=/work -e CI=1 -e DEVFLOW_OP -e DEVFLOW_OP_ARGS \
  -e DEVFLOW_OP_RESULT=/evidence/result.json \
  -v "$PWD/.devflow/ui:/ui:ro" -w /work \
  "mcr.microsoft.com/playwright:v${v}-noble" sleep 600 >/dev/null 2>&1
created=1
docker "${docker_args[@]}" start "$helper" >/dev/null 2>&1
if docker "${docker_args[@]}" exec "$helper" bash -c \
  'umask 077; cp -R /ui/. /work/ && npm ci --ignore-scripts --no-audit --no-fund >/dev/null && node node_modules/@playwright/test/cli.js test --config=playwright.config.ts --trace off' >/dev/null 2>&1; then
  rc=0
else
  rc=$?
fi
# Docker's archive/cp view does not expose this live tmpfs. Read through exec,
# while the owned helper is still running; export no native reporter/log files.
python3 .devflow/export_operation.py "${docker_args[1]}" "$helper" "$E" "$DEVFLOW_OP" "$rc"
