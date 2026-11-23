"""Headless meeting audio and a separate Codex WebRTC peer (no audio feedback)."""
import asyncio
import base64
import contextlib
import os
import re
import shutil
import time
import weakref
import uuid
from urllib.parse import urlsplit, urlunsplit
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError
from .diagnostics import failure_diagnostic, teams_join_diagnostic
from .native_media import NativeMeetingMedia, _CaptureDocument, _PCM_OBSERVER_JS

_TEAMS_LOBBY_MESSAGES = (
    'waiting for admission', 'waiting for someone to let you in',
    'someone in the meeting should let you in', 'waiting in the lobby',
    'someone will let you in when the meeting starts', '等待主持人', '等候大廳',
)

# Reuse meeting-lifecycle's semantic regions; chat/captions are meeting data,
# never evidence that admission, removal or meeting completion occurred.
_TEAMS_ALLOWED_ORIGINS = frozenset({'teams.microsoft.com', 'teams.cloud.microsoft', 'teams.live.com'})
_TEAMS_STANDALONE_BLOCKER_MESSAGES = (
    'Meeting has ended', 'This meeting has ended', '會議已結束',
    "Sorry, we couldn't connect you", 'Sorry, we couldn’t connect you',
    'Something went wrong', "We couldn't join the meeting", 'We couldn’t join the meeting',
    'Meeting not found', 'This meeting link is invalid', 'This meeting link has expired',
    'The meeting has expired', "This meeting doesn't exist", "We couldn't find this meeting",
    "We can't find this meeting",
    "We couldn't sign you in", 'We couldn’t sign you in',
    "We can't sign you in", 'We can’t sign you in',
    "You don't have permission", 'You do not have permission',
    'Sign in to join', 'Only people with access', 'Sign in with your work',
    'You need to sign in', '需要登入',
    "This browser isn't supported", 'This browser is not supported',
    "Your browser isn't supported", 'Your browser is not supported',
    'Unsupported browser', 'Try a different browser', 'Use a different browser',
    '此瀏覽器不受支援', '不支援此瀏覽器',
    'Allow third-party cookies', 'Enable third-party cookies',
    'Cookies are blocked', 'Third-party cookies are disabled',
    *_TEAMS_LOBBY_MESSAGES,
)


def _is_strict_teams_origin(url):
    if not isinstance(url, str) or not url:
        return False
    parsed = urlsplit(url)
    if parsed.scheme.lower() != 'https':
        return False
    try:
        port = parsed.port
    except ValueError:
        return False
    return parsed.hostname in _TEAMS_ALLOWED_ORIGINS and port in (None, 443)


_MEETING_STATUS_REGIONS = '[role="status"], [role="alert"], [role="dialog"]'
_MEETING_SOURCE_NAMES = ('chat', 'caption', 'transcript', 'conversation', '字幕', '聊天')
_MEETING_SOURCE_ATTRIBUTES = ('aria-label', 'aria-labelledby', 'data-tid', 'data-testid', 'id')
_MEETING_SOURCE_REGIONS = ', '.join(['[role="log"]', '[role="feed"]'] + [
    f'[{attribute}*="{name}" i]' for attribute in _MEETING_SOURCE_ATTRIBUTES for name in _MEETING_SOURCE_NAMES])
_MEETING_SOURCE_ANCESTORS = 'xpath=ancestor-or-self::*[' + ' or '.join([
    '@role="log"', '@role="feed"'] + [
    f'contains(translate(@{attribute},"ABCDEFGHIJKLMNOPQRSTUVWXYZ","abcdefghijklmnopqrstuvwxyz"),"{name}")'
    for attribute in _MEETING_SOURCE_ATTRIBUTES for name in _MEETING_SOURCE_NAMES]) + ']'


class PostJoinObservationUnavailable(RuntimeError):
    pass


class ResponseUIContextError(RuntimeError):
    pass


class ResponseUIFrameSnapshotError(RuntimeError):
    pass



# One read-only enumeration. Selection indexes stay internal; receipts contain
# only bounded counts/booleans, no track/peer IDs, labels, URLs or SDP.
RESPONSE_SENDER_JS = r"""() => {
  const hook=window.__meeting, peers=hook?.peers || [], candidates=[];
  const keys=['sender_count','track_count','audio_count','audio_live_count',
    'audio_ended_count','audio_enabled_count','audio_muted_count','eligible_count',
    'transceiver_count','receiver_audio_count','receiver_video_count','receiver_absent_count',
    'receiver_other_count','stopped_count','stopped_unknown_count','null_sender_count','null_send_eligible_count',
    ...['direction','current','audio_direction','audio_current'].flatMap(prefix=>['sendonly','sendrecv','recvonly','inactive','none','other'].map(v=>prefix+'_'+v))];
  const directionEnum=value=>value==null?'none':
    ['sendonly','sendrecv','recvonly','inactive'].includes(value)?value:'other';
  const zero=()=>Object.fromEntries(keys.map(k=>[k,0]));
  const diagnostic={...zero(),hook_available:!!hook,peers_available:Array.isArray(hook?.peers),
    peer_count:peers.length,peers_truncated:peers.length>16,peers:[]};
  const entries=[], generation=(window.__bResponseScanGeneration || 0)+1;
  window.__bResponseScanGeneration=generation;
  peers.forEach((pc,peer)=>{
    const senders=pc.getSenders(), transceivers=typeof pc.getTransceivers==='function'?pc.getTransceivers():[];
    const row={...zero(),peer_available:!!pc,closed:pc.connectionState==='closed',
      transceivers_available:typeof pc.getTransceivers==='function',
      audio_transceivers:[],audio_transceivers_truncated:false};
    row.sender_count=senders.length;
    for(const s of senders) {
      if(s.track) row.track_count++;
      if(s.track?.kind!=='audio') continue;
      row.audio_count++;
      if(s.track.readyState==='live') row.audio_live_count++;
      if(s.track.readyState==='ended') row.audio_ended_count++;
      if(s.track.enabled) row.audio_enabled_count++; else row.audio_muted_count++;
      if(!row.closed && !s.track.enabled && s.track.readyState==='live') row.eligible_count++;
    }
    const add=(sender,senderIndex,transceiver)=>{
      const slot=entries.length;
      entries.push({pc,sender,transceiver,original:sender.track,peerIndex:peer});
      candidates.push({peer,sender:senderIndex,slot,generation});
    };
    if(!row.closed) senders.filter(s=>s.track?.kind==='audio').forEach((s,sender)=>{
      if(!s.track.enabled && s.track.readyState==='live') add(s,sender,null);
    });
    row.transceiver_count=transceivers.length;
    for(const t of transceivers) {
      const kind=t.receiver?.track?.kind;
      row[kind==='audio'?'receiver_audio_count':kind==='video'?'receiver_video_count':
        !kind?'receiver_absent_count':'receiver_other_count']++;
      if(t.stopped===true) row.stopped_count++;
      if(typeof t.stopped!=='boolean') row.stopped_unknown_count++;
      for(const [prefix,value] of [['direction',t.direction],['current',t.currentDirection]])
        row[prefix+'_'+(value==null?'none':['sendonly','sendrecv','recvonly','inactive'].includes(value)?value:'other')]++;
      if(t.sender?.track===null) row.null_sender_count++;
      if(kind==='audio') {
        const direction=directionEnum(t.direction), currentDirection=directionEnum(t.currentDirection);
        row['audio_direction_'+direction]++;row['audio_current_'+currentDirection]++;
        const senderMember=senders.includes(t.sender);
        const reason=row.closed?'peer_closed':t.stopped===true?'transceiver_stopped':
          t.stopped!==false?'stopped_unknown':!t.sender?'sender_missing':
          t.sender.track!==null?'sender_track_present':
          !['sendonly','sendrecv'].includes(t.currentDirection)?'current_not_sending':
          !senderMember?'sender_not_member':'eligible';
        if(row.audio_transceivers.length<8) row.audio_transceivers.push({direction,currentDirection,
          sender_member:senderMember,sender_track_null:t.sender?.track===null,
          stopped:t.stopped===true,rejection_reason:reason});
        else row.audio_transceivers_truncated=true;
      }
      if(!row.closed && kind==='audio' && t.stopped===false && t.sender?.track===null &&
          ['sendonly','sendrecv'].includes(t.currentDirection) && senders.includes(t.sender)) {
        row.null_send_eligible_count++; row.eligible_count++;
        add(t.sender,senders.indexOf(t.sender),t);
      }
    }
    for(const key of keys) diagnostic[key]+=row[key];
    if(peer<16) diagnostic.peers.push(row);
  });
  const bounded=v=>Object.fromEntries(keys.map(k=>[k,Math.min(v[k],65535)]));
  Object.assign(diagnostic,bounded(diagnostic),{peer_count:Math.min(peers.length,65535)});
  diagnostic.peers=diagnostic.peers.map(row=>({...row,...bounded(row)}));
  window.__bResponseScan={generation,capture:hook,document,entries};
  return {candidates,diagnostic};
}"""

SELECT_RESPONSE_SENDER_JS = r"""({meetingId,generation,slot,selectionId}) => {
  const scan=window.__bResponseScan, entry=scan?.entries[slot];
  if(!entry || scan.generation!==generation || scan.document!==document ||
      scan.capture!==window.__meeting || scan.capture.peers[entry.peerIndex]!==entry.pc ||
      entry.pc.connectionState==='closed' || !entry.pc.getSenders().includes(entry.sender) ||
      entry.sender.track!==entry.original)
    throw new Error('Response sender selection changed');
  if(entry.original===null) {
    const t=entry.transceiver;
    if(!t || !entry.pc.getTransceivers().includes(t) || t.sender!==entry.sender ||
       entry.pc.getTransceivers().filter(v=>v.sender===entry.sender).length!==1 ||
       t.receiver?.track?.kind!=='audio' || t.stopped!==false ||
       !['sendonly','sendrecv'].includes(t.currentDirection))
      throw new Error('Negotiated audio sending required');
  } else if(entry.original.kind!=='audio' || entry.original.readyState!=='live' || entry.original.enabled)
    throw new Error('Existing muted live audio sender required');
  window.__bSelectedSender={...entry,selectionId,capture:scan.capture,document,href:location.href};
  window.__bMeetingId=meetingId;
  delete window.__bResponseScan;
  return true;
}"""

# Persist only counts/protocols: closing a peer clears its SDP, obscuring why
# admission failed. Never retain addresses, TURN credentials or error text.
ICE_OBSERVER_JS = r"""(pc) => {
  const started = performance.now();
  const diag = {gathered: {host:0, srflx:0, relay:0, other:0}, errors: [], states: []};
  const endpoint = url => {
    const m = String(url).match(/^(stuns?|turns?):[^?]+?(?::(\d+))?(?:\?transport=(udp|tcp))?$/);
    return m ? {scheme:m[1], port:m[2] ? Number(m[2]) : null, transport:m[3] || 'default'} : {scheme:'unknown'};
  };
  diag.servers = (pc.getConfiguration().iceServers || []).flatMap(s =>
    (Array.isArray(s.urls) ? s.urls : [s.urls]).map(endpoint));
  Object.defineProperty(pc, '__recorderIce', {value:diag});
  pc.addEventListener('icecandidate', e => {
    if (e.candidate) diag.gathered[e.candidate.type in diag.gathered ? e.candidate.type : 'other']++;
  });
  pc.addEventListener('icecandidateerror', e => {
    if (diag.errors.length < 32) diag.errors.push({code:e.errorCode, ...endpoint(e.url)});
  });
  for (const event of ['icegatheringstatechange','iceconnectionstatechange']) {
    pc.addEventListener(event, () => {
      if (diag.states.length < 32) diag.states.push({ms:Math.round(performance.now()-started),
        gathering:pc.iceGatheringState, connection:pc.iceConnectionState});
    });
  }
}"""

OWNED_AUDIO_INPUT_JS = r"""
(() => {
  if (!['teams.microsoft.com','teams.cloud.microsoft','teams.live.com'].includes(location.hostname) ||
      !navigator.mediaDevices) return;
  const media = navigator.mediaDevices, enumerate = media.enumerateDevices.bind(media);
  const deviceId = 'teams-assistant-owned-audio';
  const descriptor = {deviceId, groupId:deviceId, kind:'audioinput', label:'Meeting assistant owned audio'};
  const device = Object.freeze({...descriptor, toJSON:() => ({...descriptor})});
  let context, destination, sourceTrack, closed = false;
  const tracks = new Set();
  const unprocessed = {autoGainControl:false, echoCancellation:false, noiseSuppression:false};
  const processingNames = Object.keys(unprocessed);
  const unsatisfied = name => {
    const error = new DOMException('The owned audio input does not satisfy the constraint', 'OverconstrainedError');
    Object.defineProperty(error, 'constraint', {value:name});
    return error;
  };
  const identity = (value, name) => {
    const exact = value && typeof value === 'object' ? value.exact : undefined;
    if (exact === undefined) return;
    const choices = Array.isArray(exact) ? exact : [exact];
    if (choices.includes(deviceId) || (name === 'deviceId' && choices.includes('default'))) return;
    throw unsatisfied(name);
  };
  media.enumerateDevices = async () => [
    device, ...(await enumerate()).filter(item => item.kind === 'audiooutput')
  ];
  media.getUserMedia = async constraints => {
    if (closed) throw new DOMException('The input document is inactive', 'InvalidStateError');
    if (constraints?.video) throw new DOMException('Camera input is not provided', 'NotAllowedError');
    if (!constraints?.audio) throw new TypeError('An audio input request is required');
    const audio = typeof constraints.audio === 'object' ? constraints.audio : {};
    identity(audio.deviceId, 'deviceId'); identity(audio.groupId, 'groupId');
    const {deviceId:requestedDevice, groupId:requestedGroup, ...processing} = audio;
    // A WebAudio source has no microphone DSP. Honor its fixed, disabled
    // processing here; Chromium cannot apply capture-device constraints to it.
    for (const name of processingNames) {
      const required = processing[name]?.exact;
      if (required !== undefined && required !== false) throw unsatisfied(name);
      delete processing[name];  // Bare/ideal preferences are not requirements.
    }
    if (!context) {
      context = new AudioContext({sampleRate:48000});
      destination = context.createMediaStreamDestination();
      sourceTrack = destination.stream.getAudioTracks()[0];
      sourceTrack.enabled = false;
    }
    await context.resume();
    if (closed || context.state !== 'running')
      throw new DOMException('The owned audio input is unavailable', 'InvalidStateError');
    const track = sourceTrack.clone();
    track.enabled = false; tracks.add(track);
    const stop = track.stop.bind(track), settings = track.getSettings.bind(track);
    const capabilities = track.getCapabilities.bind(track);
    track.stop = () => { stop(); tracks.delete(track); };
    track.addEventListener('ended', () => tracks.delete(track), {once:true});
    track.getSettings = () => ({...settings(), ...unprocessed, deviceId, groupId:deviceId});
    track.getCapabilities = () => ({...capabilities(),
      autoGainControl:[false], echoCancellation:[false], noiseSuppression:[false]});
    try {
      await track.applyConstraints(processing);
      if (closed || track.readyState !== 'live')
        throw new DOMException('The owned audio input ended', 'InvalidStateError');
      return new MediaStream([track]);
    } catch (error) { track.stop(); throw error; }
  };
  addEventListener('pagehide', () => {
    closed = true;
    for (const track of tracks) track.stop();
    sourceTrack?.stop();
    if (context && context.state !== 'closed') context.close().catch(() => {});
  }, {once:true});
})();
"""

CAPTURE_JS = r"""
(() => {
  const observeIce = """ + ICE_OBSERVER_JS + r""";
  const NativePC = window.RTCPeerConnection;
  const state = window.__meeting = {tracks:0, packets:0, dropped:0, pending:0,
    frames:0, nonzero_samples:0, playout_errors:0, peers:[], attach_errors:[]};
  state.capture_clock_id = Array.from(crypto.getRandomValues(new Uint32Array(4)), x=>x.toString(16)).join('-');
  let ctx, mix, closeObserver, nativeDestination;
  const nativeRelays = new Map();
  state.native_stopped = false;
  state.native_enabled = false;
  state.native_failed = false;
  // B's local receiver must not feed its own voice back into A's meeting mix.
  // Keep it outside CapturePC's incoming-track hook and Teams peer roster.
  state.createNativeOutputPeer = () => {
    if (state.native_stopped || window.__meeting !== state)
      throw new Error('capture_document_inactive');
    return new NativePC({iceServers:[],encodedInsertableStreams:true});
  };
  const captureAudio = window.__meetingCaptureAudio !== false;
  const pendingTracks = new Set(), attached = new Map(), attaching = new Map();
  state.capture_enabled = window.__meetingCaptureDeferred !== true;
  state.observation_suppressed = false;
  const error = code => { if (state.attach_errors.length < 8) state.attach_errors.push(code); };
  state.readiness = () => {
    const entries = [...attached].filter(([t,e]) => t.readyState === 'live' && e.active);
    return {hook_present:true, capture_enabled:state.capture_enabled,
      capture_clock_id:state.capture_clock_id,
      context_state:ctx?.state || 'absent', live_attached_tracks:entries.length,
      muted_live_tracks:entries.filter(([t]) => t.muted).length,
      pending_live_tracks:[...pendingTracks].filter(t => t.readyState === 'live').length,
      attach_errors:[...state.attach_errors],
      ready:captureAudio && state.capture_enabled && ctx?.state === 'running' && entries.length > 0};
  };
  state.nativeFrameState = () => {
    const tracks = new Set([...pendingTracks,...attached.keys()]);
    for (const pc of state.peers) for (const receiver of pc.getReceivers?.() || [])
      if (receiver.track?.kind==='audio') tracks.add(receiver.track);
    return {capture_id:state.capture_clock_id, has_audio:captureAudio &&
      [...tracks].some(track=>track.readyState==='live')};
  };
  const notifyNativeSource = () => {
    Promise.resolve(window.meetingNativeSource?.({capture_id:state.capture_clock_id})).catch(()=>{});
  };
  const nativeFailure = key => {
    if (state.native_stopped || !nativeRelays.has(key)) return;
    state.native_failed = true;
    Promise.resolve(window.meetingNativeAudioFailure?.({key})).catch(()=>{});
  };
  const detach = (track, entry) => {
    if (attached.get(track) !== entry) return;
    attached.delete(track); state.tracks = attached.size; entry.active = false;
    try { entry.source.disconnect(); } catch (_) {}
    try { entry.player.pause(); entry.player.srcObject = null; entry.player.remove(); } catch (_) {}
  };
  const ensureGraph = () => {
    if (state.native_stopped || window.__meeting !== state) throw new Error('capture_document_inactive');
    if (ctx) return;
    ctx = new AudioContext({sampleRate:24000});
    mix = ctx.createGain();
    nativeDestination = ctx.createMediaStreamDestination();
    nativeDestination.stream.getAudioTracks()[0].enabled = state.native_enabled;
    nativeDestination.stream.getAudioTracks()[0].addEventListener('ended',()=> {
      for (const key of nativeRelays.keys()) nativeFailure(key);
    });
    ctx.addEventListener('statechange',()=> {
      if (ctx.state==='closed') for (const key of nativeRelays.keys()) nativeFailure(key);
    });
    mix.connect(nativeDestination);
    if (!state.observation_suppressed) closeObserver = (""" + _PCM_OBSERVER_JS + r""")(ctx,mix,state,
      () => state.readiness().ready && !state.observation_suppressed,
      () => state.capture_clock_id, packet => window.meetingAudio(packet));
  };
  state.nativeOffer = async key => {
    if (!captureAudio || nativeRelays.has(key)) throw new Error('native_media_source_unavailable');
    state.observation_suppressed = true;
    if (closeObserver) { closeObserver(); closeObserver=null; }
    ensureGraph();
    // B sender selection sees only Teams' CapturePC instances, never these
    // local relays. This also bypasses the Teams outgoing-track mute hook.
    const pc = new NativePC({iceServers:[]});
    nativeRelays.set(key,pc);
    pc.addEventListener('connectionstatechange',()=> {
      if (['failed','closed'].includes(pc.connectionState)) nativeFailure(key);
    });
    pc.addTrack(nativeDestination.stream.getAudioTracks()[0],nativeDestination.stream);
    try {
      await ctx.resume();
      if (ctx.state !== 'running' || state.native_stopped || nativeRelays.get(key)!==pc)
        throw new Error('native_media_source_unavailable');
      await pc.setLocalDescription(await pc.createOffer());
      await new Promise((resolve,reject) => {
        if (pc.iceGatheringState==='complete') return resolve();
        const done=() => {
          if (pc.iceGatheringState==='complete') { clearTimeout(timer); pc.removeEventListener('icegatheringstatechange',done); resolve(); }
        };
        const timer=setTimeout(()=>{ pc.removeEventListener('icegatheringstatechange',done); reject(new Error('native_media_ice_timeout')); },5000);
        pc.addEventListener('icegatheringstatechange',done);
      });
      return pc.localDescription.sdp;
    } catch (error) { nativeRelays.delete(key); pc.close(); throw error; }
  };
  state.nativeAnswer = async ({key,sdp}) => {
    if (window.__meeting !== state || state.native_stopped || !nativeRelays.has(key))
      throw new Error('native_media_source_scope_lost');
    await nativeRelays.get(key).setRemoteDescription({type:'answer',sdp});
    return true;
  };
  state.closeNativeRelay = key => {
    if (window.__meeting !== state) return false;
    const pc = nativeRelays.get(key);
    if (pc) { nativeRelays.delete(key); pc.close(); }
    return !nativeRelays.has(key);
  };
  state.nativeReadiness = key => {
    const pc=nativeRelays.get(key), track=nativeDestination?.stream.getAudioTracks()[0];
    return {owned:window.__meeting===state && !!pc,
      failed:state.native_failed || !!pc && ['closed','failed'].includes(pc.connectionState),
      capture_enabled:state.capture_enabled && state.native_enabled && track?.enabled===true,
      connected:pc?.connectionState==='connected', live_tracks:state.readiness().live_attached_tracks,
      context_running:ctx?.state==='running', source_live:track?.readyState==='live'};
  };
  async function attachLive(track) {
    let entry, stage = 'audio_context_create_failed';
    try {
      if (track.readyState !== 'live') return;
      ensureGraph();
      stage = 'media_source_attach_failed';
      const source = ctx.createMediaStreamSource(new MediaStream([track]));
      const player = document.createElement('audio');
      player.muted = true; player.autoplay = true; player.hidden = true;
      player.dataset.meetingRecorderPlayout = 'true'; player.srcObject = new MediaStream([track]);
      entry = {source,player,active:false}; attached.set(track,entry); state.tracks = attached.size;
      track.addEventListener('ended',() => detach(track,entry),{once:true});
      source.connect(mix); (document.body || document.documentElement).appendChild(player);
      stage = 'audio_context_resume_failed';
      await ctx.resume();
      if (track.readyState !== 'live' || attached.get(track) !== entry) { detach(track,entry); return; }
      if (ctx.state !== 'running') throw new Error('context_not_running');
      stage = 'media_playout_failed';
      const playing = player.play();
      if (playing && typeof playing.catch === 'function') playing.catch(() => {
        state.playout_errors++; error('media_playout_failed'); detach(track,entry);
      });
      entry.active = true;
    } catch (_) {
      error(stage); if (stage === 'media_playout_failed') state.playout_errors++;
      if (entry) detach(track,entry);
    }
  }
  async function attach(track) {
    if (!track || track.kind !== 'audio' || !captureAudio || state.native_stopped || track.readyState !== 'live') {
      pendingTracks.delete(track); return;
    }
    if (!state.capture_enabled) {
      const added=!pendingTracks.has(track); pendingTracks.add(track);
      if (added) notifyNativeSource();
      return;
    }
    pendingTracks.delete(track);
    if (attaching.has(track)) return await attaching.get(track);
    if (attached.has(track)) return;
    notifyNativeSource();
    const task = attachLive(track); attaching.set(track,task);
    try { await task; } finally { attaching.delete(track); }
  }
  state.attach = attach;
  state.startCapture = async ({observe=true}={}) => {
    state.observation_suppressed = !observe;
    if (!captureAudio || state.native_stopped) return state.readiness();
    state.capture_enabled = true; state.native_enabled = true;
    if (nativeDestination) nativeDestination.stream.getAudioTracks()[0].enabled = true;
    const tracks = new Set(pendingTracks);
    for (const pc of state.peers) {
      try { for (const receiver of pc.getReceivers()) if (receiver.track) tracks.add(receiver.track); }
      catch (_) { error('receiver_enumeration_failed'); }
    }
    pendingTracks.clear();
    await Promise.all([...tracks].map(t => attach(t)));
    return state.readiness();
  };
  state.stopCapture = async () => {
    state.capture_enabled = false; state.native_enabled = false; state.native_stopped = true; pendingTracks.clear();
    if (nativeDestination) for (const track of nativeDestination.stream.getTracks()) { track.enabled=false; track.stop(); }
    for (const pc of nativeRelays.values()) pc.close();
    nativeRelays.clear();
    for (const [track,entry] of attached) detach(track,entry);
    if (closeObserver) closeObserver();
    if (ctx && ctx.state !== 'closed') await ctx.close();
    return {stopped:true, source_stopped:!nativeDestination ||
      nativeDestination.stream.getTracks().every(t=>!t.enabled && t.readyState==='ended'),
      relays_closed:nativeRelays.size===0, context_closed:!ctx || ctx.state==='closed'};
  };
  class CapturePC extends NativePC {
    constructor(...args) {
      super(...args);
      observeIce(this);
      state.peers.push(this);
      this.addEventListener('track', e => { attach(e.track).catch(() => error('track_attach_failed')); });
    }
    addTrack(track, ...streams) {
      // The recorder must never publish microphone/camera audio or video.
      track.enabled = false;
      return super.addTrack(track, ...streams);
    }
    addTransceiver(track, options) {
      if (typeof track !== 'string') track.enabled = false;
      return super.addTransceiver(track, options);
    }
  }
  window.RTCPeerConnection = CapturePC;
  if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = CapturePC;
  const replace = RTCRtpSender.prototype.replaceTrack;
  RTCRtpSender.prototype.replaceTrack = function(track) {
    if (track) track.enabled = false;
    return replace.call(this, track);
  };
})();
"""

PEER_JS = r"""
async (iceServers = []) => {
  const observeIce = """ + ICE_OBSERVER_JS + r""";
  const ctx = new AudioContext({sampleRate: 24000}); await ctx.resume();
  const destination = ctx.createMediaStreamDestination();
  const pc = new RTCPeerConnection({iceServers});
  observeIce(pc);
  pc.addTrack(destination.stream.getAudioTracks()[0], destination.stream);
  const dc = pc.createDataChannel('oai-events');
  const delivery = {messages:0, json_errors:0, binding_attempted:0, binding_returned:0, binding_failed:0};
  dc.onmessage = e => {
    delivery.messages++;
    let value;
    try { value = JSON.parse(e.data); } catch (error) { delivery.json_errors++; throw error; }
    delivery.binding_attempted++;
    let result;
    try { result = window.liveEvent(value); } catch (error) { delivery.binding_failed++; throw error; }
    Promise.resolve(result).then(() => delivery.binding_returned++, () => delivery.binding_failed++);
    return result;
  };
  window.__live = {pc, dc, ctx, destination, next: 0, sentFrames: 0,
    delivery, bufferGapCount:0, bufferGapFrames:0, maxBufferGapFrames:0,
    push(data) {
      if (this.inputMode === 'native_media') throw new Error('native_media_does_not_accept_pcm_push');
      const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
      const view = new DataView(bytes.buffer);
      const b = ctx.createBuffer(1, bytes.length / 2, 24000);
      const a = b.getChannelData(0);
      for (let i = 0; i < a.length; i++) a[i] = view.getInt16(i*2, true) / 32768;
      const s = ctx.createBufferSource(); s.buffer = b; s.connect(destination);
      const when = Math.max(ctx.currentTime + .06, this.next);
      const gapFrames = this.sentFrames > 0 ? Math.max(0, Math.round((when-this.next)*24000)) : 0;
      if (gapFrames > 0) { this.bufferGapCount++; this.bufferGapFrames += gapFrames;
        this.maxBufferGapFrames = Math.max(this.maxBufferGapFrames, gapFrames); }
      s.start(when); this.next = when + b.duration; this.sentFrames += a.length;
      return Math.max(0, this.next - ctx.currentTime);
    }
  };
  await pc.setLocalDescription(await pc.createOffer());
  await Promise.race([
    new Promise(resolve => {
      if (pc.iceGatheringState === 'complete') return resolve();
      pc.addEventListener('icegatheringstatechange', () => {
        if (pc.iceGatheringState === 'complete') resolve();
      });
    }), new Promise(resolve => setTimeout(resolve, iceServers.length ? 20000 : 3000))
  ]);
  if (pc.iceGatheringState !== 'complete') throw new Error('ICE gathering did not finish; no partial offer was sent');
  return pc.localDescription.sdp;
}
"""



_APPROVED_CHROMIUM_ARGS = (
    "--autoplay-policy=no-user-gesture-required", "--disable-dev-shm-usage",
)


async def launch_sandboxed_chromium(browser_type, *, executable_path=None,
                                    proxy=None, args=None):
    """All project launches retain Chromium's sandbox, without override flags."""
    args = _APPROVED_CHROMIUM_ARGS if args is None else args
    if not isinstance(args, (list, tuple)) or any(arg not in _APPROVED_CHROMIUM_ARGS for arg in args):
        raise ValueError("Chromium launch arguments must stay within the approved list")
    options = {"executable_path": executable_path, "headless": True,
               "chromium_sandbox": True, "args": list(args)}
    if proxy is not None:
        options["proxy"] = proxy
    return await browser_type.launch(**options)


class Browser:
    def __init__(self, executable=None):
        self.executable = executable or os.environ.get("MEETING_CHROMIUM") or shutil.which("chromium")
        self.playwright = None
        self.browser = None

    async def __aenter__(self):
        from playwright.async_api import async_playwright
        self._starter = async_playwright()
        self.playwright = await self._starter.start()
        try:
            options = {"executable_path": self.executable}
            proxy_url = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy_url:
                proxy = urlsplit(proxy_url)
                hostname = proxy.hostname
                if not hostname:
                    raise ValueError("Configured HTTPS proxy has no hostname")
                if ":" in hostname:
                    hostname = f"[{hostname}]"
                server = f"{proxy.scheme}://{hostname}" + (f":{proxy.port}" if proxy.port else "")
                options["proxy"] = {"server": server}
                if proxy.username:
                    options["proxy"]["username"] = proxy.username
                if proxy.password:
                    options["proxy"]["password"] = proxy.password
            self.browser = await launch_sandboxed_chromium(self.playwright.chromium, **options)
        except BaseException:
            await self.playwright.stop()
            raise
        return self

    async def __aexit__(self, *_):
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()


class LivePeer:
    def __init__(self, browser, callback, ice_servers=None, *, media_source=None):
        self.browser = browser
        self.callback = callback
        self.ice_servers = ice_servers or []
        self.media_source = media_source

    async def offer(self):
        try:
            self.context = await self.browser.new_context()
            await self.context.expose_binding("liveEvent", lambda source, event: self.callback(event))
            self.page = await self.context.new_page()
            offer = await self.page.evaluate(PEER_JS, self.ice_servers)
            if self.media_source is not None:
                await self.page.evaluate("() => { __live.inputMode='native_media'; }")
                await self.media_source.connect_native_audio(self.page)
            return offer
        except BaseException:
            cleanup = asyncio.create_task(self.close())
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                cleanup.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            raise

    async def answer(self, sdp):
        await self.page.evaluate("sdp => window.__live.pc.setRemoteDescription({type:'answer', sdp})", sdp)

    async def wait_connected(self, timeout=20):
        await self.page.wait_for_function(
            "__live.pc.connectionState === 'connected' && __live.dc.readyState === 'open'", timeout=timeout * 1000)

    async def push(self, data):
        if self.media_source is not None:
            raise RuntimeError("native_media_does_not_accept_pcm_push")
        queued = await self.page.evaluate("data => __live.push(data)", base64.b64encode(data).decode())
        if queued > 2:
            await asyncio.sleep(queued - 1)

    async def request_session_close(self, *, timeout=2):
        page = getattr(self, "page", None)
        if page is None or page.is_closed():
            return False
        # push() schedules PCM ahead of the browser clock. Let that owned tail
        # play before closing the provider; no silence or keepalive is added.
        await page.wait_for_function("""() => {
          const live = window.__live;
          return !live?.dc || live.dc.readyState !== 'open' || !live.ctx ||
            !Number.isFinite(live.next) || live.ctx.currentTime >= live.next;
        }""", timeout=max(1, int(timeout * 1000)))
        return await page.evaluate("""() => {
          const dc = window.__live && window.__live.dc;
          if (!dc || dc.readyState !== 'open') return false;
          dc.send(JSON.stringify({type:'session.close'}));
          return true;
        }""") is True

    async def stats(self):
        # Counts and states are sufficient for ICE diagnostics; never save SDP,
        # candidate addresses, ICE credentials, or a server-issued call ID.
        result = await self.page.evaluate(r"""async () => {
          const pc = __live.pc, reports = [...(await pc.getStats()).values()];
          const sender = {audio_context_state:'unknown', audio_context_sample_rate:null,
            sender_track_sample_rate:null, sender_track_channels:null, scheduled_queue_ms:null};
          // Optional sender observations must not suppress the existing ICE report.
          try {
            const ctx = __live.ctx;
            if (ctx) {
              sender.audio_context_state = ['running','suspended','interrupted','closed'].includes(ctx.state) ? ctx.state : 'unknown';
              if (ctx.state !== 'closed') {
                sender.audio_context_sample_rate = ctx.sampleRate ?? null;
                if (__live.inputMode !== 'native_media' && Number.isFinite(__live.next) && Number.isFinite(ctx.currentTime))
                  sender.scheduled_queue_ms = Math.max(0, Math.round((__live.next-ctx.currentTime)*1000));
              }
            }
          } catch (_) {}
          try {
            const track = sender.audio_context_state === 'closed' ? null : __live.destination?.stream?.getAudioTracks?.()?.[0];
            const settings = track?.readyState === 'ended' ? null : track?.getSettings?.();
            Object.assign(sender, {sender_track_sample_rate:settings?.sampleRate ?? null,
              sender_track_channels:settings?.channelCount ?? null});
          } catch (_) {}
          const candidates = description => {
            const lines = (description?.sdp || '').split(/\r?\n/)
              .filter(line => line.startsWith('a=candidate:'));
            const fields = lines.map(line => line.trim().split(/\s+/));
            return {count: lines.length,
              udp: fields.filter(x => x[2]?.toLowerCase() === 'udp').length,
              tcp: fields.filter(x => x[2]?.toLowerCase() === 'tcp').length,
              relay: fields.filter(x => x[x.indexOf('typ') + 1] === 'relay').length};
          };
          return {connection: pc.connectionState, ice_connection: pc.iceConnectionState,
            ice_diagnostics: pc.__recorderIce || null,
            ice_gathering: pc.iceGatheringState, signaling: pc.signalingState,
            data_channel: __live.dc.readyState, input_mode:__live.inputMode || 'pcm',
            sent_frames: __live.inputMode === 'native_media' ? null : __live.sentFrames,
            ...sender,
            buffer_gap_count: __live.inputMode === 'native_media' ? null : __live.bufferGapCount,
            buffer_gap_frames: __live.inputMode === 'native_media' ? null : __live.bufferGapFrames,
            max_buffer_gap_frames: __live.inputMode === 'native_media' ? null : __live.maxBufferGapFrames,
            datachannel_delivery: {...__live.delivery},
            local_candidates: candidates(pc.localDescription),
            remote_candidates: candidates(pc.remoteDescription),
            candidate_pairs: reports.filter(x => x.type === 'candidate-pair')
              .map(x => ({state:x.state, nominated:x.nominated,
                bytesSent:x.bytesSent, bytesReceived:x.bytesReceived})),
            reports: reports.filter(x => x.type === 'outbound-rtp' && x.kind === 'audio')
              .map(x => ({bytesSent:x.bytesSent, packetsSent:x.packetsSent}))};
        }""")
        if self.media_source is not None:
            result["native_source"] = await self.media_source.native_audio_readiness()
        return result

    async def close(self):
        task = getattr(self, "_close_task", None)
        if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
            self._close_task = asyncio.create_task(self._close_owned())
            self._close_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(self._close_task)

    async def _close_owned(self):
        disconnect_failed = False
        try:
            if self.media_source is not None and hasattr(self, "page"):
                try:
                    await self.media_source.disconnect_native_audio(self.page)
                except Exception:
                    disconnect_failed = True
        finally:
            if hasattr(self, "context"):
                await asyncio.wait_for(self.context.close(), 5)
        if disconnect_failed:
            # Closing the owned target context can resolve an otherwise
            # unobservable receiver cleanup; a still-unknown source must fail.
            await self.media_source.disconnect_native_audio(self.page)


def meeting_platform(url):
    u = urlsplit(url)
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or u.port not in (None, 443):
        raise ValueError("Meeting link must be a standard HTTPS URL")
    if host in {"teams.microsoft.com", "teams.live.com", "teams.cloud.microsoft"}:
        if not u.path.startswith(("/l/meetup-join/", "/meet/", "/dl/launcher/", "/v2/")):
            raise ValueError("Teams URL is not a meeting join link")
        return "teams"
    if host in {"zoom.us", "zoom.com"} or host.endswith((".zoom.us", ".zoom.com")):
        if not re.search(r"/(?:j|wc|my)/", u.path):
            raise ValueError("Zoom URL is not a meeting join link")
        return "zoom"
    raise ValueError("Only Teams and Zoom meeting links are supported")


def browser_join_url(url):
    """Use Zoom's documented browser join route; retain the supplied passcode."""
    if meeting_platform(url) == "zoom":
        u = urlsplit(url)
        match = re.fullmatch(r"/j/(\d+)/?", u.path)
        if match:
            return urlunsplit((u.scheme, u.netloc, f"/wc/{match[1]}/join", u.query, ""))
    return url


class MeetingBrowser:
    def __init__(self, browser, on_audio, event, *, capture_audio=True, defer_capture=False, response_mode=False):
        if type(response_mode) is not bool:
            raise ValueError('Explicit response UI mode required')
        self.response_mode = response_mode
        self._response_computer_audio_verified = False
        self._response_join_page = None
        self._response_radio_dispatched = False
        self._response_preview_dispatched = False
        self._response_rescan_count = 0
        self._response_verified_roster = None
        self._response_join_started_at = None
        self._post_join_observation = None
        self.browser, self.on_audio, self.event = browser, on_audio, event
        self.capture_audio, self.defer_capture = capture_audio, defer_capture
        self._join_platform = 'teams'
        self.capture_started = False
        self.context = None
        self.page = None
        self.audio_frame = None
        self.capture_clocks = weakref.WeakKeyDictionary()
        self.dual_policy = None
        self.dual_frame = None
        self._join_state = None
        self._response_sender_diagnostic = None
        self._response_selection_id = None
        self._document_policy = None
        self._native_media = NativeMeetingMedia(self)

    def document_policy(self,frame):
        return self._document_policy.snapshot(frame) if self._document_policy is not None else {'available':False}

    def join_diagnostic(self):
        if self._join_state is None:
            return None
        state=dict(self._join_state)
        raw=state.get('computer_audio_wait')
        if isinstance(raw,dict):
            receipt=dict(raw);state['computer_audio_wait']=receipt
            now=time.monotonic();started=self._response_join_started_at
            def elapsed(sample):
                return min(900000,max(0,int((sample-started)*1000))) if type(sample) in {int,float} and started is not None else None
            def age(sample):
                return min(900000,max(0,int((now-sample)*1000))) if type(sample) in {int,float} else None
            receipt.update(primary_observed_elapsed_ms=elapsed(raw.get('_primary_sample_at')),
                primary_sample_age_ms=age(raw.get('_primary_sample_at')),
                context_observed_elapsed_ms=elapsed(raw.get('_context_sample_at')),
                context_sample_age_ms=age(raw.get('_context_sample_at')),
                context_observation_duration_ms=raw.get('_context_duration_ms'))
            if isinstance(raw.get('ui_snapshot'),dict):
                ui=dict(raw['ui_snapshot']);receipt['ui_snapshot']=ui
                ui.update(observed_elapsed_ms=elapsed(ui.get('_sampled_at')),
                    sample_age_ms=age(ui.get('_sampled_at')),
                    sample_scope=('before_context_invalidation' if raw.get('_ui_invalidated') else
                        'previous_complete_observation' if raw.get('_ui_attempt_incomplete') else 'last_complete_observation'),
                    latest_attempt_error=raw.get('_ui_latest_error','none'))
        post = self._post_join_observation
        if isinstance(post, dict):
            now = time.monotonic()
            sample = (post.get('snapshot') or {}).get('_sampled_at')
            sample_age = max(0, int((now - sample) * 1000)) if type(sample) in {int, float} else None
            reason = post.get('unavailable_reason', 'not_attempted')
            available = reason == 'none' and sample_age is not None and sample_age <= 1500
            if reason == 'none' and not available:
                reason = 'stale'
            snapshot = dict(post['snapshot']) if available else None
            if snapshot is not None:
                started = self._response_join_started_at
                snapshot.update(sample_scope='post_join_observation', sample_age_ms=sample_age,
                    observed_elapsed_ms=max(0, int((sample - started) * 1000)) if started is not None else None,
                    latest_attempt_error='none')
            state['post_join_ui'] = dict(available=available, unavailable_reason=reason,
                snapshot=snapshot, last_sample_age_ms=sample_age,
                observation_attempts=post.get('observation_attempts', 0))
        return teams_join_diagnostic(state)

    def _join_phase(self, phase):
        if self._join_state is not None and self._join_state['outcome'] == 'in_progress':
            self._join_state['phase'] = phase

    def _join_control(self, action, visible):
        if self._join_state is not None and self._join_state['outcome'] == 'in_progress' and action in self._join_state['controls_checked']:
            self._join_state['controls_checked'][action] = True
            self._join_state['controls_seen'][action] |= visible is True
            if (visible and action in {'name', 'join'} and not self._join_state['join_dispatch_started']
                    and self._join_state['page_state'] in {'unknown', 'unrecognized'}):
                self._join_state['page_state'] = 'prejoin_controls'

    def _join_action(self, action, result):
        if self._join_state is not None and self._join_state['outcome'] == 'in_progress' and action in self._join_state['actions']:
            self._join_state['actions'][action] = result
            if action == 'join' and result == 'dispatching':
                self._join_phase('join_dispatch')
                self._join_state['join_dispatch_started'] = True
            elif action == 'join' and result == 'completed':
                self._join_state['join_dispatch_completed'] = True

    def _join_error(self, action, exc):
        if self._join_state is not None and self._join_state['outcome'] == 'in_progress':
            kind = 'CancelledError' if isinstance(exc, asyncio.CancelledError) else failure_diagnostic(exc, 'meeting_join')['exception_type']
            self._join_state['exception_type'] = kind
            self._join_state['errors'] = (self._join_state['errors'] +
                [{'phase': self._join_state['phase'], 'action': action, 'exception_type': kind}])[-8:]
            self._join_action(action, 'error')

    async def _join_event(self, kind, value):
        await self.event(kind, {**value, **({'join': self.join_diagnostic()} if self._join_state is not None else {})})

    async def open(self, url=None, storage_state=None):
        self._join_state = teams_join_diagnostic({'phase': 'context_setup', 'outcome': 'in_progress'}) if url and meeting_platform(url) == 'teams' else None
        try:
            await self._open(url, storage_state)
        except (Exception, asyncio.CancelledError) as exc:
            if self._join_state is not None:
                await self._join_failed(exc)
            raise

    @staticmethod
    def _map_audio_packet(data, clock):
        import math
        mapped = dict(data)
        for field in ("source_clock", "source_started_at", "source_ended_at", "source_document"):
            mapped.pop(field, None)
        if not clock or data.get("capture_clock_id") != clock["id"]:
            return mapped
        mapped["source_document"] = clock["document"]
        if clock["offset"] is None:
            return mapped
        start, end = data.get("capture_start_ms"), data.get("capture_end_ms")
        frames, rate = data.get("frames"), data.get("sample_rate")
        if (type(start) in {int, float} and type(end) in {int, float}
                and math.isfinite(start) and math.isfinite(end) and 0 <= start <= end
                and type(frames) is int and 1 <= frames <= 2048 and rate == 24000
                and abs((end-start)/1000-frames/rate) <= .001
                and clock["offset"] + end/1000 <= time.monotonic() + .1):
            mapped["source_started_at"] = clock["offset"] + start/1000
            mapped["source_ended_at"] = clock["offset"] + end/1000
            mapped["source_clock"] = "monotonic"
        return mapped

    async def _open(self, url=None, storage_state=None):
        options = {"permissions": [], "locale": "en-US"}
        if storage_state:
            options["storage_state"] = storage_state
        self.context = await self.browser.new_context(**options)
        if self._join_state is not None:
            self._join_state['context_ready'] = True
        self.audio_frame = None
        async def audio_binding(source, data):
            if source.get("context") is not self.context or source.get("page") is not self.page:
                raise RuntimeError("capture_context_mismatch")
            frame = source["frame"]
            if self._native_media.target is not None:
                return
            if frame not in self.page.frames or frame.is_detached():
                raise RuntimeError("capture_context_mismatch")
            if self.audio_frame is None:
                self.audio_frame = frame
            if frame != self.audio_frame and self._native_media.target is None:
                raise RuntimeError("Multiple frame audio sources require an explicit mixer")
            if self.dual_policy and frame is self.dual_frame:
                if not await self.dual_policy.check():
                    raise RuntimeError("owned_sender_policy_rejected")
            else:
                # RX-only remains strict. A dual-mode exception is possible only
                # after the host has bound its exact owned sender policy.
                if not await frame.evaluate("() => !!window.__meeting && window.__meeting.peers.every(pc => pc.getSenders().every(s => !s.track || !['audio','video'].includes(s.track.kind) || !s.track.enabled))"):
                    raise RuntimeError("rx_only_sender_enabled")
            current = await frame.evaluate("() => window.__meeting?.capture_clock_id")
            clock = self.capture_clocks.get(frame)
            if clock and current != clock["id"]:
                self.capture_clocks.pop(frame, None)
                clock = None
            mapped = self._map_audio_packet(data, clock)
            await self.on_audio(mapped)
        await self.context.expose_binding("meetingAudio", audio_binding)
        await self.context.expose_binding("meetingNativeSource", self._native_media.source_available)
        await self.context.expose_binding("meetingNativeAudioFailure", self._native_media.source_failure)
        if self.response_mode:
            await self.context.add_init_script(OWNED_AUDIO_INPUT_JS)
        await self.context.add_init_script("window.__meetingCaptureAudio = " + str(self.capture_audio).lower() + "; window.__meetingCaptureDeferred = " + str(self.defer_capture).lower() + ";\n" + CAPTURE_JS)
        self._join_phase('page_create')
        self.page = await self.context.new_page()
        if self.response_mode:
            from .b_document_policy import DocumentPolicyCollector
            with contextlib.suppress(Exception):
                self._document_policy = DocumentPolicyCollector(self.page)
        if self._join_state is not None:
            self._join_state['page_ready'] = True
        self._join_phase('page_setup')
        if url:
            if meeting_platform(url) == "teams" and (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")):
                await self.context.route(re.compile(
                    r"^https://(?:teams\.microsoft\.com|teams\.cloud\.microsoft)/api/authsvc/v1\.0/authz/visitor(?:\?|$)"),
                    self._teams_visitor_auth)
            self._join_phase('goto')
            if self._join_state is not None:
                self._join_state['goto_started'] = True
            await self.page.goto(browser_join_url(url), wait_until="domcontentloaded", timeout=45000)
            if self._join_state is not None:
                self._join_state['goto_completed'] = True
        self._join_phase('page_ready')

    async def _join_failed(self, exc):
        self._join_error(None, exc)
        self._join_state['post_join_ui'] = self.join_diagnostic()['post_join_ui']
        self._post_join_observation = None  # Freeze the failure-time sample/freshness without DOM reads.
        self._join_state['outcome'] = ('cancelled' if isinstance(exc, asyncio.CancelledError) else
            'stopped' if isinstance(exc, InterruptedError) else
            'timeout' if self._join_state['exception_type'] == 'TimeoutError' else 'error')
        with contextlib.suppress(Exception):
            await self.event('meeting_join_diagnostics',
                {'browser': [], 'browser_snapshot': 'not_collected', 'join': self.join_diagnostic()})

    async def _teams_visitor_auth(self, route):
        request = route.request
        if request.method == "POST" and not request.post_data_buffer:
            # This guest-auth endpoint accepts an empty JSON object. The cloud
            # proxy/upstream returns 411 for its original zero-length POST.
            # Preserve URL, cookies, auth headers, proxy and TLS verification.
            headers = await request.all_headers()
            headers["content-length"] = "2"
            await self.event("teams_proxy_auth_compat", {"empty_post_as_json": True})
            await route.fallback(headers=headers, post_data=b"{}")
        else:
            await route.fallback()

    async def _button(self, pattern, timeout=3000, *, join_action=None, action_guard=None):
        # Meeting controls may live in a same- or cross-origin frame.
        self._join_action(join_action, 'inspecting')
        failed = False
        for frame in self.page.frames:
            locator = frame.get_by_role("button", name=re.compile(pattern, re.I)).first
            try:
                visible = await locator.is_visible()
                self._join_control(join_action, visible)
                if visible:
                    if action_guard is not None:
                        action_guard(frame)  # Last visibility await has completed.
                    self._join_action(join_action, 'dispatching')
                    await locator.click(timeout=timeout)
                    self._join_action(join_action, 'completed')
                    return True
            except Exception as exc:
                if action_guard is not None and isinstance(exc,(InterruptedError,TimeoutError,ResponseUIContextError)):
                    raise
                failed = True
                if join_action is not None:
                    self._join_error(join_action, exc)
                if (join_action == 'join' and self.response_mode and self._join_state
                        and self._join_state.get('join_dispatch_started')):
                    raise  # An uncertain dispatch cannot safely be repeated.
                continue
        self._join_action(join_action, 'error' if failed else 'not_visible')
        return False

    async def _fill(self, patterns, name):
        self._join_action('name', 'inspecting')
        failed = False
        for frame in self.page.frames:
            for pattern in patterns:
                for locator in [frame.get_by_role("textbox", name=re.compile(pattern, re.I)).first,
                                frame.get_by_placeholder(re.compile(pattern, re.I)).first]:
                    try:
                        visible = await locator.is_visible()
                        self._join_control('name', visible)
                        if visible:
                            self._join_action('name', 'dispatching')
                            await locator.fill(name, timeout=1000)
                            self._join_action('name', 'completed')
                            return True
                    except Exception as exc:
                        failed = True
                        self._join_error('name', exc)
                        continue
        self._join_action('name', 'error' if failed else 'not_visible')
        return False

    async def _mute_controls(self):
        self._join_phase('mute_controls')
        # Toggle only controls explicitly labelled as currently on.
        for pattern in [r"^(?:mute microphone|turn (?:camera|microphone) off|turn off (?:camera|microphone)|麥克風關閉|關閉麥克風|關閉攝影機)$"]:
            await self._button(pattern, join_action='mute')
        for frame in self.page.frames:
            try:
                for label in [r"microphone|麥克風", r"camera|攝影機"]:
                    switches = frame.get_by_role("switch", name=re.compile(label, re.I))
                    for index in range(await switches.count()):
                        control = switches.nth(index)
                        visible = await control.is_visible()
                        self._join_control('mute', visible)
                        if visible and await control.get_attribute("aria-checked") == "true":
                            self._join_action('mute', 'dispatching')
                            await control.click()
                            self._join_action('mute', 'completed')
            except Exception as exc:
                self._join_error('mute', exc)
                if not frame.is_detached():
                    raise

    def _response_invalidation(self, frame, roster):
        closed=self.page.is_closed()
        detached=frame is not None and frame.is_detached()
        member=frame is not None and frame in self.page.frames
        replaced=self._response_join_page is not None and self.page is not self._response_join_page
        changed=roster is not None and tuple(self.page.frames)!=roster
        reason=('page_closed' if closed else 'page_replaced' if replaced else
            'frame_detached' if detached else 'frame_removed' if frame is not None and not member else
            'roster_changed' if changed else 'none')
        return reason,dict(page_closed=closed,frame_checked=frame is not None,
            frame_detached=detached,frame_member=member)

    def _response_action_guard(self, deadline, stopped, *, frame=None, roster=None, stage='join_dispatch'):
        receipt=(self._join_state or {}).get('computer_audio_wait')
        if stopped():
            if isinstance(receipt,dict):receipt['wait_outcome']='stopped'
            raise InterruptedError('response_ui_action_stopped')
        if time.monotonic()>=deadline:
            if isinstance(receipt,dict):receipt['wait_outcome']='deadline'
            raise TimeoutError('response_ui_action_deadline')
        reason,flags=self._response_invalidation(frame,roster)
        if reason!='none':
            if isinstance(receipt,dict):
                receipt.update(flags,invalidation_reason=reason,invalidation_stage=stage,
                    invalidation_recorded=True,classification='context_invalid',_ui_invalidated=True)
            raise ResponseUIContextError('response_ui_context_invalid')

    def _response_read_guard(self, deadline, stopped, *, frame=None, roster, stage):
        self._response_action_guard(deadline,stopped,stage=stage)
        reason,flags=self._response_invalidation(frame,roster)
        if reason=='none':return
        receipt=(self._join_state or {}).get('computer_audio_wait')
        if isinstance(receipt,dict):
            receipt.update(flags,invalidation_reason=reason,invalidation_stage=stage,invalidation_recorded=True,_ui_invalidated=True)
        if self._response_radio_dispatched or (self._join_state or {}).get('join_dispatch_started'):
            if isinstance(receipt,dict):receipt['classification']='context_invalid'
            raise ResponseUIContextError('response_ui_context_invalid')
        raise ResponseUIFrameSnapshotError('response_ui_frame_snapshot_discarded')

    async def _response_radio_scan(self, deadline, stopped, roster):
        visible=[];exact=0
        for frame in roster:
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='primary_scan')
            try:
                radios=frame.get_by_role('radio',name=re.compile(r'^(?:computer audio|電腦音訊)$',re.I))
                count=await radios.count()
                self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='primary_scan')
                exact+=count;self._join_control('computer_audio',False)
                if count>16:raise RuntimeError('response_computer_audio_ambiguous')
                for index in range(count):
                    radio=radios.nth(index);shown=await radio.is_visible()
                    self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='primary_scan')
                    self._join_control('computer_audio',shown)
                    if shown:visible.append((radio,frame))
            except PlaywrightError:
                self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='primary_scan')
                raise
        self._response_read_guard(deadline,stopped,roster=roster,stage='primary_scan')
        return visible,exact

    async def _response_context_counts(self, receipt, deadline, stopped, *, roster=None):
        """Commit optional counts atomically; only live-frame SDK errors recover."""
        roster=tuple(self.page.frames) if roster is None else roster
        observed_start=time.monotonic()
        receipt['context_counts_checked']=False
        receipt['observation_error']=False
        queries=[('all_radio_count','radio',None),('dialog_count','dialog',None),
            ('known_browser_button_count','button',re.compile(
                r'^(?:continue on this browser|join on the web|join from your browser|continue in this browser|在此瀏覽器中繼續|繼續使用此瀏覽器)$',re.I)),
            ('known_join_button_count','button',re.compile(r'^(?:join now|join meeting|join|立即加入|加入會議|加入)$',re.I))]
        counts={key:0 for key,_,_ in queries}
        frame=None; observation='none'
        try:
            for frame in roster:
                for key,role,name in queries:
                    self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
                    observation=key
                    locator=frame.get_by_role(role,name=name) if name is not None else frame.get_by_role(role)
                    counts[key]+=await locator.count()
                    self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
            frame=None;observation='document_state'
            self._response_read_guard(deadline,stopped,roster=roster,stage='auxiliary_scan')
            state=await self.page.evaluate('() => document.readyState')
            state=state if isinstance(state,str) and state in {'loading','interactive','complete'} else 'unknown'
            self._response_read_guard(deadline,stopped,roster=roster,stage='auxiliary_scan')
        except PlaywrightError as exc:
            receipt['observation_error']=True
            receipt['auxiliary_error_kind']=('target_closed' if type(exc).__name__=='TargetClosedError' else
                'timeout' if isinstance(exc,PlaywrightTimeoutError) else 'playwright_error')
            receipt['auxiliary_exception_type']=type(exc).__name__ if type(exc).__name__ in {'Error','TimeoutError','TargetClosedError'} else 'Error'
            receipt['auxiliary_error_observation']=observation
            receipt['auxiliary_error_count']=min(65535,receipt.get('auxiliary_error_count',0)+1)
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
            if type(exc).__name__=='TargetClosedError':raise
            return  # No partial counts replace the previous complete observation.
        observed_end=time.monotonic()
        receipt.update(counts,document_state=state,context_counts_checked=True,
            _context_sample_at=observed_end,
            _context_duration_ms=min(900000,max(0,int((observed_end-observed_start)*1000))))
        # Extra UI metadata is read-only, sampled at most twice/second, and
        # always inside the original admission/stop/frame guards.
        snapshot=receipt.get('ui_snapshot')
        last=snapshot.get('_sampled_at') if isinstance(snapshot,dict) else None
        if last is None or receipt.get('_ui_invalidated') or observed_end-last>=.5:
            await self._response_ui_snapshot(receipt,deadline,stopped,roster)

    async def _response_ui_snapshot(self,receipt,deadline,stopped,roster,*,read_guard=None,meeting_controls=False):
        guard=read_guard or self._response_read_guard
        started=time.monotonic();receipt['_ui_attempt_incomplete']=True
        labels={
            'computer_audio_radio':('radio',('Computer audio','電腦音訊')),
            'browser_continue_button':('button',('Continue on this browser','Continue in this browser',
                'Join on the web','Join from your browser','Join in browser','Use this browser',
                'Continue in browser','在此瀏覽器中繼續','繼續使用此瀏覽器')),
            'browser_continue_link':('link',('Continue on this browser','Continue in this browser',
                'Join on the web','Join from your browser','Join in browser','Use this browser',
                'Continue in browser','在此瀏覽器中繼續','繼續使用此瀏覽器')),
            'join_button':('button',('Join now','Join meeting','Join','立即加入','加入會議','加入')),
            'guest_entry_button':('button',('Join without signing in','Join as a guest','Join as guest')),
            'guest_entry_link':('link',('Join without signing in','Join as a guest','Join as guest')),
            'sign_in_button':('button',('Sign in','Log in','Sign in to join','登入','登录')),
            'sign_in_link':('link',('Sign in','Log in','Sign in to join','登入','登录')),
            'unsupported_message':('text',("This browser isn't supported",'This browser is not supported',
                "Your browser isn't supported",'Your browser is not supported','Unsupported browser',
                'Try a different browser','Use a different browser','此瀏覽器不受支援','不支援此瀏覽器')),
            'lobby_message':('text',_TEAMS_LOBBY_MESSAGES),
            'ended_message':('text',('Meeting has ended','This meeting has ended','會議已結束')),
            'error_message':('text',("Sorry, we couldn't connect you",'Sorry, we couldn’t connect you',
                'Something went wrong',"We couldn't join the meeting",'We couldn’t join the meeting',
                'Meeting not found','This meeting link is invalid','This meeting link has expired')),
            'dismiss_button':('button',('OK','Ok','Close','Dismiss','Got it','Cancel','Retry','Try again')),
            'audio_fallback_button':('button',('Continue without audio or video',"Don't use audio",
                'Do not use audio','Join without audio','不使用音訊')),
            'continue_without_av_button':('button',('Continue without audio or video',)),
            'dont_use_audio_button':('button',("Don't use audio",'Do not use audio','不使用音訊')),
            'join_without_audio_button':('button',('Join without audio',)),
            'device_preview_dialog_message':('dialog_text',('camera and microphone','microphone and camera','mic and camera','camera and mic',
                'access your camera','access your microphone','microphone access','camera access',
                'no microphone found','no audio devices found','microphone permissions')),
            'app_open_button':('button',('Open Microsoft Teams','Open Teams','Launch it now',
                'Get the Teams app','Download the Teams app')),
            'auth_error_message':('text',("We couldn't sign you in",'We couldn’t sign you in',
                "We can't sign you in",'We can’t sign you in',"You don't have permission",
                'You do not have permission','Sign in to join','Only people with access',
                'Sign in with your work','You need to sign in')),
            'meeting_missing_message':('text',("This meeting doesn't exist","We couldn't find this meeting",
                "We can't find this meeting",'Meeting not found','This meeting link is invalid',
                'This meeting link has expired','The meeting has expired')),
            'media_blocked_message':('text',('Microphone access is blocked','Allow Teams to use your microphone',
                'Allow microphone access','No audio devices found',"We couldn't find a microphone",
                'No microphone found','Camera access is blocked')),
            'cookies_blocked_message':('text',('Allow third-party cookies','Enable third-party cookies',
                'Cookies are blocked','Third-party cookies are disabled'))}
        if meeting_controls:
            labels.update(leave_button=('button',('Leave','Leave meeting','Hang up','離開','退出','掛斷')),
                mute_button=('button',('Mute','Unmute','Mute microphone','Unmute microphone','靜音','取消靜音')))
        roles={key:0 for key in ('radio','button','link','textbox','heading','dialog')}
        names={key:0 for key in labels};frame=None;text_supported=True
        try:
            for frame in roster:
                for role in roles:
                    guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
                    roles[role]+=await frame.get_by_role(role).count()
                    guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
                for key,(role,safe_names) in labels.items():
                    guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
                    fixed='(?:'+('|'.join(re.escape(name) for name in safe_names))+')'
                    # Text classification records fixed phrase counters only,
                    # never the complete message or its dynamic suffix.
                    pattern=re.compile(fixed if role in {'text','dialog_text'} else '^'+fixed+'$',re.I)
                    if role in {'text','dialog_text'}:
                        target=frame.get_by_role('dialog') if role=='dialog_text' else frame
                        lookup=getattr(target,'get_by_text',None)
                        if not callable(lookup):text_supported=False;continue
                        locator=lookup(pattern)
                    else:locator=frame.get_by_role(role,name=pattern)
                    count=await locator.count()
                    if role in {'text','dialog_text'}:
                        # Hidden help/template text cannot label the current UI.
                        for index in range(min(count,16)):
                            guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
                            visible=await locator.nth(index).is_visible()
                            guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
                            names[key]+=int(visible)
                    else:names[key]+=count
                    guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
            guard(deadline,stopped,roster=roster,stage='auxiliary_scan')
        except PlaywrightError as exc:
            receipt['_ui_latest_error']=('target_closed' if type(exc).__name__=='TargetClosedError' else
                'timeout' if isinstance(exc,PlaywrightTimeoutError) else 'playwright_error')
            guard(deadline,stopped,frame=frame,roster=roster,stage='auxiliary_scan')
            if type(exc).__name__=='TargetClosedError':raise
            return  # No partial UI data or private error message is published.
        categories=[]
        rules={'prejoin':('computer_audio_radio','join_button'),
            'browser_choice':('browser_continue_button','browser_continue_link'),
            'guest_entry':('guest_entry_button','guest_entry_link'),
            'login_prompt':('sign_in_button','sign_in_link'),'unsupported_browser':('unsupported_message',),
            'admission_lobby':('lobby_message',),'meeting_ended':('ended_message',),'error_message':('error_message',),
            'auth_error':('auth_error_message',),'meeting_unavailable':('meeting_missing_message',),
            'media_permission_or_device_message':('media_blocked_message',),
            'cookies_message':('cookies_blocked_message',),
            'app_choice':('app_open_button',),'audio_fallback':('audio_fallback_button',)}
        if meeting_controls:rules['meeting_controls_candidate']=('leave_button',)
        for kind,keys in rules.items():
            if any(names[key]>0 for key in keys):categories.append(kind)
        unknown={role:max(0,roles[role]-sum(names[key] for key,(kind,_) in labels.items() if kind==role and key not in {'continue_without_av_button','dont_use_audio_button','join_without_audio_button'}))
            for role in ('radio','button','link')}
        host=urlsplit(getattr(self.page,'url','')).hostname
        origin=('teams' if host in {'teams.microsoft.com','teams.cloud.microsoft','teams.live.com'} else
            'microsoft_identity' if host=='login.microsoftonline.com' else
            'browser_error' if getattr(self.page,'url','').startswith('chrome-error:') else
            'other' if host else 'unknown')
        previous=receipt.get('ui_snapshot');sequence=previous.get('sample_sequence',0) if isinstance(previous,dict) else 0
        finished=time.monotonic()
        receipt['ui_snapshot']={'category':categories[0] if len(categories)==1 else
            'multiple_known_categories' if categories else 'unknown',
            'category_candidates':categories,'origin_kind':origin,'role_counts':roles,
            'safe_name_counts':names,'unknown_name_counts':unknown,'text_lookup_supported':text_supported,
            'sample_sequence':min(65535,sequence+1),'_sampled_at':finished,
            'observation_duration_ms':min(900000,max(0,int((finished-started)*1000)))}
        receipt.update(_ui_invalidated=False,_ui_attempt_incomplete=False,_ui_latest_error='none')


    async def _response_preview_navigation(self, deadline, stopped, roster):
        """Dismiss only the observed device-preview interstitial, once.

        This is prejoin navigation, never evidence of a selected audio mode.
        Every condition is freshly read under the original deadline/roster.
        """
        if self._response_preview_dispatched:
            return False
        if (self._join_state or {}).get('join_dispatch_started'):
            return False
        self._response_read_guard(deadline,stopped,roster=roster,stage='preview_scan')
        if not _is_strict_teams_origin(getattr(self.page,'url','')):
            return False
        label=re.compile(r'^Continue without audio or video$',re.I)
        join_label=re.compile(r'^(?:join now|join meeting|join|立即加入|加入會議|加入)$',re.I)
        device_label=re.compile(r'(?:mic(?:rophone)? and camera|camera and mic(?:rophone)?)',re.I)
        blocker_label=re.compile(
            '(?:' + '|'.join(re.escape(m) for m in _TEAMS_STANDALONE_BLOCKER_MESSAGES) + ')',
            re.I,
        )
        dialogs=[];global_buttons=0;button_frame=None;background_controls=False
        for frame in roster:
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
            # Background controls are safe only behind a proven modal dialog.
            for role,name in [('radio',None),('button',join_label)]:
                locator=frame.get_by_role(role,name=name) if name is not None else frame.get_by_role(role)
                count=await locator.count()
                self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
                background_controls=background_controls or bool(count)
            count=await frame.get_by_role('button',name=label).count()
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
            if count:
                button_frame=frame
            global_buttons+=count
            if global_buttons>1:return False
            matches=frame.get_by_role('dialog')
            count=await matches.count()
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
            if count>16:return False
            for index in range(count):
                dialog=matches.nth(index)
                shown=await dialog.is_visible()
                self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
                if shown:dialogs.append((dialog,frame))
                if len(dialogs)>1:return False
        if global_buttons!=1:return False
        if len(dialogs)==1:
            dialog,frame=dialogs[0]
            if not hasattr(dialog,'get_by_text') or not hasattr(dialog,'get_by_role'):return False
            if background_controls:
                modal=await dialog.get_attribute('aria-modal')
                self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
                if modal!='true':return False
            for role,name in [('radio',None),('button',join_label)]:
                controls=dialog.get_by_role(role,name=name) if name is not None else dialog.get_by_role(role)
                count=await controls.count()
                self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
                if count:return False
            message=dialog.get_by_text(device_label)
            count=await message.count()
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
            if not 1<=count<=16:return False
            shown_message=False
            for index in range(count):
                shown=await message.nth(index).is_visible()
                self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
                shown_message=shown_message or shown
            if not shown_message:return False
            matches=dialog.get_by_role('button',name=label)
            count=await matches.count()
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
            if count!=1:return False
            button=matches.nth(0)
        elif len(dialogs)==0:
            # Real Teams Web can render the device-permission interstitial as a
            # standalone full-page view without ARIA role="dialog": visible
            # heading + sole "Continue without audio or video" button, with no
            # other buttons, radios, textboxes, switches, links, or blockers.
            if background_controls or button_frame is None:
                return False
            total_buttons=0
            for scan_frame in roster:
                self._response_read_guard(deadline,stopped,frame=scan_frame,roster=roster,stage='preview_scan')
                for role in ('textbox','switch','link'):
                    count=await scan_frame.get_by_role(role).count()
                    self._response_read_guard(deadline,stopped,frame=scan_frame,roster=roster,stage='preview_scan')
                    if count:return False
                bcount=await scan_frame.get_by_role('button').count()
                self._response_read_guard(deadline,stopped,frame=scan_frame,roster=roster,stage='preview_scan')
                total_buttons+=bcount
                if total_buttons>1:return False
            if total_buttons!=1:return False
            frame=button_frame
            if not _is_strict_teams_origin(getattr(frame,'url','')):return False
            headings=frame.get_by_role('heading')
            hcount=await headings.count()
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
            if not 1<=hcount<=4:return False
            shown_heading=False
            for index in range(hcount):
                shown=await headings.nth(index).is_visible()
                self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
                shown_heading=shown_heading or shown
            if not shown_heading:return False
            lookup=getattr(frame,'get_by_text',None)
            if not callable(lookup):return False
            blockers=lookup(blocker_label)
            bmsg_count=await blockers.count()
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
            if bmsg_count>16:return False
            for index in range(bmsg_count):
                if await blockers.nth(index).is_visible():
                    self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
                    return False
                self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
            matches=frame.get_by_role('button',name=label)
            count=await matches.count()
            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
            if count!=1:return False
            button=matches.nth(0)
        else:
            return False
        if not _is_strict_teams_origin(getattr(frame,'url','')):return False
        shown=await button.is_visible()
        self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
        enabled=await button.is_enabled()
        self._response_action_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_dispatch')
        if not _is_strict_teams_origin(getattr(self.page,'url','')) or not _is_strict_teams_origin(getattr(frame,'url','')):
            return False
        if not shown or not enabled:return False
        self._join_phase('preview_navigation')
        self._join_control('audio_continue',True)
        self._response_preview_dispatched=True  # An uncertain click is never reissued.
        self._response_computer_audio_verified=False
        self._join_state['preview_navigation_dispatched']=True
        self._join_action('audio_continue','dispatching')
        try:
            await button.click(timeout=min(1000,max(1,int((deadline-time.monotonic())*1000))))
            # Navigation may replace frames; the next full radio scan uses a
            # fresh roster. Page replacement/closure/stop/deadline remain fatal.
            self._response_action_guard(deadline,stopped,stage='preview_return')
        except BaseException as exc:
            self._join_error('audio_continue',exc)
            raise
        self._join_state['preview_navigation_completed']=True
        self._join_action('audio_continue','completed')
        self._join_phase('audio_choice')
        return True

    async def _response_radio_selected(self, radio, deadline, stopped, frame, roster):
        """Read explicit ARIA or a proven enabled native HTML radio."""
        self._response_action_guard(deadline,stopped,frame=frame,roster=roster,stage='radio_readback')
        checked=await radio.get_attribute('aria-checked')
        self._response_action_guard(deadline,stopped,frame=frame,roster=roster,stage='radio_readback')
        if checked is not None:
            return checked=='true'
        native=await radio.evaluate("el => el.matches('input[type=radio]')")
        self._response_action_guard(deadline,stopped,frame=frame,roster=roster,stage='radio_readback')
        if native is not True:return False
        enabled=await radio.is_enabled()
        self._response_action_guard(deadline,stopped,frame=frame,roster=roster,stage='radio_readback')
        if enabled is not True:return False
        selected=await radio.is_checked()
        self._response_action_guard(deadline,stopped,frame=frame,roster=roster,stage='radio_readback')
        return selected is True

    async def _response_computer_audio(self, *, select, deadline, stopped):
        """Only missing prejoin controls may wait, inside the same join deadline."""
        self._response_computer_audio_verified = False
        self._join_phase('audio_choice')
        previous=(self._join_state or {}).get('computer_audio_wait')
        receipt=dict(previous) if not select and isinstance(previous,dict) else {
            'wait_outcome':'not_waited','poll_count':0,'document_state':'unknown','context_counts_checked':False}
        receipt.update(stage='select' if select else 'verify',classification='scanning',
            browser_continue_completed=(self._join_state['actions'].get('browser_continue')=='completed'))
        receipt['rescan_count']=self._response_rescan_count
        self._join_state['computer_audio_wait']=receipt
        try:
            self._join_phase('audio_choice')
            while True:
                if stopped():
                    receipt['wait_outcome']='stopped'
                    raise InterruptedError('response_computer_audio_stopped')
                if time.monotonic() >= deadline:
                    receipt['wait_outcome']='deadline'
                    raise TimeoutError('response_computer_audio_missing_deadline')
                receipt['poll_count']=min(65535,receipt['poll_count']+1)
                roster=tuple(self.page.frames)
                try:
                    try:
                        navigated = select and await self._response_preview_navigation(deadline,stopped,roster)
                    except PlaywrightError:
                        if self._response_preview_dispatched:
                            raise  # Never recover an uncertain navigation click.
                        for frame in roster:
                            self._response_read_guard(deadline,stopped,frame=frame,roster=roster,stage='preview_scan')
                        raise  # A live, unchanged frame's SDK error remains fatal.
                    if navigated:
                        continue  # Fresh radio uniqueness/selection/readback remains mandatory.
                    await self._mute_controls()
                    self._join_phase('audio_choice')
                    visible,exact=await self._response_radio_scan(deadline,stopped,roster)
                    receipt.update(frame_count=len(roster),exact_radio_count=exact,
                        visible_radio_count=len(visible),primary_scan_complete=True,_primary_sample_at=time.monotonic())
                    if len(visible)>1:
                        receipt['classification']='ambiguous'
                        raise RuntimeError('response_computer_audio_ambiguous')
                    if visible:
                        receipt['classification']='visible'
                        if receipt['wait_outcome']=='waiting':receipt['wait_outcome']='became_visible'
                        break
                    receipt['classification']='radio_hidden' if exact else 'radio_absent'
                    receipt['wait_outcome']='waiting'
                    receipt['browser_continue_completed']=(self._join_state['actions'].get('browser_continue')=='completed')
                    await self._response_context_counts(receipt,deadline,stopped,roster=roster)
                except ResponseUIFrameSnapshotError:
                    # Drop the ENTIRE scan: no previous candidate or partial count
                    # can establish uniqueness in the replacement roster.
                    visible=[];roster=None
                    self._response_rescan_count=min(65535,self._response_rescan_count+1)
                    receipt.update(rescan_count=self._response_rescan_count,primary_scan_complete=False,
                        exact_radio_count=0,visible_radio_count=0,context_counts_checked=False,
                        document_state='unknown',classification='frame_snapshot_discarded',wait_outcome='waiting')
                    for key in ('all_radio_count','dialog_count','known_browser_button_count','known_join_button_count'):
                        receipt.pop(key,None)
                    self._response_action_guard(deadline,stopped,stage='primary_scan')
                remaining=deadline-time.monotonic()
                if remaining<=0:
                    receipt['wait_outcome']='deadline'
                    raise TimeoutError('response_computer_audio_missing_deadline')
                await asyncio.sleep(min(.05,remaining))
            radio,selected_frame=visible[0]
            if stopped():
                receipt['wait_outcome']='stopped'
                raise InterruptedError('response_computer_audio_stopped')
            if time.monotonic()>=deadline:
                receipt['wait_outcome']='deadline'
                raise TimeoutError('response_computer_audio_missing_deadline')
            if select:
                checked=await self._response_radio_selected(radio,deadline,stopped,selected_frame,roster)
                if not checked:
                    self._response_action_guard(deadline,stopped,frame=selected_frame,roster=roster,stage='radio_dispatch')
                    self._response_radio_dispatched=True
                    self._join_action('computer_audio','dispatching')
                    await radio.click(timeout=min(1000,max(1,int((deadline-time.monotonic())*1000))))
            if select and await self._response_preview_navigation(deadline,stopped,tuple(self.page.frames)):
                # Dismissing a late overlay may change the underlying audio mode.
                return await self._response_computer_audio(select=True,deadline=deadline,stopped=stopped)
            shown=await radio.is_visible()
            self._response_action_guard(deadline,stopped,frame=selected_frame,roster=roster,stage='radio_readback')
            checked=await self._response_radio_selected(radio,deadline,stopped,selected_frame,roster) if shown else False
            if not shown or not checked:
                receipt['classification']='not_selected'
                raise RuntimeError('response_computer_audio_not_selected')
            await self._mute_controls()
            self._join_phase('audio_choice')
            shown=await radio.is_visible()
            self._response_action_guard(deadline,stopped,frame=selected_frame,roster=roster,stage='radio_readback')
            checked=await self._response_radio_selected(radio,deadline,stopped,selected_frame,roster) if shown else False
            if not shown or not checked:
                receipt['classification']='not_selected'
                raise RuntimeError('response_computer_audio_not_selected')
            self._response_action_guard(deadline,stopped,frame=selected_frame,roster=roster,stage='verify_return')
            self._response_verified_roster=roster
            receipt['classification']='checked'
            self._response_computer_audio_verified=True
            self._join_action('computer_audio','completed')
        except asyncio.CancelledError:
            if receipt['wait_outcome'] not in {'deadline','stopped'}:receipt['wait_outcome']='cancelled'
            raise
        except Exception as exc:
            if receipt['wait_outcome'] not in {'deadline','stopped'}:receipt['wait_outcome']='error'
            self._join_error('computer_audio',exc)
            raise

    async def _post_join_ui_snapshot(self, deadline, stopped):
        """Optional read-only sample; never changes join decisions or the deadline."""
        previous = self._post_join_observation or {}
        attempts = min(65535, previous.get('observation_attempts', 0) + 1)
        observation = dict(snapshot=None, unavailable_reason='pending', observation_attempts=attempts)
        self._post_join_observation = observation
        page = self.page
        roster = None
        def guard(_deadline, _stopped, *, frame=None, roster=None, stage=None):
            if stopped():
                raise PostJoinObservationUnavailable('stopped')
            if time.monotonic() >= deadline:
                raise PostJoinObservationUnavailable('deadline')
            if page is None or self.page is not page:
                raise PostJoinObservationUnavailable('context_changed')
            closed = getattr(page, 'is_closed', None)
            if callable(closed) and closed():
                raise PostJoinObservationUnavailable('target_closed')
            if roster is not None and tuple(page.frames) != roster:
                raise PostJoinObservationUnavailable('context_changed')
            if frame is not None and (frame not in page.frames or frame.is_detached()):
                raise PostJoinObservationUnavailable('context_changed')
        receipt = {}
        try:
            guard(deadline, stopped)
            roster = tuple(page.frames)
            guard(deadline, stopped, roster=roster)
            if not roster:
                raise PostJoinObservationUnavailable('context_changed')
            await asyncio.wait_for(self._response_ui_snapshot(receipt, deadline, stopped, roster,
                read_guard=guard, meeting_controls=True), min(.15, deadline-time.monotonic()))
            guard(deadline, stopped, roster=roster)
            if receipt.get('_ui_attempt_incomplete') or not isinstance(receipt.get('ui_snapshot'), dict):
                observation['unavailable_reason'] = receipt.get('_ui_latest_error', 'observation_failed')
            else:
                observation.update(snapshot=receipt['ui_snapshot'], unavailable_reason='none')
        except PostJoinObservationUnavailable as exc:
            observation['unavailable_reason'] = str(exc)
        except asyncio.CancelledError:
            observation['unavailable_reason'] = 'cancelled'
            raise
        except asyncio.TimeoutError:
            observation['unavailable_reason'] = 'timeout'
        except PlaywrightError as exc:
            observation['unavailable_reason'] = 'target_closed' if type(exc).__name__ == 'TargetClosedError' else 'playwright_error'
        except Exception:
            observation['unavailable_reason'] = 'observation_failed'

    async def join(self, platform, name, timeout, stopped):
        self._join_platform = platform
        self._response_computer_audio_verified = False
        self._post_join_observation = None
        self._response_join_page=self.page if self.response_mode else None
        self._response_radio_dispatched=False
        self._response_preview_dispatched=False
        self._response_rescan_count=0
        self._response_verified_roster=None
        if platform != 'teams':
            self._join_state = None
        elif self._join_state is None or self._join_state['outcome'] != 'in_progress':
            self._join_state = teams_join_diagnostic({'phase': 'starting', 'outcome': 'in_progress'})
        self._join_phase('starting')
        # Browser locators can stall during redirects; keep admission timeout
        # and stop responsive even while an individual UI operation is pending.
        self._response_join_started_at=time.monotonic()
        deadline = self._response_join_started_at + timeout
        task = asyncio.create_task(self._join(platform, name, timeout, stopped, deadline=deadline))
        try:
            while True:
                if stopped():
                    raise InterruptedError("Stopped while joining")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("Meeting admission timed out; waiting room, login, or changed join UI")
                try:
                    return await asyncio.wait_for(asyncio.shield(task), min(.5, remaining))
                except asyncio.TimeoutError:
                    if task.done():
                        return await task
        except (Exception, asyncio.CancelledError) as exc:
            receipt=(self._join_state or {}).get('computer_audio_wait')
            if (isinstance(receipt,dict) and receipt.get('classification')!='checked'
                    and receipt.get('wait_outcome') not in {'deadline','stopped','cancelled','error'}):
                receipt['wait_outcome']=('deadline' if isinstance(exc,TimeoutError) else
                    'stopped' if isinstance(exc,InterruptedError) else
                    'cancelled' if isinstance(exc,asyncio.CancelledError) else 'error')
            # Snapshot already-observed state. Do not fetch DOM or let media
            # diagnostics extend the admission deadline or replace its error.
            if not task.done():
                task.cancel()
            if self._join_state is not None:
                await self._join_failed(exc)
            else:
                await self.event('meeting_join_diagnostics', {'browser': await self.stats()})
            raise
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _join(self, platform, name, timeout, stopped, *, deadline=None):
        deadline = time.monotonic() + timeout if deadline is None else deadline
        submitted = False
        lobby_reported = False
        await self._join_event("joining", {"platform": platform})
        while time.monotonic() < deadline:
            if stopped():
                raise InterruptedError("Stopped while joining")
            self._join_phase('body_read')
            state = await self._meeting_ui_state()
            if self._join_state is not None:
                self._join_state['page_state'] = state
            if state == 'ended':
                raise RuntimeError("Meeting ended or access was refused")
            if state == 'connect_failed':
                raise RuntimeError("Teams could not connect the call; verify media connectivity and audio-device availability")
            if state == 'login_required':
                raise RuntimeError("Meeting requires account login; provide an operator-created --storage-state")
            lobby = state == 'lobby'
            if lobby and not lobby_reported:
                lobby_reported = True
                self._join_phase('lobby_wait')
                await self._join_event("waiting_for_admission", {})
            # Leave can also appear in a lobby. Confirm the UI is past it.
            self._join_phase('admission_check')
            if state == 'joined':
                if platform == 'teams' and self.response_mode and not self._response_computer_audio_verified:
                    raise RuntimeError('response_computer_audio_not_verified')
                await self._mute_controls()
                if platform == "zoom":
                    await self._button(r"^(?:join audio|join audio by computer|join with computer audio)$")
                self._join_phase('joined')
                if self._join_state is not None:
                    self._join_state.update(page_state='joined', outcome='joined')
                await self._join_event("joined", {"platform": platform})
                return
            if not submitted:
                self._join_phase('browser_choice')
                await self._button(r"^(?:continue on this browser|join on the web|join from your browser|continue in this browser|在此瀏覽器中繼續|繼續使用此瀏覽器)$", join_action='browser_continue')
                self._join_phase('audio_choice')
                if platform == 'teams' and self.response_mode:
                    await self._response_computer_audio(select=True,deadline=deadline,stopped=stopped)
                elif platform == "teams":
                    await self._button(r"^(?:continue without audio or video)$", join_action='audio_continue')
                for frame in ([] if platform == 'teams' and self.response_mode else self.page.frames):
                    try:
                        radio = frame.get_by_role("radio", name=re.compile(r"^(?:computer audio|電腦音訊)$", re.I)).first
                        visible = await radio.is_visible()
                        self._join_control('computer_audio', visible)
                        if visible and await radio.get_attribute("aria-checked") != "true":
                            self._join_action('computer_audio', 'dispatching')
                            await radio.click(timeout=1000)
                            self._join_action('computer_audio', 'completed')
                    except Exception as exc:
                        self._join_error('computer_audio', exc)
                        if not frame.is_detached():
                            raise
                self._join_phase('name_fill')
                await self._fill([r"name|姓名|名稱"], name)
                if (platform=='teams' and self.response_mode
                        and await self._response_preview_navigation(deadline,stopped,tuple(self.page.frames))):
                    continue  # Re-read the audio mode after navigation, never reuse its old proof.
                await self._mute_controls()
                if platform == 'teams' and self.response_mode:
                    await self._response_computer_audio(select=False,deadline=deadline,stopped=stopped)
                    self._response_action_guard(deadline,stopped,roster=self._response_verified_roster,stage='verify_return')
                self._join_phase('join_control_scan')
                guard=(lambda frame:self._response_action_guard(deadline,stopped,frame=frame,roster=self._response_verified_roster,stage='join_visibility')) if platform=='teams' and self.response_mode else None
                if await self._button(r"^(?:join now|join meeting|join|立即加入|加入會議|加入)$", join_action='join',action_guard=guard):
                    submitted = True
                    await self._join_event("connecting", {})
            self._join_phase('lobby_wait' if lobby else 'post_join_wait')
            if submitted and self._join_state is not None:
                await self._post_join_ui_snapshot(deadline, stopped)
            await asyncio.sleep(.5)
        raise TimeoutError("Meeting admission timed out; waiting room, login, or changed join UI")

    async def _meeting_status_text(self, roster):
        texts = []
        for frame in roster:
            try:
                regions = frame.locator(_MEETING_STATUS_REGIONS)
                for index in range(await regions.count()):
                    region = regions.nth(index)
                    if not await region.is_visible():
                        continue
                    if await region.locator(_MEETING_SOURCE_ANCESTORS).count():
                        continue
                    if await region.locator(_MEETING_SOURCE_REGIONS).count():
                        text = await region.evaluate(r"""(region, sourceSelector) => {
                            const read = node => {
                                if (node.nodeType === 3) return node.nodeValue || '';
                                if (node.nodeType !== 1 || node.matches(sourceSelector) ||
                                    node.matches('script,style,[hidden],[aria-hidden="true"]')) return '';
                                const style = getComputedStyle(node);
                                if (style.display === 'none' || style.visibility === 'hidden' ||
                                    style.visibility === 'collapse' || style.contentVisibility === 'hidden') return '';
                                return Array.from(node.childNodes, read).join(' ');
                            };
                            return read(region).replace(/\s+/g, ' ');
                        }""", _MEETING_SOURCE_REGIONS)
                    else:
                        text = await region.inner_text(timeout=500)
                    texts.append(text[:20000].lower())
            except Exception:
                if not frame.is_detached():
                    raise
        return "\n".join(texts)

    async def _meeting_ui_state(self):
        self._last_exit_reason = None
        page = self.page
        if page is None or page.is_closed():
            return 'unrecognized'
        roster = tuple(page.frames)
        text = (await self._meeting_status_text(roster) if self._join_platform == 'teams'
                else (await page.locator("body").inner_text())[:20000].lower())
        if self._join_state is not None and self._join_state['outcome'] == 'in_progress':
            self._join_state['body_observed'] = True
        if self.page is not page or tuple(page.frames) != roster:
            return 'unrecognized'
        if any(x in text for x in ["you were removed","you've been removed"]):
            self._last_exit_reason = 'meeting_removed'
            return 'ended'
        if any(x in text for x in ["meeting has ended","會議已結束"]):
            self._last_exit_reason = 'confirmed_end'
            return 'ended'
        if any(x in text for x in ["sorry, we couldn't connect you", "sorry, we couldn’t connect you"]):
            return 'connect_failed'
        if any(x in text for x in ["sign in to join", "only people with access", "sign in with your work", "需要登入"]):
            return 'login_required'
        if any(message in text for message in _TEAMS_LOBBY_MESSAGES):
            return 'lobby'
        connecting = any(x in text for x in ['connecting', '正在連線', '連線中'])
        if connecting and self._join_platform == 'teams':
            return 'connecting'
        for frame in roster:
            try:
                visible = await frame.get_by_role("button", name=re.compile(r"^(?:leave|leave meeting|hang up|離開|退出|掛斷)$", re.I)).first.is_visible()
                self._join_control('leave', visible)
                if visible:
                    corroborated = self._join_platform == 'zoom' or await frame.get_by_role(
                        "button", name=re.compile(
                            r"^(?:participants(?: \(\d+\))?|show participants|people|open participants(?: panel)?|chat|show conversation|與會者|參與者|参与者|聊天)$",
                            re.I)).first.is_visible()
                    if self.page is not page or tuple(page.frames) != roster or frame.is_detached():
                        return 'unrecognized'
                    if corroborated:
                        return 'joined'
            except Exception as exc:
                self._join_error('leave', exc)
                if not frame.is_detached():
                    raise
        return 'connecting' if connecting else 'unrecognized'

    async def admitted(self):
        return await self._meeting_ui_state() == 'joined'

    async def capture_lifecycle(self):
        state = await self._meeting_ui_state()
        return self._last_exit_reason if state == 'ended' else state

    @staticmethod
    def _rx_readiness_report(value):
        value = value if isinstance(value, dict) else {}
        context = value.get("context_state")
        result = {"hook_present": value.get("hook_present") is True,
                  "capture_enabled": value.get("capture_enabled") is True,
                  "context_state": context if context in {"absent", "running", "suspended", "closed", "interrupted"} else "unknown"}
        for key in ("live_attached_tracks", "muted_live_tracks", "pending_live_tracks"):
            count = value.get(key)
            result[key] = min(1000, max(0, count)) if type(count) is int else 0
        allowed = {"audio_context_create_failed", "media_source_attach_failed", "audio_context_resume_failed", "media_playout_failed", "receiver_enumeration_failed", "track_attach_failed"}
        errors = value.get("attach_errors")
        result["attach_errors"] = [e for e in errors[:8] if isinstance(e, str) and e in allowed] if isinstance(errors, list) else []
        result["ready"] = (value.get("ready") is True and result["hook_present"] and result["capture_enabled"]
                           and result["context_state"] == "running" and result["live_attached_tracks"] > 0)
        return result

    async def connect_native_audio(self, target_page):
        await self._native_media.connect(target_page)

    async def disconnect_native_audio(self, target_page):
        await self._native_media.disconnect(target_page)

    async def native_audio_readiness(self):
        return await self._native_media.readiness()

    @property
    def native_audio_failed(self):
        return self._native_media.failed


    async def _start_capture_frame(self, frame, *, deadline):
        """Calibrate and start one actual document within its setup lifecycle."""
        import math
        before = time.monotonic()
        sample = await asyncio.wait_for(frame.evaluate(
            "() => window.__meeting && ({id:__meeting.capture_clock_id,now:performance.now()})"),
            max(.001, deadline-before))
        after = time.monotonic()
        token = sample.get("id") if isinstance(sample, dict) else None
        if isinstance(token, str) and token:
            clock = self.capture_clocks.get(frame)
            if not clock or clock["id"] != token:
                clock = {"id":token, "document":_CaptureDocument(), "offset":None}
                self.capture_clocks[frame] = clock
            now = sample.get("now")
            if (clock["offset"] is None and type(now) in {float, int}
                    and math.isfinite(now) and now >= 0 and after-before <= .1):
                clock["offset"] = (before+after)/2-now/1000
        else:
            self.capture_clocks.pop(frame, None)
        # Bind the recording action to the actually sampled document.
        return await asyncio.wait_for(frame.evaluate("""id => {
            const state=window.__meeting;
            if (!state || state.capture_clock_id!==id) return null;
            return state.startCapture ? state.startCapture() : null;
        }""", token), max(.001, deadline-time.monotonic()))

    async def capture_readiness(self):
        reports = []
        for frame in self.page.frames:
            try:
                value = await asyncio.wait_for(frame.evaluate("() => window.__meeting?.readiness ? window.__meeting.readiness() : {hook_present:false,ready:false}"), 3)
            except Exception:
                if frame.is_detached():
                    continue
                raise
            report = self._rx_readiness_report(value)
            if self._native_media.target is None:
                clock = self.capture_clocks.get(frame)
                report["observation_clock_ready"] = bool(clock and clock["offset"] is not None
                    and isinstance(value, dict) and value.get("capture_clock_id") == clock["id"])
            reports.append(report)
        sources = [r for r in reports if r["live_attached_tracks"] or r["pending_live_tracks"]]
        observation_ready = bool(sources) and all(r.get("observation_clock_ready") for r in sources)
        if self._native_media.target is not None:
            try:
                observation = await self._native_media.observation_stats()
                observation_ready = (self._native_media._observation_running
                    and isinstance(observation, dict) and observation.get("observation_ready") is True)
            except Exception:
                observation_ready = False
        return {"ready": bool(reports) and all(r["hook_present"] for r in reports) and any(r["ready"] for r in reports),
            "observation_ready":observation_ready, "frames": reports}

    async def prepared_readiness(self):
        """Admitted + RX hook deferred + Teams TX muted; does not start capture."""
        reports = []
        for frame in self.page.frames:
            try:
                value = await frame.evaluate("""() => ({hook_present:!!window.__meeting,
                    capture_deferred:window.__meeting?.capture_enabled===false,
                    tx_muted:!!window.__meeting && window.__meeting.peers.every(pc =>
                      pc.getSenders().every(s => !s.track || !['audio','video'].includes(s.track.kind) || !s.track.enabled))})""")
            except Exception:
                if frame.is_detached():
                    continue
                raise
            reports.append({key: value.get(key) is True for key in ('hook_present','capture_deferred','tx_muted')})
        return {'prepared': bool(reports) and all(all(r.values()) for r in reports) and await self.admitted(), 'frames': reports}

    async def start_capture(self, *, timeout=10, stopped=None):
        self.rx_step = "admission_check"
        if not self.capture_audio or self.capture_started or not await self.admitted():
            raise RuntimeError("capture_not_permitted")
        self.capture_started = True
        deadline = time.monotonic() + timeout
        ready = False
        while True:
            if stopped and stopped():
                raise InterruptedError("Stopped before capture readiness")
            if time.monotonic() >= deadline:
                if ready:
                    break
                raise TimeoutError("capture_rx_not_ready")
            for frame in self.page.frames:
                self.rx_step = "receiver_scan"
                try:
                    if self._native_media.target is None:
                        await self._start_capture_frame(frame, deadline=deadline)
                    else:
                        await self._native_media._start_frame(frame, deadline=deadline)
                except Exception:
                    if not frame.is_detached():
                        raise
            if self._native_media.target is not None:
                await self._native_media._start_observation(deadline=deadline)
            self.rx_step = "readiness_check"
            report = await self.capture_readiness()
            self._capture_readiness = report
            if not report["frames"] or any(not r["hook_present"] for r in report["frames"]):
                raise RuntimeError("capture_hook_missing")
            ready = report["ready"]
            if self._native_media.target is not None:
                native = await self.native_audio_readiness()
                report["native_source"] = native
                report["observation_ready"] = native["observation_ready"]
                if native["failed"]:
                    raise RuntimeError("native_media_source_lost")
                ready = native["ready"]
                report["ready"] = ready
            elif any(r["attach_errors"] for r in report["frames"]):
                raise RuntimeError("capture_attach_failed")
            if ready and (report["observation_ready"] or time.monotonic() >= deadline):
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("capture_rx_not_ready")
            await asyncio.sleep(min(.05, max(0, deadline-time.monotonic())))
        self.rx_step = "ready"
        await self.event("rx_ready", report)
        return report

    async def response_sender(self, meeting_id):
        """Find one existing muted sender; no getUserMedia/device provisioning."""
        candidates = []
        diagnostic = {'frame_count':len(self.page.frames), 'eligible_count':0,
            'frames':[], 'frames_truncated':len(self.page.frames)>8,
            'enumeration_complete':False, 'outcome':'scanning'}
        self._response_sender_diagnostic = diagnostic
        for frame in self.page.frames:
            try:
                result = await frame.evaluate(RESPONSE_SENDER_JS)
            except Exception:
                diagnostic['outcome'] = 'evaluation_failed'
                raise
            if len(diagnostic['frames']) < 8:
                diagnostic['frames'].append(result['diagnostic'])
            candidates.extend((frame, value) for value in result['candidates'])
            diagnostic['eligible_count'] = len(candidates)
        diagnostic['enumeration_complete'] = True
        if len(candidates) != 1:
            diagnostic['outcome'] = 'none' if not candidates else 'multiple'
            raise RuntimeError("unique_muted_sender_required")
        frame, value = candidates[0]
        try:
            self._response_selection_id = uuid.uuid4().hex
            await frame.evaluate(SELECT_RESPONSE_SENDER_JS, {'meetingId':meeting_id,
                'generation':value.get('generation'), 'slot':value.get('slot'),
                'selectionId':self._response_selection_id})
        except Exception:
            diagnostic['outcome'] = 'binding_failed'
            raise
        diagnostic['outcome'] = 'selected'
        return frame, value["peer"], value["sender"]

    def bind_dual_policy(self, frame, policy):
        if self.dual_policy is not None or frame not in self.page.frames:
            raise RuntimeError("dual_policy_binding_invalid")
        self.dual_frame, self.dual_policy = frame, policy

    async def release_dual_policy(self, frame, policy):
        """Return to RX-only only after the owning runtime verified restoration."""
        if self.dual_policy is not policy or self.dual_frame is not frame or frame not in self.page.frames:
            return False
        safe = await frame.evaluate("() => !!window.__meeting && window.__meeting.peers.every(pc => pc.getSenders().every(s => !s.track || !['audio','video'].includes(s.track.kind) || !s.track.enabled))")
        if safe is not True:
            return False
        self.dual_frame = self.dual_policy = None
        return True

    async def stats(self):
        reports = []
        for frame in self.page.frames:
            try:
                s = await frame.evaluate(r"""() => window.__meeting && ({
                  tracks:__meeting.tracks, packets:__meeting.packets,
                  dropped:__meeting.dropped, frames:__meeting.frames,
                  nonzero_samples:__meeting.nonzero_samples,
                  playout_errors:__meeting.playout_errors,
                  capture_readiness:__meeting.readiness ? __meeting.readiness() : null,
                  connections:__meeting.peers.map(pc => ({
                    connection:pc.connectionState, ice_connection:pc.iceConnectionState,
                    ice_gathering:pc.iceGatheringState,
                    ice_diagnostics:pc.__recorderIce || null,
                    local_candidates:(pc.localDescription?.sdp || '').split(/\r?\n/).filter(x=>x.startsWith('a=candidate:')).length,
                    remote_candidates:(pc.remoteDescription?.sdp || '').split(/\r?\n/).filter(x=>x.startsWith('a=candidate:')).length,
                    ice_server_count:pc.getConfiguration().iceServers?.length || 0
                  }))
                })""")
                if s:
                    reports.append(s)
            except Exception:
                pass
        if self._native_media.target is not None:
            with contextlib.suppress(Exception):
                observation = await self._native_media.observation_stats()
                if observation:
                    reports.append({"native_observation":True,
                        "capture_readiness":{"context_state":observation["context_state"]},
                        **{key:observation[key] for key in
                            ("packets", "frames", "nonzero_samples", "pending", "dropped")}})
        return reports

    async def close(self):
        if not hasattr(self, "_close_task"):
            self._close_task = asyncio.create_task(self._close_owned())
            self._close_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(self._close_task)

    async def _close_owned(self):
        if getattr(self,'_document_policy',None) is not None:
            with contextlib.suppress(Exception):
                await self._document_policy.close()
        if self.context:
            # Context disposal is the final, observable recovery when an
            # earlier frame-level gate could not be confirmed.
            with contextlib.suppress(Exception):
                await self.stop_capture()
            try:
                if self.page and not self.page.is_closed():
                    await self._button(r"^(?:leave|leave meeting|hang up|離開|退出|掛斷)$")
            finally:
                await asyncio.wait_for(self.context.close(), 5)
                self._native_media.context_closed()
                await self._native_media.stop()

    async def stop_capture(self):
        if not hasattr(self, "_capture_stop_task"):
            self._capture_stop_task = asyncio.create_task(self._stop_capture_owned())
            self._capture_stop_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        await asyncio.shield(self._capture_stop_task)

    async def _stop_capture_owned(self):
        native = self._native_media.target is not None
        confirmed = True
        self._native_media.begin_stop()
        if native:
            confirmed &= await self._native_media._close_observation()
        owned_frames = self._native_media.capture_frames
        if self.audio_frame is not None:
            owned_frames.add(self.audio_frame)
        frames = tuple(self.page.frames) if self.page and not self.page.is_closed() else ()
        async def stop_frame(frame):
            try:
                value = await asyncio.wait_for(frame.evaluate("() => window.__meeting?.stopCapture?.()"), 3)
                return frame not in owned_frames or (
                    isinstance(value, dict) and all(value.get(key) is True for key in
                    ('stopped', 'source_stopped', 'relays_closed', 'context_closed')))
            except Exception:
                return frame not in owned_frames or frame.is_detached() or self.page.is_closed()
        confirmed &= all(await asyncio.gather(*(stop_frame(frame) for frame in frames)))
        try:
            await self._native_media.stop()
        except Exception:
            confirmed = False
        if not confirmed:
            if native:
                self._native_media.record_failure('native_media_cleanup_unconfirmed')
            self._native_media.cleanup_status = 'unknown'
            raise RuntimeError('native_media_cleanup_unconfirmed' if native else 'capture_cleanup_unconfirmed')
