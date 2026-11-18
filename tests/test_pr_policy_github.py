"""Exercise the collector through GitHub's HTTP boundary using synthetic data."""
import unittest
from tools.pr_policy_github import collect_pr, EvidenceError

SHA = 'a' * 40
BASE = 'b' * 40
REPO = 'timmypai/teams-transcription'


def pr():
    return {'number': 23, 'state': 'open', 'draft': False, 'changed_files': 1,
            'updated_at': '2026-10-31T00:00:00Z', 'user': {'id': 10},
            'head': {'sha': SHA, 'repo': {'full_name': REPO}},
            'base': {'sha': BASE, 'ref': 'main', 'repo': {'full_name': REPO}}}


class FakeGitHub:
    def __init__(self):
        self.heads = [pr(), pr()]
        self.thread_data = {'data': {'repository': {'pullRequest': {
            'reviewThreads': {'nodes': [{'isResolved': True}],
                             'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}}}

    def request(self, path, payload=None):
        if path == f'/repos/{REPO}/pulls/23':
            return self.heads.pop(0), {}
        if '/files?' in path:
            return [{'filename': 'teams_transcription/b_question.py',
                     'status': 'modified', 'deletions': 3,
                     'patch': 'PRIVATE PATCH MUST NOT LEAK'}], {}
        if '/check-runs?' in path:
            return {'total_count': 0, 'check_runs': []}, {}
        if '/reviews?' in path:
            return [{'id': 4, 'user': {'id': 99, 'type': 'User', 'login': 'independent-reviewer'},
                     'html_url': 'https://github.com/timmypai/teams-transcription/pull/23#pullrequestreview-4',
                     'submitted_at': '2026-11-18T00:00:00Z',
                     'state': 'COMMENTED', 'commit_id': SHA,
                     'body': 'PRIVATE REVIEW MUST NOT LEAK'}], {}
        if path == '/graphql':
            return self.thread_data, {}
        raise AssertionError(path)


class CollectorTests(unittest.TestCase):
    def test_complete_projection_contains_no_patch_or_review_body(self):
        import json
        obs = collect_pr(23, FakeGitHub())
        self.assertTrue(obs['complete'])
        self.assertTrue(obs['stable'])
        self.assertEqual(obs['head_sha'], SHA)
        self.assertEqual(obs['unresolved_threads'], 0)
        self.assertNotIn('PRIVATE', json.dumps(obs))

    def test_new_head_or_base_during_collection_is_stale(self):
        for key in ('head', 'base'):
            api = FakeGitHub()
            api.heads[1][key]['sha'] = 'c' * 40
            obs = collect_pr(23, api)
            self.assertFalse(obs['stable'])

    def test_file_count_mismatch_fails_closed(self):
        api = FakeGitHub()
        api.heads[0]['changed_files'] = 2
        with self.assertRaisesRegex(EvidenceError, '^files_incomplete$'):
            collect_pr(23, api)

    def test_thread_api_error_null_and_unknown_state_block(self):
        api = FakeGitHub()
        api.thread_data['errors'] = [{'message': 'PRIVATE API ERROR'}]
        with self.assertRaisesRegex(EvidenceError, '^threads_read_failed$'):
            collect_pr(23, api)
        api = FakeGitHub()
        nodes = api.thread_data['data']['repository']['pullRequest']['reviewThreads']['nodes']
        nodes[0]['isResolved'] = None
        with self.assertRaisesRegex(EvidenceError, '^threads_invalid$'):
            collect_pr(23, api)

    def test_unresolved_threads_remain_blocking_even_on_old_commits(self):
        api = FakeGitHub()
        nodes = api.thread_data['data']['repository']['pullRequest']['reviewThreads']['nodes']
        nodes[0]['isResolved'] = False
        self.assertEqual(collect_pr(23, api)['unresolved_threads'], 1)

    def test_repeated_thread_cursor_cannot_be_treated_as_complete(self):
        api = FakeGitHub()
        info = api.thread_data['data']['repository']['pullRequest']['reviewThreads']['pageInfo']
        info.update(hasNextPage=True, endCursor='same')
        with self.assertRaisesRegex(EvidenceError, '^threads_pagination_invalid$'):
            collect_pr(23, api)

    def test_all_pages_are_read_and_count_truncation_blocks(self):
        from tools.pr_policy_github import pages
        class Paginated:
            def request(self, path, payload=None):
                if path.endswith('page=1'):
                    return {'total_count': 101, 'check_runs': list(range(100))}, {
                        'Link': '<https://untrusted.invalid/>; rel="next"'}
                if path.endswith('page=2'):
                    return {'total_count': 101, 'check_runs': [100]}, {}
                raise AssertionError(path)
        self.assertEqual(len(pages(Paginated(), '/synthetic?', 'check_runs')), 101)
        class Truncated:
            def request(self, path, payload=None):
                return {'total_count': 101, 'check_runs': []}, {}
        with self.assertRaisesRegex(EvidenceError, '^api_count_mismatch$'):
            pages(Truncated(), '/synthetic?', 'check_runs')

    def test_pagination_bound_is_an_error_not_a_partial_pass(self):
        from tools.pr_policy_github import pages
        class Endless:
            def request(self, path, payload=None):
                return [None] * 100, {'link': '<ignored>; rel="next"'}
        with self.assertRaisesRegex(EvidenceError, '^api_pagination_limit$'):
            pages(Endless(), '/synthetic?')

    def test_http_client_rejects_external_targets_mutations_and_redirects(self):
        from tools.pr_policy_github import GitHub, NoRedirect
        api = GitHub('synthetic-token')
        for path in ('https://evil.invalid', '/repos/other/repo/pulls/1',
                     '/repos/' + REPO + '/../secrets'):
            with self.assertRaisesRegex(EvidenceError, '^api_target_rejected$'):
                api.request(path)
        with self.assertRaisesRegex(EvidenceError, '^api_mutation_rejected$'):
            api.request('/graphql', {'query': 'mutation { deleteRepository }'})
        with self.assertRaisesRegex(EvidenceError, '^api_redirect_rejected$'):
            NoRedirect().redirect_request(None, None, 302, '', {}, 'https://evil.invalid')

    def test_expired_api_deadline_is_blocking(self):
        from tools.pr_policy_github import GitHub
        api = GitHub('synthetic-token')
        api.deadline = 0
        with self.assertRaisesRegex(EvidenceError, '^api_deadline$'):
            api.request('/repos/' + REPO + '/pulls/23')

    def test_cli_always_blocks_and_does_not_emit_credentials(self):
        import json
        import os
        from pathlib import Path
        import subprocess
        import sys
        import tempfile
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as scratch:
            output = Path(scratch) / 'report.json'
            env = dict(os.environ)
            env.pop('GH_TOKEN', None)
            run = subprocess.run([sys.executable, str(root / 'tools/pr_policy_github.py'),
                                  '--pr', '23', '--output', str(output)],
                                 env=env, text=True, capture_output=True)
            self.assertEqual(run.returncode, 1)
            self.assertEqual(json.loads(output.read_text())['blockers'], ['credential_unavailable'])
            fixture = Path(scratch) / 'observation.json'
            fixture.write_text('{"private": "PRIVATE PAYLOAD"}')
            run = subprocess.run([sys.executable, str(root / 'tools/pr_policy.py'),
                                  str(fixture), '--output', str(output)],
                                 text=True, capture_output=True)
            self.assertEqual(run.returncode, 1)
            self.assertNotIn('PRIVATE', run.stdout + run.stderr + output.read_text())
            self.assertFalse(json.loads(output.read_text())['merge_allowed'])
