"""Same enumeration and actual selector; no browser, model or device."""
import json
from types import SimpleNamespace
import unittest

from teams_transcription.browser import MeetingBrowser
from teams_transcription.diagnostics import sender_selection_diagnostic
from tests.test_null_track_sender import NodeFrame, null


class AudioSenderDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def inspect(self, spec, *, accepted=False):
        frame=await NodeFrame().start([spec]);self.addAsyncCleanup(frame.close)
        meeting=MeetingBrowser(None,None,None);meeting.page=SimpleNamespace(frames=[frame])
        if accepted:
            await meeting.response_sender('one')
        else:
            with self.assertRaisesRegex(RuntimeError,'unique_muted_sender_required'):
                await meeting.response_sender('one')
        return sender_selection_diagnostic(meeting._response_sender_diagnostic)

    async def test_audio_recvonly_video_sendrecv_not_mistaken_for_audio_send(self):
        diag=await self.inspect({'senders':[None,None], 'transceivers':[
            {'sender':0,'kind':'audio','currentDirection':'recvonly'},
            {'sender':1,'kind':'video','currentDirection':'sendrecv'}]})
        peer=diag['frames'][0]['peers'][0]
        self.assertEqual((peer['current_sendrecv'],peer['audio_current_sendrecv'],peer['audio_current_recvonly']),(1,0,1))
        self.assertEqual(peer['audio_transceivers'],[{'direction':'sendrecv',
            'currentDirection':'recvonly','sender_member':True,'sender_track_null':True,
            'stopped':False,'rejection_reason':'current_not_sending'}])
        self.assertEqual(diag['eligible_count'],0)

    async def test_audio_sendrecv_sender_not_in_peer_set_rejected_with_exact_reason(self):
        diag=await self.inspect(null('sendrecv',outside_sender=True))
        row=diag['frames'][0]['peers'][0]['audio_transceivers'][0]
        self.assertEqual(row['currentDirection'],'sendrecv')
        self.assertFalse(row['sender_member'])
        self.assertEqual(row['rejection_reason'],'sender_not_member')
        self.assertEqual(diag['eligible_count'],0)

    async def test_unique_audio_sendrecv_member_stays_eligible(self):
        diag=await self.inspect(null('sendrecv'),accepted=True)
        row=diag['frames'][0]['peers'][0]['audio_transceivers'][0]
        self.assertTrue(row['sender_member']);self.assertEqual(row['rejection_reason'],'eligible')
        self.assertEqual((diag['eligible_count'],diag['outcome']),(1,'selected'))

    async def test_same_scan_audio_detail_truncates_but_full_counts_remain(self):
        diag=await self.inspect({'senders':[None]*12,'transceivers':[
            {'sender':i,'currentDirection':'recvonly'} for i in range(12)]})
        peer=diag['frames'][0]['peers'][0]
        self.assertEqual(len(peer['audio_transceivers']),8)
        self.assertTrue(peer['audio_transceivers_truncated'])
        self.assertEqual(peer['audio_current_recvonly'],12)
        self.assertEqual(peer['receiver_audio_count'],12)

    def test_receipt_fixed_enums_booleans_size_and_privacy(self):
        row={'direction':'private-url','currentDirection':['private-sdp'],
            'sender_member':'private-label','sender_track_null':True,'stopped':False,
            'rejection_reason':'private-token','id':'private-id','payload':'private-payload'}
        diag=sender_selection_diagnostic({'frames':[{'peers':[{'audio_transceivers':[row]*10,
            'audio_current_sendrecv':10**12}]}]})
        peer=diag['frames'][0]['peers'][0]
        self.assertEqual(peer['audio_current_sendrecv'],65535)
        self.assertTrue(peer['audio_transceivers_truncated'])
        self.assertEqual(len(peer['audio_transceivers']),8)
        self.assertEqual(peer['audio_transceivers'][0],{'direction':'other','currentDirection':'other',
            'sender_member':False,'sender_track_null':True,'stopped':False,'rejection_reason':'unknown'})
        self.assertNotIn('private',json.dumps(diag))


if __name__=='__main__':unittest.main()
