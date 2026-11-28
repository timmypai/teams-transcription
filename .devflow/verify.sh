#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ "${1:-}" == "--tx-receipts" ]]; then
  # Stream tests only. The module under test must be the image-installed wheel,
  # never a source checkout or an application overlay. Isolated Python ignores
  # the image's source-containing WORKDIR and inherited PYTHONPATH.
  docker --host=unix:///var/run/docker.sock exec -i "${2:?container required}" \
    python -I -B -c 'import pathlib,sys
from teams_transcription import b_browser_tx
assert "site-packages" in pathlib.Path(b_browser_tx.__file__).parts
sys.argv=["tx-receipts",sys.argv[1]]
exec(compile(sys.stdin.read(),"<tx-receipt-fixture>","exec"))' "${3:?phase required}" \
    < tests/test_tx_failure_receipts.py
  exit 0
fi

if [[ "${DEVFLOW_SMOKE:-0}" == "1" ]]; then
  # Read-only post-deploy smoke verification called by standard `.devflow/deploy.sh`.
  base_url="${DEVFLOW_BASE_URL:?DEVFLOW_BASE_URL required}"
  head_sha="${DEVFLOW_HEAD_SHA:-}"
  out_dir="${DEVFLOW_EVIDENCE:-.devflow/evidence}"
  mkdir -p "$out_dir"
  python3 - "$base_url" "$head_sha" "$out_dir/deploy-smoke.json" <<'PY'
import json
import subprocess
import sys
import urllib.request
from pathlib import Path

base_url, head_sha, out_path = sys.argv[1], sys.argv[2], Path(sys.argv[3])

with urllib.request.urlopen(f"{base_url}/healthz", timeout=10) as resp:
    health_code = resp.status
    health_body = json.loads(resp.read().decode("utf-8"))
if health_code != 200 or health_body.get("status") != "ok":
    raise SystemExit(f"healthz check failed: http={health_code} body={health_body}")

with urllib.request.urlopen(f"{base_url}/login", timeout=10) as resp:
    login_code = resp.status
if login_code != 200:
    raise SystemExit(f"login page check failed: http={login_code}")

raw = subprocess.check_output(
    ["docker", "inspect", "devflow-teams-transcription-app"],
    text=True,
    timeout=15,
)
info = json.loads(raw)[0]
host = info.get("HostConfig") or {}
labels = (info.get("Config") or {}).get("Labels") or {}
state = info.get("State") or {}
source_label = labels.get("io.devflow.source", "")

checks = {
    "running": bool(state.get("Running")),
    "user_10001": (info.get("Config") or {}).get("User") == "10001:10001",
    "read_only": bool(host.get("ReadonlyRootfs")),
    "cap_drop_all": host.get("CapDrop") == ["ALL"],
    "source_match": (not head_sha) or (source_label == head_sha),
}
receipt = {
    "kind": "deploy-smoke",
    "head_sha": head_sha,
    "source_label": source_label,
    "healthz_http": health_code,
    "login_http": login_code,
    "checks": checks,
    "ok": all(checks.values()),
}
out_path.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
print(json.dumps(receipt))
if not receipt["ok"]:
    raise SystemExit(f"container security/identity smoke failed: {checks}")
PY
  exit 0
fi

# This entry point only creates disposable hosted resources; never Oracle.
python .devflow/hosted_verify.py
python .devflow/operate_export_proof.py
python .devflow/operate_proof.py
python .devflow/calendar_readback_proof.py
