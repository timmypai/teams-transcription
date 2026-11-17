#!/usr/bin/env bash
set -euo pipefail
cd "${DEVFLOW_SOURCE_DIR:-$(dirname "$0")/..}"
: "${RUNNER_TEMP:?GitHub-hosted scratch required}"
: "${SOURCE_SHA:?exact source required}"
test "$RUNNER_ENVIRONMENT" = github-hosted
test "$(git rev-parse HEAD)" = "$SOURCE_SHA"
# Public package data only. No appdata/OAuth/registry publishing.
contexts=$(mktemp -d /tmp/teams-contexts.XXXXXX)
python tools/prefetch_codex_npm.py "$contexts/codex_npm"
python -m build --wheel --outdir "$contexts/python_wheels"
python -m pip download --only-binary=:all: --dest "$contexts/python_wheels" \
  "$contexts"/python_wheels/teams_transcription-*.whl 'setuptools>=68'
docker --host unix:///var/run/docker.sock buildx build --load --provenance=false \
  --build-context "codex_npm=$contexts/codex_npm" \
  --build-context "python_wheels=$contexts/python_wheels" \
  --label "org.opencontainers.image.source=https://github.com/timmypai/teams-transcription" \
  --label "org.opencontainers.image.revision=$SOURCE_SHA" \
  --iidfile "$RUNNER_TEMP/teams-image-id" .
