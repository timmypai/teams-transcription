#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
git diff --check
python .devflow/check.py
python -m compileall -q .devflow deploy/isolated_deployment.py
PYTHONPATH=tests:. python -m unittest test_isolated_deployment test_workflow_triggers -v
python tools/check_repo_skills.py
python .devflow/diagnostics_proof.py
if [[ ${RUNNER_ENVIRONMENT:-} == github-hosted ]]; then
  python .devflow/host_contract.py
fi
