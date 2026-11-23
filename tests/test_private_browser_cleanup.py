"""Runner controller -> real shell/exporter, with a disposable Docker CLI double."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from deploy import private_control, target_deploy as deploy
from test_calendar_export import valid


DOCKER_DOUBLE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path

socket, *args = sys.argv[1:]
if socket == '--host': socket = args.pop(0)
assert socket == f'unix:///run/user/{os.getuid()}/docker.sock' or socket == f'--host=unix:///run/user/{os.getuid()}/docker.sock'
root = Path(__file__).parent
state = root/'docker.json'
helpers = json.loads(state.read_text()) if state.exists() else {}
app = 'devflow-teams-transcription-app'
if args[0] == 'inspect' and args[1] == app:
    print('true' if args[-1] == '{{.State.Running}}' else 'synthetic-image')
elif args[:2] == ['image','inspect']:
    print(os.environ['GITHUB_SHA'])
elif args[0] == 'create':
    if (root/'unacknowledged-create').exists(): sys.exit(1)
    name = args[args.index('--name')+1]
    identity = f"{int(os.environ['GITHUB_RUN_ID']):064x}"
    labels = dict(args[i+1].split('=',1) for i,a in enumerate(args) if a == '--label')
    helpers[name] = dict(Id=identity,Name='/'+name,Config=dict(Labels=labels))
    state.write_text(json.dumps(helpers))
    print(identity)
elif args[:2] == ['container','ls']:
    for name,row in helpers.items(): print(row['Id'],name)
elif args[:2] == ['container','inspect']:
    print(json.dumps([row for row in helpers.values() if row['Id']==args[2]]))
elif args[:2] == ['rm','-f']:
    if (root/'fail-remove').exists():
        print('synthetic-private-error',file=sys.stderr); sys.exit(1)
    names = [name for name, row in helpers.items() if args[2] in (name,row['Id'])]
    assert len(names) == 1
    del helpers[names[0]]
    state.write_text(json.dumps(helpers))
elif args[0] == 'start' or args[:1] == ['exec'] and args[2] == 'bash':
    pass
elif args[:1] == ['exec'] and args[-1] == '/evidence/result.json':
    operation = os.environ['DEVFLOW_OP']
    if operation == 'calendar-occurrences':
        print(json.dumps(dict(__CALENDAR__,occurrences=[dict(occurrence_id='a'*32,revision=1,
            joinable=True,start_at='2030-01-01T09:00:00+00:00',end_at='2030-01-01T10:00:00+00:00')])))
    else:
        now = time.time()
        print(json.dumps(dict(ok=True,occurrence_id='a'*32,request_sha256='b'*64,
            revision=2,issued_at=now,expires_at=now+300,expires_in_seconds=300)))
else:
    sys.exit('unexpected synthetic Docker command')
'''


class BrowserCleanupTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        docker = self.root/'docker'
        docker.write_text(DOCKER_DOUBLE.replace('__CALENDAR__', repr(valid())))
        docker.chmod(0o700)
        self.run_id = 800
        self.env = dict(PATH=f"{self.root}{os.pathsep}{os.environ['PATH']}", GITHUB_SHA='c'*40,
                        GITHUB_RUN_ATTEMPT='1')
        self.fail_remove = self.root/'fail-remove'

    def invoke(self, action, *, actor=17, clock_shift=0):
        # Each call creates a new controller; only the private host disk survives.
        self.run_id += 1
        controller = deploy.Controller(self.root,self.run_id,1)
        self.last_failures = controller.failures
        real_time = time.time
        with (self.root/'operation.lock').open('ab') as lock, \
                patch.dict(os.environ,{**self.env,'GITHUB_RUN_ID':str(self.run_id)}), \
                patch.object(private_control.time,'time',lambda:real_time()+clock_shift), \
                patch.object(controller,'runtime',side_effect=AssertionError('no runtime dispatch allowed')):
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            controller.operation_lock = lock.fileno()
            try:
                value = private_control.run_control(controller,action,'c'*40,'d'*64,
                    (actor,time.time()),'ADMIN_PASSWORD=synthetic-private-password')
            except ValueError as exc:
                from deploy.target_diagnostics import failure
                return failure(exc)['code']
        return value

    def assert_blocked_until_owned_cleanup(self):
        previous = (self.root/'docker.json').read_bytes()
        self.assertTrue(json.loads(previous), 'Docker helper really remains after the failed removal')
        self.assertEqual(self.invoke('preview',clock_shift=301),'control_slot_occupied')
        self.assertEqual(self.invoke('join-confirmed-present-and-media',clock_shift=301),'control_slot_occupied')
        self.assertEqual(self.invoke('stop',actor=999),'control_slot_unavailable')
        self.assertEqual(self.invoke('stop'),'operation_cleanup_unverified')
        self.assertEqual((self.root/'docker.json').read_bytes(),previous)
        self.fail_remove.unlink()
        self.assertEqual(self.invoke('stop'),dict(ok=True,browser_cleanup_verified=True))
        self.assertEqual(json.loads((self.root/'docker.json').read_text()),{})
        self.assertEqual(self.invoke('join-confirmed-present-and-media'),'control_confirmation_stale')
        self.assertEqual(self.invoke('preview'),dict(ok=True,unique_occurrence_staged=True,confirmation_window_seconds=300))
        self.assertEqual(json.loads((self.root/'docker.json').read_text()),{})

    def test_failed_preview_cleanup_blocks_replacement_until_verified(self):
        self.fail_remove.touch()
        self.assertEqual(self.invoke('preview'),'operation_cleanup_unverified')
        self.assert_blocked_until_owned_cleanup()

    def test_failed_binding_cleanup_cannot_replay_or_be_overwritten(self):
        self.assertIs(self.invoke('preview')['ok'], True)
        self.fail_remove.touch()
        self.assertEqual(self.invoke('join-confirmed-present-and-media'),'operation_cleanup_unverified')
        self.assert_blocked_until_owned_cleanup()

    def test_absence_after_unacknowledged_create_does_not_prove_cleanup(self):
        (self.root/'unacknowledged-create').touch()
        self.assertEqual(self.invoke('preview'),'operation_cleanup_unverified')
        self.assertEqual(self.last_failures.primary['code'],'control_browser_failed')
        self.assertEqual(self.last_failures.secondary[0]['code'],'operation_cleanup_unverified')
        self.assertEqual(self.last_failures.cleanup,'unknown')
        self.assertEqual(self.invoke('stop'),'operation_cleanup_unverified')
        self.assertEqual(self.invoke('preview',clock_shift=301),'control_slot_occupied')

    def test_same_name_with_different_container_id_is_not_owned_cleanup(self):
        self.fail_remove.touch()
        self.assertEqual(self.invoke('preview'),'operation_cleanup_unverified')
        self.fail_remove.unlink()
        state = self.root/'docker.json'
        helpers = json.loads(state.read_text())
        [row] = helpers.values()
        row['Id'] = 'f'*64
        replacement = json.dumps(helpers)
        state.write_text(replacement)
        self.assertEqual(self.invoke('stop'),'operation_cleanup_unverified')
        self.assertEqual(state.read_text(),replacement)
