#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
base=${1:?base SHA required}
head=$PWD
test "$RUNNER_ENVIRONMENT" = github-hosted
base=$(git rev-parse --verify "${base}^{commit}")
base_tree="$RUNNER_TEMP/teams-transcription-base"
git worktree add --detach "$base_tree" "$base"
SOURCE_SHA="$base" DEVFLOW_SOURCE_DIR="$base_tree" bash .devflow/build.sh
DEVFLOW_BASELINE=1 DEVFLOW_BASE_ROOT="$base_tree" SOURCE_SHA="$base" \
  DEVFLOW_KIND=base DEVFLOW_EVIDENCE="$head/.devflow/evidence-base" \
  python .devflow/hosted_verify.py
# The UI can be Already-met. New CLI adapter is absent on base: INVALID,
# not a manufactured Given/When failure. Exemptions require independent review.
DEVFLOW_EVIDENCE="$head/.devflow/evidence-base" \
  python .devflow/acceptance.py classify-base --base-sha "$base"
git worktree remove "$base_tree"
