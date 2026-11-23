"""Export only validated operation evidence from the live helper namespace."""
import json
from pathlib import Path
import os
import re
import time
import struct
import subprocess
import sys
import zlib
from zoneinfo import ZoneInfoNotFoundError

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from teams_transcription.calendar_readback import GOOGLE_CODES, validate_operation, validate_settings_operation
from teams_transcription.control_errors import SAFE_CONTROL_FAILURE_CODES

SETTINGS_FAILURES = frozenset({'invalid_calendar_settings_args','calendar_settings_missing',
    'calendar_settings_invalid','calendar_settings_not_quiescent','calendar_revision_changed',
    'calendar_settings_save_failed','settings_page_required','missing_ADMIN_PASSWORD','login_page_required',
    'admin_login_failed','operation_failed'})
FAILURES = SETTINGS_FAILURES | GOOGLE_CODES | SAFE_CONTROL_FAILURE_CODES | frozenset({
    'invalid_binding_args','fresh_owner_confirmation_required','pending_occurrence_required',
    'binding_form_required','binding_selection_changed','binding_rejected','binding_page_required',
    'binding_result_invalid','target_occurrence_not_authorized','occurrence_outside_join_window',
    'calendar_sync_action_failed','calendar_readback_stale','schedule_timezone_missing',
    'calendar_occurrence_invalid','calendar_readback_invalid','schedule_page_required','operation_setup_failed',
})


def validate_binding(result):
    if (set(result) != {'ok','occurrence_id','revision','request_sha256','issued_at','expires_at','expires_in_seconds'}
            or result['ok'] is not True or type(result['revision']) is not int or result['revision'] <= 0
            or not re.fullmatch('[0-9a-f]{32}', result['occurrence_id'])
            or not re.fullmatch('[0-9a-f]{64}', result['request_sha256'])
            or result['expires_in_seconds'] != 300
            or any(type(result[k]) not in (int,float) for k in ('issued_at','expires_at'))
            or not 299.999 <= result['expires_at'] - result['issued_at'] <= 300.001
            or not result['issued_at'] <= time.time() < result['expires_at']):
        raise ValueError('binding_result_invalid')


def read_helper(socket, helper, name, limit):
    result = subprocess.run(['docker', '--host', socket, 'exec', helper,
                             'head', '-c', str(limit+1), '/evidence/'+name],
                            capture_output=True, timeout=15, check=True)
    if len(result.stdout) > limit:
        raise ValueError('evidence_too_large')
    return result.stdout


def validate_png(data):
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('invalid_png')
    offset, compressed, ended = 8, bytearray(), False
    expected, row_bytes = 0, 1
    while offset < len(data):
        size = struct.unpack('>I', data[offset:offset+4])[0]
        kind = data[offset+4:offset+8]
        chunk = data[offset+8:offset+8+size]
        crc = struct.unpack('>I', data[offset+8+size:offset+12+size])[0]
        if zlib.crc32(kind+chunk) != crc:
            raise ValueError('invalid_png_crc')
        if offset == 8:
            if kind != b'IHDR' or size != 13:
                raise ValueError('invalid_png_header')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', chunk)
            if not width or not height or (depth, color) not in {(8, 2), (8, 6)} or any((compression, filtering, interlace)):
                raise ValueError('invalid_png_header')
            row_bytes = width*(3 if color == 2 else 4)+1
            expected = row_bytes*height
            if expected > 128*1024*1024:
                raise ValueError('png_decode_resource_limit')
        if kind == b'IDAT':
            compressed.extend(chunk)
        offset += size+12
        if kind == b'IEND':
            ended = size == 0 and offset == len(data)
            break
    decoder = zlib.decompressobj()
    decoded = decoder.decompress(compressed, expected+1)
    if (not ended or len(decoded) != expected or not decoder.eof or decoder.unused_data
            or any(decoded[row] > 4 for row in range(0, expected, row_bytes))):
        raise ValueError('invalid_png_pixels')


def export(socket, helper, folder, operation, playwright_exit, private_result=None):
    folder = Path(folder)
    failure = {'ok': False, 'code': 'operation_export_failed'}
    try:
        if operation not in ('look','calendar-occurrences','calendar-settings','issue-binding'):
            raise ValueError('unsupported_operation')
        result = json.loads(read_helper(socket, helper, 'result.json', 1024*1024))
        if isinstance(result, dict) and result.get('ok') is False:
            # Never publish raw reporter errors, DOM, inputs or exception text.
            code = result.get('code')
            if isinstance(code,str) and code in (SETTINGS_FAILURES if operation=='calendar-settings' else FAILURES):
                failure['code'] = code
            raise ValueError('operation_not_successful')
        if playwright_exit != 0 or not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError('operation_not_successful')
        if operation == 'look' and result != dict(ok=True, path='/login', status=200, title='管理登入'):
            raise ValueError('invalid_look_metadata')
        public = result
        if operation == 'calendar-occurrences':
            validate_operation(result)
            public = {k:result[k] for k in ('ok','google','enabled_calendar_count','read','error_codes','ready')}
            public['joinable_count'] = sum(row['joinable'] for row in result['occurrences'])
        elif operation == 'issue-binding':
            if private_result is None:
                raise ValueError('binding_private_only')
            validate_binding(result)
            public = dict(ok=True,binding_staged=True,expires_in_seconds=300)
        if operation == 'calendar-settings':
            validate_settings_operation(result)
            public = {**result,'calendars':[{k:v for k,v in row.items() if k!='calendar_ref'} for row in result['calendars']]}
        if operation == 'look':
            png = read_helper(socket, helper, operation+'.png', 32*1024*1024)
            validate_png(png)
            screenshot = folder/(operation+'.png')
            screenshot.write_bytes(png)
            screenshot.chmod(0o600)
        elif private_result is not None:
            with os.fdopen(os.open(private_result, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600), 'w') as out:
                json.dump(result,out,allow_nan=False)
        (folder/'result.json').write_text(json.dumps(public, ensure_ascii=False)+'\n')
        (folder/'result.json').chmod(0o600)
        return 0
    except (OSError, ValueError, TypeError, KeyError, ZoneInfoNotFoundError, struct.error, zlib.error, subprocess.SubprocessError):
        (folder/(operation+'.png')).unlink(missing_ok=True)
        (folder/'result.json').write_text(json.dumps(failure)+'\n')
        (folder/'result.json').chmod(0o600)
        return 1


if __name__ == '__main__':
    raise SystemExit(export(*sys.argv[1:5], int(sys.argv[5]), os.environ.get('DEVFLOW_PRIVATE_RESULT')))
