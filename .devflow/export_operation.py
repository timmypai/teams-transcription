"""Export only validated operation evidence from the live helper namespace."""
import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import zlib


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


def export(socket, helper, folder, operation, playwright_exit):
    folder = Path(folder)
    failure = {'ok': False, 'code': 'operation_export_failed'}
    try:
        result = json.loads(read_helper(socket, helper, 'result.json', 1024*1024))
        if isinstance(result, dict) and result.get('ok') is False:
            # Never publish raw reporter errors, DOM, inputs or exception text.
            code = result.get('code')
            if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,80}', code):
                failure['code'] = code
            raise ValueError('operation_not_successful')
        if playwright_exit != 0 or not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError('operation_not_successful')
        if operation == 'look' and result != dict(ok=True, path='/login', status=200, title='管理登入'):
            raise ValueError('invalid_look_metadata')
        png = read_helper(socket, helper, operation+'.png', 32*1024*1024)
        validate_png(png)
        screenshot = folder/(operation+'.png')
        screenshot.write_bytes(png)
        screenshot.chmod(0o600)
        (folder/'result.json').write_text(json.dumps(result, ensure_ascii=False)+'\n')
        (folder/'result.json').chmod(0o600)
        return 0
    except (OSError, ValueError, TypeError, struct.error, zlib.error, subprocess.SubprocessError):
        (folder/(operation+'.png')).unlink(missing_ok=True)
        (folder/'result.json').write_text(json.dumps(failure)+'\n')
        (folder/'result.json').chmod(0o600)
        return 1


if __name__ == '__main__':
    raise SystemExit(export(*sys.argv[1:5], int(sys.argv[5])))
