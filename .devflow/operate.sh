#!/usr/bin/env bash
# Reviewed scripts only; disposable nonroot sandboxed browser, no app data mount.
set -euo pipefail
umask 077
cd "$(dirname "$0")/.."
case ${DEVFLOW_OP:-} in look|calendar-occurrences|issue-binding) ;; *) echo 'unsupported_operation' >&2; exit 2;; esac
E=.devflow/evidence/operate/${GITHUB_RUN_ID:-local}-${GITHUB_RUN_ATTEMPT:-1}
mkdir -p "$(dirname "$E")"
# A reused local/run directory must not supply stale success evidence.
mkdir "$E"
chmod 700 "$E"
export DEVFLOW_OPERATION_EVIDENCE="$E"
envf=$(mktemp)
chmod 600 "$envf"
created=0
cleanup() {
  rm -f "$envf"
  if [[ $created == 1 ]]; then docker "${docker_args[@]}" rm -f "$helper" >/dev/null; fi
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
except (ValueError,TypeError):
    result.write_text(json.dumps(dict(ok=False,code='invalid_DEVFLOW_OP_ARGS')))
    raise SystemExit('invalid_DEVFLOW_OP_ARGS')
password=None
for line in os.environ.get('DEVFLOW_OPS_ENV','').splitlines():
    key,sep,value=line.partition('=')
    if key=='ADMIN_PASSWORD' and sep:password=value
if os.environ['DEVFLOW_OP']!='look' and not password:
    result.write_text(json.dumps(dict(ok=False,code='missing_ADMIN_PASSWORD',
        reason='請在 repo secret DEVFLOW_OPS_ENV 設定 ADMIN_PASSWORD；不要放在 args 或聊天。')))
    raise SystemExit('missing ADMIN_PASSWORD in DEVFLOW_OPS_ENV')
Path(sys.argv[1]).write_text('ADMIN_PASSWORD='+password+'\n' if password else '')
VALIDATE
test "$(id -u)" != 0
docker_args=(--host "unix:///run/user/$(id -u)/docker.sock")
app=devflow-teams-transcription-app
# Fixed target; same installed 8080 Origin, no foreign discovery or mutation.
docker "${docker_args[@]}" inspect "$app" --format '{{.State.Running}}' | grep -x true
image=$(docker "${docker_args[@]}" inspect "$app" --format '{{.Image}}')
installed=$(docker "${docker_args[@]}" image inspect "$image" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
test "$installed" = "${GITHUB_SHA:?reviewed deployed SHA required}"
v=$(python3 -c 'import json;print(json.load(open(".devflow/ui/package.json"))["devDependencies"]["@playwright/test"])')
helper=devflow-teams-transcription-operate-${GITHUB_RUN_ID:?}-${GITHUB_RUN_ATTEMPT:?}
# Container UID != host UID under rootless Docker. Keep private evidence inside
# its namespace, then stream from that live namespace to host-owned private files.
docker "${docker_args[@]}" create --name "$helper" --user 10001:10001 --read-only \
  --cap-drop ALL --security-opt no-new-privileges --security-opt "seccomp=$PWD/docker/chromium-seccomp.json" \
  --network "container:$app" --shm-size=256m --tmpfs /tmp:rw,nosuid,nodev,size=512m \
  --tmpfs /work:rw,nosuid,nodev,size=512m,mode=700,uid=10001,gid=10001 \
  --tmpfs /evidence:rw,nosuid,nodev,size=32m,mode=700,uid=10001,gid=10001 --env-file "$envf" \
  -e BASE_URL=http://127.0.0.1:8080 -e HOME=/work -e CI=1 -e DEVFLOW_OP -e DEVFLOW_OP_ARGS \
  -e DEVFLOW_OP_RESULT=/evidence/result.json \
  -v "$PWD/.devflow/ui:/ui:ro" -w /work \
  "mcr.microsoft.com/playwright:v${v}-noble" sleep 600
created=1
docker "${docker_args[@]}" start "$helper" >/dev/null
if docker "${docker_args[@]}" exec "$helper" bash -c \
  'umask 077; cp -R /ui/. /work/ && npm ci --ignore-scripts --no-audit --no-fund >/dev/null && node node_modules/@playwright/test/cli.js test --config=playwright.config.ts --trace off' >/dev/null 2>&1; then
  rc=0
else
  rc=$?
fi
# Docker's archive/cp view does not expose this live tmpfs. Read through exec,
# while the owned helper is still running; export no native reporter/log files.
python3 .devflow/export_operation.py "${docker_args[1]}" "$helper" "$E" "$DEVFLOW_OP" "$rc"
