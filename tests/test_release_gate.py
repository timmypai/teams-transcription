"""Successful Actions evidence is required; fixtures never call GitHub or GHCR."""
import hashlib
import io
import json
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
import tempfile
import urllib.error

from deploy import release_gate as gate
from tools.release_receipt import write_receipt
from tools.image_content_check import contains_sensitive,sensitive_metadata

SHA = 'a' * 40
DIGEST = 'sha256:' + 'b' * 64


def fixture():
    proof = {'schema_version':1,'repository':gate.REPOSITORY,'source_sha':SHA,
        'image':gate.IMAGE+'@'+DIGEST,'platform':'linux/arm64','build_run_id':123,
        'build_run_attempt':1,'validation':{'offline_contracts':True,'compose_config':True,
            'compose_smoke':True,'image_content_scan':True}}
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as archive:
        archive.writestr('published-image.json',json.dumps(proof))
    data=stream.getvalue()
    run={'id':123,'run_attempt':1,'workflow_id':789,'path':gate.WORKFLOW,
        'head_sha':SHA,'head_branch':'main','event':'workflow_dispatch',
        'status':'completed','conclusion':'success',
        'repository':{'full_name':gate.REPOSITORY},'head_repository':{'full_name':gate.REPOSITORY}}
    artifacts={'artifacts':[{'id':456,'name':'arm64-image-123-1','expired':False,
        'workflow_run':{'id':123,'head_sha':SHA},
        'digest':'sha256:'+hashlib.sha256(data).hexdigest()}]}
    routes={'/actions/workflows/arm64-image.yml':{'id':789,'path':gate.WORKFLOW},
        '/actions/runs/123':run,'/actions/runs/123/artifacts?per_page=100':artifacts,
        '/actions/artifacts/456/zip':data}
    return proof,routes


class ReleaseGateTests(unittest.TestCase):
    def test_matching_successful_main_run_and_artifact_admit_exact_digest(self):
        _,routes=fixture()
        result=gate.verify_release(DIGEST,SHA,'123',request=lambda path:routes[path])
        self.assertEqual(result['image'],gate.IMAGE+'@'+DIGEST)
        self.assertEqual(result['source_sha'],SHA)
        self.assertEqual(result['build_run_id'],123)

    def test_failed_fork_branch_workflow_or_source_cannot_admit(self):
        changes=[('conclusion','failure'),('status','in_progress'),('head_sha','c'*40),
            ('head_branch','feature'),('event','pull_request'),('workflow_id',999),
            ('path','.github/workflows/elsewhere.yml'),
            ('repository',{'full_name':'attacker/repo'}),('head_repository',{'full_name':'attacker/repo'})]
        for key,value in changes:
            _,routes=fixture();routes['/actions/runs/123'][key]=value
            with self.subTest(key=key),self.assertRaises(gate.Rejected):
                gate.verify_release(DIGEST,SHA,'123',request=lambda path:routes[path])

    def test_cached_healthy_rollback_revalidates_exact_run_attempt_without_expired_artifact(self):
        _,routes=fixture()
        admitted=gate.verify_release(DIGEST,SHA,'123',request=lambda path:routes[path])
        run=routes['/actions/runs/123']
        routes={'/actions/workflows/arm64-image.yml':routes['/actions/workflows/arm64-image.yml'],
            '/actions/runs/123/attempts/1':run}
        self.assertEqual(gate.verify_release(DIGEST,SHA,'123',cached=admitted,request=lambda path:routes[path]),admitted)
        run['conclusion']='failure'
        with self.assertRaises(gate.Rejected):
            gate.verify_release(DIGEST,SHA,'123',cached=admitted,request=lambda path:routes[path])

    def test_download_redirect_strips_authorization_and_rejects_arbitrary_hosts(self):
        class Response(io.BytesIO):
            pass
        class Opener:
            def __init__(self,host):self.host=host;self.calls=[]
            def open(self,request,timeout):
                self.calls.append(request)
                if len(self.calls)==1:
                    raise urllib.error.HTTPError(request.full_url,302,'',{'Location':self.host},None)
                return Response(b'zip-fixture')
        opener=Opener('https://storage.blob.core.windows.net/artifact?signature=PRIVATE')
        with patch.object(gate.urllib.request,'build_opener',return_value=opener):
            self.assertEqual(gate.github_request('synthetic-token')('/actions/artifacts/456/zip'),b'zip-fixture')
        self.assertIsNotNone(opener.calls[0].get_header('Authorization'))
        self.assertIsNone(opener.calls[1].get_header('Authorization'))
        for host in ('https://evil.invalid/artifact','http://storage.blob.core.windows.net/a',
                'https://user@storage.blob.core.windows.net/a'):
            opener=Opener(host)
            with patch.object(gate.urllib.request,'build_opener',return_value=opener),self.assertRaises(gate.Rejected):
                gate.github_request('synthetic-token')('/actions/artifacts/456/zip')
            self.assertEqual(len(opener.calls),1)

    def test_failed_validation_cannot_create_publishable_receipt(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            (root/'repo-digest.txt').write_text(gate.IMAGE+'@'+DIGEST)
            (root/'contracts.json').write_text(json.dumps({'success':False,'tests':7,'failures':1,'errors':0,'skipped':0}))
            environment={'GITHUB_SHA':SHA,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','GITHUB_REPOSITORY':gate.REPOSITORY}
            with self.assertRaisesRegex(ValueError,'offline_contract_gate_failed'):
                write_receipt(root,environment)
            self.assertFalse((root/'published-image.json').exists())

    def test_receipt_is_bound_to_source_run_digest_and_requires_native_arm64(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root)
            (root/'repo-digest.txt').write_text(gate.IMAGE+'@'+DIGEST)
            (root/'contracts.json').write_text(json.dumps({'success':True,'tests':7,'failures':0,'errors':0,'skipped':0}))
            config={'success':True,'checks':dict.fromkeys(('nonroot_explicit_identity','read_only_drop_caps','sandbox_without_privilege_bypass','bounded_resources'),True)}
            smoke={'success':True,'checks':dict.fromkeys(('actual_default_daemon_healthy','actual_sandboxed_chromium_local_page','pinned_codex_installed','effective_cgroup_cpu_memory_pids_limits'),True)}
            content={'success':True,'architecture':'amd64','checks':dict.fromkeys(('private_paths_absent','data_root_empty','application_files_bounded',
                'known_sensitive_patterns_absent','root_auth_files_absent','credential_environment_absent','known_sensitive_metadata_absent'),True)}
            for name,value in [('compose-config.json',config),('compose-smoke.json',smoke),('image-content.json',content)]:
                (root/name).write_text(json.dumps(value))
            environment={'GITHUB_SHA':SHA,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1','GITHUB_REPOSITORY':gate.REPOSITORY}
            with self.assertRaisesRegex(ValueError,'native_arm64_image_required'):write_receipt(root,environment)
            self.assertFalse((root/'published-image.json').exists())
            content['architecture']='arm64';(root/'image-content.json').write_text(json.dumps(content))
            proof=write_receipt(root,environment)
            self.assertEqual((proof['source_sha'],proof['build_run_id'],proof['image']),(SHA,123,gate.IMAGE+'@'+DIGEST))
            smoke['checks']['pinned_codex_installed']=False;(root/'compose-smoke.json').write_text(json.dumps(smoke))
            with self.assertRaisesRegex(ValueError,'image_validation_gate_failed'):write_receipt(root,environment)

    def test_known_credentials_and_private_meeting_locator_are_not_image_content(self):
        for raw in (b'ghp_'+b'a'*36,b'sk-proj-'+b'b'*40,b'ya29.'+b'c'*40,
                b'1//'+b'd'*40,b'-----BEGIN PRIVATE KEY-----',
                b'https://teams.microsoft.com/l/meetup-join/'+b'e'*50):
            self.assertTrue(contains_sensitive(raw))
        self.assertFalse(contains_sensitive(b'public application source without credentials'))
        self.assertTrue(sensitive_metadata({'Env':['BENIGN=ghp_'+'a'*36]}))
        self.assertTrue(sensitive_metadata({'Labels':{'benign':'ghp_'+'a'*36}}))

    def test_missing_expired_or_tampered_artifact_is_rejected(self):
        for change in ('missing','expired','digest'):
            _,routes=fixture();artifacts=routes['/actions/runs/123/artifacts?per_page=100']['artifacts']
            if change=='missing':artifacts.clear()
            elif change=='expired':artifacts[0]['expired']=True
            else:artifacts[0]['digest']='sha256:'+'0'*64
            with self.subTest(change=change),self.assertRaises(gate.Rejected):
                gate.verify_release(DIGEST,SHA,'123',request=lambda path:routes[path])

    def test_artifact_cannot_claim_another_digest_or_skip_a_validation(self):
        for change in ('digest','source','validation'):
            proof,routes=fixture()
            if change=='digest':proof['image']=gate.IMAGE+'@sha256:'+'c'*64
            elif change=='source':proof['source_sha']='c'*40
            else:proof['validation']['compose_smoke']=False
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as archive:archive.writestr('published-image.json',json.dumps(proof))
            data=stream.getvalue();routes['/actions/artifacts/456/zip']=data
            routes['/actions/runs/123/artifacts?per_page=100']['artifacts'][0]['digest']='sha256:'+hashlib.sha256(data).hexdigest()
            with self.subTest(change=change),self.assertRaises(gate.Rejected):
                gate.verify_release(DIGEST,SHA,'123',request=lambda path:routes[path])


if __name__=='__main__':unittest.main()
