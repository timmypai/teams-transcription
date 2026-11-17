"""Collect bounded, read-only GitHub observations from trusted main only.

No PR code, artifacts, patches, review prose, labels, reactions, or comment commands
are executed or treated as approval. API errors are reduced to fixed codes.
"""
import argparse
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.request

try:
    from tools.pr_policy import evaluate
except ModuleNotFoundError:  # direct invocation from tools/
    from pr_policy import evaluate

REPOSITORY = 'paulpai0412/teams-transcription'
MAX_PAGES = 30
MAX_BYTES = 4 * 1024 * 1024
THREAD_QUERY = '''query($number:Int!, $cursor:String) {
  repository(owner:"paulpai0412", name:"teams-transcription") {
    pullRequest(number:$number) {
      reviewThreads(first:100, after:$cursor) {
        nodes { isResolved }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}'''


class EvidenceError(Exception):
    """Fixed public code only; never include a response body or credential."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EvidenceError('api_redirect_rejected')


class GitHub:
    def __init__(self, token):
        if not token or len(token) > 16384 or '\n' in token or '\r' in token:
            raise EvidenceError('credential_unavailable')
        self.token = token
        self.deadline = time.monotonic() + 90
        self.opener = urllib.request.build_opener(NoRedirect())

    def request(self, path, payload=None):
        if not (path.startswith(f'/repos/{REPOSITORY}/') or path == '/graphql'):
            raise EvidenceError('api_target_rejected')
        if '..' in path or '#' in path or '\\' in path:
            raise EvidenceError('api_target_rejected')
        if payload is not None and (path != '/graphql' or payload.get('query') != THREAD_QUERY):
            raise EvidenceError('api_mutation_rejected')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise EvidenceError('api_deadline')
        request = urllib.request.Request(
            'https://api.github.com' + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Bearer ' + self.token,
                     'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json',
                     'X-GitHub-Api-Version': '2022-11-28',
                     'User-Agent': 'teams-transcription-pr-policy-shadow'})
        try:
            with self.opener.open(request, timeout=min(20, remaining)) as response:
                body = response.read(MAX_BYTES + 1)
                if len(body) > MAX_BYTES:
                    raise EvidenceError('api_response_too_large')
                return json.loads(body), dict(response.headers)
        except EvidenceError:
            raise
        except (OSError, ValueError, urllib.error.URLError):
            raise EvidenceError('api_read_failed') from None


def pages(api, path, key=None):
    """Generate our own same-host page URLs; do not follow API-provided links."""
    result = []
    total = None
    for page in range(1, MAX_PAGES + 1):
        body, headers = api.request(f'{path}&per_page=100&page={page}')
        items = body if key is None else body[key]
        if not isinstance(items, list) or len(items) > 100:
            raise EvidenceError('api_page_invalid')
        if key:
            reported = body['total_count']
            if type(reported) is not int or reported < 0:
                raise EvidenceError('api_count_invalid')
            if total is not None and reported != total:
                raise EvidenceError('api_count_changed')
            total = reported
        result.extend(items)
        link = next((v for k, v in headers.items() if k.lower() == 'link'), '')
        has_next = 'rel="next"' in link
        if not has_next:
            # A full page without a Link is uncertain; request the next page.
            if len(items) == 100:
                continue
            if total is not None and len(result) != total:
                raise EvidenceError('api_count_mismatch')
            return result
    raise EvidenceError('api_pagination_limit')


def unresolved_threads(api, number):
    cursor = None
    seen = set()
    unresolved = 0
    for _ in range(MAX_PAGES):
        body, _ = api.request('/graphql', {'query': THREAD_QUERY,
                                         'variables': {'number': number, 'cursor': cursor}})
        if body.get('errors'):
            raise EvidenceError('threads_read_failed')
        threads = body['data']['repository']['pullRequest']['reviewThreads']
        nodes = threads['nodes']
        if not isinstance(nodes, list) or len(nodes) > 100:
            raise EvidenceError('threads_invalid')
        for node in nodes:
            if not isinstance(node, dict) or type(node.get('isResolved')) is not bool:
                raise EvidenceError('threads_invalid')
            unresolved += not node['isResolved']
        info = threads['pageInfo']
        if info['hasNextPage'] is False:
            return unresolved
        if info['hasNextPage'] is not True:
            raise EvidenceError('threads_invalid')
        cursor = info['endCursor']
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise EvidenceError('threads_pagination_invalid')
        seen.add(cursor)
    raise EvidenceError('threads_pagination_limit')


def collect_pr(number, api):
    if type(number) is not int or not 0 < number < 2 ** 31:
        raise EvidenceError('pr_number_invalid')
    prefix = f'/repos/{REPOSITORY}'
    before, _ = api.request(f'{prefix}/pulls/{number}')
    sha = before['head']['sha']
    if not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha):
        raise EvidenceError('head_invalid')
    files = pages(api, f'{prefix}/pulls/{number}/files?')
    if len(files) != before['changed_files'] or len(files) >= 3000:
        raise EvidenceError('files_incomplete')
    checks = pages(api, f'{prefix}/commits/{sha}/check-runs?filter=all', 'check_runs')
    reviews = pages(api, f'{prefix}/pulls/{number}/reviews?')
    threads = unresolved_threads(api, number)
    after, _ = api.request(f'{prefix}/pulls/{number}')
    def identity(pr):
        return (pr['number'], pr['head']['sha'], pr['base']['sha'], pr['base']['ref'],
                pr['head']['repo']['full_name'], pr['base']['repo']['full_name'],
                pr['updated_at'], pr['state'], pr['draft'], pr['changed_files'])
    if before['number'] != number or before['base']['repo']['full_name'] != REPOSITORY:
        raise EvidenceError('pr_target_mismatch')
    return {
        'repository': REPOSITORY, 'number': number, 'head_sha': sha,
        'base_sha': before['base']['sha'], 'base_ref': before['base']['ref'],
        'head_repository': before['head']['repo']['full_name'],
        'state': before['state'], 'draft': before['draft'],
        'author_id': before['user']['id'], 'complete': True,
        'stable': identity(before) == identity(after),
        'files': [{k: f[k] for k in ('filename', 'status', 'deletions')} for f in files],
        'checks': [{k: c[k] for k in ('id', 'name', 'head_sha', 'status', 'conclusion')}
                   | {'app_id': c['app']['id']} for c in checks],
        'reviews': [{k: r[k] for k in ('id', 'commit_id', 'state')}
                    | {'user_id': r['user']['id']} for r in reviews],
        'unresolved_threads': threads,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pr', required=True, type=int)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    try:
        result = evaluate(collect_pr(args.pr, GitHub(os.environ.get('GH_TOKEN'))))
    except EvidenceError as exc:
        result = {'schema': 1, 'mode': 'shadow', 'merge_allowed': False,
                  'blockers': [str(exc)]}
    except (KeyError, TypeError, ValueError, OSError):
        result = {'schema': 1, 'mode': 'shadow', 'merge_allowed': False,
                  'blockers': ['evidence_or_policy_invalid']}
    args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
