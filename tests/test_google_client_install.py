"""Fixed installed CLI, real private file boundary; no real client/account/network."""
import contextlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from deploy import target_runtime as runtime

CANARY = 'PRIVATE-google-secret-canary'
REDIRECT = 'http://127.0.0.1:8080/google/callback'


def client():
    return json.dumps({'web':dict(client_id='synthetic.apps.googleusercontent.com',client_secret=CANARY,
        redirect_uris=[REDIRECT],auth_uri='https://accounts.google.com/o/oauth2/auth',
        token_uri='https://oauth2.googleapis.com/token')})


class GoogleClientInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.root.chmod(0o700)
        runtime.initialize(self.root, 'synthetic-disposable-password')

    def cli(self, raw, *args):
        output = io.StringIO()
        path = Path
        # Only replace the fixed installed mount. No installation root CLI exists.
        with patch.object(sys,'argv',['runtime','install-google-client',*args]), \
                patch.object(sys,'stdin',io.StringIO(raw)), contextlib.redirect_stdout(output), \
                patch.object(runtime,'Path',side_effect=lambda value:path(self.root if value=='/data' else value)):
            try: code = runtime.main()
            except SystemExit as error: code = error.code
        self.assertNotIn(CANARY,output.getvalue())
        return code, json.loads(output.getvalue() or '{}')

    def test_fixed_cli_installs_private_persistent_client_and_repeats_without_replacing(self):
        code,value = self.cli(client())
        self.assertEqual(code,0,'fixed installed CLI must accept dedicated Web client input')
        self.assertEqual(value,dict(installed=True,existing=False,client_private=True,redirect_registered=True,
            oauth_verified=False,google_read_verified=False))
        target = self.root/'config/google-client.json'
        info = target.stat()
        self.assertEqual((info.st_uid,info.st_gid,stat.S_IMODE(info.st_mode)),(os.getuid(),os.getgid(),0o600))
        self.assertEqual(json.loads(target.read_text())['web']['client_secret'],CANARY)
        before = (info.st_ino,target.read_bytes())
        code,value = self.cli(client())
        self.assertEqual(code,0)
        self.assertTrue(value['existing'])
        self.assertEqual((target.stat().st_ino,target.read_bytes()),before)
        self.assertFalse((self.root/'config/google-credentials.json').exists())

    def test_missing_malformed_installed_token_and_unregistered_redirect_never_write(self):
        invalid = ['', CANARY, '[]', '{"web":{},"web":{}}',
            json.dumps({'installed':json.loads(client())['web']}),
            json.dumps({**json.loads(client()),'refresh_token':CANARY}),
            client().replace(REDIRECT,'http://127.0.0.1:18101/google/callback'),
            client().replace(REDIRECT,REDIRECT+'?secret='+CANARY),
            client().replace(CANARY,'x'*65537)]
        for raw in invalid:
            with self.subTest(size=len(raw)):
                code,value = self.cli(raw)
                self.assertEqual(code,1)
                self.assertIn(value['code'],runtime.GOOGLE_INSTALL_CODES)
                self.assertFalse((self.root/'config/google-client.json').exists())

    def test_existing_wrong_mode_symlink_and_conflicting_client_are_not_repaired(self):
        target = self.root/'config/google-client.json'
        target.write_text(client()); target.chmod(0o644)
        self.assertEqual(self.cli(client())[0],1)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode),0o644)
        target.unlink()
        other = self.root/'private'; other.write_text(CANARY); other.chmod(0o600)
        target.symlink_to(other)
        self.assertEqual(self.cli(client())[0],1)
        self.assertEqual(other.read_text(),CANARY)
        target.unlink()
        self.assertEqual(self.cli(client())[0],0)
        before = target.read_bytes()
        self.assertEqual(self.cli(client().replace(CANARY,'different-canary'))[0],1)
        self.assertEqual(target.read_bytes(),before)

    def test_install_failure_paused_scope_gid_and_directory_modes_fail_closed(self):
        with patch.object(runtime.os,'link',side_effect=OSError(CANARY)):
            code,value = self.cli(client())
        self.assertEqual((code,value['code']),(1,'google_client_install_failed'))
        self.assertEqual(list((self.root/'config').glob('.google-client-*')),[])
        with patch.object(runtime.os,'getgid',return_value=os.getgid()+1):
            self.assertEqual(self.cli(client())[0],1)
        from teams_transcription.assistant_store import AssistantStore
        AssistantStore(self.root).update_settings({'scheduler_paused':False})
        self.assertEqual(self.cli(client())[0],1)
        AssistantStore(self.root).update_settings({'scheduler_paused':True})
        config = self.root/'config'; config.chmod(0o750)
        self.assertEqual(self.cli(client())[0],1)
        self.assertEqual(stat.S_IMODE(config.stat().st_mode),0o750)
        config.chmod(0o700)
        path = config/'runtime.yml'; settings = json.loads(path.read_text())
        settings['google_client_config_file'] = str(self.root/'wrong.json')
        path.write_text(json.dumps(settings))
        self.assertEqual(self.cli(client())[0],1)
        self.assertFalse((self.root/'wrong.json').exists())

    def test_adapter_fixed_stdin_chain_validates_persistence_and_sanitizes_output(self):
        from deploy import target_deploy as adapter
        image = 'sha256:'+'b'*64
        state = dict(container_id='c'*64,image=image)
        installed = dict(Id=state['container_id'],Image=image,
            Config=dict(User='10001:10001'),State=dict(Running=True,StartedAt='synthetic',Health=dict(Status='healthy')),
            HostConfig=dict(ReadonlyRootfs=True,CapDrop=['ALL'],SecurityOpt=['no-new-privileges','seccomp=fixed'],
                Privileged=False,Memory=4294967296,NanoCpus=2000000000,PidsLimit=512),
            Mounts=[dict(Type='volume',Name=adapter.VOLUME,Destination='/data',RW=True)])
        calls=[]
        def command(args,**kwargs):
            calls.append(args)
            self.assertNotIn(CANARY,' '.join(args))
            if 'inspect' in args:return json.dumps([installed])
            self.assertIn('exec',args)
            self.assertIn(adapter.APP,args)
            self.assertIn(adapter.RUNTIME,args)
            if args[-1]=='smoke':return json.dumps(dict(health=True,anonymous_login_page=True))
            self.assertEqual(args[-1],'install-google-client')
            code,value = self.cli(kwargs['input'])
            self.assertEqual(code,0)
            return json.dumps(value)
        controller = adapter.Controller(self.root,1,1,command=command)
        installed['Mounts'][0]['RW']=False
        with self.assertRaisesRegex(ValueError,'deployed_persistence_unverified'):
            controller.install_google_client(state,client())
        self.assertFalse((self.root/'config/google-client.json').exists())
        installed['Mounts'][0]['RW']=True
        result = controller.install_google_client(state,client())
        self.assertTrue(result['installed'])
        self.assertFalse(result['oauth_verified'] or result['google_read_verified'])
        self.assertNotIn(CANARY,json.dumps(result))

    def test_arguments_cannot_change_path_or_bind_identity(self):
        self.assertEqual(self.cli(client(),'--id','a'*32)[0],1)
