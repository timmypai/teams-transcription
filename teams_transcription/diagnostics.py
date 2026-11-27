"""Bounded failure metadata; never persist exception text or browser content."""
import math
import re
import traceback

PHASES = {"prepare", "prepared", "owner_wait", "live_start", "browser_open", "meeting_join", "rx_start",
          "rx_scan", "rx_capture", "rx_drain", "live_drain", "summary", "cleanup"}
FILES = {"browser.py", "live.py", "worker.py", "meeting_store.py", "storage.py"}
EXCEPTION_TYPES = {"TimeoutError", "Error", "TargetClosedError", "RuntimeError",
                   "ValueError", "OSError", "QueueFull", "InterruptedError", "RxContractError"}

LIVE_CODES = {'live_session_expired', 'live_session_closed', 'live_transport_reset',
              'live_media_not_ready',
              'live_realtime_error', 'live_app_server_exit', 'live_notification_invalid',
              'live_source_failed', 'live_receiver_failed', 'live_error_unknown'}
LIVE_CHANNELS = {'rpc', 'datachannel', 'receiver', 'source', 'unknown'}
LIVE_STAGES = {'rpc_start', 'account_read', 'thread_start', 'ice_gather', 'realtime_start',
               'sdp_answer', 'sdp_apply', 'media_connect', 'connected', 'closing', 'unknown'}
LIVE_TYPES = {'session.closed', 'error', 'thread/realtime/closed', 'thread/realtime/error',
              'client/exit', 'notification_invalid', 'receiver_failed', 'source_failed', 'unknown'}
LIVE_REASONS = {'expired', 'requested', 'client_request', 'transport_closed', 'error', 'stopped', 'unknown'}

JOIN_PHASES = {'context_setup', 'page_create', 'page_setup', 'goto', 'page_ready',
               'starting', 'body_read', 'admission_check', 'browser_choice', 'audio_choice', 'preview_navigation',
               'name_fill', 'mute_controls', 'join_control_scan', 'join_dispatch',
               'post_join_wait', 'lobby_wait', 'joined', 'unknown'}
JOIN_PAGE_STATES = {'unknown', 'unrecognized', 'prejoin_controls', 'connecting', 'lobby',
                    'joined', 'login_required', 'connect_failed', 'ended'}
JOIN_ACTIONS = {'browser_continue', 'audio_continue', 'computer_audio', 'name', 'mute', 'join'}
JOIN_CONTROLS = JOIN_ACTIONS | {'leave'}
JOIN_RESULTS = {'not_attempted', 'inspecting', 'not_visible', 'dispatching', 'completed', 'error'}
JOIN_OUTCOMES = {'in_progress', 'joined', 'timeout', 'stopped', 'cancelled', 'error', 'unknown'}
JOIN_EXCEPTION_TYPES = EXCEPTION_TYPES | {'Exception', 'CancelledError', 'ResponseUIContextError', 'none'}

# B startup receipts contain fixed labels only, never exception text or paths.
RESPONSE_START_PHASES = {'not_started', 'guard_construct', 'guard_prepare',
    'sender_select', 'policy_construct', 'sink_construct', 'sink_bind',
    'policy_check', 'policy_bind', 'native_construct', 'native_start',
    'cli_preflight', 'cli_version_spawn', 'cli_version_wait', 'cli_version_check',
    'rpc_initialize', 'account_read', 'thread_start', 'peer_bind', 'ice_offer',
    'realtime_start_ack', 'sdp_answer', 'sdp_authorize', 'sdp_apply',
    'media_connected', 'native_output_attach', 'context_bind', 'input_policy_check',
    'ready', 'native_stop', 'policy_release', 'policy_revoke'}
RESPONSE_EXCEPTION_TYPES = JOIN_EXCEPTION_TYPES | {'FileNotFoundError',
    'PermissionError', 'ConnectionError', 'KeyError', 'AttributeError',
    'TypeError', 'UnicodeDecodeError'}
RESPONSE_OUTPUT_STEPS = {'output_scope', 'relay_scope', 'source_offer',
    'target_answer', 'source_answer', 'target_ready', 'source_check',
    'sender_install', 'initial_mute'}

# Exact messages authored by our relay. Never forward browser exception text,
# stack, URLs, SDP or arbitrary DOM messages. Unknown errors retain the existing
# phase/type/step receipt until independently diagnosed.
RESPONSE_OUTPUT_MESSAGES = {
    'Native Live audio receiver unavailable':'live_receiver_unavailable',
    'No-PLC Live decoder unavailable':'live_decoder_unavailable',
    'Native output ICE timeout':'relay_ice_timeout',
    'Native output source already owned':'source_already_owned',
    'Native output source invalidated':'source_invalidated',
    'Native output source disconnected or invalidated':'source_disconnected',
    'TX document/meeting/peer binding invalidated':'target_binding_invalidated',
    'Native TX already owned':'target_already_owned',
    'Select an existing muted meeting audio sender':'sender_not_muted',
    'Native TX receiver unavailable':'target_receiver_unavailable',
    'TX revoked during decode setup':'target_revoked_during_setup',
    'Native TX decoder invalidated':'target_decoder_invalidated',
    'Native no-PLC Opus decoder unavailable':'opus_decoder_unavailable',
    'Native TX binding invalidated':'target_binding_invalidated',
    'Native TX relay not ready':'relay_not_ready',
    'Native TX disconnected or ownership/meeting binding mismatch':'target_not_ready',
    'b_no_plc_setup_failed':'decoder_setup_failed',
    'b_no_plc_source_ended':'decoder_source_ended',
}
RESPONSE_OUTPUT_CODES = set(RESPONSE_OUTPUT_MESSAGES.values())
RESPONSE_TARGET_STAGES = {'remote_description','receiver_check','decoder_setup',
    'decoder_binding','codec_preferences','local_description','local_binding',
    'ice_gather','gathered_binding'}
RESPONSE_TARGET_TYPES = {'Error','TypeError','NotSupportedError','SecurityError',
    'InvalidStateError','OperationError','NetworkError','AbortError','TimeoutError','unknown'}
RESPONSE_DECODER_STAGES = {'receiver_api','browser_api','encoded_streams','audio_context',
    'context_resume','track_generator','source_graph','monitor_graph','worklet_url',
    'worklet_module','render_meter','decoder_create','graph_guard','unknown'}
RESPONSE_DECODER_REASONS = {'receiver_unsupported','api_unsupported','audio_context_invalid',
    'graph_mutated','source_ended','unknown'}

RESPONSE_TX_CHECKPOINTS = {'before_replace_track','after_replace_track','final_verify'}
RESPONSE_TX_CONDITIONS = {'binding_present','document_match','meeting_match','location_match',
    'capture_match','peer_match','peer_open','sender_member','transceiver_member',
    'transceiver_sender_match','transceiver_active','transceiver_audio','transceiver_sending',
    'state_present','binding_match','state_current','owner_match','not_disposed','output_live',
    'receiver_track_match','input_live','receiver_member','decoder_receiver_match',
    'decoder_track_match','decoder_valid','sender_track_match','relay_connected','control_open'}
RESPONSE_TX_REVOKES = {'source_disconnected','source_invalidated','relay_disconnected',
    'decoder_failed','receiver_ended','document_closed','foreign_control','attach_failed',
    'relay_not_ready','stats_unavailable','explicit_stop','dual_policy_rejected','meeting_changed',
    'guard_binding_lost','guard_owner_replaced','guard_sender_track_changed','guard_decoder_invalid','invalidated'}
RESPONSE_SOURCE_GROUPS = {'source_ownership','source_document','source_peer','receiver_track',
    'decoder','source_relay_control'}

RESPONSE_RUNTIME_PHASES = {'audio_callback', 'datachannel', 'document', 'rx_push',
    'notification', 'rpc', 'speech_start', 'speech_completion', 'continuation', 'unknown'}
RESPONSE_RUNTIME_CODES = {'response_failed', 'response_output_backpressure',
    'response_decoder_failed', 'response_output_delivery_failed',
    'response_document_invalidated', 'response_datachannel_error',
    'response_rpc_error', 'response_transport_closed', 'response_app_server_exit',
    'response_input_failed', 'response_notification_failed', 'response_pcm_invalid',
    'response_pcm_delivery_failed', 'response_speech_start_failed',
    'response_speech_completion_failed', 'response_continuation_failed',
    'response_continuation_overflow', 'response_turn_identity_capacity',
    'response_input_policy_failed'}


def response_start_failure(exc, phase, *, output_step=None):
    kind = type(exc).__name__ if exc is not None else 'none'
    value = {'phase': phase, 'exception_type': kind}
    if output_step is not None:value['output_step'] = output_step
    if phase == 'native_output_attach' and exc is not None:
        try:message = str(exc)
        except Exception:message = ''
        if len(message) <= 8192:
            first = message.split('\n', 1)[0]
            for prefix in ('Page.evaluate: Error: ', 'Frame.evaluate: Error: ', 'Error: '):
                if first.startswith(prefix):
                    first = first[len(prefix):]
                    break
            code = RESPONSE_OUTPUT_MESSAGES.get(first)
            if code is not None:value['output_error_code'] = code
            prefix='Native TX disconnected or ownership/meeting binding mismatch/tx_ready/'
            if output_step=='sender_install' and first.startswith(prefix):
                parts=first[len(prefix):].split('/')
                if (len(parts)==3 and parts[0] in RESPONSE_TX_CHECKPOINTS
                        and parts[1] in RESPONSE_TX_CONDITIONS and parts[2] in RESPONSE_TX_REVOKES | {'none'}):
                    value.update(output_error_code='target_not_ready',checkpoint=parts[0],
                        first_failed_condition=parts[1],first_revoke_reason=None if parts[2]=='none' else parts[2],
                        source_failure_group=None)
            match = re.fullmatch(r'native_tx_setup/([a-z_]{1,32})/([A-Za-z]{1,32})'
                r'(?:/decoder/([a-z_]{1,32})/([A-Za-z]{1,32})/([a-z_]{1,32}))?', first)
            if match and match[1] in RESPONSE_TARGET_STAGES and match[2] in RESPONSE_TARGET_TYPES:
                value['target_stage'], value['target_exception_type'] = match[1],match[2]
                if match[1]=='decoder_setup' and match[3] in RESPONSE_DECODER_STAGES \
                        and match[4] in RESPONSE_TARGET_TYPES and match[5] in RESPONSE_DECODER_REASONS:
                    value.update(decoder_stage=match[3],decoder_exception_type=match[4],decoder_reason=match[5])
    return response_start_receipt(value)


def response_start_receipt(value):
    result = {'phase': value.get('phase') if isinstance(value.get('phase'), str)
                and value['phase'] in RESPONSE_START_PHASES else 'unknown',
            'exception_type': value.get('exception_type')
                if isinstance(value.get('exception_type'), str)
                and value['exception_type'] in RESPONSE_EXCEPTION_TYPES else 'Exception'}
    if result['phase'] == 'native_output_attach' and 'output_step' in value:
        result['output_step'] = value['output_step'] if isinstance(value['output_step'], str) \
            and value['output_step'] in RESPONSE_OUTPUT_STEPS else 'unknown'
    if result['phase'] == 'native_output_attach' and 'output_error_code' in value:
        code = value['output_error_code']
        result['output_error_code'] = code if isinstance(code, str) and code in RESPONSE_OUTPUT_CODES else 'unknown'
    if (result['phase']=='native_output_attach' and result.get('output_step')=='sender_install'
            and result.get('output_error_code')=='target_not_ready'):
        for field,allowed in (('checkpoint',RESPONSE_TX_CHECKPOINTS),
                ('first_failed_condition',RESPONSE_TX_CONDITIONS),('first_revoke_reason',RESPONSE_TX_REVOKES),
                ('source_failure_group',RESPONSE_SOURCE_GROUPS)):
            if field in value:
                item=value[field]
                result[field]=item if isinstance(item,str) and item in allowed else None
        if result.get('first_revoke_reason') not in {'source_invalidated','source_disconnected','relay_disconnected'}:
            if 'source_failure_group' in result:result['source_failure_group']=None
    if result['phase'] == 'native_output_attach' and value.get('output_step') == 'target_answer':
        if 'target_document_policy' in value:
            from .b_document_policy import sanitize_document_policy_snapshot
            result['target_document_policy']=sanitize_document_policy_snapshot(value['target_document_policy'])
        for field, allowed in (('target_stage',RESPONSE_TARGET_STAGES),('target_exception_type',RESPONSE_TARGET_TYPES)):
            if field in value:
                item=value[field]
                result[field]=item if isinstance(item,str) and item in allowed else 'unknown'
        if result.get('target_stage')=='decoder_setup':
            for field,allowed in (('decoder_stage',RESPONSE_DECODER_STAGES),
                    ('decoder_exception_type',RESPONSE_TARGET_TYPES),('decoder_reason',RESPONSE_DECODER_REASONS)):
                if field in value:
                    item=value[field]
                    result[field]=item if isinstance(item,str) and item in allowed else 'unknown'
            environment=decoder_environment_snapshot(value.get('target_environment'))
            if environment is not None:result['target_environment']=environment
    return result


def decoder_environment_snapshot(value):
    if not isinstance(value,dict):return None
    result={key:value.get(key) if type(value.get(key)) is bool else None for key in
        ('secure_context','cross_origin_isolated','meta_csp_present','receiver_encoded_streams','audio_decoder','encoded_audio_chunk',
         'track_generator','audio_worklet_node','audio_context','context_audio_worklet')}
    state=value.get('context_state')
    result['context_state']=state if isinstance(state,str) and state in \
        ('running','suspended','interrupted','closed') else 'unknown'
    rate=value.get('sample_rate')
    result['sample_rate']=rate if type(rate) is int and 8000<=rate<=384000 else None
    return result


def response_runtime_failure_receipt(value):
    if not isinstance(value, dict):
        return None
    elapsed = value.get('elapsed_ms')
    return {'code': value.get('code') if isinstance(value.get('code'), str)
                and value['code'] in RESPONSE_RUNTIME_CODES else 'response_failed',
            'phase': value.get('phase') if isinstance(value.get('phase'), str)
                and value['phase'] in RESPONSE_RUNTIME_PHASES else 'unknown',
            'exception_type': value.get('exception_type')
                if isinstance(value.get('exception_type'), str)
                and value['exception_type'] in RESPONSE_EXCEPTION_TYPES else 'Exception',
            'elapsed_ms': min(elapsed, 86400000)
                if type(elapsed) is int and elapsed >= 0 else None}


def response_policy_failure_receipt(value):
    if not isinstance(value, dict):
        return None
    from .b_dual_policy import OWNED_IDENTITY_DETAILS
    reasons = {'policy_missing','meeting_scope','policy_faulted','document_scope',
        'binding_scope','authorization_scope','owned_decoder','owned_identity','foreign_sender',
        'evaluation_timeout','evaluation_error','invalid_result','invalid_binding'}
    reason, elapsed = value.get('reason'), value.get('elapsed_ms')
    kind = value.get('exception_type')
    safe_reason = reason if isinstance(reason,str) and reason in reasons else 'unknown'
    receipt = {'reason': safe_reason,
        'exception_type': kind if isinstance(kind,str) and kind in {'none','TimeoutError','Exception'} else 'Exception',
        'elapsed_ms': min(elapsed,86400000) if type(elapsed) is int and elapsed>=0 else None}
    if safe_reason == 'owned_identity' and 'detail' in value:
        detail = value.get('detail')
        receipt['detail'] = detail if isinstance(detail, str) and detail in OWNED_IDENTITY_DETAILS else 'unknown'
        if receipt['detail']=='revoked_guard_sender_track_changed':
            from .b_dual_policy import REPLACEMENT_FIELDS, replacement_receipt
            if REPLACEMENT_FIELDS.intersection(value):receipt.update(replacement_receipt(value))
    return receipt


def response_capture_delivery_receipt(value):
    if not isinstance(value,dict):
        return None
    return {key:min(value[key],2147483647) if type(value.get(key)) is int and value[key]>=0 else None
        for key in ('received_packets','delivered_packets','scope_rejected_packets',
            'scope_pending_packets','owner_rejected_packets','policy_rejected_packets',
            'consumer_unavailable_packets',
            'clock_rejected_packets','callback_error_packets')}


def response_progress_diagnostic(value):
    """Read-only B progress: fixed labels/counts, never transcript or media."""
    if not isinstance(value, dict):
        return None
    def section(parent, name):
        item = parent.get(name)
        return item if isinstance(item, dict) else {}
    def count(item, maximum=2147483647):
        return min(item, maximum) if type(item) is int and item >= 0 else 0
    def measurement(item):
        if type(item) is int and item >= 0:
            return min(item, 2147483647)
        if type(item) is float and math.isfinite(item) and item >= 0:
            return min(item, 2147483647)
        return None
    def label(item, allowed):
        return item if isinstance(item, str) and item in allowed else 'unknown'
    from .b_question_input import METHODS, OUTCOMES, ITEM_LIMIT, EVENT_LIMIT
    routed = value.get('question_input')
    if isinstance(routed,dict) and type(routed.get('version')) is int and routed['version']==1:
        routed_counts=section(routed,'counts')
        routed_events=routed.get('events')
        routed_events=routed_events[-EVENT_LIMIT:] if isinstance(routed_events,list) else []
        routed={'version':1,'mode':label(routed.get('mode'),{'unselected','flat','canonical'}),
            'session_bound':routed.get('session_bound') is True,
            'counts':{key:count(routed_counts.get(key)) for key in sorted(OUTCOMES)},
            'events':[{'event':label(item.get('event'),set(METHODS.values())),
                'outcome':label(item.get('outcome'),OUTCOMES),
                'characters':count(item.get('characters')),
                'received_seconds':min(86400.,measurement(item.get('received_seconds')))
                    if measurement(item.get('received_seconds')) is not None else None}
                for item in routed_events if isinstance(item,dict)],
            'event_limit':EVENT_LIMIT,'identity_count':count(routed.get('identity_count'),ITEM_LIMIT),
            'identity_limit':ITEM_LIMIT}
    else:routed=None
    receipts = section(value, 'question_receipts')
    counts = section(receipts, 'counts')
    pending = section(value, 'input_pending')
    fanout = section(value, 'fanout')
    transcripts = section(value, 'transcript_notifications')
    framing = value.get('question_framing')
    framing = ({
        'provenance': label(framing.get('provenance'), {'application'}),
        'boundary': label(framing.get('boundary'), {'pcm_quiet_and_stable_delta'}),
        **{key: framing.get(key) if type(framing.get(key)) is bool else None
            for key in ('heard_activity','capture_recent','followup_active')},
        **{key: measurement(framing.get(key)) for key in
            ('observed_quiet_seconds','silence_seconds','text_settle_seconds',
             'capture_freshness_seconds','capture_gap_tolerance_seconds')},
        'emitted_requests': count(framing.get('emitted_requests')),
    } if isinstance(framing, dict) else None)
    tx = section(value, 'tx')
    speech = section(value, 'speech_completion')
    drain = section(speech, 'drain_result')
    context = section(value, 'context')
    context_error = value.get('context_failure_code')
    events = receipts.get('events')
    events = events[-64:] if isinstance(events, list) else []
    reasons = {'invalid_question', 'state_not_listening', 'scope_changed',
        'runtime_failed', 'stopped', 'duplicate_request'}
    last_rejection = next((label(event.get('reason'), reasons)
        for event in reversed(events) if isinstance(event, dict) and event.get('reason') is not None), None)
    error = speech.get('error_code')
    deliveries = value.get('tool_deliveries')
    deliveries = deliveries[-16:] if isinstance(deliveries, list) else []
    safe_deliveries = []
    for item in deliveries:
        if not isinstance(item, dict):
            continue
        indexes = item.get('selected_record_indexes')
        indexes = indexes if isinstance(indexes, list) else []
        version = item.get('source_version')
        safe_deliveries.append({
            'question_number':count(item.get('question_number')),
            'phase':label(item.get('phase'), {'preparing','sending','delivered','cancelled','failed'}),
            'started_elapsed_ms':count(item.get('started_elapsed_ms'), 86400000),
            'source_version':count(version) if type(version) is int and version >= 0 else None,
            'available':item.get('available') is True,
            'selected_ref_count':count(item.get('selected_ref_count')),
            'selected_record_indexes':[min(index, 2147483647) for index in indexes[:64]
                if type(index) is int and index > 0],
            'selected_indexes_truncated':item.get('selected_indexes_truncated') is True or len(indexes)>64,
            'omitted_for_budget':item.get('omitted_for_budget') is True,
            'payload_bytes':count(item.get('payload_bytes')),
            'sent_bytes':count(item.get('sent_bytes')),
            'marker_returned':item.get('marker_returned') is True,
            'model_consumption_verified':False})
    return {'schema_version': 4,
        'state': label(value.get('state'), {'starting', 'sleeping', 'listening', 'greeting',
            'thinking', 'answering', 'failed', 'stopping', 'stopped', 'stop_unknown', 'unavailable'}),
        'rx': {'accepted_packets': count(value.get('rx_accepted_packets')),
            'peer_push_packets': count(value.get('rx_peer_push_packets')),
            'dropped_packets': count(value.get('rx_dropped'))},
        'fanout': {key: count(fanout.get(key)) for key in
            ('clock_missing_packets', 'offered_packets', 'accepted_packets', 'rejected_packets', 'error_packets')},
        'transcripts': {key: count(transcripts.get(key)) for key in
            ('user_delta', 'user_done', 'assistant_delta', 'assistant_done')},
        'question_framing': framing,
        'question_input': routed,
        'tx': {'available': tx.get('available') is True,
            'connected': tx.get('connected') is True,
            'counter_domain': label(tx.get('counter_domain'),
                {'legacy_render_cumulative_v1', 'target_decoded_input_v2'}),
            'render_evidence': render_history_diagnostic(tx.get('render_evidence')),
            **{key: count(tx.get(key)) for key in ('packets', 'frames')},
            **{key: measurement(tx.get(key)) for key in ('duration', 'energy')}},
        'pending_input': {'characters': count(pending.get('characters')),
            'has_wake': pending.get('has_wake') is True,
            'has_question': pending.get('has_question') is True},
        'question_receipts': {**{key: count(counts.get(key)) for key in ('received', 'admitted', 'rejected')},
            'last_rejection': last_rejection},
        'tool_calls': count(value.get('tool_calls')),
        'tool_deliveries':safe_deliveries,
        'context': {'sent_bytes': count(context.get('sent_bytes')),
            'total_limit_bytes': None,
            'committed_records': count(context.get('committed_records')),
            'pending': context.get('pending') is True,
            'failure_code': None if context_error is None else label(context_error, {
                'context_same_version_conflict','context_version_rollback','context_failure'})},
        'speech': {'phase': label(speech.get('phase'), {'not_started', 'waiting_greeting_text',
                'waiting_done', 'waiting_activity', 'waiting_quiet', 'draining', 'closing', 'closed'}),
            'error_code': None if error is None else label(error, {'native_error', 'speech_task_timeout',
                'speech_task_failed', 'speech_cancelled', 'speech_timeout', 'speech_failed'}),
            'done_is_set': speech.get('done_is_set') is True,
            'output_active': speech.get('output_active') is True,
            'drained': False,
            'quiet': drain.get('quiet') is True,
            'truncated': drain.get('truncated') is True,
            'connected': speech.get('connected') is True,
            'counter_domain': label(speech.get('counter_domain'),
                {'legacy_render_cumulative_v1', 'target_decoded_input_v2'}),
            'render_activity_observed': speech.get('render_activity_observed') is True,
            'render_evidence': render_history_diagnostic(speech.get('render_evidence')),
            'frames': count(speech.get('frames')),
            'activity_frames': None if speech.get('counter_domain') == 'target_decoded_input_v2'
                else count(speech.get('activity_frames')),
            **{key: measurement(speech.get(key)) for key in
                ('received_duration', 'received_energy', 'quiet_received_seconds')}}}


def render_history_diagnostic(value):
    """Fixed scalar history observations; never PCM, page data or identifiers."""
    if not isinstance(value, dict) or value.get('kind') != 'native-history-v1':
        return None
    def frame(v, minimum=0):
        return v if type(v) is int and minimum <= v <= 10**15 else None
    def measurement(v):
        return v if type(v) in (int, float) and math.isfinite(v) and 0 <= v <= 10**12 else None
    return {'kind': 'native-history-v1',
        **{key: frame(value.get(key)) for key in ('clock_frame', 'coverage_epoch', 'window_frames',
            'quiet_from_frame', 'quiet_through_frame')},
        'latest_active_block_min_frame': frame(value.get('latest_active_block_min_frame'), -33280),
        **{key: measurement(value.get(key)) for key in ('peak_rms', 'quiet_seconds')},
        **{key: value.get(key) if type(value.get(key)) is bool else None for key in
            ('fresh', 'coverage_valid', 'active', 'graph_checkpoint_observed')}}


def sender_selection_diagnostic(value):
    """Fixed counters only; bound size even if a page supplies hostile values."""
    if not isinstance(value, dict):
        return None
    def count(v):
        return min(v, 65535) if type(v) is int and v >= 0 else 0
    def direction(v):
        return v if isinstance(v, str) and v in {'sendonly','sendrecv','recvonly','inactive','none','other'} else 'other'
    def audio_rows(peer):
        rows=peer.get('audio_transceivers')
        rows=rows if isinstance(rows,list) else []
        allowed={'peer_closed','transceiver_stopped','stopped_unknown','sender_missing',
            'sender_track_present','current_not_sending','sender_not_member','eligible'}
        return {'audio_transceivers_truncated':peer.get('audio_transceivers_truncated') is True or len(rows)>8,
            'audio_transceivers':[{'direction':direction(row.get('direction')),
                'currentDirection':direction(row.get('currentDirection')),
                'sender_member':row.get('sender_member') is True,
                'sender_track_null':row.get('sender_track_null') is True,
                'stopped':row.get('stopped') is True,
                'rejection_reason':row.get('rejection_reason')
                    if isinstance(row.get('rejection_reason'),str)
                    and row['rejection_reason'] in allowed else 'unknown'}
                for row in rows[:8] if isinstance(row,dict)]}
    def counters(v):
        return {key:count(v.get(key)) for key in ('sender_count', 'track_count',
            'audio_count', 'audio_live_count', 'audio_ended_count',
            'audio_enabled_count', 'audio_muted_count', 'eligible_count',
            'transceiver_count', 'receiver_audio_count', 'receiver_video_count',
            'receiver_absent_count', 'receiver_other_count', 'stopped_count',
            'stopped_unknown_count', 'null_sender_count', 'null_send_eligible_count',
            *(prefix+'_'+kind for prefix in ('direction','current','audio_direction','audio_current')
                for kind in ('sendonly','sendrecv','recvonly','inactive','none','other')))}
    frames = value.get('frames')
    frames = frames if isinstance(frames, list) else []
    result = {'schema_version':1, 'frame_count':count(value.get('frame_count')),
        'eligible_count':count(value.get('eligible_count')), 'frames':[],
        'enumeration_complete':value.get('enumeration_complete') is True,
        'frames_truncated':value.get('frames_truncated') is True or len(frames) > 8,
        'outcome':value.get('outcome') if isinstance(value.get('outcome'), str)
            and value['outcome'] in {'scanning','none','multiple','selected',
                'evaluation_failed','binding_failed'} else 'unknown'}
    for frame in frames[:8]:
        if not isinstance(frame, dict):
            continue
        peers = frame.get('peers')
        peers = peers if isinstance(peers, list) else []
        result['frames'].append({**counters(frame),
            'hook_available':frame.get('hook_available') is True,
            'peers_available':frame.get('peers_available') is True,
            'peer_count':count(frame.get('peer_count')),
            'peers_truncated':frame.get('peers_truncated') is True or len(peers) > 16,
            'peers':[{**counters(peer), 'peer_available':peer.get('peer_available') is True,
                'transceivers_available':peer.get('transceivers_available') is True,
                **audio_rows(peer),
                'closed':peer.get('closed') is True}
                for peer in peers[:16] if isinstance(peer, dict)]})
    return result


PREJOIN_UI_ROLES=('radio','button','link','textbox','heading','dialog')
PREJOIN_UI_NAMES=('computer_audio_radio','browser_continue_button','browser_continue_link',
    'join_button','guest_entry_button','guest_entry_link','sign_in_button','sign_in_link',
    'unsupported_message','lobby_message','ended_message','error_message','dismiss_button',
    'audio_fallback_button','app_open_button','auth_error_message','meeting_missing_message',
    'media_blocked_message','cookies_blocked_message','continue_without_av_button',
    'dont_use_audio_button','join_without_audio_button','device_preview_dialog_message','leave_button','mute_button')
PREJOIN_UI_CATEGORIES={'unknown','prejoin','browser_choice','guest_entry','login_prompt',
    'unsupported_browser','admission_lobby','meeting_ended','error_message','multiple_known_categories',
    'auth_error','meeting_unavailable','media_permission_or_device_message','cookies_message','app_choice','audio_fallback','meeting_controls_candidate'}


def prejoin_ui_diagnostic(value):
    """Only fixed enums/counters: never browser text, names, URLs or attributes."""
    if not isinstance(value,dict):
        return None
    def count(item):
        return min(item,65535) if type(item) is int and item>=0 else 0
    def millis(item):
        return min(item,900000) if type(item) is int and item>=0 else None
    roles=value.get('role_counts');roles=roles if isinstance(roles,dict) else {}
    names=value.get('safe_name_counts');names=names if isinstance(names,dict) else {}
    unknown=value.get('unknown_name_counts');unknown=unknown if isinstance(unknown,dict) else {}
    categories=value.get('category_candidates');categories=categories if isinstance(categories,list) else []
    scopes={'last_complete_observation','previous_complete_observation','before_context_invalidation','not_available','post_join_observation'}
    return {'schema_version':1,
        'category':value.get('category') if isinstance(value.get('category'),str)
            and value['category'] in PREJOIN_UI_CATEGORIES else 'unknown',
        'category_candidates':sorted({x for x in categories if isinstance(x,str)
            and x in PREJOIN_UI_CATEGORIES and x not in {'unknown','multiple_known_categories'}})[:16],
        'origin_kind':value.get('origin_kind') if isinstance(value.get('origin_kind'),str) and value.get('origin_kind') in
            {'teams','microsoft_identity','browser_error','other','unknown'} else 'unknown',
        'role_counts':{key:count(roles.get(key)) for key in PREJOIN_UI_ROLES},
        'safe_name_counts':{key:count(names.get(key)) for key in PREJOIN_UI_NAMES},
        'unknown_name_counts':{key:count(unknown.get(key)) for key in ('radio','button','link')},
        'text_lookup_supported':value.get('text_lookup_supported') is True,
        'sample_scope':value.get('sample_scope') if isinstance(value.get('sample_scope'),str) and value.get('sample_scope') in scopes else 'not_available',
        'sample_time_basis':'complete_scan_end',
        'sample_sequence':count(value.get('sample_sequence')),
        'observed_elapsed_ms':millis(value.get('observed_elapsed_ms')),
        'sample_age_ms':millis(value.get('sample_age_ms')),
        'observation_duration_ms':millis(value.get('observation_duration_ms')),
        'latest_attempt_error':value.get('latest_attempt_error') if isinstance(value.get('latest_attempt_error'),str) and value.get('latest_attempt_error') in
            {'none','playwright_error','timeout','target_closed'} else 'none'}


def post_join_ui_diagnostic(value):
    """Only a fresh complete post-Join sample may publish UI categories/counts."""
    value = value if isinstance(value, dict) else {}
    reasons = {'none', 'not_attempted', 'pending', 'stale', 'stopped', 'deadline', 'cancelled',
        'timeout', 'target_closed', 'context_changed', 'observation_failed', 'playwright_error'}
    reason = value.get('unavailable_reason')
    reason = reason if isinstance(reason, str) and reason in reasons else 'not_attempted'
    snapshot = prejoin_ui_diagnostic(value.get('snapshot'))
    available = (value.get('available') is True and reason == 'none' and snapshot is not None
        and snapshot['sample_scope'] == 'post_join_observation'
        and snapshot['sample_age_ms'] is not None and snapshot['sample_age_ms'] <= 1500)
    if reason == 'none' and not available:
        reason = 'stale'
    age = value.get('last_sample_age_ms')
    attempts = value.get('observation_attempts')
    return {'schema_version': 1, 'observation_scope': 'post_join_only',
        'available': available, 'unavailable_reason': reason, 'freshness_limit_ms': 1500,
        'last_sample_age_ms': min(age, 900000) if type(age) is int and age >= 0 else None,
        'observation_attempts': min(attempts, 65535) if type(attempts) is int and attempts >= 0 else 0,
        'snapshot': snapshot if available else None}


def computer_audio_wait_diagnostic(value):
    if not isinstance(value, dict):
        return None
    def enum(key, values, fallback):
        item=value.get(key)
        return item if isinstance(item,str) and item in values else fallback
    result={'stage':enum('stage',{'select','verify'},'select'),
        'wait_outcome':enum('wait_outcome',{'not_waited','waiting','became_visible','deadline','stopped','cancelled','error'},'error'),
        'classification':enum('classification',{'scanning','radio_absent','radio_hidden','visible','ambiguous','not_selected','checked','context_invalid','frame_snapshot_discarded'},'scanning'),
        'invalidation_reason':enum('invalidation_reason',{'unknown','none','page_closed','page_replaced','frame_detached','frame_removed','roster_changed'},'unknown'),
        'invalidation_stage':enum('invalidation_stage',{'none','primary_scan','auxiliary_scan','radio_readback','radio_dispatch','verify_return','join_visibility','join_dispatch','preview_scan','preview_dispatch','preview_return'},'none'),
        'auxiliary_error_kind':enum('auxiliary_error_kind',{'none','playwright_error','timeout','target_closed'},'none'),
        'auxiliary_exception_type':enum('auxiliary_exception_type',{'none','Error','TimeoutError','TargetClosedError'},'none'),
        'auxiliary_error_observation':enum('auxiliary_error_observation',{'none','all_radio_count','dialog_count',
            'known_browser_button_count','known_join_button_count','document_state'},'none'),
        'document_state':enum('document_state',{'loading','interactive','complete','unknown'},'unknown')}
    for key in ('poll_count','rescan_count','auxiliary_error_count','frame_count','exact_radio_count','visible_radio_count',
                'all_radio_count','dialog_count','known_browser_button_count','known_join_button_count'):
        item=value.get(key)
        result[key]=min(item,65535) if type(item) is int and item>=0 else 0
    result['browser_continue_completed']=value.get('browser_continue_completed') is True
    result['context_counts_checked']=value.get('context_counts_checked') is True
    result['observation_error']=value.get('observation_error') is True
    for key in ('page_closed','frame_checked','frame_detached','frame_member','invalidation_recorded','primary_scan_complete'):
        result[key]=value.get(key) is True
    count_keys=('all_radio_count','dialog_count','known_browser_button_count','known_join_button_count')
    prior_complete=(all(type(value.get(key)) is int and value[key]>=0 for key in count_keys)
        and isinstance(value.get('document_state'),str) and value.get('document_state') in {'loading','interactive','complete'})
    result['context_counts_scope']=('last_missing_scan' if result['context_counts_checked'] else
        'previous_complete_missing_scan' if prior_complete else 'not_checked')
    result['invalidation_scope']='last_recorded_invalidation' if result['invalidation_recorded'] else 'none'
    result['primary_counts_scope']='last_complete_primary_scan' if result['primary_scan_complete'] else 'incomplete_or_discarded'
    for key in ('primary_observed_elapsed_ms','primary_sample_age_ms','context_observed_elapsed_ms',
                'context_sample_age_ms','context_observation_duration_ms'):
        item=value.get(key)
        result[key]=min(item,900000) if type(item) is int and item>=0 else None
    result['ui_snapshot']=prejoin_ui_diagnostic(value.get('ui_snapshot'))
    return result


def teams_join_diagnostic(value):
    """Fixed enums/booleans only; independent of page/provider payload shape."""
    value = value if isinstance(value, dict) else {}
    def enum(item, allowed, fallback):
        return item if isinstance(item, str) and item in allowed else fallback
    result = {'schema_version': 1,
              'phase': enum(value.get('phase'), JOIN_PHASES, 'unknown'),
              'page_state': enum(value.get('page_state'), JOIN_PAGE_STATES, 'unknown'),
              'outcome': enum(value.get('outcome'), JOIN_OUTCOMES, 'unknown'),
              'exception_type': enum(value.get('exception_type'), JOIN_EXCEPTION_TYPES, 'none')}
    for key in ('context_ready', 'page_ready', 'goto_started', 'goto_completed',
                'body_observed', 'join_dispatch_started', 'join_dispatch_completed',
                'preview_navigation_dispatched','preview_navigation_completed'):
        result[key] = value.get(key) is True
    for field in ('controls_checked', 'controls_seen'):
        controls = value.get(field)
        controls = controls if isinstance(controls, dict) else {}
        result[field] = {key: controls.get(key) is True for key in sorted(JOIN_CONTROLS)}
    actions = value.get('actions')
    actions = actions if isinstance(actions, dict) else {}
    result['actions'] = {key: enum(actions.get(key), JOIN_RESULTS, 'not_attempted')
                         for key in sorted(JOIN_ACTIONS)}
    errors = value.get('errors')
    result['errors'] = [{'phase': enum(e.get('phase'), JOIN_PHASES, 'unknown'),
                         'action': enum(e.get('action'), JOIN_CONTROLS, 'unknown'),
                         'exception_type': enum(e.get('exception_type'), JOIN_EXCEPTION_TYPES, 'Exception')}
                        for e in errors[-8:] if isinstance(e, dict)] if isinstance(errors, list) else []
    result['computer_audio_wait']=computer_audio_wait_diagnostic(value.get('computer_audio_wait'))
    result['post_join_ui']=post_join_ui_diagnostic(value.get('post_join_ui'))
    return result


def live_failure(value):
    """Persist only fixed enums, independently of a provider's raw error text."""
    value = value if isinstance(value, dict) else {}
    fields = [('error_code', LIVE_CODES, 'live_error_unknown'),
              ('channel', LIVE_CHANNELS, 'unknown'), ('type', LIVE_TYPES, 'unknown'),
              ('reason', LIVE_REASONS, 'unknown'), ('stage', LIVE_STAGES, 'unknown')]
    return {key: value.get(key) if isinstance(value.get(key), str) and value[key] in allowed else fallback
            for key, allowed, fallback in fields}


def failure_diagnostic(exc, phase):
    kind = type(exc).__name__
    if kind not in EXCEPTION_TYPES:
        kind = "Exception"
    result = {"phase": phase if phase in PHASES else "unknown", "exception_type": kind,
              "error_code": "timeout" if isinstance(exc, TimeoutError) else "operation_failed"}
    for frame in reversed(traceback.extract_tb(exc.__traceback__)):
        name = frame.filename.rsplit("/", 1)[-1]
        if name in FILES:
            result["source"] = {"file": name, "line": frame.lineno}
            break
    return result


SCREEN_CONTEXT_MODES = {'off', 'on_demand', 'watch_and_cache'}
SCREEN_CAPTURE_METHOD_LABELS = {'webrtc_video_frame', 'teams_stage_dom', 'none'}
SCREEN_CAPTURE_MATCH_REASONS = {
    'explicit_share', 'label_share', 'track_share',
    'powerpoint_live_frame', 'stage_dom_candidate',
    'blank_video', 'no_video', 'below_size', 'camera_excluded',
    'unmatched_video', 'evaluate_error', 'page_unavailable',
}
SCREEN_ERROR_CODES = {
    'vision_account_denied', 'vision_model_invalid', 'vision_image_invalid',
    'vision_turn_failed', 'vision_response_invalid', 'vision_timeout',
    'vision_cleanup_unknown', 'vision_tool_forbidden', 'vision_analyzer_closed',
    'vision_question_invalid', 'screen_watch_failed', 'screen_context_failed',
    'vision_close_failed', 'frame_cache_close_failed', 'screen_cleanup_unknown',
}
SCREEN_CLEANUP_LABELS = {'idle', 'cleaning', 'cleaned', 'unknown'}


def screen_context_diagnostic(value):
    """Allowlisted screen-context status: counts and fixed labels only, never OCR/images."""
    if not isinstance(value, dict):
        return None
    def count(item, maximum=2147483647):
        return min(item, maximum) if type(item) is int and item >= 0 else 0
    mode = value.get('mode')
    method = value.get('capture_method')
    err = value.get('last_error_code')
    cleanup = value.get('cleanup_status')
    cache = value.get('cache')
    cache_diag = None
    if isinstance(cache, dict):
        cache_diag = {
            'cached_frames': count(cache.get('cached_frames'), 1024),
            'cached_bytes': count(cache.get('cached_bytes')),
            'max_frames': count(cache.get('max_frames'), 1024),
            'closed': cache.get('closed') is True,
        }
    probe = value.get('probe')
    probe_diag = None
    if isinstance(probe, dict):
        reason = probe.get('match_reason')
        probe_diag = {
            'frames_checked': count(probe.get('frames_checked'), 256),
            'videos_total': count(probe.get('videos_total'), 1024),
            'videos_ready': count(probe.get('videos_ready'), 1024),
            'videos_excluded_camera': count(probe.get('videos_excluded_camera'), 1024),
            'videos_unmatched': count(probe.get('videos_unmatched'), 1024),
            'iframes_total': count(probe.get('iframes_total'), 256),
            'canvases_large': count(probe.get('canvases_large'), 256),
            'powerpoint_frames_seen': count(probe.get('powerpoint_frames_seen'), 64),
            'max_video_width': count(probe.get('max_video_width'), 16384),
            'max_video_height': count(probe.get('max_video_height'), 16384),
            'max_rect_width': count(probe.get('max_rect_width'), 16384),
            'max_rect_height': count(probe.get('max_rect_height'), 16384),
            'match_reason': reason if isinstance(reason, str) and reason in SCREEN_CAPTURE_MATCH_REASONS else 'unknown',
        }
    return {
        'mode': mode if isinstance(mode, str) and mode in SCREEN_CONTEXT_MODES else 'unknown',
        'started': value.get('started') is True,
        'share_active': value.get('share_active') is True,
        'capture_method': method if isinstance(method, str) and method in SCREEN_CAPTURE_METHOD_LABELS else None,
        'observations_saved': count(value.get('observations_saved')),
        'vision_calls': count(value.get('vision_calls')),
        'vision_failures': count(value.get('vision_failures')),
        'cache_hits': count(value.get('cache_hits')),
        'deep_look_calls': count(value.get('deep_look_calls')),
        'last_error_code': err if isinstance(err, str) and err in SCREEN_ERROR_CODES else ('unknown' if err else None),
        'last_capture_seq': count(value.get('last_capture_seq')),
        'last_frame_width': count(value.get('last_frame_width'), 16384) if value.get('last_frame_width') is not None else None,
        'last_frame_height': count(value.get('last_frame_height'), 16384) if value.get('last_frame_height') is not None else None,
        'cache': cache_diag,
        'probe': probe_diag,
        'cleanup_status': cleanup if isinstance(cleanup, str) and cleanup in SCREEN_CLEANUP_LABELS else 'unknown',
    }
