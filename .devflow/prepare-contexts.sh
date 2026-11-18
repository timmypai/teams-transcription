#!/bin/sh
# Runs only in an owned disposable public-dependency build container.
set -eu
umask 022
case "$(uname -m)" in x86_64) platform=linux-x64 ;; aarch64) platform=linux-arm64 ;; *) exit 1 ;; esac
python /source/tools/prefetch_codex_npm.py /workspace/codex_npm --platform "$platform"
python -m pip install --disable-pip-version-check 'build==1.3.0'
mkdir /workspace/app
cp /source/pyproject.toml /workspace/app/
cp -R /source/teams_transcription /workspace/app/
cd /workspace/app
python -m build --wheel --outdir /workspace/python_wheels
python -m pip download --only-binary=:all: --dest /workspace/python_wheels \
  /workspace/python_wheels/teams_transcription-*.whl 'setuptools>=68'
# Public package/source context only, under the host's private state parent.
# Let the host owner remove rootful-container-created files without CHOWN caps.
chmod -R a+rwX /workspace/app /workspace/codex_npm /workspace/python_wheels
