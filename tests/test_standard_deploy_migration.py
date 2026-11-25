"""Behavioral regression tests for the standard devflow deployment migration.

Covers:
1. Sourcing `.devflow/config.env` executes zero commands and exports DEVFLOW_REVIEW_TEST_CMD.
2. First-cutover failure seeds `:live` + pre-cutover source SHA from the running container
   and rolls back cleanly without losing prior image/source identity.
3. Successful standard deploy persists `source_sha`, acquires the shared `operation.lock`,
   and synchronizes retained `target_deploy.py` state (`state.json` + verified release) so
   `control.yml` / `readiness.yml` / `google-client.yml` validate the replaced container.
4. Subsequent candidate failure restores BOTH prior image ID and prior `io.devflow.source` SHA.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from deploy.target_deploy import Controller, APP, OWNER, PROJECT, REPO, VOLUME

ROOT = Path(__file__).resolve().parents[1]


def _make_exec(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


class StandardDeployMigrationTests(unittest.TestCase):
    def test_sourcing_config_env_executes_no_commands_and_sets_review_test_cmd(self) -> None:
        cmd = (
            'python3() { printf "UNEXPECTED_CMD %s\\n" "$*"; return 99; }; '
            'set -euo pipefail; set -a; . .devflow/config.env; set +a; '
            'printf "REVIEW=%s\\nLOCK=%s\\n" "${DEVFLOW_REVIEW_TEST_CMD:-}" "${DEVFLOW_LOCK_FILE:-}"'
        )
        proc = subprocess.run(
            ["bash", "-c", cmd],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr + proc.stdout)
        self.assertNotIn("UNEXPECTED_CMD", proc.stdout)
        self.assertIn("REVIEW=PYTHONPATH=tests:. python3 -m unittest", proc.stdout)
        self.assertIn("operation.lock", proc.stdout)

    def test_first_cutover_rollback_state_sync_and_source_sha_preservation(self) -> None:
        sha_old = "a" * 40
        sha_v2 = "b" * 40
        sha_bad = "c" * 40
        img_old = "sha256:" + ("1" * 64)
        img_v2 = "sha256:" + ("2" * 64)
        img_bad = "sha256:" + ("3" * 64)

        with tempfile.TemporaryDirectory(prefix="teams-std-mig-") as tmp:
            base = Path(tmp)
            repo = base / "repo"
            (repo / ".devflow").mkdir(parents=True)
            (repo / "deploy").mkdir()
            (repo / "docker").mkdir()
            for rel in (
                ".devflow/config.env",
                ".devflow/deploy.sh",
                ".devflow/deploy-post.sh",
                ".devflow/capture.sh",
                ".devflow/redact.py",
                "deploy/compose.yml",
                "docker/chromium-seccomp.json",
            ):
                shutil.copy2(ROOT / rel, repo / rel)

            # Stub deploy-build.sh & verify.sh (controllable via MOCK_VERIFY_RC)
            _make_exec(repo / ".devflow" / "deploy-build.sh", "#!/usr/bin/env bash\nexit 0\n")
            _make_exec(repo / ".devflow" / "verify.sh", '#!/usr/bin/env bash\nexit "${MOCK_VERIFY_RC:-0}"\n')

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
            _make_exec(
                mock_bin / "docker",
                """#!/usr/bin/env python3
import json, os, sys
from pathlib import Path

args = sys.argv[1:]
state_path = Path(os.environ["MOCK_DOCKER_STATE"])
log_path = Path(os.environ["MOCK_DOCKER_LOG"])
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
    target = args[-1]
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
    img_ref = os.environ["DEVFLOW_IMAGE"]
    src_sha = os.environ["DEVFLOW_SOURCE_SHA"]
    img_id = st["images"].get(img_ref)
    if not img_id:
        img_id = os.environ.get("MOCK_BUILT_IMAGE_ID", "sha256:" + ("9" * 64))
        st["images"][img_ref] = img_id
    st["images"][img_id] = img_id
    st["container"]["Id"] = f"cid-{src_sha[:8]}-{img_id[-8:]}"
    st["container"]["Image"] = img_id
    st["container"]["Config"]["Labels"]["io.devflow.source"] = src_sha
    save()
    sys.exit(0)

if args[:1] == ["logs"]:
    sys.exit(0)

sys.exit(0)
""",
            )

            home = base / "home"
            home.mkdir()
            env = {
                **os.environ,
                "HOME": str(home),
                "PATH": f"{mock_bin}:{os.environ['PATH']}",
                "MOCK_DOCKER_STATE": str(state_file),
                "MOCK_DOCKER_LOG": str(log_file),
            }

            # 1. First-cutover failure: must seed :live from pre-existing container and roll back to img_old + sha_old
            env1 = {**env, "GITHUB_SHA": sha_v2, "MOCK_BUILT_IMAGE_ID": img_v2, "MOCK_VERIFY_RC": "1"}
            p1 = subprocess.run(["bash", ".devflow/deploy.sh"], cwd=repo, env=env1, text=True, capture_output=True)
            self.assertEqual(p1.returncode, 1, p1.stdout + p1.stderr)
            self.assertIn("rollback healthy", p1.stderr)
            st1 = json.loads(state_file.read_text())
            self.assertEqual(st1["container"]["Image"], img_old)
            self.assertEqual(st1["container"]["Config"]["Labels"]["io.devflow.source"], sha_old)

            # 2. Successful standard deploy at sha_v2: saves release + source_sha + syncs target_deploy state.json
            env2 = {**env, "GITHUB_SHA": sha_v2, "MOCK_BUILT_IMAGE_ID": img_v2, "MOCK_VERIFY_RC": "0"}
            p2 = subprocess.run(["bash", ".devflow/deploy.sh"], cwd=repo, env=env2, text=True, capture_output=True)
            self.assertEqual(p2.returncode, 0, p2.stdout + p2.stderr)
            live = home / ".devflow-live" / "teams-transcription"
            self.assertEqual((live / "source_sha").read_text().strip(), sha_v2)
            self.assertTrue((live / "docker" / "chromium-seccomp.json").is_file())

            # Retained target_deploy.py consumer validates synced state & release cleanly!
            ctrl_root = home / ".devflow-live" / "timmypai-teams-transcription"
            synced_state = json.loads((ctrl_root / "state.json").read_text())
            self.assertEqual(synced_state["phase"], "healthy")
            self.assertEqual(synced_state["source_sha"], sha_v2)
            self.assertEqual(synced_state["image"], img_v2)
            st2 = json.loads(state_file.read_text())
            self.assertEqual(synced_state["container_id"], st2["container"]["Id"])
            ctrl = Controller(ctrl_root, 1, 1, command=lambda *a, **k: "")
            verified = ctrl.verify_release(ctrl_root / "releases" / synced_state["release"])
            self.assertEqual(verified["image"], img_v2)
            self.assertEqual(verified["source_sha"], sha_v2)

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


if __name__ == "__main__":
    unittest.main()
