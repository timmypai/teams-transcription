"""Allowlisted observation only; never grants admission or changes policy."""
from datetime import datetime
import uuid
import re
from zoneinfo import ZoneInfo

from .occurrence_window import in_join_window

GOOGLE_CODES = frozenset({
    'google_oauth_client_required', 'google_authorization_required', 'google_reauthorization_required',
    'private_calendar_configuration_invalid', 'private_calendar_directory_required',
    'google_transport_failed', 'google_token_exchange_failed', 'invalid_google_token_response',
    'calendar_response_too_large', 'invalid_google_response', 'calendar_request_failed',
    'invalid_calendar_window', 'invalid_calendar_pagination', 'calendar_window_too_large',
    'invalid_calendar_event', 'invalid_recurring_identity', 'calendar_event_too_large', 'calendar_poll_failed',
})


def safe_code(value):
    return value if isinstance(value,str) and value in GOOGLE_CODES else 'calendar_poll_failed'


def poll_readback(status, enabled, read, codes):
    google = {key:status.get(key) if type(status.get(key)) is bool else None
        for key in ('configured','authorized','reauthorization_required')}
    google['error_code'] = (None if 'error_code' in status and status['error_code'] is None
        else safe_code(status.get('error_code')))
    ready = (google['configured'] is True and google['authorized'] is True
        and google['reauthorization_required'] is False and google['error_code'] is None
        and enabled > 0 and read['completed'] and read['succeeded'] == enabled and not codes)
    return dict(poll_id=uuid.uuid4().hex,google=google,enabled_calendar_count=enabled,
        read=dict(read),error_codes=sorted({safe_code(code) for code in codes}),ready=ready)


def occurrence_availability(row, now):
    try:
        start = datetime.fromisoformat(row['start_at'])
        end = datetime.fromisoformat(row['end_at'])
        valid = (row['state'] == 'pending' and start.tzinfo is not None and end.tzinfo is not None
            and end.timestamp() > max(now,start.timestamp()))
        joinable = valid and in_join_window(row, now)
        return bool(valid and (start.timestamp() >= now or joinable)), bool(joinable)
    except (ValueError, TypeError, KeyError, OverflowError):
        return False, False


def validate_operation(value):
    """Validate the entire host export, including absence of private extra keys."""
    def require(condition):
        if not condition:
            raise ValueError('invalid_calendar_metadata')

    require(isinstance(value,dict) and set(value) == {
        'ok','timezone','poll_id','google','enabled_calendar_count','read','error_codes','ready','occurrences'})
    require(value['ok'] is True and type(value['ready']) is bool)
    require(isinstance(value['poll_id'],str) and re.fullmatch('[a-f0-9]{32}',value['poll_id']))
    require(isinstance(value['timezone'],str) and len(value['timezone']) <= 80)
    ZoneInfo(value['timezone'])
    google, read, enabled = value['google'], value['read'], value['enabled_calendar_count']
    require(isinstance(google,dict) and set(google) == {'configured','authorized','reauthorization_required','error_code'})
    require(all(type(google[key]) is bool for key in ('configured','authorized','reauthorization_required')))
    require(google['error_code'] is None or isinstance(google['error_code'],str) and google['error_code'] in GOOGLE_CODES)
    require(type(enabled) is int and enabled >= 0)
    require(isinstance(read,dict) and set(read) == {'attempted','succeeded','failed','event_count','completed'})
    require(all(type(read[key]) is int and read[key] >= 0 for key in ('attempted','succeeded','failed','event_count')))
    require(type(read['completed']) is bool and read['attempted'] <= enabled
        and read['attempted'] == read['succeeded']+read['failed']
        and (not read['completed'] or read['attempted'] == enabled))
    codes = value['error_codes']
    require(isinstance(codes,list) and all(isinstance(code,str) and code in GOOGLE_CODES for code in codes))
    expected = (google['configured'] and google['authorized'] and not google['reauthorization_required']
        and google['error_code'] is None and enabled > 0 and read['completed'] and read['succeeded'] == enabled and not codes)
    require(value['ready'] == expected)
    require(isinstance(value['occurrences'],list) and (value['ready'] or not value['occurrences']))
    for row in value['occurrences']:
        require(isinstance(row,dict) and set(row) == {'occurrence_id','revision','start_at','end_at','joinable'})
        require(isinstance(row['occurrence_id'],str) and re.fullmatch('[a-f0-9]{32}',row['occurrence_id']))
        require(type(row['revision']) is int and 0 < row['revision'] < 2**53)
        require(type(row['joinable']) is bool)
        require(all(isinstance(row[key],str) and re.fullmatch(
            r'\d{4}-\d\d-\d\dT[\d:.]+(?:Z|[+-]\d\d:\d\d)',row[key]) for key in ('start_at','end_at')))
        start, end = (datetime.fromisoformat(row[key]) for key in ('start_at','end_at'))
        require(start.tzinfo is not None and end.tzinfo is not None and end > start)


def validate_settings_operation(value):
    def require(condition):
        if not condition:raise ValueError('invalid_calendar_settings_metadata')
    require(isinstance(value,dict) and set(value)=={'ok','scheduler_paused','calendars','saved'})
    require(value['ok'] is True and type(value['scheduler_paused']) is bool and type(value['saved']) is bool)
    require(not value['saved'] or value['scheduler_paused'])
    require(isinstance(value['calendars'],list))
    seen = set(); primary = 0
    for row in value['calendars']:
        require(isinstance(row,dict) and set(row)=={'calendar_ref','revision','primary','enabled',
            'recording_authorized','speaking_authorized','join_early_seconds','max_lateness_seconds'})
        require(isinstance(row['calendar_ref'],str) and re.fullmatch('[a-f0-9]{32}',row['calendar_ref'])
            and row['calendar_ref'] not in seen)
        seen.add(row['calendar_ref'])
        require(type(row['revision']) is int and 0<row['revision']<2**53)
        require(all(type(row[key]) is bool for key in ('primary','enabled','recording_authorized','speaking_authorized')))
        require(type(row['join_early_seconds']) is int and 0<=row['join_early_seconds']<=600
            and type(row['max_lateness_seconds']) is int and 0<=row['max_lateness_seconds']<=1800)
        primary += row['primary']
    require(primary<=1)
