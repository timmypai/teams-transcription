"""Negative & positive proof that production `.devflow/check.sh` enforces `--pr` on `pull_request`."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)
    return proc.stdout.strip()


def _commit(repo: Path, msg: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _run_check_sh(
    repo: Path,
    *,
    event_name: str,
    base_sha: str | None = None,
    title: str = "",
    body: str = "",
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    env["GITHUB_EVENT_NAME"] = event_name
    env["RUNNER_ENVIRONMENT"] = ""
    if base_sha is None:
        env.pop("GITHUB_EVENT_PATH", None)
        return subprocess.run(["bash", ".devflow/check.sh"], cwd=repo, env=env, capture_output=True, text=True, check=False)
    ev_file = repo.parent / "event.json"
    ev_file.write_text(
        json.dumps({"pull_request": {"base": {"sha": base_sha}, "title": title, "body": body}}),
        encoding="utf-8",
    )
    env["GITHUB_EVENT_PATH"] = str(ev_file)
    return subprocess.run(["bash", ".devflow/check.sh"], cwd=repo, env=env, capture_output=True, text=True, check=False)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="devflow-check-pr-proof-") as tmp:
        tmp_path = Path(tmp)
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-b", "main", "-q")
        _git(repo, "config", "user.name", "Check Proof")
        _git(repo, "config", "user.email", "proof@example.invalid")

        # Copy production `.devflow/check.sh`, `check.py`, `run_pr_gate.py`, and active rules.
        shutil.copytree(ROOT / ".devflow" / "rules", repo / ".devflow" / "rules")
        for name in ("check.sh", "check.py", "run_pr_gate.py"):
            dst = repo / ".devflow" / name
            shutil.copy2(ROOT / ".devflow" / name, dst)
        (repo / ".devflow" / "check.sh").chmod(0o755)

        # Minimal pass-through stubs for non-lint steps in `.devflow/check.sh`.
        (repo / "deploy").mkdir()
        (repo / "deploy" / "isolated_deployment.py").write_text("VALUE = 1\n", encoding="utf-8")
        (repo / "tests").mkdir()
        for tname in ("test_isolated_deployment.py", "test_workflow_triggers.py"):
            (repo / "tests" / tname).write_text(
                "import unittest\nclass Ok(unittest.TestCase):\n    def test_ok(self):\n        pass\n",
                encoding="utf-8",
            )
        (repo / "tools").mkdir()
        (repo / "tools" / "check_repo_skills.py").write_text("print('skills stub ok')\n", encoding="utf-8")
        for pname in ("diagnostics_proof.py", "check_pr_proof.py"):
            (repo / ".devflow" / pname).write_text("print('proof stub ok')\n", encoding="utf-8")
        (repo / "teams_transcription").mkdir()
        (repo / "teams_transcription" / "__init__.py").write_text("", encoding="utf-8")
        (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        base_sha = _commit(repo, "base")

        # 1. Valid 1-file PR passes end-to-end through `bash .devflow/check.sh`.
        _git(repo, "checkout", "-q", "-b", "pr-ok")
        (repo / "teams_transcription" / "one.py").write_text("OK = 1\n", encoding="utf-8")
        _commit(repo, "feat: one file")
        p_ok = _run_check_sh(repo, event_name="pull_request", base_sha=base_sha, title="feat: one file", body="ok")
        assert p_ok.returncode == 0, f"expected pass for valid PR, got {p_ok.returncode}:\n{p_ok.stdout}\n{p_ok.stderr}"

        # 2. Missing GITHUB_EVENT_PATH on pull_request fails closed (exit != 0).
        p_missing = _run_check_sh(repo, event_name="pull_request", base_sha=None)
        assert p_missing.returncode != 0, "expected failure when GITHUB_EVENT_PATH is missing on pull_request"

        # 3. `fix:` PR without Lesson / No-lesson fails through `bash .devflow/check.sh`.
        p_fix = _run_check_sh(repo, event_name="pull_request", base_sha=base_sha, title="fix: missing lesson", body="none")
        assert p_fix.returncode != 0 and "Lesson: L-NNNN" in (p_fix.stdout + p_fix.stderr), (
            f"expected Lesson gate failure:\n{p_fix.stdout}\n{p_fix.stderr}"
        )

        # 4. Oversized 36-file PR fails `pr-diff-budget` through `bash .devflow/check.sh`.
        _git(repo, "checkout", "-q", "main")
        _git(repo, "checkout", "-q", "-b", "pr-big")
        for i in range(36):
            (repo / "teams_transcription" / f"probe_{i:02d}.py").write_text(f"VAL = {i}\n", encoding="utf-8")
        _commit(repo, "feat: oversized")
        p_big = _run_check_sh(repo, event_name="pull_request", base_sha=base_sha, title="feat: oversized", body="ok")
        out_big = p_big.stdout + p_big.stderr
        assert p_big.returncode != 0 and "changed_files=36" in out_big, (
            f"expected pr-diff-budget failure for 36 files, got {p_big.returncode}:\n{out_big}"
        )

    print("check_pr_proof: production .devflow/check.sh PR-gate checks verified (1 pass, 3 negative fail-closed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
