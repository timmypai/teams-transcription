"""Opt-in assistant text evidence for an explicitly synthetic Live probe only.

Never call this from ordinary meeting logging. RPC and bound DC observations
stay separate; no transcript is deduplicated or fed back into the parser here.
"""
import copy

TEXT_BYTES = 32768
EVENT_LIMIT = 128
ASSEMBLY_HEAD_BYTES = 24576
ASSEMBLY_TAIL_BYTES = 8192
LANES = frozenset({'rpc_delta', 'rpc_done', 'data_delta', 'data_done'})


def text_prefix(text, limit=TEXT_BYTES):
    """Bound work/allocation before encoding; preserve whole UTF-8 characters."""
    if type(limit) is not int or not 0 <= limit <= TEXT_BYTES:
        raise ValueError('invalid_synthetic_evidence_text_limit')
    if not isinstance(text, str):
        return {'text': '', 'bytes': 0, 'truncated': False, 'invalid_text': True}
    try:
        encoded = text[:limit].encode('utf-8')
    except UnicodeError:
        return {'text': '', 'bytes': 0, 'truncated': False, 'invalid_text': True}
    prefix = encoded[:limit].decode('utf-8', errors='ignore')
    return {'text': prefix, 'bytes': len(prefix.encode('utf-8')),
            'truncated': len(text) > limit or len(encoded) > limit, 'invalid_text': False}


def text_suffix(text, limit=ASSEMBLY_TAIL_BYTES):
    """Bounded end of assembly for examining marker/trailing text on truncation."""
    if type(limit) is not int or not 0 <= limit <= TEXT_BYTES:
        raise ValueError('invalid_synthetic_evidence_text_limit')
    if not isinstance(text, str):
        return {'text': '', 'bytes': 0, 'truncated': False, 'invalid_text': True}
    if not limit:
        return {'text': '', 'bytes': 0, 'truncated': bool(text), 'invalid_text': False}
    try:
        encoded = text[-limit:].encode('utf-8')
    except UnicodeError:
        return {'text': '', 'bytes': 0, 'truncated': False, 'invalid_text': True}
    suffix = encoded[-limit:].decode('utf-8', errors='ignore')
    return {'text': suffix, 'bytes': len(suffix.encode('utf-8')),
            'truncated': len(text) > limit or len(encoded) > limit, 'invalid_text': False}


class LiveSummaryOutputEvidence:
    def __init__(self):
        self.events = []
        self.text_bytes = 0
        self.observed_events = 0
        self.dropped_events = 0

    def record(self, lane, text, observed_ms):
        if lane not in LANES:
            return
        self.observed_events = min(1000000, self.observed_events + 1)
        if len(self.events) >= EVENT_LIMIT:
            self.dropped_events = min(1000000, self.dropped_events + 1)
            return
        prefix = text_prefix(text, TEXT_BYTES - self.text_bytes)
        self.text_bytes += prefix['bytes']
        self.events.append({'sequence': self.observed_events, 'lane': lane,
            'observed_ms': observed_ms if type(observed_ms) is int and 0 <= observed_ms <= 1000000 else None,
            **prefix})

    def snapshot(self, assembly):
        return {'version': 1, 'scope': 'explicit_synthetic_probe_assistant_only',
            'rpc_order_scope': 'own_receiver_processing_order',
            'data_order_scope': 'bound_callback_observation',
            'channels_merged': False, 'text_byte_limit': TEXT_BYTES,
            'assembly_text_byte_limit': TEXT_BYTES, 'event_limit': EVENT_LIMIT,
            'retained_text_bytes': self.text_bytes, 'observed_events': self.observed_events,
            'dropped_events': self.dropped_events, 'events': copy.deepcopy(self.events),
            'assembly': copy.deepcopy(assembly), 'service_contract_verified': False}
