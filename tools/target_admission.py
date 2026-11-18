"""Readonly admission for this target's MANUAL deploy/control (never a merger).

GitHub-native reviews and Actions identities are read, not supplied by a caller.
The independent review lane owns full-diff/scope/base-exception judgment.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess

try:
    from tools.pr_policy_github import GitHub, pages, unresolved_threads
except ModuleNotFoundError:
    from pr_policy_github import GitHub, pages, unresolved_threads

REPO='timmypai/teams-transcription'
SHA=re.compile('[0-9a-f]{40}\\Z')


def require(value, code):
    if not value:
        raise ValueError(code)


def validate_release(e):
    """Pure seam. Not an approval-token parser; only collector calls it in CLI."""
    require(e['repository']==REPO and e['default_branch']=='main','target_mismatch')
    require(SHA.fullmatch(e['source_sha']) and e['tip']==e['source_sha'],'default_tip_required')
    require(e['tree']==e['reviewed_tree'] and SHA.fullmatch(e['tree']),'reviewed_tree_mismatch')
    pr=e['pr']; head=pr['head_sha']
    require(SHA.fullmatch(head) and pr['merged'] is True and pr['merge_commit_sha']==e['source_sha']
        and pr['base_ref']=='main' and pr['head_repository']==REPO,'merged_pr_mismatch')
    require(type(pr['unresolved_threads']) is int and pr['unresolved_threads']==0,'review_threads_not_clear')
    reviews=e['reviews'];latest={}
    for r in reviews:
        require(type(r['id']) is int and r['id']>0,'review_unknown')
        latest[r['user']['login']]=r
    require(bool(latest),'independent_review_missing')
    # Unknown/newer review cannot be hidden behind an older approval.
    for login,r in latest.items():
        require(r['state']=='APPROVED' and r['commit_id']==head,'current_head_review_required')
        require(login!=pr['author'] and r['user']['type']=='User'
            and r.get('author_association') in {'OWNER','MEMBER','COLLABORATOR'},'review_not_independent')
        require(isinstance(r.get('body'),str) and bool(r['body'].strip()),'review_conclusion_missing')
        require(r['html_url']==f"https://github.com/{REPO}/pull/{pr['number']}#pullrequestreview-{r['id']}",
            'review_link_mismatch')
    run=e['run']
    require(run['repository']['full_name']==REPO and run['head_repository']['full_name']==REPO
        and run['head_sha']==head and run['event']=='pull_request'
        and run['path']=='.github/workflows/ci.yml' and run['status']=='completed'
        and run['conclusion']=='success' and type(run['id']) is int and run['id']>0
        and type(run['workflow_id']) is int and run['workflow_id']>0
        and type(run['run_attempt']) is int and run['run_attempt']>0,'ci_identity_required')
    review=list(latest.values())[-1]
    return dict(repository=REPO,source_sha=e['source_sha'],reviewed_head_sha=head,tree=e['tree'],
        pr_number=pr['number'],reviewer=review['user']['login'],review_url=review['html_url'],
        review_conclusion=review['state'],ci_run_id=run['id'],ci_run_attempt=run['run_attempt'],
        ci_workflow_id=run['workflow_id'],ci_run_url=f"https://github.com/{REPO}/actions/runs/{run['id']}")


def collect(source,api):
    require(isinstance(source,str) and SHA.fullmatch(source),'sha_invalid')
    prefix=f'/repos/{REPO}'
    def read(path):return api.request(prefix+path)[0]
    repo=read('/')
    branch=repo['default_branch']
    require(branch=='main','default_branch_mismatch')
    before=read('/branches/main')['commit']['sha']
    pulls=pages(api,prefix+f'/commits/{source}/pulls?')
    matched=[p for p in pulls if p.get('merged_at') and p.get('merge_commit_sha')==source]
    require(len(matched)==1,'merged_pr_missing_or_ambiguous')
    number=matched[0]['number'];pr=read(f'/pulls/{number}')
    head=pr['head']['sha']
    require(SHA.fullmatch(head),'head_invalid')
    reviews=pages(api,prefix+f'/pulls/{number}/reviews?')
    runs=pages(api,prefix+f'/actions/workflows/ci.yml/runs?head_sha={head}&event=pull_request','workflow_runs')
    require(bool(runs),'ci_missing')
    newest=max(runs,key=lambda r:r['id'])
    run=read(f"/actions/runs/{newest['id']}")
    workflow=read('/actions/workflows/ci.yml')
    require(run['workflow_id']==workflow['id'],'workflow_identity_mismatch')
    tree=read(f'/git/commits/{source}')['tree']['sha']
    reviewed_tree=read(f'/git/commits/{head}')['tree']['sha']
    data=dict(repository=repo['full_name'],default_branch=branch,tip=before,source_sha=source,
        tree=tree,reviewed_tree=reviewed_tree,reviews=reviews,run=run,
        pr=dict(number=number,merged=pr['merged'],merge_commit_sha=pr['merge_commit_sha'],head_sha=head,
            author=pr['user']['login'],base_ref=pr['base']['ref'],
            head_repository=pr['head']['repo']['full_name'],unresolved_threads=unresolved_threads(api,number)))
    receipt=validate_release(data)
    after=read('/branches/main')['commit']['sha']
    pr_after=read(f'/pulls/{number}')
    require(before==after==source and pr_after['head']['sha']==head and pr_after['updated_at']==pr['updated_at'],
        'snapshot_changed')
    return receipt


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    try:
        require(os.environ.get('GITHUB_REPOSITORY')==REPO
            and os.environ.get('GITHUB_EVENT_NAME')=='workflow_dispatch'
            and os.environ.get('GITHUB_REF')=='refs/heads/main','manual_main_only')
        source=os.environ.get('GITHUB_SHA')
        require(subprocess.check_output(['git','rev-parse','HEAD'],text=True).strip()==source,'checkout_mismatch')
        receipt=collect(source,GitHub(os.environ.get('GH_TOKEN')))
        args.output.write_text(json.dumps(receipt,indent=2)+'\n')
        return 0
    except Exception:
        # No raw API, PR prose, token or subprocess output in logs.
        print('target_admission_rejected')
        return 1


if __name__=='__main__':
    raise SystemExit(main())
