"""Revocable native-track output; only scalar receiver observations leave Chromium."""
import asyncio
import contextlib
import math
import time

from .b_audio import PCM_ACTIVITY_RMS_THRESHOLD, PulseTXSink
from .diagnostics import response_start_failure, response_start_receipt


HISTORY_COUNTER_DOMAIN = 'target_decoded_input_v2'
LEGACY_COUNTER_DOMAIN = 'legacy_render_cumulative_v1'
_HISTORY_FIELDS = ('kind','clock_frame','coverage_epoch','coverage_valid','fresh',
    'window_frames','peak_rms','active','latest_active_block_min_frame',
    'quiet_from_frame','quiet_through_frame','quiet_seconds','graph_checkpoint_observed')


def render_history_evidence(value, previous=None):
    """Validate fixed native clock evidence; never infer it from decoded input."""
    def invalid():raise RuntimeError('Invalid native render history observation')
    if not isinstance(value,dict) or any(key not in value for key in _HISTORY_FIELDS):invalid()
    result={key:value[key] for key in _HISTORY_FIELDS}
    if result['kind']!='native-history-v1' or type(result['window_frames']) is not int or result['window_frames']!=32768:invalid()
    for key in ('clock_frame','coverage_epoch','quiet_through_frame'):
        if type(result[key]) is not int or not 0<=result[key]<=2**53-1:invalid()
    for key in ('coverage_valid','fresh','active','graph_checkpoint_observed'):
        if type(result[key]) is not bool:invalid()
    for key in ('peak_rms','quiet_seconds'):
        try:
            valid=type(result[key]) in (int,float) and math.isfinite(result[key]) and result[key]>=0
        except OverflowError:valid=False
        if not valid:invalid()
    clock=result['clock_frame'];start=result['quiet_from_frame'];through=result['quiet_through_frame']
    latest=result['latest_active_block_min_frame']
    if type(latest) is not int or not -32768-512<=latest<=clock:invalid()
    if result['active']!=(result['peak_rms']>=8/32768):invalid()
    if result['active'] and latest==-1 or not result['active'] and latest!=-1:invalid()
    if through>clock or start is not None and (type(start) is not int or not 0<=start<=clock+512):invalid()
    if (result['active'] or not result['coverage_valid']) and start is not None:invalid()
    available=0. if start is None else max(0,through-start)/48000
    if result['quiet_seconds']>available+1e-12:invalid()
    if previous is not None:
        if clock<previous['clock_frame'] or result['coverage_epoch']<previous['coverage_epoch']:
            raise RuntimeError('Native render history observation reset')
        if clock==previous['clock_frame'] and (result['fresh'] or result['coverage_epoch']!=previous['coverage_epoch']):invalid()
    return result


class NativeTrackOutput:
    """One persistent relay, with per-question permission and local quiet evidence."""

    def __init__(self, meeting_id, sink, *, clock=time.monotonic):
        self.meeting_id, self.sink, self.clock = meeting_id, sink, clock
        self.enabled = self.active = self.closed = False
        self.counter_domain = LEGACY_COUNTER_DOMAIN
        self.render_evidence = None
        self._history_activity_seen = False
        self._render_scope_floor = None
        self._render_quiet_generation = 0
        self.frames = self.activity_frames = 0
        self._source_activity_frames = 0
        self.received_duration = self.received_energy = self.quiet_received_seconds = 0.0
        self.last_active = 0.0
        self.connected = False
        self.last_delivery = None
        self._previous = None
        self._lock = asyncio.Lock()
        self._startup_step = 'output_scope'
        self.startup_error = None

    @property
    def startup_step(self):
        return getattr(self.sink, 'startup_step', None) if self._startup_step == 'relay_attach' else self._startup_step

    @property
    def quiet_received_seconds(self):
        return min(self._source_quiet_seconds, self._render_quiet_seconds)

    @quiet_received_seconds.setter
    def quiet_received_seconds(self, seconds):
        # A new provider turn invalidates both tails through the existing API.
        self._source_quiet_seconds = self._render_quiet_seconds = seconds
        self._render_quiet_floor = None
        self._render_pending_baseline = True
        self._render_quiet_generation += 1
        if self.counter_domain == HISTORY_COUNTER_DOMAIN:self._render_quiet_seconds = 0.

    @property
    def render_activity_observed(self):
        return self._history_activity_seen if self.counter_domain == HISTORY_COUNTER_DOMAIN else self.activity_frames > 0

    async def attach_live(self, page):
        self._startup_step = 'output_scope'
        try:
            if self.closed or self.active:
                raise RuntimeError("Native output unavailable")
            self._startup_step = 'relay_attach'
            await self.sink.attach_live(page)
            self._startup_step = 'sender_install'
            await self.sink.start()
            self._startup_step = 'initial_mute'
            await self.sink.set_muted(True)
        except BaseException as exc:
            if self.startup_error is None:
                relay_error = getattr(self.sink, 'startup_error', None) if self._startup_step in {'relay_attach','sender_install'} else None
                self.startup_error = response_start_receipt(relay_error) if isinstance(relay_error, dict) else \
                    response_start_failure(exc, 'native_output_attach', output_step=self.startup_step)
            raise

    def start_output(self):
        if self.closed or self.active:
            raise RuntimeError("Output is closed or speech is active")
        self.enabled = True

    async def _sample(self):
        sample = await self.sink.observe()
        if not isinstance(sample, dict) or sample.get('connected') is not True:
            raise RuntimeError("Native relay disconnected")
        domain=sample.get('counter_domain',LEGACY_COUNTER_DOMAIN)
        if domain not in (LEGACY_COUNTER_DOMAIN,HISTORY_COUNTER_DOMAIN):
            raise RuntimeError('Invalid native relay counter domain')
        if self._previous is not None and domain!=self._previous.get('counter_domain',LEGACY_COUNTER_DOMAIN):
            raise RuntimeError('Native relay counter domain changed')
        if domain == HISTORY_COUNTER_DOMAIN:
            previous=self._previous.get('render_evidence') if self._previous is not None else None
            sample={**sample,'render_evidence':render_history_evidence(sample.get('render_evidence'),previous)}
        elif 'render_evidence' in sample:
            raise RuntimeError('Invalid native relay counter domain')
        for key in ('frames', 'packets', 'energy', 'duration',
                    'source_frames', 'source_packets', 'source_energy', 'source_duration'):
            value = sample.get(key)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value < 0):
                raise RuntimeError("Invalid native relay observation")
            if (domain == HISTORY_COUNTER_DOMAIN and key in ('frames','packets','source_frames','source_packets')
                    and (type(value) is not int or value>2**53-1)):
                raise RuntimeError("Invalid native relay observation")
            if self._previous is not None and value < self._previous[key]:
                raise RuntimeError("Native relay observation reset")
        self.counter_domain=domain
        self.render_evidence=dict(sample['render_evidence']) if domain == HISTORY_COUNTER_DOMAIN else None
        if domain == HISTORY_COUNTER_DOMAIN:self.activity_frames=None
        self.connected = True
        return sample

    async def begin_speech(self, meeting_id):
        if meeting_id != self.meeting_id:
            raise ValueError("Invalid output scope")
        async with self._lock:
            if self.closed or self.active:
                raise RuntimeError("Output unavailable or busy")
            self.frames = self.activity_frames = 0
            self._history_activity_seen = False
            self._source_activity_frames = 0
            self.received_duration = self.received_energy = self.quiet_received_seconds = 0.0
            self.last_active = 0.0
            self.last_delivery = None
            try:
                self._previous = await self._sample()
                if self.counter_domain == HISTORY_COUNTER_DOMAIN:
                    self._render_scope_floor=self._previous['render_evidence']['clock_frame']
                    self._render_quiet_floor=self._render_scope_floor
                    self._render_pending_baseline=False
                self.active = True
                if self.enabled:
                    unmuted=await self.sink.set_muted(False)
                    if self.counter_domain == HISTORY_COUNTER_DOMAIN:
                        floor=unmuted.get('clock_frame') if isinstance(unmuted,dict) else None
                        if (not isinstance(unmuted,dict) or unmuted.get('counter_domain')!=HISTORY_COUNTER_DOMAIN
                                or type(floor) is not int or not self._render_scope_floor<=floor<=2**53-1):
                            raise RuntimeError('Invalid native unmute clock')
                        # Taken in the existing target enable action, not the
                        # earlier sample or the eventual host ACK time.
                        self._render_scope_floor=floor
                        self._render_quiet_floor=max(floor,self._render_quiet_floor or 0)
            except BaseException as exc:
                self.active = False
                if not isinstance(exc, asyncio.CancelledError):
                    self.enabled = False
                with contextlib.suppress(Exception):
                    await PulseTXSink._finish_cleanup(self.sink.pause())
                raise

    async def observe(self):
        async with self._lock:
            if not self.active or self.closed:
                raise RuntimeError("Native output lease inactive")
            try:
                quiet_generation = self._render_quiet_generation
                pending_history_baseline = self._render_pending_baseline
                sample = await self._sample()
                previous, self._previous = self._previous, sample
                frames = sample['frames'] - previous['frames']
                duration = sample['duration'] - previous['duration']
                energy = sample['energy'] - previous['energy']
                source_frames = sample['source_frames'] - previous['source_frames']
                source_duration = sample['source_duration'] - previous['source_duration']
                source_energy = sample['source_energy'] - previous['source_energy']
                source_packets = sample['source_packets'] - previous['source_packets']
                self.frames += frames
                self.received_duration += duration
                self.received_energy += energy
                # Only real decoded input extends source quiet; native render
                # underflow zeros are not evidence that the provider went quiet.
                if source_packets > 0 and source_frames > 0 and source_duration > 0:
                    rms = math.sqrt(source_energy / source_duration) * 32768
                    if rms >= PCM_ACTIVITY_RMS_THRESHOLD:
                        self._source_activity_frames += source_frames
                        self._source_quiet_seconds = 0.0
                    elif self._source_activity_frames > 0:
                        self._source_quiet_seconds += source_duration
                # Playout can lag input. Its voice resets only its own tail, and
                # its quiet can finish after real source packets stop arriving.
                if self.counter_domain == HISTORY_COUNTER_DOMAIN:
                    self._observe_render_history(sample['render_evidence'],previous['render_evidence'],quiet_generation)
                    if pending_history_baseline or quiet_generation != self._render_quiet_generation:
                        # The first post-reset source delta still starts at a
                        # pre-notification counter. Freeze its new endpoint;
                        # only subsequent real packets can establish quiet.
                        self._source_quiet_seconds = 0.
                elif frames > 0 and duration > 0:
                    rms = math.sqrt(energy / duration) * 32768
                    if rms >= PCM_ACTIVITY_RMS_THRESHOLD:
                        self.activity_frames += frames
                        self.last_active = self.clock()
                        self._render_quiet_seconds = 0.0
                    elif self.activity_frames > 0:
                        self._render_quiet_seconds += duration
                current_render=(self.counter_domain != HISTORY_COUNTER_DOMAIN or
                    sample['render_evidence']['fresh'] and sample['render_evidence']['coverage_valid']
                    and sample['render_evidence']['graph_checkpoint_observed'])
                return (self._source_activity_frames > 0 and self.render_activity_observed and current_render
                        and self.quiet_received_seconds >= .8)
            except BaseException as exc:
                self.active = False
                if not isinstance(exc, asyncio.CancelledError):
                    self.connected = self.enabled = False
                with contextlib.suppress(Exception):
                    await PulseTXSink._finish_cleanup(self.sink.pause())
                raise

    def _observe_render_history(self, value, previous, quiet_generation):
        clock=value['clock_frame']
        if (value['active'] and value['fresh'] and value['graph_checkpoint_observed']
                and value['latest_active_block_min_frame']>=self._render_scope_floor):
            self._history_activity_seen=True;self.last_active=self.clock()
        if quiet_generation != self._render_quiet_generation:
            # A turn arrived while the browser observation was in flight. Its
            # capture may predate notification: retain lease activity, but do
            # not consume the pending quiet reset or restore either old tail.
            self._render_quiet_seconds=0.
            return
        if self._render_pending_baseline:
            # A turn reset discards old quiet, not activity in this still-open
            # lease. Its first poll may already contain the entire short turn.
            # Only begin_speech moves the activity fence before unmuting; a
            # late continuation therefore cannot backfill the muted interval.
            self._render_quiet_floor=clock;self._render_pending_baseline=False
            self._render_quiet_seconds=0.
        changed=value['coverage_epoch']!=previous['coverage_epoch']
        if not value['graph_checkpoint_observed']:
            self._render_quiet_seconds=0.;self._render_quiet_floor=clock
        elif not value['coverage_valid']:
            self._render_quiet_seconds=0.
            if not value['active']:self._render_quiet_floor=clock
        elif changed and not value['active']:
            self._render_quiet_floor=clock;self._render_quiet_seconds=0.
        if value['active']:
            self._render_quiet_seconds=0.
            self._render_quiet_floor=clock
        elif (value['fresh'] and value['coverage_valid'] and value['graph_checkpoint_observed']
                and self._history_activity_seen and value['quiet_from_frame'] is not None):
            start=max(value['quiet_from_frame'],self._render_scope_floor,self._render_quiet_floor)
            self._render_quiet_seconds=min(value['quiet_seconds'],max(0,value['quiet_through_frame']-start)/48000)
        elif value['quiet_from_frame'] is None:
            self._render_quiet_seconds=0.

    async def end_speech(self, *, quiet=False):
        async with self._lock:
            was_active, self.active = self.active, False
            if was_active:
                try:
                    await PulseTXSink._finish_cleanup(self.sink.pause())
                except BaseException as exc:
                    if not isinstance(exc, asyncio.CancelledError):
                        self.enabled = False
                    raise
                self.last_delivery = {
                    'status': 'quiet' if quiet else 'stopped',
                    'truncated': False, 'drained': False, 'reason': None}
            return dict(self.last_delivery or {
                'status': 'idle', 'truncated': False, 'drained': False, 'reason': None})

    async def stop_output(self):
        self.enabled = False
        return await self.end_speech()

    async def close(self):
        self.closed = True
        try:
            await self.stop_output()
        finally:
            await PulseTXSink._finish_cleanup(self.sink.stop())
