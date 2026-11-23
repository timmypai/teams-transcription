"""Hosted CI subprocess adapter; disposable app only, never a real runtime dispatch."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from deploy import target_deploy as deploy


def main():
    assert os.environ['RUNNER_ENVIRONMENT'] == 'github-hosted'
    action, fixture_root, image, container, actor = sys.argv[1:]
    assert action in deploy.PRIVATE_CONTROL_ACTIONS
    root = Path(fixture_root)
    home = root/'home'
    sha = os.environ['GITHUB_SHA']
    state = dict(phase='healthy',source_sha=sha,image=image,container_id=container,release='synthetic')
    receipt = dict(repository=deploy.REPO,source_sha=sha)
    run = int(os.environ['GITHUB_RUN_ID'])
    row = dict(id=run,run_attempt=1,event='workflow_dispatch',path='.github/workflows/control.yml',
               head_branch='main',head_sha=sha,repository=dict(full_name=deploy.REPO),
               head_repository=dict(full_name=deploy.REPO),actor=dict(id=int(actor)),
               triggering_actor=dict(id=int(actor)),created_at=datetime.now(timezone.utc).isoformat())
    original = deploy.Controller

    def no_dispatch(*args, **kwargs):
        raise AssertionError('runtime_dispatch_forbidden')

    def controller(*args,**kwargs):
        c = original(*args,**kwargs)
        c.write(c.root/'state.json',json.dumps(state))
        c.verify_release = lambda release: None
        c.preflight = lambda state: deploy.snapshot(c.containers())
        c.health = lambda image: (container, {})
        c.runtime = no_dispatch
        return c

    with patch.object(deploy,'ROOT',root),patch.object(deploy.Path,'home',return_value=home), \
            patch.object(deploy,'manual_environment',return_value=sha),patch.object(deploy,'collect',return_value=receipt), \
            patch.object(deploy,'Controller',side_effect=controller),patch.object(deploy,'GitHub') as github, \
            patch.object(sys,'argv',['target_deploy.py',action]):
        github.return_value.request.return_value = (row,{})
        return deploy.main()


if __name__ == '__main__':
    raise SystemExit(main())
