#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
git diff --check
python -m compileall -q .devflow deploy/isolated_deployment.py
PYTHONPATH=tests:. python -m unittest test_isolated_deployment test_workflow_triggers -v
python tools/check_repo_skills.py
