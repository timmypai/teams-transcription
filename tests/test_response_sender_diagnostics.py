"""Actual selection method and JS on Node-only fixtures; no browser/media I/O."""
import json
import subprocess
import tempfile
from types import SimpleNamespace
import unittest

from teams_transcription.browser import MeetingBrowser
from teams_transcription.diagnostics import sender_selection_diagnostic
from teams_transcription.response_runtime import ProductionResponseController
from teams_transcription.storage import Session
from lifecycle_probe import session_events


NODE = r"""
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
let getSendersCalls=0;
const peers=input.peers.map(p=>({connectionState:p.closed?'closed':'connected',
  getSenders:()=>{getSendersCalls++;return p.tracks.map(t=>({track:t?Object.freeze(t):null}));}}));
const window=input.hook===false?{}:{__meeting:{peers}};
const document={}, location={href:'local-fixture'};
const value=eval('('+input.script+')')();
process.stdout.write(JSON.stringify({value,getSendersCalls}));
"""


def audio(*, enabled=False, state='live'):
    return {'kind':'audio','enabled':enabled,'readyState':state,
        'id':'private-id','label':'private-label'}


class NodeFrame:
    def __init__(self, peers=(), *, hook=True):
        self.peers, self.hook = list(peers), hook
        self.enumerations = self.sender_reads = 0
        self.bindings = []

    async def evaluate(self, script, args=None):
        if args is not None:
            self.bindings.append(args)
            return None
        self.enumerations += 1
        result = subprocess.run(['node','-e',NODE], input=json.dumps(
            {'script':script,'peers':self.peers,'hook':self.hook}),
            text=True, capture_output=True, check=True)
        value = json.loads(result.stdout)
        self.sender_reads += value['getSendersCalls']
        return value['value']


class SenderDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def meeting(self, frames):
        meeting = MeetingBrowser(None,None,None)
        meeting.page = SimpleNamespace(frames=frames)
        return meeting

    async def test_actual_method_zero_one_two_candidates_preserves_unique_rule(self):
        for count in [0,1,2]:
            with self.subTest(count=count):
                frame = NodeFrame([{'tracks':[audio() for _ in range(count)]}])
                meeting = self.meeting([frame])
                if count == 1:
                    selected, peer, sender = await meeting.response_sender('local-fixture')
                    self.assertIs(selected,frame)
                    self.assertEqual((peer,sender),(0,0))
                    self.assertEqual([value['meetingId'] for value in frame.bindings],['local-fixture'])
                else:
                    with self.assertRaisesRegex(RuntimeError,'unique_muted_sender_required'):
                        await meeting.response_sender('local-fixture')
                    self.assertEqual(frame.bindings,[])
                diagnostic = sender_selection_diagnostic(meeting._response_sender_diagnostic)
                self.assertEqual(diagnostic['eligible_count'],count)
                self.assertTrue(diagnostic['enumeration_complete'])
                self.assertEqual(diagnostic['outcome'],{0:'none',1:'selected',2:'multiple'}[count])
                self.assertEqual((frame.enumerations,frame.sender_reads),(1,1))

    async def test_same_enumeration_counts_missing_ended_enabled_and_closed_tracks(self):
        tracks = [None, {'kind':'video','enabled':False,'readyState':'live'},
            audio(enabled=True), audio(state='ended'), audio()]
        frame = NodeFrame([{'tracks':tracks},{'closed':True,'tracks':[audio()]}])
        meeting = self.meeting([frame])
        await meeting.response_sender('local-fixture')
        diagnostic = sender_selection_diagnostic(meeting._response_sender_diagnostic)
        first,closed = diagnostic['frames'][0]['peers']
        self.assertEqual({k:first[k] for k in ['sender_count','track_count','audio_count',
            'audio_live_count','audio_ended_count','audio_enabled_count','audio_muted_count','eligible_count']},
            dict(sender_count=5,track_count=4,audio_count=3,audio_live_count=2,
                audio_ended_count=1,audio_enabled_count=1,audio_muted_count=2,eligible_count=1))
        self.assertTrue(closed['closed']); self.assertEqual(closed['eligible_count'],0)
        self.assertEqual((frame.enumerations,frame.sender_reads),(1,2))
        self.assertNotIn('private',json.dumps(diagnostic))

    async def test_missing_hook_empty_peers_and_multiple_frames_are_distinct(self):
        frames = [NodeFrame(hook=False),NodeFrame(),NodeFrame([{'tracks':[audio()]}]),
            NodeFrame([{'tracks':[audio()]}])]
        meeting = self.meeting(frames)
        with self.assertRaisesRegex(RuntimeError,'unique_muted_sender_required'):
            await meeting.response_sender('local-fixture')
        diagnostic = sender_selection_diagnostic(meeting._response_sender_diagnostic)
        self.assertEqual((diagnostic['frame_count'],diagnostic['eligible_count']),(4,2))
        self.assertFalse(diagnostic['frames'][0]['hook_available'])
        self.assertFalse(diagnostic['frames'][0]['peers_available'])
        self.assertTrue(diagnostic['frames'][1]['hook_available'])
        self.assertTrue(diagnostic['frames'][1]['peers_available'])
        self.assertEqual(diagnostic['frames'][1]['peer_count'],0)
        self.assertTrue(all(f.enumerations==1 and not f.bindings for f in frames))

    async def test_actual_controller_failure_persists_counts_after_cleanup(self):
        for count in [0,2]:
            with self.subTest(count=count),tempfile.TemporaryDirectory() as root:
                config={'source':'meeting','provider':'codex','allowed_egress_hosts':['example.com']}
                session=Session(root).create(config)
                frame=NodeFrame([{'tracks':[audio() for _ in range(count)]}])
                controller=ProductionResponseController(session,self.meeting([frame]),None,config,[])
                self.assertFalse(await controller.start())
                await controller.stop()
                event=session_events(session)[-1]
                self.assertEqual(event['startup_error'],{'phase':'sender_select','exception_type':'RuntimeError'})
                self.assertEqual(event['cleanup_errors'],[])
                self.assertEqual(event['sender_selection'],controller.status()['sender_selection'])
                self.assertEqual(event['sender_selection']['eligible_count'],count)
                self.assertNotIn('private',json.dumps(event))
                self.assertFalse(controller.status()['output_enabled'])
                self.assertIsNone(controller.native)

    def test_receipt_size_values_and_fields_are_bounded(self):
        peer={'sender_count':10**9,'audio_count':True,'audio_live_count':-1,
            'peer_available':True,'closed':False,'id':'secret','url':'secret','sdp':'secret'}
        frame={'hook_available':True,'peers_available':True,'peer_count':10**9,'peers':[peer]*40}
        value=sender_selection_diagnostic({'frame_count':10**9,'eligible_count':10**9,
            'outcome':'secret','frames':[frame]*20,'label':'secret'})
        self.assertEqual((value['frame_count'],value['eligible_count']),(65535,65535))
        self.assertEqual(value['outcome'],'unknown')
        self.assertEqual(len(value['frames']),8);self.assertTrue(value['frames_truncated'])
        self.assertEqual(len(value['frames'][0]['peers']),16)
        self.assertTrue(value['frames'][0]['peers_truncated'])
        self.assertEqual(value['frames'][0]['peers'][0]['audio_count'],0)
        self.assertNotIn('secret',json.dumps(value))


if __name__=='__main__':
    unittest.main()
