"""Target review migration: the public diagnostic stays readonly and fail closed."""
import json
from pathlib import Path
import unittest

from tools.pr_policy import evaluate

ROOT = Path(__file__).resolve().parents[1]
SHA = 'a' * 40


class HermesMigrationTests(unittest.TestCase):
    def observation(self):
        return dict(repository='timmypai/teams-transcription', number=1,
            head_repository='timmypai/teams-transcription', base_ref='main',
            head_sha=SHA, base_sha='b'*40, state='open', draft=False,
            author_id=10, complete=True, stable=True, unresolved_threads=0,
            files=[dict(filename='README.md',status='modified',deletions=0)],
            checks=[dict(name='devflow-ci',head_sha=SHA,status='completed',conclusion='success')],
            reviews=[dict(id=12,commit_id=SHA,state='APPROVED',user_id=99,
                login='independent-hermes-reviewer',html_url='https://github.com/timmypai/teams-transcription/pull/1#pullrequestreview-12',
                submitted_at='2026-11-18T00:00:00Z',body_present=True)])

    def test_target_has_no_cloud_identity_prerequisite_but_shadow_never_merges(self):
        policy=json.loads((ROOT/'policy/pr-policy.json').read_text())
        self.assertEqual(policy['repository'],'timmypai/teams-transcription')
        self.assertEqual(policy['review_adapter'],'hermes-independent-github')
        result=evaluate(self.observation())
        self.assertNotIn('repository_mismatch',result['blockers'])
        self.assertFalse(any('codex' in x or 'native_review' in x for x in result['blockers']))
        self.assertFalse(result['merge_allowed'])
        self.assertIn('shadow_only',result['blockers'])
        self.assertIn('required_test_provenance_unverified',result['blockers'])
        self.assertEqual(result['review_evidence'][0]['head_sha'],SHA)

    def test_stale_self_unknown_and_missing_review_are_rejected(self):
        for mutate, expected in [
            (lambda o:o.update(reviews=[]),'current_head_review_missing'),
            (lambda o:o['reviews'][0].update(commit_id='b'*40),'current_head_review_missing'),
            (lambda o:o['reviews'][0].update(user_id=10),'author_review_not_independent'),
            (lambda o:o['reviews'][0].update(state='UNKNOWN'),'review_not_clear'),
            (lambda o:o['reviews'][0].update(body_present=False),'review_conclusion_missing'),
        ]:
            with self.subTest(expected=expected):
                obs=self.observation();mutate(obs)
                self.assertIn(expected,evaluate(obs)['blockers'])
                self.assertFalse(evaluate(obs)['merge_allowed'])

    def test_later_changes_requested_or_dismissal_cancels_prior_approval(self):
        for state in ('CHANGES_REQUESTED','DISMISSED','PENDING','COMMENTED'):
            obs=self.observation()
            obs['reviews'].append({**obs['reviews'][0],'id':13,'state':state})
            self.assertIn('review_not_clear',evaluate(obs)['blockers'])

    def test_manual_paths_still_require_independent_scope_decision(self):
        obs=self.observation();obs['files'][0]['filename']='.devflow/deploy.sh'
        self.assertIn('classification_requires_manual',evaluate(obs)['blockers'])
