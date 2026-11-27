"""Fail-closed Pull Request gate runner invoked by `.devflow/check.sh` on `pull_request` events."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

SHA40_RE = re.compile(r"^[0-9a-f]{40}$")


def main() -> int:
    event_path_raw = os.environ.get("GITHUB_EVENT_PATH", "").strip()
    if not event_path_raw:
        print("CONFIG pull_request gate requires GITHUB_EVENT_PATH", file=sys.stderr)
        return 2
    event_path = Path(event_path_raw)
    if not event_path.is_file():
        print(f"CONFIG GITHUB_EVENT_PATH not found: {event_path}", file=sys.stderr)
        return 2
    try:
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as err:
        print(f"CONFIG invalid GITHUB_EVENT_PATH JSON: {err}", file=sys.stderr)
        return 2
    pr = payload.get("pull_request") if isinstance(payload, dict) else None
    base = pr.get("base") if isinstance(pr, dict) else None
    base_sha = str(base.get("sha") or "").strip().lower() if isinstance(base, dict) else ""
    if not SHA40_RE.fullmatch(base_sha):
        print(f"CONFIG pull_request.base.sha invalid or missing: {base_sha!r}", file=sys.stderr)
        return 2

    title = str(pr.get("title") or "") if isinstance(pr, dict) else ""
    body = str(pr.get("body") or "") if isinstance(pr, dict) else ""
    env = os.environ.copy()
    env["PR_TITLE"] = title
    env["PR_BODY"] = body
    check_py = Path(__file__).resolve().parent / "check.py"
    proc = subprocess.run([sys.executable, str(check_py), "--pr", base_sha], env=env, check=False)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
