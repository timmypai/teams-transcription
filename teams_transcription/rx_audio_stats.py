"""Fixed, count-only RX observations; no audio or arbitrary peer/event payloads."""
import asyncio
import struct

from .diagnostics import PHASES
from .storage import stamp

MAX_COUNT = 2**53 - 1
CONTEXT_STATES = {'absent', 'running', 'suspended', 'closed', 'interrupted', 'unknown'}
PEER_STATES = {'new', 'connecting', 'connected', 'disconnected', 'failed', 'closed', 'unknown'}
CHANNEL_STATES = {'connecting', 'open', 'closing', 'closed', 'unknown'}
LIVE_EVENT_KEYS = ('rpc_events', 'rpc_item_started', 'rpc_item_completed', 'rpc_transcript_done', 'rpc_transcript_delta',
                   'rpc_role_user', 'rpc_role_assistant', 'rpc_role_unknown',
                   'rpc_error', 'rpc_closed', 'datachannel_events', 'datachannel_transcript_completed',
                   'datachannel_role_user', 'datachannel_role_assistant', 'datachannel_role_unknown',
                   'datachannel_transcript_filtered', 'normalizer_filtered_events',
                   'datachannel_speech_started', 'datachannel_speech_stopped', 'datachannel_error',
                   'datachannel_closed', 'datachannel_input_transcript_delta', 'datachannel_output_transcript_delta',
                   'datachannel_turn_done', 'datachannel_turn_role_user', 'datachannel_turn_role_assistant',
                   'datachannel_turn_role_unknown', 'datachannel_session_started', 'datachannel_session_updated',
                   'datachannel_output_audio_delta', 'datachannel_delegation_created', 'datachannel_unrecognized_type',
                   'rpc_notifications_seen', 'rpc_done_seen_before_scope', 'rpc_completed_seen_before_scope',
                   'rpc_scope_rejected_missing', 'rpc_scope_rejected_null', 'rpc_scope_rejected_foreign',
                   'rpc_scope_rejected_invalid', 'rpc_final_scope_rejected',
                   'rpc_item_added', 'rpc_item_transcript_delta', 'rpc_unknown_method',
                   'normalizer_unconfirmed_canonical')


def count(value):
    return min(value, MAX_COUNT) if type(value) is int and value >= 0 else None


def total(values):
    values = [count(v) for v in values]
    return min(sum(values), MAX_COUNT) if all(v is not None for v in values) else None


def nonzero_samples(data):
    return sum(sample != 0 for (sample,) in struct.iter_unpack('<h', data))


class RxAudioSnapshot:
    def __init__(self, session, *, input_mode='pcm'):
        self.session = session
        self.input_mode = input_mode
        self.browser = self.transport = None
        self.python = dict.fromkeys(('incoming_packets', 'incoming_frames', 'incoming_nonzero_samples',
                                    'queued_packets', 'queued_frames', 'queued_nonzero_samples',
                                    'consumed_packets', 'consumed_frames', 'consumed_nonzero_samples',
                                    'invalid_packets', 'queue_overflows'), 0)
        self.push = dict.fromkeys(('attempted_packets', 'attempted_frames', 'returned_packets',
                                  'returned_frames', 'returned_nonzero_samples',
                                  'returned_last_nonzero_frame', 'returned_trailing_exact_zero_frames',
                                  'returned_max_exact_zero_frames'), 0)
        self.final_callbacks_completed = 0

    def incoming(self, data):
        self.python['incoming_packets'] += 1
        self.python['incoming_frames'] += len(data) // 2
        self.python['incoming_nonzero_samples'] += nonzero_samples(data)

    def queued(self, data):
        self.python['queued_packets'] += 1
        self.python['queued_frames'] += len(data) // 2
        self.python['queued_nonzero_samples'] += nonzero_samples(data)

    def consumed(self, data):
        nonzero = nonzero_samples(data)
        self.python['consumed_packets'] += 1
        self.python['consumed_frames'] += len(data) // 2
        self.python['consumed_nonzero_samples'] += nonzero
        return nonzero

    def push_attempted(self, data):
        self.push['attempted_packets'] += 1
        self.push['attempted_frames'] += len(data) // 2

    def push_returned(self, data, nonzero):
        offset = self.push['returned_frames']
        # Observe PCM only; do not retain it or infer speech/VAD/finality.
        for index, (sample,) in enumerate(struct.iter_unpack('<h', data), 1):
            if sample:
                self.push['returned_last_nonzero_frame'] = offset + index
                self.push['returned_trailing_exact_zero_frames'] = 0
            else:
                self.push['returned_trailing_exact_zero_frames'] += 1
                self.push['returned_max_exact_zero_frames'] = max(
                    self.push['returned_max_exact_zero_frames'], self.push['returned_trailing_exact_zero_frames'])
        self.push['returned_packets'] += 1
        self.push['returned_frames'] += len(data) // 2
        self.push['returned_nonzero_samples'] += nonzero

    def observe_browser(self, reports):
        # An empty/detached poll cannot erase the last observed track state.
        if not isinstance(reports, list) or not reports or len(reports) > 128 or not all(isinstance(r, dict) for r in reports):
            return
        readiness = [r.get('capture_readiness') if isinstance(r.get('capture_readiness'), dict) else {} for r in reports]
        self.browser = {'observed_at': stamp(), 'frame_reports': len(reports),
            **{key: total(r.get(key) for r in reports) for key in
               ('packets', 'frames', 'nonzero_samples', 'tracks', 'dropped', 'playout_errors')},
            'live_tracks': total(r.get('live_attached_tracks') for r in readiness),
            'muted_tracks': total(r.get('muted_live_tracks') for r in readiness),
            'context_states': sorted({r.get('context_state') if isinstance(r.get('context_state'), str)
                and r['context_state'] in CONTEXT_STATES else 'unknown' for r in readiness}),
            'attach_error_count': total(len(r['attach_errors']) if isinstance(r.get('attach_errors'), list) else None for r in readiness)}

    def observe_transport(self, value):
        if not isinstance(value, dict) or not value:
            return
        reports = value.get('reports')
        reports = reports if isinstance(reports, list) and len(reports) <= 128 and all(isinstance(r, dict) for r in reports) else None
        mode = value.get('input_mode')
        mode = mode if isinstance(mode, str) and mode in {'native_media', 'pcm'} else self.input_mode
        self.transport = {'observed_at': stamp(), 'input_mode': mode,
            'scheduled_frames': count(value.get('sent_frames')) if mode == 'pcm' else None,
            'connection': value.get('connection') if isinstance(value.get('connection'), str) and value['connection'] in PEER_STATES else 'unknown',
            'data_channel': value.get('data_channel') if isinstance(value.get('data_channel'), str) and value['data_channel'] in CHANNEL_STATES else 'unknown',
            'outbound_packets': total(r.get('packetsSent') for r in reports) if reports is not None else None,
            'outbound_bytes': total(r.get('bytesSent') for r in reports) if reports is not None else None}
        self.transport.update({key: count(value.get(key)) for key in
            ('audio_context_sample_rate', 'sender_track_sample_rate', 'sender_track_channels')})
        self.transport.update({key: count(value.get(key)) if mode == 'pcm' else None for key in
            ('scheduled_queue_ms', 'buffer_gap_count', 'buffer_gap_frames', 'max_buffer_gap_frames')})
        source = value.get('native_source')
        self.transport['native_source'] = ({
            **{key: source.get(key) if type(source.get(key)) is bool else None
                for key in ('available', 'ready', 'observation_ready', 'failed', 'stopped', 'capture_enabled')},
            **{key: count(source.get(key)) if source.get('available') is not False else None for key in
                ('frame_count', 'connected_frames', 'live_tracks', 'packets_received', 'bytes_received')},
            'failure_code': source.get('failure_code') if isinstance(source.get('failure_code'), str)
                and source['failure_code'] in {'none', 'native_media_scope_lost', 'native_media_setup_failed',
                    'native_media_source_lost', 'native_media_cleanup_unconfirmed'} else 'unknown',
            'cleanup_status': source.get('cleanup_status') if isinstance(source.get('cleanup_status'), str)
                and source['cleanup_status'] in {'idle', 'pending', 'cleaned', 'unknown'} else 'unknown'}
            if mode == 'native_media' and isinstance(source, dict) else None)
        state = value.get('audio_context_state')
        self.transport['audio_context_state'] = state if isinstance(state, str) and state in CONTEXT_STATES else 'unknown'
        delivery = value.get('datachannel_delivery')
        self.transport['datachannel_delivery'] = {key: count(delivery.get(key)) for key in
            ('messages', 'json_errors', 'binding_attempted', 'binding_returned', 'binding_failed')} if isinstance(delivery, dict) else None

    def persist(self, *, phase, queue_depth, provider, session=None):
        events = getattr(provider, 'received_events', None)
        events = events if isinstance(events, dict) else None
        rpc = getattr(provider, 'rpc', None)
        reader = getattr(rpc, 'notification_counts', None)
        reader = reader if isinstance(reader, dict) else None
        notifications = getattr(rpc, 'notifications', None)
        receiver = getattr(provider, 'receiver', None)
        mode = getattr(rpc, 'thread_history_mode', None)
        snapshot = {'schema_version': 2, 'meeting_id': self.session.ident, 'updated_at': stamp(),
            'input_mode': self.input_mode,
            'phase': phase if phase in PHASES else 'unknown', 'browser': self.browser,
            'python': {**{key: count(value) for key, value in self.python.items()}, 'queue_depth': count(queue_depth),
                'all_zero': self.python['consumed_nonzero_samples'] == 0 if self.python['consumed_frames'] else None},
            'live_push': {key: count(value) for key, value in self.push.items()} if self.input_mode == 'pcm' else None,
            'live_transport': self.transport,
            'server_events': {'visibility': 'public_callbacks_only', 'available': events is not None,
                'thread_history_mode': mode if isinstance(mode, str) and mode in ('legacy', 'paginated') else 'unknown',
                'rpc_reader_counts': {key: count(reader.get(key)) for key in
                    ('notifications', 'transcript_done', 'item_completed')} if reader is not None else None,
                'rpc_queue_depth': count(notifications.qsize()) if isinstance(notifications, asyncio.Queue) else None,
                'receiver_done': receiver.done() if isinstance(receiver, asyncio.Task) else None,
                'counts': {key: count(events.get(key)) for key in LIVE_EVENT_KEYS} if events is not None else None,
                'normalized_finals': count(getattr(provider, 'source_count', None)),
                'persisted_final_callbacks': count(self.final_callbacks_completed)}}
        # This merge never writes state, readiness, error or diagnostic fields.
        (session or self.session).update(rx_audio=snapshot, pcm_audio_seconds=self.python['consumed_frames'] / 24000,
            **({'audio_seconds': self.python['consumed_frames'] / 24000} if self.input_mode == 'pcm' else {}))
        return snapshot
