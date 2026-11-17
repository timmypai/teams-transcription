"""Strict projections: output is fixed event names, timestamps and numbers.

Unknown log lines are counted and discarded wholesale. No regex replacement of
arbitrary text, no user-selected patterns, and no secret-bearing string fallback.
"""
from datetime import datetime, timezone
import math
import re

MAX_LOG_BYTES=65536
MAX_LOG_LINES=100
WINDOW_SECONDS=900
EVENTS=(
    (re.compile(r'INFO:\s+Application startup complete\.'),'server_started'),
    (re.compile(r'INFO:\s+Application shutdown complete\.'),'server_stopped'),
    (re.compile(r'INFO:\s+Started server process \[[0-9]{1,10}\]'),'server_process_started'),
    (re.compile(r'INFO:\s+Finished server process \[[0-9]{1,10}\]'),'server_process_stopped'),
    (re.compile(r'INFO:\s+127\.0\.0\.1:[0-9]{1,5} - "GET /healthz? HTTP/1\.[01]" 200 OK'),'http_health_success'),
)


def recent_logs(raw):
    if not isinstance(raw,bytes):raise ValueError('log_bytes_required')
    byte_limit=len(raw)>MAX_LOG_BYTES
    data=raw[:MAX_LOG_BYTES]
    if byte_limit:data=data.rsplit(b'\n',1)[0] if b'\n' in data else b''
    lines=data.splitlines()
    line_limit=len(lines)>=MAX_LOG_LINES
    lines=lines[-MAX_LOG_LINES:]
    events=[]
    omitted=0
    for line in lines:
        # Decode errors and non-UTC/malformed timestamps cannot become log text.
        text=line.decode('utf-8',errors='replace')
        match=re.fullmatch(r'(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z) (.*)',text)
        if not match:
            omitted+=1
            continue
        try:stamp=datetime.fromisoformat(match[1].replace('Z','+00:00')).astimezone(timezone.utc).isoformat()
        except ValueError:
            omitted+=1
            continue
        code=next((code for pattern,code in EVENTS if pattern.fullmatch(match[2])),None)
        if code:events.append({'timestamp':stamp,'event':code})
        else:omitted+=1
    return {'window_seconds':WINDOW_SECONDS,'max_lines':MAX_LOG_LINES,'max_bytes':MAX_LOG_BYTES,
        'events':events,'omitted_lines':omitted,'line_limit_reached':line_limit,
        'byte_limit_reached':byte_limit,'raw_log_bodies_included':False}


def resource_usage(value):
    cpu=value['CPUPerc']
    if not isinstance(cpu,str) or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)?%',cpu):raise ValueError('cpu_usage_invalid')
    cpu=float(cpu[:-1])
    if not math.isfinite(cpu) or not 0<=cpu<=10000:raise ValueError('cpu_usage_invalid')
    memory=value['MemUsage']
    match=re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)(B|kB|MB|GB|TB|KiB|MiB|GiB|TiB) / ([0-9]+(?:\.[0-9]+)?)(B|kB|MB|GB|TB|KiB|MiB|GiB|TiB)',memory)
    if not match:raise ValueError('memory_usage_invalid')
    units={'B':1,'kB':1000,'MB':1000**2,'GB':1000**3,'TB':1000**4,
        'KiB':1024,'MiB':1024**2,'GiB':1024**3,'TiB':1024**4}
    used,limit=(int(float(match[n])*units[match[n+1]]) for n in (1,3))
    pids=value['PIDs']
    if not isinstance(pids,str) or not re.fullmatch(r'[0-9]{1,10}',pids):raise ValueError('pids_usage_invalid')
    if used>2**63-1 or limit>2**63-1 or int(pids)>2147483647:raise ValueError('usage_out_of_range')
    return {'cpu_percent':cpu,'memory_used_bytes':used,'memory_limit_bytes':limit,'pids':int(pids)}


def database_flags(value):
    allowed=('database_present','quiescent','scheduler_paused','active_meeting','active_lease',
        'pending_summary','unreviewed_cleanup','backup_created','maintenance')
    result={}
    for key in allowed:
        if key in value:
            if type(value[key]) is not bool:raise ValueError('database_flag_invalid')
            result[key]=value[key]
    schema=value.get('schema')
    if schema is not None and (not isinstance(schema,str) or not re.fullmatch(r'[0-9a-f]{64}',schema)):
        raise ValueError('database_schema_invalid')
    result['schema']=schema
    return result


def release_link(value,image):
    if value is None:return {'verified_release_record':False}
    if (value.get('repository')!='paulpai0412/teams-transcription' or value.get('image')!=image
            or not re.fullmatch(r'[0-9a-f]{40}',value.get('source_sha',''))
            or type(value.get('build_run_id')) is not int or not 0<value['build_run_id']<10**20
            or type(value.get('build_run_attempt')) is not int or not 0<value['build_run_attempt']<1000000):
        raise ValueError('release_record_invalid')
    return {'verified_release_record':True,'source_sha':value['source_sha'],
        'build_run_id':value['build_run_id'],'build_run_attempt':value['build_run_attempt'],
        'build_run_url':'https://github.com/paulpai0412/teams-transcription/actions/runs/'+str(value['build_run_id'])}
