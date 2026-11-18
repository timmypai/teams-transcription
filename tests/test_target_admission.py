"""Manual Actions public admission seam, synthetic GitHub metadata only."""
import copy
import unittest

from tools.target_admission import validate_release, collect

SHA='a'*40
HEAD='b'*40


def evidence():
    return dict(repository='timmypai/teams-transcription',default_branch='main',tip=SHA,
        source_sha=SHA,tree='c'*40,reviewed_tree='c'*40,
        pr=dict(number=1,merged=True,merge_commit_sha=SHA,head_sha=HEAD,author='writer',author_id=9,
            base_ref='main',head_repository='timmypai/teams-transcription',unresolved_threads=0),
        reviews=[dict(id=2,user=dict(login='paulpai0412',id=273007156,type='User'),state='APPROVED',
            commit_id=HEAD,body='Reviewed full diff and policy; base exceptions individually accepted.',
            author_association='COLLABORATOR',html_url='https://github.com/timmypai/teams-transcription/pull/1#pullrequestreview-2')],
        run=dict(id=3,run_attempt=1,head_sha=HEAD,event='pull_request',path='.github/workflows/ci.yml',
            status='completed',conclusion='success',repository=dict(full_name='timmypai/teams-transcription'),
            head_repository=dict(full_name='timmypai/teams-transcription'),workflow_id=4))


class TargetAdmissionTests(unittest.TestCase):
    def test_designated_real_github_none_identity_with_synthetic_release(self):
        # Historical GitHub review 5955663902: identity only; release/CI are
        # explicitly synthetic, NOT an approval of any new head or deployment.
        e=evidence()
        e['reviews'][0]['user']=dict(login='paulpai0412',id=273007156,type='User')
        e['reviews'][0]['author_association']='NONE'
        self.assertEqual(validate_release(e)['reviewer'],'paulpai0412')

    def test_none_stranger_spoof_missing_bot_and_self_are_rejected(self):
        for user in (
            dict(login='stranger',id=8,type='User'),
            dict(login='paulpai0412',id=8,type='User'),
            dict(login='stranger',id=273007156,type='User'),
            dict(login='paulpai0412',type='User'),
            dict(login='paulpai0412',id=True,type='User'),
            dict(login='paulpai0412',id=273007156,type='Bot'),
        ):
            e=evidence();e['reviews'][0].update(user=user,author_association='NONE')
            with self.subTest(user=user),self.assertRaises(ValueError):validate_release(e)
        e=evidence();e['pr']['author']='paulpai0412'
        e['reviews'][0].update(user=dict(login='paulpai0412',id=273007156,type='User'),author_association='NONE')
        with self.assertRaises(ValueError):validate_release(e)

    def test_collector_uses_canonical_repo_endpoint_and_rechecks_identity(self):
        e=evidence();repo=e['repository']
        pr=dict(number=1,merged=True,merged_at='2026-11-18T00:00:00Z',merge_commit_sha=SHA,
            head=dict(sha=HEAD,repo=dict(full_name=repo)),base=dict(ref='main'),
            user=dict(login='writer',id=9),updated_at='2026-11-18T00:00:00Z')
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
        result=collect(SHA,API())
        self.assertEqual(result['reviewed_head_sha'],HEAD)
        self.assertEqual(result['reviewer_id'],273007156)
        self.assertEqual(calls.count('/repos/'+repo+'/branches/main'),2)
        self.assertNotIn('/repos/'+repo+'/',calls)
        # Exercise API numeric ID propagation, not a pure seam fixture alone.
        for bad in (None,8,True):
            e['reviews'][0]['user']['id']=bad
            with self.subTest(id=bad),self.assertRaises(ValueError):collect(SHA,API())
        e['reviews'][0]['user']['id']=273007156
        for target in ('review','tip','pr','run','threads'):
            class MovingAPI(API):
                def __init__(self):self.counts={}
                def request(self,path,payload=None):
                    value,headers=super().request(path,payload)
                    self.counts[path]=self.counts.get(path,0)+1
                    value=copy.deepcopy(value)
                    if self.counts[path]>1:
                        if target=='review' and '/reviews?' in path:value[0]['state']='DISMISSED'
                        if target=='tip' and path.endswith('/branches/main'):value['commit']['sha']='d'*40
                        if target=='pr' and path.endswith('/pulls/1'):value['updated_at']='changed'
                        if target=='run' and path.endswith('/actions/runs/3'):value['run_attempt']=2
                        if target=='threads' and path=='/graphql':
                            value['data']['repository']['pullRequest']['reviewThreads']['nodes']=[dict(isResolved=False)]
                    return value,headers
            with self.subTest(changed=target),self.assertRaisesRegex(ValueError,'snapshot_changed'):
                collect(SHA,MovingAPI())

    def test_reviewed_exact_tree_and_ci_are_admitted(self):
        result=validate_release(evidence())
        self.assertEqual(result['reviewed_head_sha'],HEAD)
        self.assertEqual(result['source_sha'],SHA)
        self.assertEqual(result['reviewer'],'paulpai0412')
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
