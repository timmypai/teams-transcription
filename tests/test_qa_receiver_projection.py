"""Execute the real page adapter in Node against synthetic receiver stats."""
import json
import subprocess
import unittest

from teams_transcription.qa_probe import NoPhysicalSink
from teams_transcription.qa_rx_evidence import owned_cleanup_verified


class ReceiverProjectionTests(unittest.IsolatedAsyncioTestCase):
    async def snapshot(self,case):
        class Page:
            async def evaluate(self,script,owner):
                setup="""
const receiver={async getStats(){
 const rows=new Map();
 if(kind!=='empty'&&kind!=='missing')rows.set('audio',{type:'inbound-rtp',kind:'audio',
   ssrc:42,packetsReceived:3,bytesReceived:150,private:'PRIVATE-SDP-TOKEN'});
 if(kind==='ambiguous')rows.set('other',{type:'inbound-rtp',kind:'audio',ssrc:43});
 rows.set('video',{type:'inbound-rtp',kind:'video',ssrc:99,packetsReceived:999});
 if(kind==='replaced')valid=false;
 return rows;
}};
let valid=true;
global.window={__live:{pc:{connectionState:'connected'},qaOutput:{owner,
 receiver,valid:()=>valid,noPlc:{track:{enabled:false},stats:()=>({
 input:kind==='empty'?{packets:0,frames:0,duration:0,energy:0}:
   {packets:2,frames:960,duration:.02,energy:.001},
 render:{frames:960,duration:.02,energy:.001}})}}}};
"""
                program=('const kind='+json.dumps(case)+';const owner='+json.dumps(owner)+';'+setup+
                    '\n('+script+')(owner).then(v=>console.log(JSON.stringify(v))).catch(()=>process.exit(1));')
                result=subprocess.run(['node','-e',program],capture_output=True,timeout=5)
                if result.returncode:raise ValueError('synthetic_receiver_rejected')
                return json.loads(result.stdout)
        sink=NoPhysicalSink();sink.page=Page()
        return await sink.rx_snapshot()

    async def test_exact_receiver_projects_audio_counters_and_no_private_stats(self):
        value=await self.snapshot('valid')
        self.assertEqual(value['inbound'],{'ssrc':42,'packets_received':3,'bytes_received':150})
        self.assertEqual(value['source']['frames'],960)
        self.assertNotIn('PRIVATE',json.dumps(value))

    async def test_no_first_packet_can_report_only_a_proven_zero_baseline(self):
        value=await self.snapshot('empty')
        self.assertEqual(value['inbound'],{'ssrc':None,'packets_received':0,'bytes_received':0})
        self.assertEqual(value['source'],{'packets':0,'frames':0,'duration':0,'energy':0})

    async def test_missing_ambiguous_or_replaced_receiver_never_projects_a_snapshot(self):
        for case in ('missing','ambiguous','replaced'):
            with self.subTest(case=case),self.assertRaises(ValueError):await self.snapshot(case)


class OwnedCleanupReceiptTests(unittest.TestCase):
    def test_ack_and_reaping_are_independent_required_proofs(self):
        valid={'status':'cleaned','stop_status':'stopped','stop_ack_received':True,
            'stop_closed_received':True,'native_cleanup_status':'cleaned','reaped':True,
            'pending_operation':False,'live_processes':0,'pending_close_tasks':0}
        self.assertTrue(owned_cleanup_verified(valid))
        changes={'status':'unknown','stop_status':'unknown','stop_ack_received':False,
            'stop_closed_received':False,'native_cleanup_status':'unknown','reaped':False,
            'pending_operation':True,'live_processes':1,'pending_close_tasks':1}
        for name,value in changes.items():
            with self.subTest(field=name):self.assertFalse(owned_cleanup_verified({**valid,name:value}))
        self.assertFalse(owned_cleanup_verified({**valid,'live_processes':False}))
        self.assertFalse(owned_cleanup_verified({'status':'cleaned','stop_status':'stopped'}))
