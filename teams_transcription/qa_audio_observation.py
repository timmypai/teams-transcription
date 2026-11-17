"""Private, bounded synthetic PCM evidence. No audio device, model or meeting I/O."""
import base64
import hashlib
import json
import math
import os
import struct
import time
from pathlib import Path

AUDIO_BYTES = 64*1024*1024
METADATA_BYTES = 8*1024*1024
WINDOW_MS = 120000
BATCH_PCM_BYTES = 512*1024

# Fixed fields only; encoded payloads, IDs, URLs and text have no metadata slot.
EVENT_FIELDS = {
    'packet':('packet_number','read_started_ms','dequeue_ms','receive_ms','rtp_timestamp',
        'encoded_bytes','expected_frames','decode_started_ms','decode_done_ms',
        'write_ready_started_ms','write_ready_done_ms','write_done_ms','observer_ms'),
    'render_bin':('audio_frame','audio_ms','frames','channels','zero_frames','missing_frames',
        'energy','observer_ms','received_ms'),
    'stats':('browser_ms','packets_received','packets_lost','jitter_seconds'),
    'mute':('host_elapsed_ms',),
    'observer':('browser_ms','copy_ms','export_ms','attach_ms','poll_wall_ms','host_read_begin_ms','host_read_end_ms','render_captured_frames','render_total_frames',
        'render_pending_frames','dropped_chunks','overhead_clock_resolution_ms'),
}


def number(value, *, negative=False):
    return value if type(value) in (int,float) and math.isfinite(value) and \
        (-10**15 if negative else 0) <= value <= 10**15 else None


class AudioObservation:
    def __init__(self,directory,*,meeting_id,authorized_synthetic,audio_byte_cap=AUDIO_BYTES):
        if meeting_id!='synthetic-qa' or authorized_synthetic is not True:
            raise ValueError('synthetic_scope_required')
        if type(audio_byte_cap) is not int or not 8 <= audio_byte_cap <= AUDIO_BYTES:
            raise ValueError('observer_cap_invalid')
        self.directory=Path(directory)
        self.directory.mkdir(mode=0o700)  # Exclusive; no existing recordings overwritten.
        self.files={}
        try:
            for name in ('source.f32le','render.f32le','index.jsonl','events.jsonl'):
                fd=os.open(self.directory/name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
                self.files[name]=os.fdopen(fd,'wb')
        except BaseException:
            for stream in self.files.values():stream.close()
            raise
        self.cap=audio_byte_cap
        self.audio_bytes={'source':0,'render':0}
        self.frames={'source':0,'render':0}
        self.channels={'source':None,'render':None}
        self.metadata_bytes=0
        self.truncated=False
        self.reasons=set()
        self.closed=False
        self.host_ingest_ms=0.
        self.last_source_ms=None
        self.last_packet=None
        self.first_loss=self.last_loss=None
        self.jitter_max=None
        self.findings=dict.fromkeys(('decoded_silent_frames','render_exact_zero_frames',
            'render_zero_after_source_stall_candidate_frames','rtp_timestamp_gap_frames',
            'rtp_nonforward_events','render_missing_input_frames'),0)

    def limit(self,reason):
        self.truncated=True
        self.reasons.add(reason)

    def _meta(self,name,row):
        raw=(json.dumps(row,separators=(',',':'),allow_nan=False)+'\n').encode()
        if self.metadata_bytes+len(raw)>METADATA_BYTES-65536:
            self.limit('metadata_byte_cap');return False
        self.files[name].write(raw);self.metadata_bytes+=len(raw)
        return True

    def event(self,row):
        if not isinstance(row,dict) or row.get('kind') not in EVENT_FIELDS:return
        kind=row['kind']
        safe={'kind':kind,**{key:number(row.get(key),negative=key=='packets_lost')
            for key in EVENT_FIELDS[kind]}}
        if not self._meta('events.jsonl',safe):return
        if kind=='stats':
            loss=safe['packets_lost']
            if loss is not None:
                if self.first_loss is None:self.first_loss=loss
                self.last_loss=loss
            jitter=safe['jitter_seconds']
            if jitter is not None:self.jitter_max=max(self.jitter_max or 0,jitter)
        elif kind=='render_bin':
            self.findings['render_missing_input_frames']+=safe['missing_frames'] or 0
        elif kind=='packet':
            rtp,expected=safe['rtp_timestamp'],safe['expected_frames']
            if type(rtp) is not int or not 0<=rtp<=0xffffffff:return
            if self.last_packet is not None:
                before,count=self.last_packet
                delta=(rtp-before)&0xffffffff
                if delta==0 or delta>=0x80000000:
                    self.findings['rtp_nonforward_events']+=1;return
                elif count is not None:self.findings['rtp_timestamp_gap_frames']+=max(0,delta-count)
            self.last_packet=(rtp,expected)

    def _pcm(self,row):
        if not isinstance(row,dict) or row.get('kind') not in self.audio_bytes:
            self.limit('invalid_pcm_record');return
        kind=row['kind'];channels=row.get('channels');frames=row.get('frames')
        data=row.get('data');at=number(row.get('observed_ms'));media=number(row.get('media_ms'))
        if (type(channels) is not int or channels not in (1,2) or type(frames) is not int
                or not 0<frames<=8192 or at is None or media is None
                or not isinstance(data,str) or len(data)>90000):
            self.limit('invalid_pcm_record');return
        try:raw=base64.b64decode(data,validate=True)
        except (ValueError,TypeError):self.limit('invalid_pcm_record');return
        if len(raw)!=frames*channels*4 or len(raw)>65536:
            self.limit('invalid_pcm_record');return
        values=struct.unpack('<'+'f'*(frames*channels),raw)
        if any(not math.isfinite(v) for v in values):self.limit('invalid_pcm_record');return
        if (self.audio_bytes[kind]+len(raw)>self.cap//2 or
                sum(self.audio_bytes.values())+len(raw)>self.cap):
            self.limit('audio_byte_cap');return
        if self.frames[kind]+frames>48000*120:
            self.limit('audio_frame_cap');return
        if self.channels[kind] not in (None,channels):self.limit('channel_change');return
        index={'kind':kind,'offset_bytes':self.audio_bytes[kind],'frames':frames,'channels':channels,
            'sample_rate':48000,'format':'f32le_interleaved','media_ms':media,'observed_ms':at,
            'packet_number':number(row.get('packet_number'))}
        if not self._meta('index.jsonl',index):return
        self.files[kind+'.f32le'].write(raw)
        self.channels[kind]=channels;self.audio_bytes[kind]+=len(raw);self.frames[kind]+=frames
        rms=math.sqrt(sum(v*v for v in values)/len(values))
        if kind=='source':
            self.last_source_ms=at
            if rms<.002:self.findings['decoded_silent_frames']+=frames
        elif all(v==0 for v in values):
            self.findings['render_exact_zero_frames']+=frames
            if self.last_source_ms is None or at-self.last_source_ms>20:
                self.findings['render_zero_after_source_stall_candidate_frames']+=frames

    def ingest(self,batch):
        if self.closed:raise RuntimeError('observer_closed')
        started=time.perf_counter()
        try:
            if not isinstance(batch,dict):self.limit('invalid_batch');return
            elapsed=number(batch.get('elapsed_ms'))
            if elapsed is None:self.limit('invalid_batch');return
            if elapsed>WINDOW_MS:self.limit('window_cap');return
            pcm=batch.get('pcm');events=batch.get('events')
            if not isinstance(pcm,list) or len(pcm)>256 or not isinstance(events,list) or len(events)>512:
                self.limit('batch_cap');return
            if sum(len(x.get('data','')) for x in pcm if isinstance(x,dict)
                    and isinstance(x.get('data'),str))>BATCH_PCM_BYTES*4//3+1024:
                self.limit('batch_byte_cap');return
            # Per-stream ordering preserved. Cross-thread/browser times are not exact alignment.
            for row in pcm:self._pcm(row)
            for row in events:self.event(row)
            status=batch.get('status',{})
            if isinstance(status,dict):
                for name in ('audio_cap','event_cap','window_cap','observer_error','render_tail_unflushed'):
                    if status.get(name) is True:self.limit(name)
        finally:self.host_ingest_ms+=(time.perf_counter()-started)*1000

    def close(self,*,poll_wall_ms=None,polls=None):
        if self.closed:raise RuntimeError('observer_closed')
        self.closed=True
        started=time.perf_counter()
        first_error=None
        for stream in self.files.values():
            try:stream.close()
            except OSError as error:first_error=first_error or error
        if first_error is not None:raise first_error
        files={}
        for name in self.files:
            h=hashlib.sha256()
            with (self.directory/name).open('rb') as stream:
                for chunk in iter(lambda:stream.read(1024*1024),b''):h.update(chunk)
            files[name]={'bytes':(self.directory/name).stat().st_size,'sha256':h.hexdigest()}
        report={'schema_version':1,'scope':'fixed shared synthetic QA first no-PLC receiver only',
            'analysis_scope':'all captured startup/idle/answer phases; not word segmentation or audibility proof',
            'clock':'source callback AudioContext.currentTime and render AudioWorklet currentFrame/sampleRate; common context, not sample-exact playout alignment',
            'audio_byte_cap':self.cap,'metadata_byte_cap':METADATA_BYTES,'window_seconds':120,
            'audio_bytes':self.audio_bytes,'frames':self.frames,'channels':self.channels,
            'truncated':self.truncated,'limit_reasons':sorted(self.reasons),'findings':self.findings,
            'reported_loss_delta':None if self.first_loss is None else self.last_loss-self.first_loss,
            'reported_jitter_max_seconds':self.jitter_max,'underflow_confirmed_ms':None,'decoded_silence_rms_threshold':.002,
            'poll_wall_ms':number(poll_wall_ms),'polls':number(polls),
            'host_ingest_ms':self.host_ingest_ms,'finalize_before_manifest_ms':(time.perf_counter()-started)*1000,
            'observer_noninterference_verified':False,'audio_quality_verified':False,
            'limits':['Decoded silence is not proof of a natural speech pause.',
                'RTP timestamp gaps may be DTX, loss or reordering; no sequence number proof.',
                'Render zeros/source stall are underflow candidates, not confirmed lost words.',
                'No sample-exact source/render alignment or Teams/second-relay audibility.',
                'Observer copies, messages, polling and I/O can alter timing; worklet timing may be coarse.',
                'Manifest-write overhead is outside the reported finalization measurement.'],
            'files':files}
        raw=(json.dumps(report,indent=2,allow_nan=False)+'\n').encode()
        if len(raw)>65536:raise RuntimeError('observer_manifest_cap')
        fd=os.open(self.directory/'manifest.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'wb') as stream:stream.write(raw)
        return report
