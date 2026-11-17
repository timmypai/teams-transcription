"""Owned, local-only WebRTC links from every capture document to one Live peer."""
import asyncio
import time
import uuid


class _CaptureDocument:
    """Host-issued observation generation; never accepted from page payload."""


_PCM_OBSERVER_JS = r"""(ctx,source,state,ready,clockId,send) => {
  const processor=ctx.createScriptProcessor(2048,1,1), silent=ctx.createGain();
  silent.gain.value=0; source.connect(processor);
  processor.connect(silent); silent.connect(ctx.destination);
  processor.onaudioprocess=e => {
    if (!ready()) return;
    const a=e.inputBuffer.getChannelData(0);
    const bytes=new Uint8Array(a.length*2), view=new DataView(bytes.buffer);
    let sum=0;
    for (let i=0;i<a.length;i++) {
      const x=Math.max(-1,Math.min(1,a[i])); sum+=x*x;
      view.setInt16(i*2,x<0?x*32768:x*32767,true);
      if (view.getInt16(i*2,true)!==0) state.nonzero_samples++;
    }
    const startFrame=state.frames; state.frames+=a.length;
    if (state.pending>=16) { state.dropped++; return; }
    state.pending++; state.packets++;
    const capturedEnd=performance.now();
    Promise.resolve(send({data:btoa(String.fromCharCode(...bytes)),sample_rate:ctx.sampleRate,
      start_frame:startFrame,frames:a.length,rms:Math.sqrt(sum/a.length),
      capture_clock_id:clockId(),capture_end_ms:capturedEnd,
      capture_start_ms:capturedEnd-a.length/ctx.sampleRate*1000}))
      .catch(()=>{ state.dropped++; }).finally(()=>state.pending--);
  };
  return () => {
    processor.onaudioprocess=null; source.disconnect(processor);
    processor.disconnect(); silent.disconnect();
  };
}"""

_START_OBSERVER_JS = r"""owner => {
  const live=window.__live;
  if (!live || live.inputMode!=='native_media' || live.nativeOwner!==owner ||
      live.ctx?.state==='closed' || live.ctx?.sampleRate!==24000) return false;
  if (live.nativeObservation) return live.nativeObservation.owner===owner &&
    !live.nativeObservation.closed;
  const state={owner,closed:false,pending:0,dropped:0,packets:0,frames:0,nonzero_samples:0};
  const source=live.ctx.createMediaStreamSource(live.destination.stream);
  const close=CREATE_PCM_OBSERVER(live.ctx,source,state,
    ()=>window.__live===live && live.nativeOwner===owner && !state.closed &&
      live.ctx.state==='running',()=>owner,packet=>window.nativeMeetingObservation(packet));
  state.close=()=>{ if (!state.closed) { state.closed=true; close(); source.disconnect(); } };
  live.nativeObservation=state;
  return true;
}""".replace('CREATE_PCM_OBSERVER', '(' + _PCM_OBSERVER_JS + ')')

_CLOSE_OBSERVER_JS = r"""owner => {
  const live=window.__live;
  if (!live || live.nativeOwner!==owner) return false;
  const state=live.nativeObservation;
  if (!state) return true;
  if (state.owner!==owner) return false;
  state.close();
  return state.closed;
}"""

_OBSERVER_STATS_JS = r"""owner => {
  const live=window.__live, state=live?.nativeObservation;
  if (!live || live.nativeOwner!==owner || !state || state.owner!==owner) return null;
  return {packets:state.packets,frames:state.frames,nonzero_samples:state.nonzero_samples,
    pending:state.pending,dropped:state.dropped,context_state:live.ctx.state,
    observation_ready:!state.closed && live.ctx.state==='running'};
}"""


_RECEIVER_JS = r"""async ({key,sdp}) => {
  const live=window.__live;
  if (!live || live.inputMode!=='native_media' || !live.ctx || live.ctx.state==='closed')
    throw new Error('native_media_target_unavailable');
  const relays=live.nativeRelays ||= new Map();
  if (relays.has(key)) throw new Error('native_media_duplicate_relay');
  const pc=new RTCPeerConnection({iceServers:[]});
  const relay={pc,nodes:new Set(),tracks:new Set(),players:new Set(),failed:false,closed:false};
  relays.set(key,relay);
  const fail=() => {
    if (relay.closed) return;
    relay.failed=true;
    Promise.resolve(window.nativeMeetingMediaFailure({key})).catch(()=>{});
  };
  const contextChanged=() => { if (live.ctx.state==='closed') fail(); };
  live.ctx.addEventListener('statechange',contextChanged);
  pc.addEventListener('connectionstatechange',()=> {
    if (['failed','closed'].includes(pc.connectionState)) fail();
  });
  const close=() => {
    if (relay.closed) return;
    relay.closed=true;
    live.ctx.removeEventListener('statechange',contextChanged);
    pc.close();
    for (const source of relay.nodes) source.disconnect();
    for (const player of relay.players) { player.pause(); player.srcObject=null; player.remove(); }
    for (const track of relay.tracks) track.stop();
    relay.nodes.clear(); relay.players.clear(); relay.tracks.clear();
    relays.delete(key);
  };
  relay.close=close;
  pc.addEventListener('track',({track}) => {
    if (relay.closed || window.__live!==live) { track.stop(); return; }
    if (track.kind!=='audio' || relay.tracks.has(track)) return;
    relay.tracks.add(track);
    try {
      const stream=new MediaStream([track]), player=document.createElement('audio');
      player.muted=true; player.srcObject=stream; relay.players.add(player);
      const source=live.ctx.createMediaStreamSource(stream);
      source.connect(live.destination); relay.nodes.add(source);
      Promise.resolve(player.play()).catch(()=>{ fail(); source.disconnect(); });
      track.addEventListener('ended',()=>{ fail(); source.disconnect(); },{once:true});
    } catch (_) { fail(); close(); }
  });
  try {
    await pc.setRemoteDescription({type:'offer',sdp});
    await pc.setLocalDescription(await pc.createAnswer());
    await new Promise((resolve,reject) => {
      if (pc.iceGatheringState==='complete') return resolve();
      const done=() => {
        if (pc.iceGatheringState==='complete') { clearTimeout(timer); pc.removeEventListener('icegatheringstatechange',done); resolve(); }
      };
      const timer=setTimeout(()=>{ pc.removeEventListener('icegatheringstatechange',done); reject(new Error('native_media_ice_timeout')); },5000);
      pc.addEventListener('icegatheringstatechange',done);
    });
    return pc.localDescription.sdp;
  } catch (error) { close(); throw error; }
}"""

_CLOSE_RECEIVER_JS = r"""key => {
  const live=window.__live;
  if (!live || live.inputMode!=='native_media') return false;
  const relay=live.nativeRelays?.get(key);
  if (relay) relay.close();
  return !live.nativeRelays?.has(key);
}"""

_RECEIVER_STATS_JS = r"""async owner => {
  const relays=[...(window.__live?.nativeRelays?.values() || [])];
  const live=window.__live, observation=live?.nativeObservation;
  let connected=0, tracks=0, packets=0, bytes=0, failed=false, packetsAvailable=true;
  for (const relay of relays) {
    connected+=relay.pc.connectionState==='connected' ? 1 : 0;
    failed ||= relay.failed || ['failed','closed'].includes(relay.pc.connectionState);
    tracks += [...relay.tracks].filter(t=>t.readyState==='live').length;
    try {
      for (const report of (await relay.pc.getStats()).values()) {
        if (report.type==='inbound-rtp' && report.kind==='audio') {
          packets+=report.packetsReceived || 0; bytes+=report.bytesReceived || 0;
        }
      }
    } catch (_) { packetsAvailable=false; }
  }
  return {frame_count:relays.length,connected_frames:connected,receiver_tracks:tracks,
    packets_received:packetsAvailable?packets:null,bytes_received:packetsAvailable?bytes:null,
    observation_ready:live?.nativeOwner===owner && observation?.owner===owner &&
      !observation.closed && live.ctx.state==='running',
    graph_running:window.__live?.ctx?.state==='running',failed};
}"""


class NativeMeetingMedia:
    """Own actual capture documents; empty meeting UI frames are not sources."""
    def __init__(self, meeting):
        self.meeting = meeting
        self.target = None
        self.frames = ()
        self.documents = {}
        self._started_documents = {}
        self._candidates = {}
        self._candidate_generations = {}
        self.links = []
        self.failed = False
        self.failure_code = 'none'
        self.stopped = False
        self.cleanup_status = 'idle'
        self._lock = asyncio.Lock()
        self._cleanup_task = None
        self._tasks = set()
        self._pending_frames = set()
        self._connected_keys = set()
        self._scope_checks = {}
        self._watching = False
        self._source_closed = False
        self._last = None
        self._target_owner = uuid.uuid4().hex
        self._observation_clock = {"id":self._target_owner,
            "document":_CaptureDocument(), "offset":None}
        self._observation_running = False
        self._observation_attempted = False
        self._observation_lock = asyncio.Lock()
        self._observation_close_task = None
        self._observation_counts = dict.fromkeys(('received_packets','delivered_packets',
            'scope_rejected_packets','scope_pending_packets','owner_rejected_packets',
            'policy_rejected_packets','consumer_unavailable_packets',
            'clock_rejected_packets','callback_error_packets'),0)

    def observation_snapshot(self):
        """Host callback decisions only, not browser emission or provider receipt."""
        return dict(self._observation_counts)

    def _observation_count(self, key):
        self._observation_counts[key] = min(2147483647,self._observation_counts[key]+1)

    def _consumer_admitted(self):
        ready = getattr(self.meeting, 'response_capture_ready', None)
        try:
            admitted = ready is None or ready() is True
        except Exception:
            self._observation_count('callback_error_packets')
            admitted = False
        if not admitted:
            self._observation_count('consumer_unavailable_packets')
        return admitted

    async def _observe(self, source, packet):
        self._observation_count('received_packets')
        if not self._consumer_admitted():
            return
        async with self._observation_lock:
            if not self._consumer_admitted():
                return
            if (self.stopped or self.failed or not self._observation_running
                    or not self.documents or not self._connected_keys
                    or not isinstance(packet, dict) or source.get('page') is not self.target
                    or source.get('context') is not self.target.context
                    or source.get('frame') is not self.target.main_frame):
                self._observation_count('scope_rejected_packets')
                return
            if self._scope_checks:
                self._observation_count('scope_pending_packets')
                return
            try:
                owned = await asyncio.wait_for(self.target.evaluate("""owner => {
                    const live=window.__live, observation=live?.nativeObservation;
                    return live?.nativeOwner===owner && observation?.owner===owner &&
                        !observation.closed && live.ctx.state==='running';
                }""", self._target_owner), 3)
            except Exception:
                self._observation_count('callback_error_packets')
                raise
            if not self._consumer_admitted():
                return
            if not owned or self.stopped or self.failed or self._scope_checks:
                self._observation_count('owner_rejected_packets' if not owned else
                    'scope_pending_packets' if self._scope_checks else 'scope_rejected_packets')
                return
            if self.meeting.dual_policy and not await self.meeting.dual_policy.check():
                self._observation_count('policy_rejected_packets')
                return
            if not self._consumer_admitted():
                return
            if self.stopped or self.failed or self._scope_checks:
                self._observation_count('scope_pending_packets' if self._scope_checks else 'scope_rejected_packets')
                return
            mapped = self.meeting._map_audio_packet(packet, self._observation_clock)
            if mapped.get('source_clock') != 'monotonic':
                self._observation_count('clock_rejected_packets')
                raise RuntimeError('native_observation_packet_unavailable')
            try:
                await self.meeting.on_audio(mapped)
            except Exception:
                self._observation_count('callback_error_packets')
                raise
            self._observation_count('delivered_packets')

    async def _start_frame(self, frame, *, deadline):
        token = self.documents.get(frame)
        if token is None:
            return False  # Empty UI documents are not owned media sources.
        if self.stopped or self.failed or frame.is_detached():
            raise RuntimeError('native_media_source_scope_lost')
        if self._started_documents.get(frame) == token:
            return True
        started = await asyncio.wait_for(frame.evaluate("""async token => {
            const state=window.__meeting;
            if (!state || state.capture_clock_id!==token) return false;
            await state.startCapture({observe:false});
            return window.__meeting===state && state.capture_clock_id===token &&
                state.capture_enabled===true;
        }""", token), max(.001, deadline-time.monotonic()))
        if (started is not True or self.stopped or self.failed
                or self.documents.get(frame) != token):
            raise RuntimeError('native_media_source_scope_lost')
        # B clock retries must not re-run an already admitted A source start
        # with an almost-exhausted setup budget.
        self._started_documents[frame] = token
        return True

    async def _start_observation(self, *, deadline):
        """One real 24k clock and PCM timeline for the existing native mix."""
        if self._observation_running or self.stopped or self.failed:
            return self._observation_running
        if not self.meeting.capture_started or not self.documents or not self._connected_keys:
            return False
        import math
        before = time.monotonic()
        try:
            sample = await asyncio.wait_for(self.target.evaluate(
                "() => window.__live && ({id:__live.nativeOwner,now:performance.now()})"),
                max(.001, deadline-before))
            after = time.monotonic()
            if (not isinstance(sample, dict) or sample.get("id") != self._target_owner
                    or type(sample.get("now")) not in {float, int}
                    or not math.isfinite(sample["now"]) or sample["now"] < 0 or after-before > .1):
                return False
            self._observation_clock["offset"] = (before+after)/2-sample["now"]/1000
            self._observation_running = True
            self._observation_attempted = True
            started = await asyncio.wait_for(self.target.evaluate(
                _START_OBSERVER_JS, self._target_owner), max(.001, deadline-time.monotonic()))
            self._observation_running = started is True
        except Exception:
            # B/WAV observation is not A's native source. No fallback, invented
            # clock, permanent retry task, or A source-failure latch is added.
            self._observation_running = False
        return self._observation_running

    async def observation_stats(self):
        if self.target is None or self.target.is_closed():
            return None
        return await asyncio.wait_for(self.target.evaluate(
            _OBSERVER_STATS_JS, self._target_owner), 3)

    async def _close_observation(self):
        self._observation_running = False
        if not self._observation_attempted or self.target.is_closed():
            return True
        if self._observation_close_task is None:
            self._observation_close_task = asyncio.create_task(self._close_observation_owned())
            self._observation_close_task.add_done_callback(
                lambda task: task.exception() if not task.cancelled() else None)
        return await asyncio.shield(self._observation_close_task)

    async def _close_observation_owned(self):
        try:
            return await asyncio.wait_for(self.target.evaluate(
                _CLOSE_OBSERVER_JS, self._target_owner), 3) is True
        except Exception:
            return self.target.is_closed()

    @property
    def capture_frames(self):
        return self.documents.keys() | self._candidates.keys()

    def _spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self._tasks.add(task)
        def finished(task):
            self._tasks.discard(task)
            if not task.cancelled():
                task.exception()
        task.add_done_callback(finished)
        return task

    def record_failure(self, code):
        self.failed = True
        if self.failure_code == 'none':
            self.failure_code = code

    def _invalidate(self, code='native_media_scope_lost'):
        if self.stopped:
            return
        self.record_failure(code)
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._close_links())
            self._cleanup_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)

    def source_available(self, source, value):
        if (self.stopped or self.failed or not isinstance(value, dict) or
                source.get('context') is not self.meeting.context or source.get('page') is not self.meeting.page):
            return
        frame, token = source.get('frame'), value.get('capture_id')
        if frame not in self.meeting.page.frames or not isinstance(token, str) or len(token) > 128:
            return
        self._candidates[frame] = token
        self._candidate_generations[frame] = self._candidate_generations.get(frame, 0) + 1
        if self.target is None:
            return
        if frame in self.documents:
            if self.documents[frame] != token:
                self._invalidate()
            return
        if frame not in self._pending_frames:
            self._pending_frames.add(frame)
            self._spawn(self._connect_added_frame(frame))

    def source_failure(self, source, value):
        if (source.get('context') is self.meeting.context and source.get('page') is self.meeting.page and
                isinstance(value, dict) and (source.get('frame'), value.get('key')) in self.links):
            self._invalidate('native_media_source_lost')

    def _target_failure(self, source, value):
        if (source.get('page') is self.target and source.get('frame') is self.target.main_frame and
                isinstance(value, dict) and any(key == value.get('key') for _, key in self.links)):
            self._invalidate('native_media_source_lost')

    def _source_detached(self, frame):
        self._candidates.pop(frame, None)
        self._candidate_generations.pop(frame, None)
        if frame in self.documents:
            self._invalidate()

    def _source_closed_event(self, page=None):
        self._source_closed = True
        self._invalidate()

    def _source_navigated(self, frame):
        if frame in self.documents:
            self._check_scope(frame, self.documents[frame], False)

    def _target_navigated(self, frame):
        if frame is self.target.main_frame:
            self._check_scope(frame, self._target_owner, True)

    def _target_closed_event(self, page=None):
        self._invalidate()

    def _check_scope(self, frame, token, target):
        if self.stopped or frame in self._scope_checks:
            return
        async def check():
            try:
                expression = ("() => window.__live?.nativeOwner" if target else
                              "() => window.__meeting?.capture_clock_id")
                current = await asyncio.wait_for(frame.evaluate(expression), 3)
                if current != token:
                    self._invalidate()
            except Exception:
                # A slow observation is not evidence of a lost document.
                if frame.is_detached() or self.meeting.page.is_closed() or self.target.is_closed():
                    self._invalidate()
            finally:
                self._scope_checks.pop(frame, None)
        self._scope_checks[frame] = self._spawn(check())

    def _watch(self):
        self.meeting.page.on('framedetached', self._source_detached)
        self.meeting.page.on('framenavigated', self._source_navigated)
        self.meeting.page.on('close', self._source_closed_event)
        self.target.on('framenavigated', self._target_navigated)
        self.target.on('close', self._target_closed_event)
        self._watching = True

    def _unwatch(self):
        if self._watching:
            self.meeting.page.remove_listener('framedetached', self._source_detached)
            self.meeting.page.remove_listener('framenavigated', self._source_navigated)
            self.meeting.page.remove_listener('close', self._source_closed_event)
            self.target.remove_listener('framenavigated', self._target_navigated)
            self.target.remove_listener('close', self._target_closed_event)
            self._watching = False

    async def _connect_frame(self, frame, token):
        if frame in self.documents:
            return
        if self.stopped or self.failed or frame.is_detached():
            raise RuntimeError('native_media_source_scope_lost')
        key = uuid.uuid4().hex
        self.documents[frame] = token
        self.frames = tuple(self.documents)
        self.links.append((frame, key))
        offer = await frame.evaluate('key => __meeting.nativeOffer(key)', key)
        answer = await self.target.evaluate(_RECEIVER_JS, {'key':key, 'sdp':offer})
        await frame.evaluate('p => __meeting.nativeAnswer(p)', {'key':key, 'sdp':answer})
        await self.target.wait_for_function("""key => {
          const relay=__live.nativeRelays?.get(key);
          return relay?.pc.connectionState==='connected' && relay.tracks.size===1 && !relay.failed;
        }""", arg=key, timeout=10000)
        if self.stopped or self.failed:
            raise RuntimeError('native_media_source_scope_lost')
        self._connected_keys.add(key)

    async def _connect_added_frame(self, frame):
        try:
            async with self._lock:
                deadline = time.monotonic() + 20
                async with asyncio.timeout(20):
                    while not self.stopped and not self.failed and not frame.is_detached():
                        generation = self._candidate_generations.get(frame, 0)
                        token = self._candidates.get(frame)
                        state = await frame.evaluate('() => window.__meeting?.nativeFrameState?.()')
                        if generation != self._candidate_generations.get(frame, 0):
                            # Reconcile a newer source announcement, not a
                            # failed setup retry. Ownership has not begun yet.
                            continue
                        if not state or not state.get('has_audio') or state.get('capture_id') != token:
                            return
                        await self._connect_frame(frame, token)
                        if self.meeting.capture_started:
                            await self._start_frame(frame, deadline=deadline)
                        return
        except asyncio.CancelledError:
            if not self.stopped:
                self._invalidate('native_media_setup_failed')
            raise
        except Exception:
            self._invalidate('native_media_setup_failed')
        finally:
            self._pending_frames.discard(frame)

    async def connect(self, target):
        async with self._lock:
            if self.target is not None or self.stopped or self.failed:
                raise RuntimeError('native_media_already_connected')
            if (self.meeting.page is None or self.meeting.page.is_closed() or
                    target.is_closed() or target.context is self.meeting.context):
                raise RuntimeError('native_media_isolation_required')
            self.target = target
            self.cleanup_status = 'pending'
            try:
                async with asyncio.timeout(20):
                    await target.context.expose_binding('nativeMeetingMediaFailure', self._target_failure)
                    await target.context.expose_binding('nativeMeetingObservation', self._observe)
                    await target.evaluate('owner => { __live.nativeOwner=owner; }', self._target_owner)
                    self._watch()
                    for frame in tuple(self.meeting.page.frames):
                        try:
                            value = await asyncio.wait_for(frame.evaluate(
                                '() => window.__meeting?.nativeFrameState?.()'), 3)
                            if value and value.get('has_audio'):
                                self._candidates[frame] = value['capture_id']
                        except Exception:
                            # Track hooks also announce actual sources. An
                            # unrelated UI frame navigating is not source loss.
                            pass
                    for frame, token in tuple(self._candidates.items()):
                        if frame.is_detached():
                            continue
                        state = await frame.evaluate('() => window.__meeting?.nativeFrameState?.()')
                        if state and state.get('has_audio') and state.get('capture_id') == token:
                            await self._connect_frame(frame, token)
            except BaseException as exc:
                self.record_failure('native_media_setup_failed')
                self._unwatch()
                cleanup = asyncio.create_task(self._close_links_unlocked())
                self._cleanup_task = cleanup
                cleanup.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    pass
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise RuntimeError('native_media_setup_failed') from None

    async def _close_links_unlocked(self):
        observation_closed = await self._close_observation()
        async def close_source(frame, key):
            if self._source_closed or frame.is_detached() or self.meeting.page.is_closed():
                return True
            try:
                return await asyncio.wait_for(frame.evaluate(
                    'key => !!window.__meeting?.closeNativeRelay && __meeting.closeNativeRelay(key)', key), 3) is True
            except Exception:
                return self._source_closed or frame.is_detached() or self.meeting.page.is_closed()

        async def close_target(key):
            if self.target.is_closed():
                return True
            try:
                return await asyncio.wait_for(self.target.evaluate(_CLOSE_RECEIVER_JS, key), 3) is True
            except Exception:
                return self.target.is_closed()

        async def close_link(frame, key):
            return all(await asyncio.gather(close_source(frame, key), close_target(key)))

        clean = all(await asyncio.gather(*(close_link(frame,key) for frame,key in self.links)))
        clean &= observation_closed
        self.cleanup_status = 'cleaned' if clean else 'unknown'
        if not clean:
            self.record_failure('native_media_cleanup_unconfirmed')
        return clean

    async def _close_links(self):
        async with self._lock:
            return await self._close_links_unlocked()

    def begin_stop(self):
        self.stopped = True
        self._unwatch()
        for task in tuple(self._tasks):
            task.cancel()

    async def disconnect(self, target):
        if self.target is None:
            return
        if target is not self.target:
            raise RuntimeError('native_media_target_scope_mismatch')
        self.begin_stop()
        if self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)
        task = self._cleanup_task
        if task is not None and not task.done():
            await asyncio.shield(task)
        if self.cleanup_status == 'cleaned':
            return
        cleanup = asyncio.create_task(self._close_links())
        self._cleanup_task = cleanup
        if not await asyncio.shield(cleanup):
            raise RuntimeError('native_media_cleanup_unconfirmed')

    async def stop(self):
        self.begin_stop()
        if self.target is not None:
            await self.disconnect(self.target)

    def context_closed(self):
        self._source_closed = True
        self.stopped = True
        if self.target is None:
            self.cleanup_status = 'cleaned'

    async def readiness(self):
        result = dict(ready=False, available=False, failed=self.failed, failure_code=self.failure_code,
            stopped=self.stopped, capture_enabled=False, observation_ready=False,
            frame_count=len(self.frames), connected_frames=0,
            live_tracks=0, cleanup_status=self.cleanup_status, packets_received=None, bytes_received=None)
        if self.target is None:
            return result
        if self.stopped:
            result['available'] = True
            if self._last:
                for key in ('packets_received', 'bytes_received'):
                    result[key] = self._last[key]
            return result
        if (self.meeting.page.is_closed() or self.target.is_closed() or
                any(frame.is_detached() for frame in self.frames)):
            self._invalidate()
        links = tuple((frame,key) for frame,key in self.links if key in self._connected_keys)
        try:
            source = await asyncio.gather(*(asyncio.wait_for(frame.evaluate(
                'key => window.__meeting?.nativeReadiness?.(key) || {owned:false}', key), 3)
                for frame, key in links))
            receiver = await asyncio.wait_for(self.target.evaluate(
                _RECEIVER_STATS_JS, self._target_owner), 3)
            if (any(not state.get('owned') or state.get('failed') or not state.get('source_live') for state in source) or
                    receiver['failed'] or receiver['frame_count'] < len(links)):
                self._invalidate('native_media_source_lost')
            enabled = bool(source) and all(state.get('capture_enabled') for state in source)
            live_tracks = sum(state.get('live_tracks', 0) for state in source)
            connected = min(sum(state.get('connected', False) for state in source), receiver['connected_frames'])
            result.update(available=True, capture_enabled=enabled, live_tracks=min(65535, live_tracks),
                connected_frames=connected, packets_received=receiver['packets_received'],
                bytes_received=receiver['bytes_received'])
            result['ready'] = (enabled and live_tracks > 0 and connected == len(self.frames) and
                not self._pending_frames and receiver['receiver_tracks'] == len(self.frames) and
                receiver['graph_running'] and all(state.get('context_running') for state in source))
            result['observation_ready'] = (result['ready'] and self._observation_running
                and receiver.get('observation_ready') is True)
        except Exception:
            # Snapshot timeouts and unavailable getStats observations do not
            # establish a terminal media fault. Native events own that latch.
            pass
        result.update(failed=self.failed, failure_code=self.failure_code,
            stopped=self.stopped, cleanup_status=self.cleanup_status)
        if self.failed:
            result['ready'] = False
            result['observation_ready'] = False
        self._last = result
        return result
