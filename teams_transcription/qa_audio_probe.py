"""Explicit passive audio observation around the fixed c4090ed shared synthetic QA."""
import argparse
import asyncio
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import patch

from . import b_live,qa_probe
from .b_no_plc import NO_PLC_RECEIVER_JS
from .qa_audio_observation import AudioObservation,WINDOW_MS
from .qa_audio_tap import instrument_receiver

BASE_HASHES={
 'qa_probe.py':'c718c7e08600b383a9916be3684393dcd531fb86007be8811b60a2431be39197',
 'b_no_plc.py':'5fb5e4322c27d36f74a31fe8cbc740fd232088d58db5e74ba417fff224525384',
 'b_live.py':'1415852671c4377860511f93df284c12750f88165cbcef8021349c4090304547',
 'b_media_profile.py':'1dfac1d3515b40874fe52408b1540660a2c8ae661620e095feb566f207257882'}

READ_JS=r"""async finish=>{
 const o=window.__qaAudioObservation,live=window.__live;
 if(!o||!live?.noPlc)return null;
 if(finish)await o.finish();
 const batch=o.take(),receiver=live.noPlc.receiver;
 if(!live.pc.getReceivers().includes(receiver))return null;
 const report=await receiver.getStats();
 const rows=[...report.values()].filter(r=>r.type==='inbound-rtp'&&(r.kind==='audio'||r.mediaType==='audio')&&r.isRemote!==true);
 const r=rows.length===1?rows[0]:{};
 batch.events.push({kind:'stats',browser_ms:performance.now(),packets_received:r.packetsReceived??null,
  packets_lost:r.packetsLost??null,jitter_seconds:r.jitter??null});
 return batch;
}"""


def verify_base():
    for name,digest in BASE_HASHES.items():
        if hashlib.sha256(Path(qa_probe.__file__).with_name(name).read_bytes()).hexdigest()!=digest:
            raise ValueError('observer_requires_reviewed_c4090ed_base')
    if b_live.NO_PLC_PEER_JS.count(NO_PLC_RECEIVER_JS)!=1:
        raise ValueError('pinned_peer_contract_changed')


class PassiveClient:
    def __init__(self,observation):
        self.observation=observation;self.native=None;self.task=None
        self.stop_requested=False;self.failed=False;self.host_events=[]
        self.started=time.monotonic();self.poll_ms=0.;self.polls=0
    def muted(self):
        if len(self.host_events)<64:
            self.host_events.append({'kind':'mute','host_elapsed_ms':(time.monotonic()-self.started)*1000})
        else:self.observation.limit('mute_event_cap')
    def capture(self,native):
        if self.native is not None:raise ValueError('shared_qa_one_native_only')
        self.native=native;self.task=asyncio.create_task(self.poll())
    async def read(self,*,finish=False,timeout=.4):
        peer=getattr(self.native,'peer',None);page=getattr(peer,'page',None)
        if page is None or not getattr(peer,'_document_valid',False):return None
        started=time.perf_counter();host_begin=(time.monotonic()-self.started)*1000
        try:
            async with asyncio.timeout(timeout):batch=await page.evaluate(READ_JS,finish)
            if batch is not None:
                # Save actual observer round-trip, including JavaScript serialization/IPC.
                batch['events'].append({'kind':'observer','poll_wall_ms':(time.perf_counter()-started)*1000,
                    'host_read_begin_ms':host_begin,'host_read_end_ms':(time.monotonic()-self.started)*1000,
                    'browser_ms':batch.get('browser_ms')})
                batch['events'].extend(self.host_events);self.host_events=[]
                self.observation.ingest(batch)
            return batch
        finally:self.poll_ms+=(time.perf_counter()-started)*1000;self.polls+=1
    async def poll(self):
        try:
            while not self.stop_requested:
                if (time.monotonic()-self.started)*1000>WINDOW_MS:
                    self.observation.limit('host_window_cap');break
                await self.read()
                if not self.stop_requested:await asyncio.sleep(.25)
        except asyncio.CancelledError:raise
        except Exception:
            self.failed=True;self.observation.limit('observer_read_failed')
            # No retry after uncertain JavaScript completion. Do not stop Native QA.
    async def finish(self,*,deadline):
        self.stop_requested=True
        remaining=max(0,min(.75,deadline-asyncio.get_running_loop().time()))
        finish_by=asyncio.get_running_loop().time()+remaining
        if self.task is not None and not self.task.done():
            done,_=await asyncio.wait({self.task},timeout=min(.45,remaining))
            if not done:
                self.failed=True;self.observation.limit('observer_task_unfinished');self.task.cancel()
        if not self.failed:
            for attempt in range(4):  # At most2MiB final drain, inside the original cleanup window.
                left=finish_by-asyncio.get_running_loop().time()
                if left<=0:self.observation.limit('observer_final_drain_timeout');break
                try:
                    batch=await self.read(finish=attempt==0,timeout=left)
                    if batch is None:self.observation.limit('observer_unavailable');break
                    if batch.get('pending_chunks')==0 and batch.get('pending_events')==0:break
                except Exception:
                    self.failed=True;self.observation.limit('observer_final_drain_failed');break
            else:self.observation.limit('observer_final_drain_cap')
        for event in self.host_events:self.observation.event(event)
        self.host_events=[]


async def run_observed_probe(args):
    if args.authorize_live is not True:raise ValueError('explicit_live_authorization_required')
    if getattr(args,'qa_start_stage',None)!='question':raise ValueError('fixed_question_stage_required')
    if getattr(args,'peer_media_profile','legacy')!='legacy':raise ValueError('fixed_legacy_profile_required')
    if getattr(args,'private_result',None) is None:raise ValueError('private_result_path_required')
    if Path(args.private_result).resolve().is_relative_to(Path(args.audio_observer_dir).resolve()):
        raise ValueError('separate_observer_directory_required')
    verify_base()
    observation=AudioObservation(args.audio_observer_dir,meeting_id='synthetic-qa',authorized_synthetic=True)
    client=PassiveClient(observation)
    native_type,sink_type,cleanup=qa_probe.NativeLiveResponder,qa_probe.NoPhysicalSink,qa_probe.cleanup_probe
    class ObservedNative(native_type):
        def __init__(self,*values,**options):
            if len(values)<3 or values[2]!='synthetic-qa' or options.get('muted_probe_text_gate') is not True:
                raise ValueError('synthetic_scope_required')
            super().__init__(*values,**options);client.capture(self)
    class ObservedSink(sink_type):
        async def pause(self):
            result=await super().pause();client.muted();return result
    async def observed_cleanup(native,browser,owned,*,tasks,deadline=None,operation_task=None,before_stop=None):
        deadline=deadline if deadline is not None else asyncio.get_running_loop().time()+qa_probe.CLEANUP_SECONDS
        async def finish_observation():
            if before_stop is not None:await before_stop()
            await client.finish(deadline=deadline)
        if client.task is not None and client.task not in tasks:tasks.append(client.task)
        return await cleanup(native,browser,owned,tasks=tasks,deadline=deadline,
            operation_task=operation_task,before_stop=finish_observation)
    script=b_live.NO_PLC_PEER_JS.replace(NO_PLC_RECEIVER_JS,instrument_receiver(NO_PLC_RECEIVER_JS))
    try:
        with patch.object(b_live,'NO_PLC_PEER_JS',script), \
                patch.object(qa_probe,'NativeLiveResponder',ObservedNative), \
                patch.object(qa_probe,'NoPhysicalSink',ObservedSink), \
                patch.object(qa_probe,'cleanup_probe',observed_cleanup):
            result=await qa_probe.run_probe(args)
    finally:
        client.stop_requested=True
        if client.task is not None and not client.task.done():
            client.task.cancel()
            done,_=await asyncio.wait({client.task},timeout=.4)
            if not done:observation.limit('observer_task_unfinished')
        for event in client.host_events:observation.event(event)
        try:report=observation.close(poll_wall_ms=client.poll_ms,polls=client.polls)
        except Exception as error:
            report={'written':False,'audio_quality_verified':False,
                'error_code':'observer_finalize_failed','exception_type':type(error).__name__}
    # Base QA's accepted/content/RX/cleanup result is not rewritten into audio-quality success.
    result['audio_observer']=report
    return result


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--authorize-live',action='store_true')
    p.add_argument('--config',required=True);p.add_argument('--private-result',required=True,type=Path)
    p.add_argument('--audio-observer-dir',required=True,type=Path)
    p.add_argument('--qa-start-stage',choices=('question',),required=True)
    p.add_argument('--peer-media-profile',choices=('legacy',),default='legacy')
    p.add_argument('--chromium');p.add_argument('--ice-servers-file')
    args=p.parse_args(argv)
    try:result=asyncio.run(run_observed_probe(args))
    except Exception as error:
        print(json.dumps({'accepted':False,'audio_observer_error':'observer_probe_failed',
            'exception_type':type(error).__name__}));return 1
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['accepted'] else 1


if __name__=='__main__':raise SystemExit(main())
