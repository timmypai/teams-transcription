"""Admission and bounded audit for the hosted, disposable deployment harness.

This is NOT a VM isolation boundary. No host identities or sockets are guessed.
This hosted harness has no Oracle enable flag: the real manual adapter is
deploy/target_deploy.py, only after independent review. The operator has accepted
the shared devflow user/rootless daemon; no dedicated execution gate is required.
"""
import argparse
import json
import re

REPOSITORY = 'timmypai/teams-transcription'
SOURCE_SHA = 'c792cf9f313dc1ba2fafe8eb1c811b8e862adc66'


def admit_candidate(repository, branch, expected_sha, candidate_sha, source_sha, image):
    if repository != REPOSITORY or branch != 'main':
        raise ValueError('candidate_repository_or_branch_mismatch')
    if not re.fullmatch('[0-9a-f]{40}', expected_sha or ''):
        raise ValueError('expected_sha_invalid')
    if candidate_sha != expected_sha:
        raise ValueError('candidate_sha_mismatch')
    if source_sha != SOURCE_SHA:
        raise ValueError('source_lineage_mismatch')
    if not re.fullmatch('sha256:[0-9a-f]{64}', image or ''):
        raise ValueError('immutable_image_required')
    return dict(repository=repository, branch=branch, source_sha=source_sha,
                deployed_sha=candidate_sha, image_digest=image)


class OperationAudit:
    """Reject a complete plan before executing any mutation. Exact tuples only.

    The caller owns the executor and lifecycle. This log proves only those
    operations, not arbitrary host filesystem non-interference. Production
    AC-2 additionally needs real deployment operation/snapshot evidence, not
    this Python list. Project naming is not a permissions isolation boundary.
    """
    def __init__(self, allowlist):
        self.allowlist = frozenset(tuple(row) for row in allowlist)
        self.events = []

    def execute(self, plan, executor):
        plan = [tuple(row) for row in plan]
        if not plan or any(row not in self.allowlist for row in plan):
            raise ValueError('foreign_target_refused')
        for action, kind, target in plan:
            row = dict(action=action, kind=kind, target=target, outcome='attempted')
            self.events.append(row)
            try:
                executor(action, kind, target)
            except BaseException:
                row['outcome'] = 'failed'
                raise
            row['outcome'] = 'succeeded'

    def report(self, before, after):
        if before != after:
            raise ValueError('foreign_snapshot_changed')
        if any((e['action'], e['kind'], e['target']) not in self.allowlist
               or e['outcome'] != 'succeeded' for e in self.events):
            raise ValueError('operation_audit_incomplete')
        return dict(scope='disposable-hosted-controller-only',
                    vm_acceptance=False, outside_mutations=0,
                    foreign_before=before, foreign_after=after,
                    allowlist=[list(row) for row in sorted(self.allowlist)],
                    operations=self.events.copy(),
                    limitation='Not proof of arbitrary host filesystem writes; target VM deployment unverified')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['oracle-preflight'])
    parser.parse_args()
    print(json.dumps(dict(enabled=False, repository=REPOSITORY,
                          vm_mutation_allowlist=[],
                          error='target_manual_workflow_required')))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
