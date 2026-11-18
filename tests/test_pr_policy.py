"""Public policy boundary: synthetic API observations never authorize a merge."""
import copy
import unittest

from tools.pr_policy import evaluate

SHA = 'a' * 40
BASE = 'b' * 40


def observation():
    return {
        'repository': 'timmypai/teams-transcription', 'number': 23,
        'head_sha': SHA, 'base_sha': BASE, 'base_ref': 'main',
        'head_repository': 'timmypai/teams-transcription',
        'state': 'open', 'draft': False, 'author_id': 10,
        'complete': True, 'stable': True,
        'files': [{'filename': 'teams_transcription/b_question.py',
                   'status': 'modified', 'deletions': 3}],
        'checks': [], 'reviews': [], 'unresolved_threads': 0,
    }


class PRPolicyTests(unittest.TestCase):
    def test_only_designated_identity_clears_identity_diagnostic_not_merge(self):
        obs=observation()
        review=dict(id=1,commit_id=SHA,user_id=273007156,login='paulpai0412',type='User',
            author_association='NONE',body_present=True,submitted_at='2026-11-18T00:00:00Z',
            state='APPROVED',html_url='https://github.com/timmypai/teams-transcription/pull/23#pullrequestreview-1')
        obs['reviews']=[review]
        self.assertNotIn('review_identity_not_designated',evaluate(obs)['blockers'])
        self.assertFalse(evaluate(obs)['merge_allowed'])
        for update in (dict(user_id=None),dict(user_id=8),dict(login='stranger'),dict(type='Bot')):
            obs['reviews']=[{**review,**update}]
            self.assertIn('review_identity_not_designated',evaluate(obs)['blockers'])

    def test_core_fix_is_candidate_and_never_native_review_pass_by_silence(self):
        result = evaluate(observation())
        self.assertEqual(result['candidate_tier'], 'core')
        self.assertIn('independent_review_scope_unverified', result['blockers'])
        self.assertFalse(result['merge_allowed'])
        self.assertEqual(result['head_sha'], SHA)
        self.assertEqual(result['mode'], 'shadow')

    def test_security_and_policy_changes_need_manual_review(self):
        for path in ('.github/workflows/ci.yml', 'policy/pr-policy.json',
                     'AGENTS.md', 'teams_transcription/codex_auth.py',
                     'teams_transcription/runtime_guard.py', 'deploy/stagingctl.py',
                     'Dockerfile', 'pyproject.toml', 'docs/QA-PASSIVE-AUDIO.md'):
            with self.subTest(path=path):
                obs = observation()
                obs['files'][0]['filename'] = path
                result = evaluate(obs)
                self.assertEqual(result['candidate_tier'], 'manual')
                self.assertIn('classification_requires_manual', result['blockers'])
                self.assertFalse(result['merge_allowed'])

    def test_removal_rename_and_reduced_tests_need_manual_review(self):
        for entry in (
            {'filename': 'tests/test_regression.py', 'status': 'modified', 'deletions': 1},
            {'filename': 'docs/a.md', 'status': 'removed', 'deletions': 1},
            {'filename': 'docs/a.md', 'status': 'renamed',
             'previous_filename': 'policy/pr-policy.json', 'deletions': 0},
        ):
            with self.subTest(entry=entry):
                obs = observation()
                obs['files'] = [entry]
                self.assertEqual(evaluate(obs)['candidate_tier'], 'manual')

    def test_additive_regression_test_follows_core_fix(self):
        obs = observation()
        obs['files'].append({'filename': 'tests/test_regression.py',
                             'status': 'added', 'deletions': 0})
        self.assertEqual(evaluate(obs)['candidate_tier'], 'core')

    def test_unknown_paths_and_malformed_entries_fail_closed(self):
        for path in ('new_scope/file.sh', '../policy/pr-policy.json',
                     '/etc/passwd', 'docs/../policy/pr-policy.json', 'docs//a.md'):
            obs = observation()
            obs['files'][0]['filename'] = path
            self.assertEqual(evaluate(obs)['candidate_tier'], 'unknown')
            self.assertFalse(evaluate(obs)['merge_allowed'])
        for bad in (None, [], {}, {'files': [None]}):
            self.assertFalse(evaluate(bad)['merge_allowed'])

    def test_synthetic_successful_ci_and_approved_review_cannot_authorize_merge(self):
        obs = observation()
        obs['checks'] = [
            {'name': n, 'head_sha': SHA, 'status': 'completed', 'conclusion': 'success'}
            for n in ('devflow-ci',)]
        obs['reviews'] = [{'commit_id': SHA, 'user_id': 99,
                           'state': 'APPROVED', 'login': 'codex-looking-bot'}]
        result = evaluate(obs)
        self.assertNotIn('required_check_not_successful', result['blockers'])
        self.assertIn('review_conclusion_missing', result['blockers'])
        self.assertIn('semantic_scope_unverified', result['blockers'])
        self.assertIn('required_test_provenance_unverified', result['blockers'])
        self.assertFalse(result['merge_allowed'])

    def test_skipped_stale_pending_failed_or_duplicate_checks_are_not_success(self):
        for conclusion in ('skipped', 'neutral', 'failure', None):
            obs = observation()
            obs['checks'] = [{'name': 'devflow-ci', 'head_sha': SHA,
                              'status': 'completed', 'conclusion': conclusion}]
            self.assertIn('required_check_not_successful', evaluate(obs)['blockers'])
        for overrides in ({'head_sha': BASE}, {'status': 'in_progress'}):
            obs = observation()
            check = {'name': 'devflow-ci', 'head_sha': SHA,
                     'status': 'completed', 'conclusion': 'success'}
            check.update(overrides)
            obs['checks'] = [check]
            self.assertIn('required_check_not_successful', evaluate(obs)['blockers'])
        obs = observation()
        check = {'name': 'devflow-ci', 'head_sha': SHA,
                 'status': 'completed', 'conclusion': 'success'}
        obs['checks'] = [check, copy.deepcopy(check)]
        self.assertIn('required_check_not_successful', evaluate(obs)['blockers'])

    def test_stale_self_and_requested_changes_reviews_do_not_clear_gate(self):
        obs = observation()
        obs['reviews'] = [{'commit_id': BASE, 'user_id': 99, 'state': 'APPROVED'}]
        self.assertIn('current_head_review_missing', evaluate(obs)['blockers'])
        obs['reviews'] = [{'commit_id': SHA, 'user_id': 10, 'state': 'APPROVED'}]
        self.assertIn('author_review_not_independent', evaluate(obs)['blockers'])
        obs['reviews'][0].update(user_id=99, state='CHANGES_REQUESTED')
        self.assertIn('review_not_clear', evaluate(obs)['blockers'])

    def test_unknown_threads_and_changed_snapshot_block(self):
        for value in (None, -1, True, 1):
            obs = observation()
            obs['unresolved_threads'] = value
            result = evaluate(obs)
            self.assertTrue({'threads_unknown', 'unresolved_review_threads'} & set(result['blockers']))
            self.assertFalse(result['merge_allowed'])
        for field, blocker in (('complete', 'evidence_incomplete'),
                               ('stable', 'snapshot_changed')):
            obs = observation()
            obs[field] = False
            self.assertIn(blocker, evaluate(obs)['blockers'])

    def test_mode_cannot_be_switched_by_input_or_policy(self):
        import json
        from tools.pr_policy import POLICY
        policy = json.loads(POLICY.read_text())
        policy['mode'] = 'enforce'
        result = evaluate(observation(), policy)
        self.assertIn('policy_unsupported', result['blockers'])
        self.assertFalse(result['merge_allowed'])
