"""Manual Actions public admission seam, synthetic GitHub metadata only."""
import copy
import unittest

from tools.target_admission import validate_release, collect

SHA='a'*40
HEAD='b'*40


def evidence():
    return dict(repository='timmypai/teams-transcription',default_branch='main',tip=SHA,
        source_sha=SHA,tree='c'*40,reviewed_tree='c'*40,
        pr=dict(number=1,merged=True,merge_commit_sha=SHA,head_sha=HEAD,author='writer',
            base_ref='main',head_repository='timmypai/teams-transcription',unresolved_threads=0),
        reviews=[dict(id=2,user=dict(login='reviewer',type='User'),state='APPROVED',
            commit_id=HEAD,body='Reviewed full diff and policy; base exceptions individually accepted.',
            author_association='COLLABORATOR',html_url='https://github.com/timmypai/teams-transcription/pull/1#pullrequestreview-2')],
        run=dict(id=3,run_attempt=1,head_sha=HEAD,event='pull_request',path='.github/workflows/ci.yml',
            status='completed',conclusion='success',repository=dict(full_name='timmypai/teams-transcription'),
            head_repository=dict(full_name='timmypai/teams-transcription'),workflow_id=4))


class TargetAdmissionTests(unittest.TestCase):
    def test_collector_uses_canonical_repo_endpoint_and_rechecks_identity(self):
        e=evidence();repo=e['repository']
        pr=dict(number=1,merged=True,merged_at='2026-11-18T00:00:00Z',merge_commit_sha=SHA,
            head=dict(sha=HEAD,repo=dict(full_name=repo)),base=dict(ref='main'),
            user=dict(login='writer'),updated_at='2026-11-18T00:00:00Z')
        calls=[]
        class API:
            def request(self,path,payload=None):
                calls.append(path)
                prefix='/repos/'+repo
                if path==prefix:return dict(full_name=repo,default_branch='main'),{}
                if path==prefix+'/branches/main':return dict(commit=dict(sha=SHA)),{}
                if '/commits/'+SHA+'/pulls?' in path:return [pr],{}
                if path==prefix+'/pulls/1':return pr,{}
                if '/pulls/1/reviews?' in path:return e['reviews'],{}
                if '/workflows/ci.yml/runs?' in path:return dict(total_count=1,workflow_runs=[e['run']]),{}
                if path==prefix+'/actions/runs/3':return e['run'],{}
                if path==prefix+'/actions/workflows/ci.yml':return dict(id=4),{}
                if '/git/commits/' in path:return dict(tree=dict(sha='c'*40)),{}
                if path=='/graphql':return dict(data=dict(repository=dict(pullRequest=dict(reviewThreads=dict(
                    nodes=[],pageInfo=dict(hasNextPage=False,endCursor=None)))))),{}
                raise AssertionError('noncanonical or unknown API endpoint: '+path)
        self.assertEqual(collect(SHA,API())['reviewed_head_sha'],HEAD)
        self.assertEqual(calls.count('/repos/'+repo+'/branches/main'),2)
        self.assertNotIn('/repos/'+repo+'/',calls)

    def test_reviewed_exact_tree_and_ci_are_admitted(self):
        result=validate_release(evidence())
        self.assertEqual(result['reviewed_head_sha'],HEAD)
        self.assertEqual(result['source_sha'],SHA)
        self.assertEqual(result['reviewer'],'reviewer')
        self.assertEqual(result['ci_run_attempt'],1)

    def test_stale_self_missing_unknown_or_unresolved_are_not_admitted(self):
        for update in (
            {'tip':'d'*40},{'reviewed_tree':'d'*40},{'reviews':[]},
            {'reviews':[{**evidence()['reviews'][0],'commit_id':SHA}]},
            {'reviews':[{**evidence()['reviews'][0],'user':dict(login='writer',type='User')}]},
            {'reviews':[{**evidence()['reviews'][0],'state':'UNKNOWN'}]},
            {'reviews':[{**evidence()['reviews'][0],'body':''}]},
            {'pr':{**evidence()['pr'],'unresolved_threads':None}},
            {'pr':{**evidence()['pr'],'unresolved_threads':1}},
        ):
            with self.subTest(update=update),self.assertRaises(ValueError):
                validate_release({**evidence(),**update})

    def test_workflow_run_identity_is_required_not_a_green_marker(self):
        for update in (dict(head_sha=SHA),dict(event='push'),dict(run_attempt=0),
                       dict(conclusion='skipped'),dict(path='.github/workflows/evil.yml'),
                       dict(head_repository=dict(full_name='foreign/repo'))):
            e=evidence();e['run'].update(update)
            with self.subTest(update=update),self.assertRaises(ValueError):validate_release(e)

    def test_later_review_withdrawal_invalidates_old_approval(self):
        for state in ('CHANGES_REQUESTED','DISMISSED','PENDING','COMMENTED'):
            e=copy.deepcopy(evidence());e['reviews'].append({**e['reviews'][0],'id':3,'state':state})
            with self.assertRaises(ValueError):validate_release(e)
