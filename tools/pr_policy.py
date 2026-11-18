"""Read-only PR diagnostics for independent Hermes review + GitHub Actions.

There is deliberately no merge API, approval override, or switch to enforce mode.
Only trusted main may supply this program and its policy. Observations are not an
attestation; native GitHub review is independently checked in the review lane.
"""
import argparse
import fnmatch
import hashlib
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / 'policy' / 'pr-policy.json'
SHA = re.compile(r'[0-9a-f]{40}\Z')


def evaluate(observation, policy=None):
    """Return a sanitized blocking report; never authorize a write action."""
    policy = json.loads(POLICY.read_text()) if policy is None else policy
    blockers = ['shadow_only', 'independent_review_scope_unverified',
                'semantic_scope_unverified', 'required_test_provenance_unverified']
    canonical = json.dumps(policy, sort_keys=True, separators=(',', ':')).encode()
    result = {
        'schema': 1, 'mode': 'shadow', 'merge_allowed': False,
        'repository': 'timmypai/teams-transcription', 'pr_number': None,
        'policy_sha256': hashlib.sha256(canonical).hexdigest(),
        'head_sha': None, 'base_sha': None, 'candidate_tier': 'unknown',
        'blockers': blockers, 'observed_review_count': 0,
        'current_head_review_count': 0, 'observed_check_count': 0, 'review_evidence': [],
    }
    if (policy.get('schema') != 1 or policy.get('mode') != 'shadow'
            or policy.get('repository') != 'timmypai/teams-transcription'
            or policy.get('review_adapter') != 'hermes-independent-github'):
        blockers.append('policy_unsupported')
        return result
    if not isinstance(observation, dict):
        blockers.append('observation_invalid')
        return result
    number = observation.get('number')
    if type(number) is int and 0 < number < 2 ** 31:
        result['pr_number'] = number
    else:
        blockers.append('pr_number_invalid')
    for key in ('head_sha', 'base_sha'):
        value = observation.get(key)
        if isinstance(value, str) and SHA.fullmatch(value):
            result[key] = value
        else:
            blockers.append('sha_invalid')
    if observation.get('repository') != policy['repository']:
        blockers.append('repository_mismatch')
    if observation.get('head_repository') != policy['repository']:
        blockers.append('fork_requires_manual')
    if observation.get('base_ref') != 'main':
        blockers.append('base_requires_manual')
    if observation.get('state') != 'open' or observation.get('draft') is not False:
        blockers.append('pr_not_ready')
    if observation.get('complete') is not True:
        blockers.append('evidence_incomplete')
    if observation.get('stable') is not True:
        blockers.append('snapshot_changed')
    tiers = []
    files = observation.get('files')
    if not isinstance(files, list) or not files:
        blockers.append('files_unknown')
    else:
        for entry in files:
            tiers.append(classify(entry, policy))
        result['candidate_tier'] = (
            'manual' if 'manual' in tiers else 'unknown' if 'unknown' in tiers
            else 'core' if 'core' in tiers else 'ordinary')
    if result['candidate_tier'] in ('manual', 'unknown'):
        blockers.append('classification_requires_manual')
    reviews = observation.get('reviews')
    checks = observation.get('checks')
    if not isinstance(reviews, list) or not isinstance(checks, list):
        blockers.append('review_or_checks_unknown')
        return result
    result['observed_check_count'] = len(checks)
    result['observed_review_count'] = len(reviews)
    current = [r for r in reviews if isinstance(r, dict)
               and r.get('commit_id') == result['head_sha']]
    result['current_head_review_count'] = len(current)
    if not current:
        blockers.append('current_head_review_missing')

    author_id = observation.get('author_id')
    if type(author_id) is not int or author_id <= 0:
        blockers.append('author_unknown')
    if any(r.get('user_id') == author_id for r in current):
        blockers.append('author_review_not_independent')
    # API order is chronological. Latest review from each principal supersedes
    # earlier approval, including withdrawal, comments and unknown states.
    latest = {}
    for review in reviews:
        if isinstance(review, dict):
            latest[review.get('user_id')] = review
    if not latest or any(r.get('state') != 'APPROVED'
            or r.get('commit_id') != result['head_sha'] for r in latest.values()):
        blockers.append('review_not_clear')
    for r in current:
        if (not isinstance(r.get('login'), str) or not r['login']
                or r.get('body_present') is not True
                or not isinstance(r.get('submitted_at'), str)
                or r.get('html_url') != f"https://github.com/timmypai/teams-transcription/pull/{number}#pullrequestreview-{r.get('id')}"):
            blockers.append('review_conclusion_missing')
            continue
        result['review_evidence'].append(dict(reviewer=r['login'], head_sha=r['commit_id'],
            review_url=r['html_url'], conclusion=r.get('state'), submitted_at=r['submitted_at']))
    threads = observation.get('unresolved_threads')
    if type(threads) is not int or threads < 0:
        blockers.append('threads_unknown')
    elif threads:
        blockers.append('unresolved_review_threads')
    required = policy['required_checks'].get(result['candidate_tier'], [])
    result['required_checks'] = required
    for name in required:
        matches = [c for c in checks if isinstance(c, dict)
                   and c.get('name') == name]
        # Ambiguity, reruns, skipped/neutral and a successful stale SHA all block.
        if (len(matches) != 1 or matches[0].get('head_sha') != result['head_sha']
                or matches[0].get('status') != 'completed'
                or matches[0].get('conclusion') != 'success'):
            blockers.append('required_check_not_successful')
            break
    return result


def classify(entry, policy):
    """Path screening only; semantic risk still needs independent assessment."""
    if not isinstance(entry, dict):
        return 'unknown'
    path = entry.get('filename')
    status = entry.get('status')
    if (not isinstance(path, str) or not path or path.startswith('/')
            or '\\' in path or any(p in ('', '.', '..') for p in path.split('/'))):
        return 'unknown'
    if status not in ('added', 'modified', 'removed', 'renamed', 'copied'):
        return 'unknown'
    # A rename/copy can hide a protected previous path; require manual assessment.
    if status in ('removed', 'renamed', 'copied'):
        return 'manual'
    if any(fnmatch.fnmatchcase(path, pattern) for pattern in policy['manual_paths']):
        return 'manual'
    if path.startswith('tests/'):
        deletions = entry.get('deletions')
        if type(deletions) is not int or deletions < 0:
            return 'unknown'
        return 'manual' if deletions else 'ordinary'
    for tier in ('core', 'ordinary'):
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in policy[tier + '_paths']):
            return tier
    return 'unknown'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('observation', type=Path,
                        help='synthetic or collected JSON; never a merge authorization')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = evaluate(json.loads(args.observation.read_text()))
    except (OSError, ValueError, TypeError, KeyError):
        result = {'schema': 1, 'mode': 'shadow', 'merge_allowed': False,
                  'blockers': ['invalid_input_or_policy']}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 1  # This command is a blocking diagnostic, including for synthetic green CI.


if __name__ == '__main__':
    raise SystemExit(main())
