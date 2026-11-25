"""Explicit production dual-channel sender guard; RX-only policy is untouched.

Opt-in wiring (no A API is assumed):
    policy = OwnedSenderDualPolicy(frame, meeting_id)
    sink = BrowserTXSink(frame, meeting_id=meeting_id,
                         dual_channel_policy=policy.authorize)
    await sink.bind()
    # The dual-mode RX callback must await policy.check() before every PCM packet.
    # False is a capability failure: stop the session/output; never forward it.

The runtime owns window.__bMeetingId and must update it before meeting changes.
The callback alone is insufficient: the host must wire check into its RX path
and accept packets only from this bound frame/meeting. This class guards TX
capability; the host owns RX source routing.
This guard has no join/device/network behavior. It never acquires a microphone.
"""
import asyncio
import time
import uuid


OWNED_IDENTITY_DETAILS = frozenset({
    'revoked_source_disconnected', 'revoked_source_invalidated',
    'revoked_relay_disconnected', 'revoked_decoder_failed',
    'revoked_receiver_ended', 'revoked_document_closed',
    'revoked_foreign_control', 'revoked_attach_failed',
    'revoked_relay_not_ready', 'revoked_stats_unavailable',
    'revoked_explicit_stop', 'revoked_dual_policy_rejected',
    'revoked_meeting_changed', 'revoked_guard_binding_lost',
    'revoked_guard_owner_replaced', 'revoked_guard_sender_track_changed',
    'revoked_guard_decoder_invalid', 'revoked_other',
    'sender_track_changed', 'owned_track_ended', 'relay_not_connected',
    'control_not_open', 'decoder_invalid', 'transceiver_direction_changed',
    'owned_state_mismatch',
})


DUAL_CHECK_JS = r"""({policyId,meetingId,expected,authorize,initialized,receipt=false}) => {
  const result=(accepted,reason,detail)=>receipt?(detail?{accepted,reason,detail}:{accepted,reason}):accepted;
  const muteOwned=state=>{if(state) state.revoke('dual_policy_rejected');};
  window.__bDualPolicies ||= new Map();
  let policy=window.__bDualPolicies.get(policyId);
  if(!policy) {
    if(initialized) return result(false,'policy_missing');
    if(window.__bMeetingId!==meetingId || !window.__meeting?.peers) return result(false,'meeting_scope');
    policy={document,capture:window.__meeting,href:location.href,meetingId,
      binding:null,trackId:null,faulted:false,failureDetail:null};
    window.__bDualPolicies.set(policyId,policy);
  }
  const existing=policy.binding && window.__bTXOwnedStates?.get(policy.binding.owner);
  const fail=(reason,muteForeign=false,detail=null)=>{
    policy.failureReason ||= reason;
    if(policy.failureReason==='owned_identity' && detail && !policy.failureDetail) policy.failureDetail=detail;
    policy.faulted=true; muteOwned(existing);
    if(muteForeign) for(const pc of policy.capture.peers) for(const sender of pc.getSenders()) {
      if(sender.track && ['audio','video'].includes(sender.track.kind)) sender.track.enabled=false;
    }
    return result(false,policy.failureReason,policy.failureReason==='owned_identity'?policy.failureDetail:null);
  };
  if(policy.faulted) return fail('policy_faulted');
  // A reused frame/document cannot carry its old TX exception into a new meeting.
  if(policy.document!==document || policy.href!==location.href ||
     window.__bMeetingId!==meetingId || window.__meeting!==policy.capture) return fail('document_scope');
  const current=b=>b && b.document===document && b.href===location.href &&
    b.meetingId===meetingId && b.capture===policy.capture &&
    policy.capture.peers[b.peerIndex]===b.pc && b.pc.connectionState!=='closed' &&
    b.pc.getSenders().includes(b.sender) && (b.original!==null ||
      (b.pc.getTransceivers().includes(b.transceiver) && b.transceiver.sender===b.sender));
  if(policy.binding && !current(policy.binding)) return fail('binding_scope');
  let binding=policy.binding, trackId=policy.trackId;
  if(authorize) {
    binding=window.__bTXBindings?.get(expected?.owner);
    if(!current(binding) || (policy.binding && policy.binding!==binding) ||
       expected.meeting_id!==meetingId || expected.document_id!==binding.documentId ||
       expected.peer_id!==binding.peerId || expected.sender_id!==binding.senderId)
      return fail('authorization_scope');
    trackId=expected.track_id;
  }
  const owned=binding && window.__bTXOwnedStates?.get(binding.owner);
  const ownedIntact=owned && owned.installed && !owned.disposed && owned.binding===binding &&
    window.__bTX===owned && owned.pc===binding.pc && owned.sender===binding.sender &&
    owned.sender.track===owned.track && owned.track.kind==='audio' &&
    owned.track.readyState==='live' &&
    owned.relay.connectionState==='connected' && owned.control?.readyState==='open' &&
    owned.relay.getReceivers().includes(owned.receiver) &&
    owned.receiver?.track===owned.inputTrack &&
    owned.decoder?.receiver===owned.receiver && owned.decoder.inputTrack===owned.inputTrack &&
    owned.decoder.track===owned.track && owned.decoder.valid() &&
    (binding.original!==null || (binding.transceiver.stopped===false &&
      binding.transceiver.receiver?.track?.kind==='audio' &&
      ['sendonly','sendrecv'].includes(binding.transceiver.currentDirection)));
  const ownedValid=ownedIntact && owned.track.id===trackId;
  // stop() revokes output immediately but may await replaceTrack(original).
  // Both sides of that restore must keep RX alive while every sender is muted.
  const normalStopping=owned && owned.stopping===true && owned.disposed &&
    owned.binding===binding && window.__bTX===owned &&
    owned.pc===binding.pc && owned.sender===binding.sender && owned.muted &&
    !owned.track?.enabled && !owned.original?.enabled &&
    (owned.sender.track===owned.track || owned.sender.track===owned.original);
  if(authorize && !ownedValid) {muteOwned(owned);return fail('owned_decoder',true);}
  // Identity loss faults even a muted replacement, before the periodic guard.
  // A new, muted relay may await authorization after a normal restore.
  if(owned && !normalStopping && (owned.disposed || (owned.installed && !ownedIntact))) {
    const knownRevokes=new Set([
      'source_disconnected','source_invalidated','relay_disconnected','decoder_failed',
      'receiver_ended','document_closed','foreign_control','attach_failed',
      'relay_not_ready','stats_unavailable','explicit_stop','dual_policy_rejected',
      'meeting_changed','guard_binding_lost','guard_owner_replaced',
      'guard_sender_track_changed','guard_decoder_invalid'
    ]);
    let detail='owned_state_mismatch';
    if(owned.disposed) {
      const r=owned.revokeReason;
      detail=knownRevokes.has(r)?('revoked_'+r):'revoked_other';
    } else if(owned.sender?.track!==owned.track) {
      detail='sender_track_changed';
    } else if(owned.track?.readyState!=='live') {
      detail='owned_track_ended';
    } else if(owned.relay?.connectionState!=='connected') {
      detail='relay_not_connected';
    } else if(owned.control?.readyState!=='open') {
      detail='control_not_open';
    } else if(!owned.decoder?.valid?.()) {
      detail='decoder_invalid';
    } else if(binding.original===null && (binding.transceiver?.stopped!==false ||
        !['sendonly','sendrecv'].includes(binding.transceiver?.currentDirection))) {
      detail='transceiver_direction_changed';
    }
    muteOwned(owned);return fail('owned_identity',true,detail);
  }
  // Only this exact owned native sender/track may be enabled. Check ALL peers and
  // both audio/video; unrelated microphones/cameras stay disabled.
  let foreign=false;
  for(const pc of policy.capture.peers) for(const sender of pc.getSenders()) {
    const track=sender.track;
    if(track && ['audio','video'].includes(track.kind) && track.enabled &&
       !(ownedValid && !owned.muted && pc===binding.pc && sender===binding.sender && track===owned.track))
      foreign=true;
  }
  if(foreign) {
    // Authorize may be the first encounter with a lease; revoke that lease too.
    muteOwned(owned);
    return fail('foreign_sender',true);
  }
  if(authorize) {policy.binding=binding;policy.trackId=trackId;}
  return result(true,'none');
}"""


class OwnedSenderDualPolicy:
    """Inert until called; permits one bound receiver's no-PLC rendered track.

    authorize(binding) is BrowserTXSink's callback. check() is the production RX
    every-packet guard. A failure latches; create a new policy/sink after a meeting
    lifecycle change rather than silently rebinding this capability.
    """
    _FIELDS = ('meeting_id','document_id','peer_id','sender_id','owner','track_id')
    _REASONS = {'policy_missing','meeting_scope','policy_faulted','document_scope',
        'binding_scope','authorization_scope','owned_decoder','owned_identity','foreign_sender'}
    _OWNED_IDENTITY_DETAILS = OWNED_IDENTITY_DETAILS

    def __init__(self, frame, meeting_id, *, timeout=2, clock=time.monotonic, on_failure=None):
        if not isinstance(meeting_id,str) or not meeting_id:
            raise ValueError('An explicit meeting identity is required')
        if not isinstance(timeout,(int,float)) or not 0 < timeout <= 5:
            raise ValueError('A bounded dual-policy timeout is required')
        self.frame, self.meeting_id, self.timeout = frame, meeting_id, timeout
        self.policy_id = uuid.uuid4().hex
        self.faulted = False
        self._clock, self._origin, self._on_failure = clock, clock(), on_failure
        self._failure = None
        self._initialized = False
        self._lock = asyncio.Lock()

    def failure_snapshot(self):
        return dict(self._failure) if self._failure is not None else None

    def _fail(self, reason, exception_type='none', *, detail=None):
        self.faulted = True
        if self._failure is not None:
            return
        snapshot = dict(reason=reason, exception_type=exception_type,
            elapsed_ms=min(86400000, max(0, int((self._clock()-self._origin)*1000))))
        if reason == 'owned_identity' and isinstance(detail, str) and detail in self._OWNED_IDENTITY_DETAILS:
            snapshot['detail'] = detail
        self._failure = snapshot
        if self._on_failure is not None:
            try:
                self._on_failure()
            except Exception:
                pass  # The capability remains rejected even if its owner cannot notify.

    async def _evaluate(self, expected=None, *, revoke=False):
        if self.faulted and not revoke:
            return False
        async with self._lock:
            if self.faulted and not revoke:
                return False
            try:
                result=await asyncio.wait_for(self.frame.evaluate(DUAL_CHECK_JS,
                    {'policyId':self.policy_id,'meetingId':self.meeting_id,
                     'expected':expected,'authorize':expected is not None,
                     'initialized':self._initialized,'receipt':True}),self.timeout)
            except TimeoutError:
                self._fail('evaluation_timeout', 'TimeoutError')
                return False
            except Exception:
                self._fail('evaluation_error', 'Exception')
                return False
            keys = set(result) if isinstance(result, dict) else set()
            valid_keys = keys == {'accepted', 'reason'} or (
                keys == {'accepted', 'reason', 'detail'}
                and result.get('accepted') is False
                and result.get('reason') == 'owned_identity'
                and isinstance(result.get('detail'), str)
                and result.get('detail') in self._OWNED_IDENTITY_DETAILS
            )
            if (not isinstance(result,dict) or not valid_keys
                    or type(result['accepted']) is not bool or not isinstance(result['reason'],str)
                    or result['reason'] not in self._REASONS | {'none'}
                    or (result['reason']=='none') is not result['accepted']):
                self._fail('invalid_result')
                return False
            self._initialized |= result['accepted'] and not self.faulted
            if not result['accepted']:
                self._fail(result['reason'], detail=result.get('detail'))
            return result['accepted'] and not self.faulted

    async def authorize(self,binding):
        if (not isinstance(binding,dict) or
                any(not isinstance(binding.get(key),str) or not binding[key] for key in self._FIELDS) or
                binding['meeting_id']!=self.meeting_id):
            self._fail('invalid_binding')
            # Still perform the check to revoke a known existing lease.
            await self._evaluate({}, revoke=True)
            return False
        return await self._evaluate({key:binding[key] for key in self._FIELDS})

    async def check(self):
        return await self._evaluate()
