#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
python .devflow/regression.py "${DEVFLOW_EVIDENCE:-.devflow/evidence-test}"
