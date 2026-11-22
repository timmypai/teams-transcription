#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
# This entry point only creates disposable hosted resources; never Oracle.
python .devflow/hosted_verify.py
python .devflow/operate_proof.py
