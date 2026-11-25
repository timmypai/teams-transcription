#!/usr/bin/env bash
# Post-deploy / post-rollback hook invoked by standard `.devflow/deploy.sh` while
# `DEVFLOW_LOCK_FILE` is still held. Reconciles the authoritative live container
# and standard release into the retained control/readiness state directory so
# `control.yml`, `readiness.yml`, and `google-client.yml` stay synchronized.
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
import hashlib
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

ROOT = Path.cwd()
REPO = "timmypai/teams-transcription"
APP = "devflow-teams-transcription-app"
VOLUME = "devflow-teams-transcription-data"
NETWORK = "devflow-teams-transcription"

state_root = Path.home() / ".devflow-live" / "timmypai-teams-transcription"
os.umask(0o077)
state_root.parent.mkdir(mode=0o700, exist_ok=True)
state_root.mkdir(mode=0o700, exist_ok=True)
os.chmod(state_root, 0o700)

owner_path = state_root / "owner.json"
owner_expected = {"repository": REPO, "uid": os.getuid()}
if owner_path.exists():
    existing_owner = json.loads(owner_path.read_text(encoding="utf-8"))
    if existing_owner != owner_expected:
        raise SystemExit(f"deploy-post: state_owner_collision: {existing_owner}")
else:
    tmp = owner_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(owner_expected) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(owner_path)


def docker_json(*args: str):
    raw = subprocess.check_output(["docker", *args], text=True, timeout=30)
    data = json.loads(raw)
    return data[0] if isinstance(data, list) else data


container = docker_json("inspect", APP)
volume = docker_json("volume", "inspect", VOLUME)
network = docker_json("network", "inspect", NETWORK)

image = container["Image"]
container_id = container["Id"]
labels = (container.get("Config") or {}).get("Labels") or {}
source_sha = os.environ.get("DEVFLOW_SOURCE_SHA") or labels.get("io.devflow.source") or ""
if not source_sha:
    raise SystemExit("deploy-post: missing DEVFLOW_SOURCE_SHA")

phase = os.environ.get("DEVFLOW_DEPLOY_PHASE", "deploy")
live_dir = Path(os.environ.get("DEVFLOW_LIVE_DIR") or (Path.home() / ".devflow-live" / "teams-transcription"))
use_live = phase == "rollback" and live_dir.is_dir()

compose_src = (live_dir / "compose.yml") if (use_live and (live_dir / "compose.yml").is_file()) else (ROOT / "deploy" / "compose.yml")
config_src = (live_dir / "config.env") if (use_live and (live_dir / "config.env").is_file()) else (ROOT / ".devflow" / "config.env")
seccomp_src = (
    (live_dir / "docker" / "chromium-seccomp.json")
    if (use_live and (live_dir / "docker" / "chromium-seccomp.json").is_file())
    else (ROOT / "docker" / "chromium-seccomp.json")
)

releases_dir = state_root / "releases"
releases_dir.mkdir(mode=0o700, exist_ok=True)
rel_name = f"{source_sha}-std-{int(time.time())}-{uuid.uuid4().hex[:8]}"
rel_dir = releases_dir / rel_name
rel_dir.mkdir(mode=0o700)
(rel_dir / "docker").mkdir(mode=0o700)

files_map = {
    "compose.yml": compose_src.read_bytes(),
    "config.env": config_src.read_bytes(),
    "chromium-seccomp.json": seccomp_src.read_bytes(),
}
for name, data in files_map.items():
    p = rel_dir / name
    p.write_bytes(data)
    os.chmod(p, 0o600)
(rel_dir / "docker" / "chromium-seccomp.json").write_bytes(files_map["chromium-seccomp.json"])
os.chmod(rel_dir / "docker" / "chromium-seccomp.json", 0o600)

rel_info = {
    "repository": REPO,
    "source_sha": source_sha,
    "image": image,
    "phase": phase,
    "files": {name: hashlib.sha256(data).hexdigest() for name, data in files_map.items()},
}
rel_json = rel_dir / "release.json"
rel_json.write_text(json.dumps(rel_info, indent=2) + "\n", encoding="utf-8")
os.chmod(rel_json, 0o600)

state = {
    "phase": "healthy",
    "source_sha": source_sha,
    "image": image,
    "container_id": container_id,
    "volume_created_at": volume["CreatedAt"],
    "network_id": network["Id"],
    "release": rel_name,
    "updated_by": "devflow-standard-deploy",
    "deploy_phase": phase,
}
state_path = state_root / "state.json"
tmp_state = state_root / "state.json.tmp"
tmp_state.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
os.chmod(tmp_state, 0o600)
tmp_state.replace(state_path)
print(f"deploy-post: synced {state_path} (phase={phase}, sha={source_sha[:12]}, container={container_id[:12]})")
PY
