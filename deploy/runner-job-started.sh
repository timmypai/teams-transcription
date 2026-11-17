#!/usr/bin/env bash
# Install root-owned outside the runner directory. No commands or paths from input.
set -euo pipefail
deny() { printf '%s\n' 'Runner policy rejected this job.' >&2; exit 1; }
[[ "${GITHUB_REPOSITORY:-}" == 'paulpai0412/teams-transcription' ]] || deny
[[ "${GITHUB_EVENT_NAME:-}" == 'workflow_dispatch' ]] || deny
[[ "${GITHUB_REF:-}" == 'refs/heads/main' ]] || deny
case "${GITHUB_WORKFLOW_REF:-}" in
  paulpai0412/teams-transcription/.github/workflows/oracle-staging.yml@refs/heads/main) ;;
  paulpai0412/teams-transcription/.github/workflows/oracle-diagnostics.yml@refs/heads/main) ;;
  paulpai0412/teams-transcription/.github/workflows/oracle-qa.yml@refs/heads/main) ;;
  *) deny ;;
esac
