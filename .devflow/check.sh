#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
git diff --check
python -m compileall -q .devflow deploy/isolated_deployment.py
PYTHONPATH=tests:. python -m unittest test_isolated_deployment test_workflow_triggers -v
python tools/check_repo_skills.py
python .devflow/diagnostics_proof.py
python .devflow/build_evidence_proof.py # Lesson: L-0006 fixed consumer must distinguish next actions
if [[ ${RUNNER_ENVIRONMENT:-} == github-hosted ]]; then
  python .devflow/host_contract.py
fi
