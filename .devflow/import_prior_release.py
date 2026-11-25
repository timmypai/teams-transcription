#!/usr/bin/env python3
"""Materialize a verified legacy `target_deploy.py` release into standard `$LIVE` before cutover.

Pure stdlib (zero transitive imports). Runs before `docker compose up` while
`DEVFLOW_LOCK_FILE` is held:
- If `$LIVE/image` already exists: no-op (already on standard devflow releases).
- If `~/.devflow-live/timmypai-teams-transcription/state.json` exists: verifies
  `phase == "healthy"`, verifies `releases/<release>/release.json` SHA-256 digests
  for `compose.yml`, `config.env`, and `chromium-seccomp.json`, verifies the exact
  immutable image exists in Docker, and atomically materializes a complete standard
  `$LIVE` snapshot (`image`, `source_sha`, `compose.yml`, `config.env`, `.env`,
  `chromium-seccomp.json`, and `docker/chromium-seccomp.json`). Fails closed on any
  tampering, missing image, or unhealthy state before replacement.
- If the app container exists without `state.json`: fails closed before replacement.
- If neither `state.json` nor the app container exists (fresh host): no-op.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import uuid

REPO = "timmypai/teams-transcription"
APP = "devflow-teams-transcription-app"
IMAGE_RE = re.compile(r"^sha256:[0-9a-f]{64}\Z")
REQUIRED_FILES = frozenset({"compose.yml", "config.env", "chromium-seccomp.json"})


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], text=True, capture_output=True, timeout=30, check=check)


def verify_legacy_release(legacy_root: Path, rel_dir: Path) -> dict:
    if rel_dir.parent != legacy_root / "releases" or rel_dir.resolve() != rel_dir:
        raise ValueError("release_path_rejected")
    info = json.loads((rel_dir / "release.json").read_text(encoding="utf-8"))
    if not isinstance(info.get("image"), str) or not IMAGE_RE.fullmatch(info["image"]):
        raise ValueError("release_image_invalid")
    files = info.get("files")
    if not isinstance(files, dict) or set(files) != REQUIRED_FILES:
        raise ValueError("release_incomplete")
    for name, digest in files.items():
        if name not in REQUIRED_FILES or hashlib.sha256((rel_dir / name).read_bytes()).hexdigest() != digest:
            raise ValueError("release_config_tampered")
    return info


def main() -> int:
    os.umask(0o077)
    app_name = os.environ.get("DEVFLOW_APP") or "teams-transcription"
    live = Path(os.environ.get("DEVFLOW_LIVE_DIR") or (Path.home() / ".devflow-live" / app_name))
    if (live / "image").is_file():
        return 0

    legacy_root = Path.home() / ".devflow-live" / "timmypai-teams-transcription"
    state_path = legacy_root / "state.json"

    if not state_path.exists():
        probe = _docker("inspect", APP, check=False)
        if probe.returncode == 0:
            raise SystemExit(
                f"import_prior_release: refusing first cutover — container {APP} exists without verified {state_path}"
            )
        return 0

    state = json.loads(state_path.read_text(encoding="utf-8"))
    if state.get("phase") != "healthy":
        raise SystemExit(
            f"import_prior_release: refusing first cutover — legacy state phase is {state.get('phase')!r}, expected 'healthy'"
        )
    rel_name = state.get("release") or ""
    if not rel_name:
        raise SystemExit("import_prior_release: legacy state missing release name")

    rel_dir = legacy_root / "releases" / rel_name
    try:
        info = verify_legacy_release(legacy_root, rel_dir)
    except (ValueError, OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f"import_prior_release: {exc}") from exc

    image = state.get("image") or ""
    source_sha = state.get("source_sha") or ""
    if image != info.get("image") or source_sha != info.get("source_sha"):
        raise SystemExit("import_prior_release: legacy state.json disagrees with verified release.json")
    if info.get("repository") not in (None, REPO):
        raise SystemExit("import_prior_release: legacy release repository mismatch")

    img_probe = _docker("image", "inspect", image, check=False)
    if img_probe.returncode != 0:
        raise SystemExit(f"import_prior_release: prior immutable image {image} not present in Docker")

    live_d = Path(f"{live}.d")
    live_d.mkdir(mode=0o700, parents=True, exist_ok=True)
    snap_dir = live_d / f"imported-{source_sha[:12]}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"
    snap_dir.mkdir(mode=0o700)
    (snap_dir / "docker").mkdir(mode=0o700)

    for fname in ("compose.yml", "config.env", "chromium-seccomp.json"):
        data = (rel_dir / fname).read_bytes()
        out = snap_dir / fname
        out.write_bytes(data)
        os.chmod(out, 0o600)
    seccomp_nested = snap_dir / "docker" / "chromium-seccomp.json"
    seccomp_nested.write_bytes((rel_dir / "chromium-seccomp.json").read_bytes())
    os.chmod(seccomp_nested, 0o600)

    env_file = snap_dir / ".env"
    env_file.write_text("", encoding="utf-8")
    os.chmod(env_file, 0o600)

    (snap_dir / "image").write_text(image + "\n", encoding="utf-8")
    os.chmod(snap_dir / "image", 0o600)
    (snap_dir / "source_sha").write_text(source_sha + "\n", encoding="utf-8")
    os.chmod(snap_dir / "source_sha", 0o600)

    link_tmp = Path(f"{live}.new.{os.getpid()}")
    if link_tmp.exists() or link_tmp.is_symlink():
        link_tmp.unlink()
    link_tmp.symlink_to(snap_dir)
    os.replace(link_tmp, live)
    print(f"import_prior_release: imported verified release {rel_name} (sha={source_sha[:12]}) -> {live}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
