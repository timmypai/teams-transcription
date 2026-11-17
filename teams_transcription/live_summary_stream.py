"""Bounded application assembly of pinned Live assistant transcript notifications.

This is not a provider response-completion contract. A caller supplies a nonce
and validates a JSON-plus-sentinel candidate before considering publication.
Only flat RPC delta/done are understood; no public Realtime event is aliased.
Done is a snapshot replacing the current delta segment, not a response end.
Distinct done segments are retained. A repeated done with no intervening delta
and identical text is an application snapshot replay; no source ID is invented.
Without a source ID/revision, a different done with no delta cannot be identified
as a correction of a prior segment; it remains a distinct segment and is
revalidated. This helper does not solve that ambiguous correction case.
"""
import json
import re
import time
import uuid
from types import MappingProxyType

from .live_summary_output_evidence import ASSEMBLY_HEAD_BYTES, text_prefix, text_suffix
from .summary import SECTIONS, validate_summary


class LiveSummaryStreamError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('duplicate_json_key')
        value[key] = item
    return value


def _reject_constant(_value):
    raise ValueError('non_json_constant')


class LiveSummaryStream:
    def __init__(self, meeting_id, thread_id, entries, *, nonce=None,
                 max_bytes=524288, max_chars=None, timeout=60, clock=time.monotonic):
        nonce = uuid.uuid4().hex if nonce is None else nonce
        if (not isinstance(meeting_id, str) or not meeting_id or not isinstance(thread_id, str) or not thread_id
                or not isinstance(nonce, str) or not re.fullmatch('[0-9a-f]{32}', nonce)
                or type(max_bytes) is not int or not 512 <= max_bytes <= 4 * 1024 * 1024
                or max_chars is not None and (type(max_chars) is not int or not 256 <= max_chars <= 4 * 1024 * 1024)
                or type(timeout) not in (int, float) or not 0 < timeout <= 600):
            raise ValueError('invalid_live_summary_stream_config')
        frozen, ids, input_bytes = [], set(), 0
        for entry in entries:
            if (not isinstance(entry, dict) or not isinstance(entry.get('id'), str)
                    or not 1 <= len(entry['id']) <= 200 or not isinstance(entry.get('text'), str)
                    or entry['id'] in ids or len(entry['text']) > 65536):
                raise ValueError('invalid_live_summary_sources')
            minimal = {'id': entry['id'], 'text': entry['text']}
            try:
                input_bytes += len(json.dumps(minimal, ensure_ascii=False).encode('utf-8'))
            except UnicodeError:
                raise ValueError('invalid_live_summary_sources') from None
            if input_bytes > 65536 or len(frozen) >= 2048:
                raise ValueError('live_summary_sources_overflow')
            ids.add(entry['id'])
            frozen.append(MappingProxyType(minimal))
        if not frozen:
            raise ValueError('invalid_live_summary_sources')
        self.entries = tuple(frozen)
        self.meeting_id, self.thread_id, self.nonce = meeting_id, thread_id, nonce
        self.marker = '[[SUMMARY_END:' + nonce + ']]'
        self.max_bytes, self.max_chars, self.clock = max_bytes, max_chars, clock
        self.deadline = clock() + timeout
        self._segments, self._current = [], ''
        self._committed_bytes = self._current_bytes = self._committed_chars = 0
        self._summary, self._failure = None, None
        self._events, self._duplicates = 0, 0
        self._candidate_state = 'no_assistant_text'
        self._json_decoded = False
        self._schema_state = 'not_checked'
        self._last_assembly_bytes = 0
        self._capture_text = False
        self._last_assembly_text = None
        self._last_assembly_tail = None

    def enable_synthetic_text_evidence(self):
        """Explicitly enable bounded head/tail retention before any assistant event."""
        if self._events:
            raise ValueError('summary_stream_capture_requires_fresh_stream')
        self._capture_text = True

    def diagnostic_snapshot(self):
        # Does not invoke _check, mutate assembly, or turn timeout into success.
        result = {'candidate_state': self._candidate_state, 'json_decoded': self._json_decoded,
            'assembly_snapshot_scope': 'last_evaluated_candidate_before_clear',
            'schema_state': self._schema_state, 'assembly_bytes': self._last_assembly_bytes,
            'stream_state': self.status()['state'], 'stream_error': self._failure,
            'assistant_events': self._events, 'snapshot_replays': self._duplicates,
            'provider_response_done': False}
        if self._capture_text:
            result['expected_marker'] = self.marker
            result['assembly_prefix'] = dict(self._last_assembly_text or text_prefix(''))
            result['assembly_suffix'] = dict(self._last_assembly_tail or text_suffix(''))
        return result

    @property
    def ready(self):
        return self._summary is not None and self._failure is None

    def status(self):
        return {'meeting_id': self.meeting_id, 'thread_id': self.thread_id,
                'state': 'failed' if self._failure else 'application_complete' if self.ready else 'collecting',
                'error': self._failure, 'assistant_events': self._events,
                'snapshot_replays': self._duplicates, 'provider_response_done': False,
                'service_contract_verified': False}

    def _fail(self, code):
        self._failure, self._summary = code, None
        self._segments, self._current = [], ''
        self._committed_bytes = self._current_bytes = self._committed_chars = 0
        raise LiveSummaryStreamError(code)

    def _check(self):
        if self._failure:
            raise LiveSummaryStreamError(self._failure)
        if not self.ready and self.clock() >= self.deadline:
            self._fail('summary_stream_timeout')

    def feed(self, method, params, *, meeting_id=None):
        self._check()
        if meeting_id is not None and meeting_id != self.meeting_id:
            return self.ready
        if not isinstance(params, dict) or not isinstance(method, str):
            self._fail('summary_stream_invalid_binding')
        thread = params.get('threadId')
        if isinstance(thread, str) and thread != self.thread_id:
            return self.ready
        if method == 'client/exit':
            self._fail('summary_stream_transport_exit')
        if thread != self.thread_id:
            self._fail('summary_stream_invalid_binding')
        item = params.get('item')
        if (method in {'thread/realtime/handoff', 'thread/realtime/handoffRequested', 'delegation.created'}
                or isinstance(item, dict) and item.get('type') in {'handoff_request', 'handoffRequest'}):
            self._fail('summary_stream_handoff_rejected')
        if method.startswith(('turn/', 'item/')):
            self._fail('summary_stream_regular_turn_rejected')
        if method in {'thread/realtime/error', 'thread/realtime/closed'}:
            self._fail('summary_stream_provider_closed' if method.endswith('/closed') else 'summary_stream_provider_error')
        if method not in {'thread/realtime/transcript/delta', 'thread/realtime/transcript/done'}:
            return self.ready
        if params.get('role') != 'assistant':
            return self.ready
        field = 'delta' if method.endswith('/delta') else 'text'
        text = params.get(field)
        if not isinstance(text, str):
            self._fail('summary_stream_invalid_transcript')
        try:
            text_bytes = len(text.encode('utf-8')) if len(text) <= self.max_bytes else self.max_bytes + 1
            if text_bytes > self.max_bytes:
                self._fail('summary_stream_overflow')
        except UnicodeError:
            self._fail('summary_stream_invalid_transcript')
        self._events += 1
        if method.endswith('/done') and not self._current and self._segments and self._segments[-1] == text:
            self._duplicates += 1
            return self.ready
        retained_bytes = self._committed_bytes + (self._current_bytes if method.endswith('/delta') else 0)
        retained_chars = self._committed_chars + (len(self._current) if method.endswith('/delta') else 0)
        if (retained_bytes + text_bytes > self.max_bytes
                or self.max_chars is not None and retained_chars + len(text) > self.max_chars):
            self._fail('summary_stream_overflow')
        if method.endswith('/delta'):
            self._current += text
            self._current_bytes += text_bytes
        else:
            self._current, self._current_bytes = '', 0
            if text:
                self._segments.append(text)
                self._committed_bytes += text_bytes
                self._committed_chars += len(text)
        assembled = ''.join(self._segments) + self._current
        self._last_assembly_bytes = self._committed_bytes + self._current_bytes
        if self._capture_text:
            self._last_assembly_text = text_prefix(assembled, ASSEMBLY_HEAD_BYTES)
            self._last_assembly_tail = text_suffix(assembled)
        self._summary = None
        self._validate_candidate(assembled)
        return self.ready

    def _validate_candidate(self, assembled):
        text = assembled.lstrip()
        self._json_decoded = False
        self._schema_state = 'not_checked'
        decoder = json.JSONDecoder(object_pairs_hook=_strict_object, parse_constant=_reject_constant)
        try:
            value, end = decoder.raw_decode(text)
        except (ValueError, RecursionError):
            # A marker quoted inside summary text is source data. Detect only
            # an unquoted completed marker before rejecting malformed JSON.
            quoted = escaped = False
            unquoted_marker = False
            for index, character in enumerate(text):
                if not quoted and re.match(r'\[\[SUMMARY_END:[0-9a-f]{32}\]\]', text[index:index + 48]):
                    unquoted_marker = True
                    break
                if escaped:
                    escaped = False
                elif quoted and character == '\\':
                    escaped = True
                elif character == '"':
                    quoted = not quoted
            if unquoted_marker:
                self._candidate_state = 'invalid_json_with_unquoted_marker'
                self._fail('summary_stream_invalid_json')
            self._candidate_state = 'json_not_decodable'
            return  # Incomplete deltas are not model/provider failure evidence.
        self._json_decoded = True
        tail = text[end:].lstrip()
        if not tail:
            self._candidate_state = 'json_decoded_missing_marker'
            return
        if self.marker.startswith(tail):
            if tail != self.marker:
                self._candidate_state = 'partial_marker'
                return
        elif not tail.startswith(self.marker):
            if re.match(r'^\[\[SUMMARY_END:[0-9a-f]{32}\]\]', tail):
                self._candidate_state = 'nonce_mismatch'
                self._fail('summary_stream_nonce_mismatch')
            if '[[SUMMARY_END:'.startswith(tail) or tail.startswith('[[SUMMARY_END:') and ']]' not in tail:
                self._candidate_state = 'partial_marker'
                return
            self._candidate_state = 'unexpected_trailing_text'
            self._fail('summary_stream_unexpected_trailing_text')
        if tail[len(self.marker):].strip():
            self._candidate_state = 'unexpected_trailing_text'
            self._fail('summary_stream_unexpected_trailing_text')
        try:
            validate_summary(value, self.entries)
            for section in SECTIONS:
                for item in value[section]:
                    if set(item) != {'text', 'sources', 'owner', 'date'}:
                        raise ValueError('invalid_summary_item_keys')
        except (ValueError, TypeError):
            self._schema_state = 'invalid'
            self._candidate_state = 'invalid_summary'
            self._fail('summary_stream_invalid_summary')
        self._schema_state = 'valid'
        self._candidate_state = 'application_complete'
        self._summary = value

    def result(self):
        self._check()
        if not self.ready:
            raise LiveSummaryStreamError('summary_stream_incomplete')
        # A caller can read again after draining critical notifications. No
        # publication or provider-completion claim is performed by this helper.
        return {'summary': json.loads(json.dumps(self._summary)), 'provenance': {
            'provider': 'gpt_live', 'model': 'gpt-live-1-codex',
            'meeting_id': self.meeting_id, 'thread_id': self.thread_id,
            'criterion': 'application_sentinel', 'provider_response_done': False,
            'provider_final': False, 'service_contract_verified': False}}
