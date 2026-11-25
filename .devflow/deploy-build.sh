#!/usr/bin/env bash
# Project-specific image build hook invoked by standard `.devflow/deploy.sh`.
# Prepares offline public-only `codex_npm` and `python_wheels` build contexts
# and builds `$DEVFLOW_IMAGE` with full transparent logs. No runtime secrets.
set -euo pipefail
cd "$(dirname "$0")/.."

: "${DEVFLOW_IMAGE:?DEVFLOW_IMAGE required}"
: "${DEVFLOW_SOURCE_SHA:?DEVFLOW_SOURCE_SHA required}"

# Before building/replacing anything on first cutover, materialize any verified
# legacy release into standard `$LIVE` so rollback uses exact prior files.
python3 .devflow/import_prior_release.py

base_image=$(sed -n 's/^ARG PYTHON_BASE=//p' Dockerfile | head -n 1)
if [[ -z "$base_image" ]]; then
  echo "::error::deploy-build: ARG PYTHON_BASE not found in Dockerfile" >&2
  exit 1
fi

contexts=$(mktemp -d "${TMPDIR:-/tmp}/teams-build-contexts.XXXXXX")
cleanup() { rm -rf "$contexts" 2>/dev/null || true; }
trap cleanup EXIT
chmod 0777 "$contexts"

echo "deploy-build: preparing offline codex_npm + python_wheels contexts with $base_image"
docker run --rm \
  --label "io.devflow.owner=timmypai/teams-transcription" \
  --cap-drop ALL \
  --security-opt no-new-privileges \
  --mount "type=bind,src=$PWD,dst=/source,readonly" \
  --mount "type=bind,src=$contexts,dst=/workspace" \
  --entrypoint sh \
  "$base_image" \
  /source/.devflow/prepare-contexts.sh

echo "deploy-build: building ${DEVFLOW_IMAGE} (source ${DEVFLOW_SOURCE_SHA:0:12})"
docker buildx build \
  --load \
  --provenance=false \
  --build-context "codex_npm=$contexts/codex_npm" \
  --build-context "python_wheels=$contexts/python_wheels" \
  --label "org.opencontainers.image.source=https://github.com/timmypai/teams-transcription" \
  --label "org.opencontainers.image.revision=${DEVFLOW_SOURCE_SHA}" \
  --label "io.devflow.owner=timmypai/teams-transcription" \
  -t "${DEVFLOW_IMAGE}" \
  .
