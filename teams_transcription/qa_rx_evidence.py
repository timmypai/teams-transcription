"""Scalar, same-answer RX proof for the explicitly authorized synthetic QA probe."""
import asyncio
import math
import time

from .b_audio import PCM_ACTIVITY_RMS_THRESHOLD
from .b_native_output import NativeTrackOutput


def receiver_snapshot(value):
    """Project only typed counters; no provider content, track IDs or raw errors."""
    if not isinstance(value, dict) or value.get('connected') is not True:
        raise ValueError('qa_rx_snapshot_invalid')
    result={'connected':True}
    for group, fields in {
            'inbound':('ssrc','packets_received','bytes_received'),
            'source':('packets','frames','duration','energy'),
            'render':('frames','duration','energy')}.items():
        raw=value.get(group)
        if not isinstance(raw,dict):raise ValueError('qa_rx_snapshot_invalid')
        result[group]={}
        for field in fields:
            number=raw.get(field)
            if field=='ssrc' and number is None:
                result[group][field]=None
                continue
            integer=field not in {'duration','energy'}
            if (type(number) not in (int,float) or integer and type(number) is not int
                    or not 0 <= number <= (2**32-1 if field=='ssrc' else 10**15) or not math.isfinite(number)):
                raise ValueError('qa_rx_snapshot_invalid')
            result[group][field]=number
    if result['inbound']['ssrc'] is None and (any(result['inbound'][name]!=0
            for name in ('packets_received','bytes_received')) or any(result['source'].values())):
        raise ValueError('qa_rx_snapshot_invalid')
    return result


def answer_rx(window):
    result={'verified':False,'reason':'incomplete_answer_window','inbound_delta':None,
        'source_delta':None,'render_delta':None,'source_rms_pcm16':None}
    if not window or window.get('complete') is not True:return result
    try:
        before,after=(receiver_snapshot(window[name]) for name in ('before','after'))
    except (KeyError,TypeError,ValueError):
        return {**result,'reason':'receiver_snapshot_unavailable'}
    if before['inbound']['ssrc'] is not None and before['inbound']['ssrc'] != after['inbound']['ssrc']:
        return {**result,'reason':'receiver_changed'}
    for group in ('inbound','source','render'):
        delta={name:after[group][name]-number for name,number in before[group].items() if name!='ssrc'}
        if any(number<0 for number in delta.values()):return {**result,'reason':'receiver_counter_reset'}
        result[group+'_delta']=delta
    inbound,source=result['inbound_delta'],result['source_delta']
    if not all(inbound[name]>0 for name in ('packets_received','bytes_received')):
        return {**result,'reason':'no_inbound_audio_rtp'}
    if not all(source[name]>0 for name in ('packets','frames','duration','energy')):
        return {**result,'reason':'no_decoded_source_audio'}
    rms=math.sqrt(source['energy']/source['duration'])*32768
    if not math.isfinite(rms):return {**result,'reason':'receiver_snapshot_unavailable'}
    result['source_rms_pcm16']=rms
    if rms<PCM_ACTIVITY_RMS_THRESHOLD:
        return {**result,'reason':'decoded_source_below_activity_threshold'}
    return {**result,'verified':True,'reason':None}


def owned_cleanup_verified(value):
    return (isinstance(value,dict) and value.get('status')=='cleaned'
        and value.get('stop_status')=='stopped' and value.get('stop_ack_received') is True
        and value.get('stop_closed_received') is True and value.get('native_cleanup_status')=='cleaned'
        and value.get('reaped') is True and value.get('pending_operation') is False
        and all(type(value.get(name)) is int and value[name]==0
            for name in ('live_processes','pending_close_tasks')))


class ProbeTrackOutput(NativeTrackOutput):
    """Keep the production output graph; bound two snapshots to an answer lease."""
    def __init__(self,meeting_id,sink,round_scope):
        super().__init__(meeting_id,sink)
        self.round_scope=round_scope
        self._rx_window=None

    async def _snapshot(self):
        try:
            async with asyncio.timeout(.5):
                return receiver_snapshot(await self.sink.rx_snapshot())
        except Exception:
            return None  # Unknown samples fail admission; never echo exception data.

    async def begin_speech(self,meeting_id):
        await super().begin_speech(meeting_id)
        row=self.round_scope()
        if row is not None:
            window=row.get('audio_rx')
            if window is None:
                window=row['audio_rx']={'before':await self._snapshot(),'after':None,
                    'complete':False,'started_monotonic':time.monotonic(),'ended_monotonic':None}
            window['complete']=False
            self._rx_window=window

    async def end_speech(self,*,quiet=False):
        window=self._rx_window if self.active else None
        self._rx_window=None
        sample=await self._snapshot() if window is not None else None
        result=await super().end_speech(quiet=quiet)
        if window is not None:
            window.update(after=sample,complete=quiet is True and result.get('status')=='quiet',
                ended_monotonic=time.monotonic())
        return result
