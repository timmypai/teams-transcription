#!/usr/bin/env bash
# Local adaptation of devflow build → up → health → readonly smoke → exact release
# rollback. Deliberately no push trigger, tag fallback, prune or DB restore.
set -euo pipefail
cd "$(dirname "$0")/.."
exec python3 deploy/target_deploy.py deploy
