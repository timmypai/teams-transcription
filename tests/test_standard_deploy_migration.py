"""Behavioral regression tests for the standard devflow deployment migration.

Covers:
1. Sourcing `.devflow/config.env` executes zero commands and exports DEVFLOW_REVIEW_TEST_CMD.
2. First-cutover with an authentic verified legacy release imports the exact prior
   compose/config/seccomp/image/source_sha into `$LIVE` BEFORE container replacement;
   an injected first-cutover failure rolls back using `$LIVE/compose.yml` + original
   seccomp/config/image/source_sha (NOT the candidate checkout) without touching `/data`.
3. Tampered or missing legacy release refuses cutover BEFORE `docker compose up` runs.
4. Successful standard deploy persists `source_sha`, holds the shared `operation.lock`,
   and synchronizes retained `target_deploy.py` state (`state.json` + verified release).
5. Subsequent candidate failure restores BOTH prior image ID and prior `io.devflow.source` SHA.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from deploy.target_deploy import Controller, OWNER, PROJECT, REPO, VOLUME

ROOT = Path(__file__).resolve().parents[1]


def _make_exec(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _setup_repo_copy(repo: Path) -> None:
    (repo / ".devflow").mkdir(parents=True)
    (repo / "deploy").mkdir()
    (repo / "docker").mkdir()
    (repo / "tools").mkdir()
    for rel in (
        "Dockerfile",
        ".devflow/config.env",
        ".devflow/deploy.sh",
        ".devflow/deploy-build.sh",
        ".devflow/deploy-post.sh",
        ".devflow/import_prior_release.py",
        ".devflow/prepare-contexts.sh",
        ".devflow/capture.sh",
        ".devflow/redact.py",
        "deploy/compose.yml",
        "deploy/target_deploy.py",
        "deploy/target_diagnostics.py",
        "deploy/private_control.py",
        "tools/target_admission.py",
        "tools/pr_policy_github.py",
        "docker/chromium-seccomp.json",
    ):
        shutil.copy2(ROOT / rel, repo / rel)


def _seed_legacy_release(
    home: Path,
    *,
    sha: str,
    image: str,
    container_id: str,
    compose_text: str,
    config_text: str,
    seccomp_text: str,
    phase: str = "healthy",
) -> tuple[Path, dict]:
    ctrl_root = home / ".devflow-live" / "timmypai-teams-transcription"
    ctrl_root.mkdir(parents=True, mode=0o700, exist_ok=True)
    os.chmod(ctrl_root, 0o700)
    (ctrl_root / "owner.json").write_text(json.dumps({"repository": REPO, "uid": os.getuid()}), encoding="utf-8")
    os.chmod(ctrl_root / "owner.json", 0o600)
    rel_dir = ctrl_root / "releases" / "legacy-prev-release"
    rel_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    files = {
        "compose.yml": compose_text,
        "config.env": config_text,
        "chromium-seccomp.json": seccomp_text,
    }
    info = {
        "repository": REPO,
        "source_sha": sha,
        "image": image,
        "files": {k: hashlib.sha256(v.encode("utf-8")).hexdigest() for k, v in files.items()},
    }
    for name, text in {**files, "release.json": json.dumps(info)}.items():
        p = rel_dir / name
        p.write_text(text, encoding="utf-8")
        os.chmod(p, 0o600)
    state = {
        "phase": phase,
        "source_sha": sha,
        "image": image,
        "container_id": container_id,
        "release": rel_dir.name,
        "volume_created_at": "2026-11-20T00:00:00Z",
        "network_id": "net-123",
    }
    sp = ctrl_root / "state.json"
    sp.write_text(json.dumps(state), encoding="utf-8")
    os.chmod(sp, 0o600)
    return ctrl_root, info


MOCK_DOCKER = """#!/usr/bin/env python3
import fcntl, json, os, sys
from pathlib import Path

args = sys.argv[1:]
state_path = Path(os.environ["MOCK_DOCKER_STATE"])
log_path = Path(os.environ["MOCK_DOCKER_LOG"])
lock_path = Path(os.environ["HOME"]) / ".devflow-live" / "timmypai-teams-transcription" / "operation.lock"
if lock_path.exists():
    with lock_path.open("r+") as lf:
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
            raise SystemExit("MOCK_DOCKER_ERROR: shared operation.lock was NOT held")
        except BlockingIOError:
            pass

st = json.loads(state_path.read_text())
with log_path.open("a") as fh:
    fh.write(json.dumps({
        "args": args,
        "source": os.environ.get("DEVFLOW_SOURCE_SHA", ""),
        "image": os.environ.get("DEVFLOW_IMAGE", ""),
        "release": os.environ.get("DEVFLOW_RELEASE_DIR", ""),
    }) + "\\n")

def save():
    state_path.write_text(json.dumps(st))

if args[:2] == ["image", "inspect"]:
    ref = args[2]
    if ref not in st["images"]:
        sys.exit(1)
    if "--format" in args:
        print(st["images"][ref])
    else:
        print(json.dumps([{"Id": st["images"][ref]}]))
    sys.exit(0)

if args[:1] == ["buildx"]:
    # Record newly built candidate tag -> immutable image ID
    if "-t" in args:
        tag = args[args.index("-t") + 1]
        img_id = os.environ["MOCK_BUILT_IMAGE_ID"]
        st["images"][tag] = img_id
        st["images"][img_id] = img_id
        save()
    sys.exit(0)

if args[:1] == ["run"]:
    sys.exit(0)

if args[:1] == ["tag"]:
    src, dst = args[1], args[2]
    st["images"][dst] = st["images"].get(src, src)
    save()
    sys.exit(0)

if args[:1] == ["images"]:
    sys.exit(0)

if args[:1] == ["ps"]:
    if "-q" in args or "-aq" in args:
        print(st["container"]["Id"])
    else:
        print(json.dumps(st["container"]))
    sys.exit(0)

if args[:2] == ["volume", "inspect"]:
    print(json.dumps([st["volume"]]))
    sys.exit(0)

if args[:2] == ["network", "inspect"]:
    print(json.dumps([st["network"]]))
    sys.exit(0)

if args[:1] == ["inspect"]:
    c = st["container"]
    if "--format" in args:
        fmt = args[args.index("--format") + 1]
        if fmt == "{{.Image}}":
            print(c["Image"])
        elif "io.devflow.source" in fmt:
            print(c["Config"]["Labels"].get("io.devflow.source", ""))
        else:
            print("teams-transcription")
    else:
        print(json.dumps([c]))
    sys.exit(0)

if args[:1] == ["compose"] and "up" in args:
    f_idx = args.index("-f")
    compose_path = Path(args[f_idx + 1])
    rel_dir = Path(os.environ["DEVFLOW_RELEASE_DIR"])
    compose_text = compose_path.read_text()
    # Verify seccomp referenced by compose actually exists under DEVFLOW_RELEASE_DIR
    if "/docker/chromium-seccomp.json" in compose_text:
        assert (rel_dir / "docker" / "chromium-seccomp.json").is_file(), f"missing nested seccomp in {rel_dir}"
    elif "/chromium-seccomp.json" in compose_text:
        assert (rel_dir / "chromium-seccomp.json").is_file(), f"missing flat seccomp in {rel_dir}"
    img_ref = os.environ["DEVFLOW_IMAGE"]
    src_sha = os.environ["DEVFLOW_SOURCE_SHA"]
    img_id = st["images"][img_ref]
    st["images"][img_id] = img_id
    st["container"]["Id"] = f"cid-{src_sha[:8]}-{img_id[-8:]}"
    st["container"]["Image"] = img_id
    st["container"]["Config"]["Labels"]["io.devflow.source"] = src_sha
    st["last_compose_sha256"] = __import__("hashlib").sha256(compose_text.encode()).hexdigest()
    st["last_compose_file"] = str(compose_path)
    st["last_release_dir"] = str(rel_dir)
    save()
    sys.exit(0)

if args[:1] == ["logs"]:
    sys.exit(0)

sys.exit(0)
"""


class StandardDeployMigrationTests(unittest.TestCase):
    def test_sourcing_config_env_executes_no_commands_and_sets_review_test_cmd(self) -> None:
        cmd = (
            'python3() { printf "UNEXPECTED_CMD %s\\n" "$*"; return 99; }; '
            'set -euo pipefail; set -a; . .devflow/config.env; set +a; '
            'printf "REVIEW=%s\\nLOCK=%s\\n" "${DEVFLOW_REVIEW_TEST_CMD:-}" "${DEVFLOW_LOCK_FILE:-}"'
        )
        proc = subprocess.run(["bash", "-c", cmd], cwd=ROOT, text=True, capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertNotIn("UNEXPECTED_CMD", proc.stdout)
        self.assertIn("REVIEW=PYTHONPATH=tests:. python3 -m unittest", proc.stdout)
        self.assertIn("operation.lock", proc.stdout)

    def test_first_cutover_imports_verified_prior_release_and_rolls_back_exact_files(self) -> None:
        sha_old = "a" * 40
        sha_v2 = "b" * 40
        sha_bad = "c" * 40
        img_old = "sha256:" + ("1" * 64)
        img_v2 = "sha256:" + ("2" * 64)
        img_bad = "sha256:" + ("3" * 64)

        with tempfile.TemporaryDirectory(prefix="teams-std-mig-") as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _setup_repo_copy(repo)
            # Controllable verify.sh; deploy.sh, deploy-build.sh, import_prior_release.py, deploy-post.sh are UNMODIFIED!
            _make_exec(repo / ".devflow" / "verify.sh", '#!/usr/bin/env bash\nexit "${MOCK_VERIFY_RC:-0}"\n')

            # Deliberately distinct legacy compose (flat /chromium-seccomp.json + legacy comment) & legacy seccomp
            old_compose = (
                (ROOT / "deploy" / "compose.yml")
                .read_text(encoding="utf-8")
                .replace("/docker/chromium-seccomp.json", "/chromium-seccomp.json")
                + "# legacy-release-marker\n"
            )
            old_seccomp = json.dumps({"defaultAction": "SCMP_ACT_ERRNO", "legacyMarker": True}) + "\n"
            old_config = (ROOT / ".devflow" / "config.env").read_text(encoding="utf-8")

            home = base / "home"
            home.mkdir()
            ctrl_root, legacy_info = _seed_legacy_release(
                home,
                sha=sha_old,
                image=img_old,
                container_id="cid-old-" + ("0" * 56),
                compose_text=old_compose,
                config_text=old_config,
                seccomp_text=old_seccomp,
            )
            app_data_sentinel = base / "data-sentinel.txt"
            app_data_sentinel.write_text("must-not-change\n", encoding="utf-8")

            mock_bin = base / "bin"
            mock_bin.mkdir()
            state_file = base / "mock_docker.json"
            log_file = base / "mock_docker.log"
            state_file.write_text(
                json.dumps(
                    {
                        "images": {img_old: img_old},
                        "container": {
                            "Id": "cid-old-" + ("0" * 56),
                            "Image": img_old,
                            "Config": {
                                "User": "10001:10001",
                                "Labels": {
                                    OWNER: REPO,
                                    "io.devflow.source": sha_old,
                                    "com.docker.compose.project": PROJECT,
                                    "com.docker.compose.service": "teams-transcription",
                                },
                            },
                            "HostConfig": {
                                "PortBindings": {"8080/tcp": [{"HostIp": "127.0.0.1", "HostPort": "18101"}]}
                            },
                            "State": {"Running": True, "Status": "running", "StartedAt": "2026-11-25T00:00:00Z"},
                        },
                        "volume": {"Name": VOLUME, "CreatedAt": "2026-11-20T00:00:00Z", "Labels": {OWNER: REPO}},
                        "network": {"Id": "net-123", "Name": PROJECT, "Labels": {OWNER: REPO}},
                    }
                ),
                encoding="utf-8",
            )
            _make_exec(mock_bin / "curl", "#!/usr/bin/env bash\nexit 0\n")
            _make_exec(mock_bin / "docker", MOCK_DOCKER)

            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{mock_bin}:{os.environ['PATH']}",
                "MOCK_DOCKER_STATE": str(state_file),
                "MOCK_DOCKER_LOG": str(log_file),
            }

            # 1. First-cutover failure: MUST rollback using the imported legacy compose/seccomp/image/sha, NOT candidate!
            env1 = {**env, "GITHUB_SHA": sha_v2, "MOCK_BUILT_IMAGE_ID": img_v2, "MOCK_VERIFY_RC": "1"}
            p1 = subprocess.run(["bash", ".devflow/deploy.sh"], cwd=repo, env=env1, text=True, capture_output=True)
            self.assertEqual(p1.returncode, 1, p1.stdout + p1.stderr)
            self.assertIn("rollback healthy", p1.stderr)
            live = home / ".devflow-live" / "teams-transcription"
            self.assertTrue(live.is_symlink())
            st1 = json.loads(state_file.read_text())
            self.assertEqual(st1["container"]["Image"], img_old)
            self.assertEqual(st1["container"]["Config"]["Labels"]["io.devflow.source"], sha_old)
            self.assertEqual(st1["last_compose_file"], str(live / "compose.yml"))
            self.assertEqual(st1["last_release_dir"], str(live))
            self.assertEqual(st1["last_compose_sha256"], legacy_info["files"]["compose.yml"])
            # Synced state after rollback must preserve exact original legacy compose & seccomp digests!
            rb_state = json.loads((ctrl_root / "state.json").read_text())
            rb_info = Controller(ctrl_root, 1, 1, command=lambda *a, **k: "").verify_release(
                ctrl_root / "releases" / rb_state["release"]
            )
            self.assertEqual(rb_info["files"]["compose.yml"], legacy_info["files"]["compose.yml"])
            self.assertEqual(rb_info["files"]["chromium-seccomp.json"], legacy_info["files"]["chromium-seccomp.json"])
            self.assertEqual(app_data_sentinel.read_text(), "must-not-change\n")

            # 2. Successful standard deploy at sha_v2: saves release + source_sha + syncs target_deploy state.json
            env2 = {**env, "GITHUB_SHA": sha_v2, "MOCK_BUILT_IMAGE_ID": img_v2, "MOCK_VERIFY_RC": "0"}
            p2 = subprocess.run(["bash", ".devflow/deploy.sh"], cwd=repo, env=env2, text=True, capture_output=True)
            self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
            self.assertEqual((live / "source_sha").read_text().strip(), sha_v2)
            self.assertTrue((live / "docker" / "chromium-seccomp.json").is_file())
            synced_state = json.loads((ctrl_root / "state.json").read_text())
            self.assertEqual(synced_state["phase"], "healthy")
            self.assertEqual(synced_state["source_sha"], sha_v2)
            self.assertEqual(synced_state["image"], img_v2)
            st2 = json.loads(state_file.read_text())
            self.assertEqual(synced_state["container_id"], st2["container"]["Id"])
            v2_info = Controller(ctrl_root, 1, 1, command=lambda *a, **k: "").verify_release(
                ctrl_root / "releases" / synced_state["release"]
            )
            self.assertEqual(v2_info["image"], img_v2)
            self.assertEqual(v2_info["source_sha"], sha_v2)

            # 3. Subsequent candidate failure at sha_bad: rollback restores BOTH img_v2 AND sha_v2 (NOT sha_bad!)
            env3 = {**env, "GITHUB_SHA": sha_bad, "MOCK_BUILT_IMAGE_ID": img_bad, "MOCK_VERIFY_RC": "1"}
            p3 = subprocess.run(["bash", ".devflow/deploy.sh"], cwd=repo, env=env3, text=True, capture_output=True)
            self.assertEqual(p3.returncode, 1, p3.stdout + p3.stderr)
            self.assertIn("rollback healthy", p3.stderr)
            st3 = json.loads(state_file.read_text())
            self.assertEqual(st3["container"]["Image"], img_v2)
            self.assertEqual(st3["container"]["Config"]["Labels"]["io.devflow.source"], sha_v2)
            synced_after_rb = json.loads((ctrl_root / "state.json").read_text())
            self.assertEqual(synced_after_rb["source_sha"], sha_v2)
            self.assertEqual(synced_after_rb["image"], img_v2)
            self.assertEqual(synced_after_rb["container_id"], st3["container"]["Id"])

    def test_tampered_legacy_release_refuses_first_cutover_before_compose_up(self) -> None:
        sha_old = "a" * 40
        sha_v2 = "b" * 40
        img_old = "sha256:" + ("1" * 64)
        img_v2 = "sha256:" + ("2" * 64)

        with tempfile.TemporaryDirectory(prefix="teams-std-tamper-") as tmp:
            base = Path(tmp)
            repo = base / "repo"
            _setup_repo_copy(repo)
            _make_exec(repo / ".devflow" / "verify.sh", "#!/usr/bin/env bash\nexit 0\n")

            home = base / "home"
            home.mkdir()
            ctrl_root, _ = _seed_legacy_release(
                home,
                sha=sha_old,
                image=img_old,
                container_id="cid-old-" + ("0" * 56),
                compose_text=(ROOT / "deploy" / "compose.yml").read_text(encoding="utf-8"),
                config_text=(ROOT / ".devflow" / "config.env").read_text(encoding="utf-8"),
                seccomp_text=(ROOT / "docker" / "chromium-seccomp.json").read_text(encoding="utf-8"),
            )
            # Tamper with legacy compose.yml after release.json hash was written
            (ctrl_root / "releases" / "legacy-prev-release" / "compose.yml").write_text("tampered\n", encoding="utf-8")

            mock_bin = base / "bin"
            mock_bin.mkdir()
            state_file = base / "mock_docker.json"
            log_file = base / "mock_docker.log"
            state_file.write_text(
                json.dumps(
                    {
                        "images": {img_old: img_old},
                        "container": {"Id": "cid-old", "Image": img_old, "Config": {"Labels": {}}, "State": {"Running": True}},
                        "volume": {"Name": VOLUME, "CreatedAt": "2026-11-20T00:00:00Z", "Labels": {OWNER: REPO}},
                        "network": {"Id": "net-123", "Name": PROJECT, "Labels": {OWNER: REPO}},
                    }
                ),
                encoding="utf-8",
            )
            _make_exec(mock_bin / "curl", "#!/usr/bin/env bash\nexit 0\n")
            _make_exec(mock_bin / "docker", MOCK_DOCKER)

            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{mock_bin}:{os.environ['PATH']}",
                "GITHUB_SHA": sha_v2,
                "MOCK_BUILT_IMAGE_ID": img_v2,
                "MOCK_DOCKER_STATE": str(state_file),
                "MOCK_DOCKER_LOG": str(log_file),
            }
            proc = subprocess.run(["bash", ".devflow/deploy.sh"], cwd=repo, env=env, text=True, capture_output=True)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("release_config_tampered", proc.stderr)
            entries = [json.loads(line) for line in log_file.read_text().splitlines()] if log_file.exists() else []
            self.assertFalse(any(e["args"][:1] == ["compose"] and "up" in e["args"] for e in entries), entries)


if __name__ == "__main__":
    unittest.main()
