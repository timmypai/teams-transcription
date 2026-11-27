#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
git diff --check
python .devflow/check.py
if [[ ${GITHUB_EVENT_NAME:-} == pull_request ]]; then
  python .devflow/run_pr_gate.py
fi
python -m compileall -q .devflow deploy/isolated_deployment.py
PYTHONPATH=tests:. python -m unittest test_isolated_deployment test_workflow_triggers -v
python tools/check_repo_skills.py
python .devflow/diagnostics_proof.py
python .devflow/check_pr_proof.py
if [[ ${RUNNER_ENVIRONMENT:-} == github-hosted ]]; then
  python .devflow/host_contract.py
fi
