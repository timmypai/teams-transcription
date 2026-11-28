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


REPLACEMENT_FIELDS = {'replacement_track','replacement_enabled','replacement_ready_state'}
REPLACEMENT_TRACKS = {'original','owned_audio_input','other_audio','other'}


def replacement_receipt(value):
    """Scalars only; missing/unobserved or invalid diagnostic values stay null."""
    value=value if isinstance(value,dict) else {}
    def label(field,allowed):
        item=value.get(field)
        return item if isinstance(item,str) and item in allowed else None
    return {'replacement_track':label('replacement_track',REPLACEMENT_TRACKS),
        'replacement_enabled':value.get('replacement_enabled') if type(value.get('replacement_enabled')) is bool else None,
        'replacement_ready_state':label('replacement_ready_state',{'live','ended'})}


DUAL_CHECK_JS = r"""({policyId,meetingId,expected,authorize,initialized,receipt=false}) => {
  let diagnostic=null;
  const result=(accepted,reason,detail,replacement)=>{
    const value=receipt?(detail?
    {accepted,reason,detail,...(reason==='owned_identity' && detail==='revoked_guard_sender_track_changed'?replacement:{})}:
    {accepted,reason}):accepted;
    if(receipt && !accepted && diagnostic) value.tx_first_failure=diagnostic;
    return value;
  };
  const site=authorize?'dual_authorize':'dual_check';
  const muteOwned=state=>{flushCondition();if(state) state.revoke('dual_policy_rejected',site);};
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
  let diagnosticState=existing, diagnosticEligible=!existing?.disposed, firstCondition=null;
  // A false ownedIntact can be a legal muted transition. Commit only when the
  // existing branch actually rejects, not during an accepted capability check.
  const condition=(value,name)=>{
    if(value===false && diagnosticEligible && firstCondition===null) firstCondition=name;
    return value;
  };
  const flushCondition=()=>{
    if(firstCondition!==null) try {diagnosticState?.txDiagnostic?.failure(site,firstCondition);}catch{}
  };
  const fail=(reason,muteForeign=false,detail=null)=>{
    policy.failureReason ||= reason;
    if(policy.failureReason==='owned_identity' && detail && !policy.failureDetail) {
      policy.failureDetail=detail;
      if(detail==='revoked_guard_sender_track_changed') {
        const snapshot={replacement_track:null,replacement_enabled:null,replacement_ready_state:null};
        try {
          const value=existing?.replacementFailure;
          const track=value?.replacement_track, enabled=value?.replacement_enabled, ready=value?.replacement_ready_state;
          snapshot.replacement_track=['original','owned_audio_input','other_audio','other'].includes(track)?track:null;
          snapshot.replacement_enabled=typeof enabled==='boolean'?enabled:null;
          snapshot.replacement_ready_state=['live','ended'].includes(ready)?ready:null;
        }catch{}
        policy.replacementFailure=snapshot;
      }
    }
    policy.faulted=true; muteOwned(existing);
    if(muteForeign) for(const pc of policy.capture.peers) for(const sender of pc.getSenders()) {
      if(sender.track && ['audio','video'].includes(sender.track.kind)) sender.track.enabled=false;
    }
    // Reuse the existing rejected check response, after all required muting.
    try {
      if(!policy.txFirstFailure) {
        diagnosticState?.txDiagnostic?.reject();
        policy.txFirstFailure=diagnosticState?.txDiagnostic?.snapshot();
      }
      diagnostic=policy.txFirstFailure;
    }catch{}
    return result(false,policy.failureReason,policy.failureReason==='owned_identity'?policy.failureDetail:null,policy.replacementFailure);
  };
  if(policy.faulted) return fail('policy_faulted');
  // A reused frame/document cannot carry its old TX exception into a new meeting.
  if(!condition(policy.document===document,'document_match') || !condition(policy.href===location.href,'location_match') ||
     !condition(window.__bMeetingId===meetingId,'meeting_match') || !condition(window.__meeting===policy.capture,'capture_match')) return fail('document_scope');
  const current=b=>b && condition(b.document===document,'document_match') && condition(b.href===location.href,'location_match') &&
    condition(b.meetingId===meetingId,'meeting_match') && condition(b.capture===policy.capture,'capture_match') &&
    condition(policy.capture.peers[b.peerIndex]===b.pc,'peer_match') && condition(b.pc.connectionState!=='closed','peer_open') &&
    condition(b.pc.getSenders().includes(b.sender),'sender_member') && (b.original!==null ||
      (condition(b.pc.getTransceivers().includes(b.transceiver),'transceiver_member') && condition(b.transceiver.sender===b.sender,'transceiver_sender_match')));
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
  diagnosticState=owned; diagnosticEligible=!owned?.disposed;
  const ownedIntact=owned && condition(owned.installed,'installed') && condition(!owned.disposed,'not_disposed') && condition(owned.binding===binding,'binding_match') &&
    condition(window.__bTX===owned,'state_current') && condition(owned.pc===binding.pc,'peer_match') && condition(owned.sender===binding.sender,'sender_match') &&
    condition(owned.sender.track===owned.track,'sender_track_match') && condition(owned.track.kind==='audio','output_audio') &&
    condition(owned.track.readyState==='live','output_live') &&
    condition(owned.relay.connectionState==='connected','relay_connected') && condition(owned.control?.readyState==='open','control_open') &&
    condition(owned.relay.getReceivers().includes(owned.receiver),'receiver_member') &&
    condition(owned.receiver?.track===owned.inputTrack,'receiver_track_match') &&
    condition(owned.decoder?.receiver===owned.receiver,'decoder_receiver_match') && condition(owned.decoder.inputTrack===owned.inputTrack,'decoder_input_match') &&
    condition(owned.decoder.track===owned.track,'decoder_track_match') && condition(owned.decoder.valid(),'decoder_valid') &&
    (binding.original!==null || (condition(binding.transceiver.stopped===false,'transceiver_active') &&
      condition(binding.transceiver.receiver?.track?.kind==='audio','transceiver_audio') &&
      condition(['sendonly','sendrecv'].includes(binding.transceiver.currentDirection),'transceiver_sending')));
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
    flushCondition(); // Commit before the existing detail predicates can revoke synchronously.
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
        if self._failure is None:return None
        result=dict(self._failure)
        if 'tx_first_failure' in result:result['tx_first_failure']=dict(result['tx_first_failure'])
        return result

    def _fail(self, reason, exception_type='none', *, detail=None, replacement=None, tx_failure=None):
        self.faulted = True
        if self._failure is not None:
            return
        snapshot = dict(reason=reason, exception_type=exception_type,
            elapsed_ms=min(86400000, max(0, int((self._clock()-self._origin)*1000))))
        if reason == 'owned_identity' and isinstance(detail, str) and detail in self._OWNED_IDENTITY_DETAILS:
            snapshot['detail'] = detail
            if detail=='revoked_guard_sender_track_changed' and replacement is not None:
                snapshot.update(replacement_receipt(replacement))
        from .diagnostics import tx_first_failure_receipt
        diagnostic=tx_first_failure_receipt(tx_failure)
        if diagnostic is not None:snapshot['tx_first_failure']=diagnostic
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
            # An optional diagnostic cannot alter the legacy capability decision.
            tx_failure=result.get('tx_first_failure') if isinstance(result,dict) else None
            if isinstance(result,dict) and 'tx_first_failure' in result:
                result={key:value for key,value in result.items() if key!='tx_first_failure'}
            keys = set(result) if isinstance(result, dict) else set()
            valid_keys = keys == {'accepted', 'reason'} or (
                (keys == {'accepted', 'reason', 'detail'} or (
                    keys == {'accepted','reason','detail'} | REPLACEMENT_FIELDS
                    and result.get('detail') == 'revoked_guard_sender_track_changed'
                    and all(result[key] is None or (type(result[key]) is bool if key=='replacement_enabled'
                        else isinstance(result[key],str) and result[key] in (
                            REPLACEMENT_TRACKS if key=='replacement_track' else {'live','ended'}))
                        for key in REPLACEMENT_FIELDS)))
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
                self._fail(result['reason'], detail=result.get('detail'),
                    replacement=result if REPLACEMENT_FIELDS <= keys else None,tx_failure=tx_failure)
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
