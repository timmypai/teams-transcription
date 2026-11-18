"""Read-only strict consumer for downloaded failure.json. No fixes/retries/commands.

Exit0 means only structurally valid input, NEVER deployment/Oracle PASS. Outputs
fixed enums/booleans only. Invalid, legacy, incomplete, mixed/unknown or secondary
failure evidence routes unknown_stop. A small output/exit125 proves no cause.
"""
import argparse
import json
from pathlib import Path
import re

from deploy.build_projection import CLASSES, OPTIONS, COMMANDS
from deploy.target_diagnostics import PHASES, CODES, MAX_BYTES, MAX_LINES, MAX_LINE_BYTES
from deploy.target_runtime import SAFE_FAILURE_CODES

NEXT_ACTIONS=frozenset({'source_compatibility_review','host_capability_review','public_build_review','unknown_stop'})
LIMIT=65536


def require(condition):
    if not condition:raise ValueError('evidence_invalid')


def integer(value,lo,hi):return type(value) is int and lo<=value<=hi


def keys(value,names):require(type(value) is dict and set(value)==set(names))


def output(value):
    keys(value,('bytes','lines','max_line_bytes','events','truncated'))
    for key,limit in [('bytes',MAX_BYTES),('lines',MAX_LINES),('max_line_bytes',MAX_LINE_BYTES),('events',0)]:
        require(integer(value[key],0,limit))
    require(type(value['truncated']) is bool)
    require(value['max_line_bytes']<=value['bytes'] and value['lines']<=value['bytes'])
    if value['lines']==MAX_LINES:require(value['truncated'])


def failure(value,version):
    require(type(value) is dict and set(value) in ({'phase','code','exit_status'},{'phase','code','exit_status','output'}))
    require(value['phase'] in (PHASES if version==2 else PHASES-{'build_preflight'}))
    require(value['code'] in (CODES if version==2 else CODES-{'capability_missing','capability_unknown'})
        or value['code'] in {'target_runtime_rejected:'+code for code in SAFE_FAILURE_CODES})
    require(value['exit_status'] is None or integer(value['exit_status'],-255,255))
    if 'output' in value:output(value['output'])
    if value['code']=='command_failed':
        require(value['exit_status'] not in (None,0) and 'output' in value
            and value['phase'] in {'build_preflight','prepare_contexts','image_build'})
    if value['code'] in {'command_timeout','command_start_failed','output_limit','capability_missing','capability_unknown'}:
        require(value['exit_status'] is None)
    if value['code'].startswith('capability_'):require(value['phase']=='build_preflight' and 'output' not in value)


def validate(doc):
    require(type(doc) is dict and type(doc.get('schema_version')) is int and doc['schema_version'] in (1,2))
    version=doc['schema_version']
    root={'schema_version','repository','source_sha','run_id','run_attempt','action','primary_failure','secondary_failures','cleanup'}
    keys(doc,root|({'build_diagnostics'} if version==2 else set()))
    require(doc['repository']=='timmypai/teams-transcription')
    require(doc['source_sha'] is None or type(doc['source_sha']) is str and re.fullmatch('[0-9a-f]{40}',doc['source_sha']) is not None)
    require(doc['run_id'] is None or integer(doc['run_id'],1,2**63-1))
    require(doc['run_attempt'] is None or integer(doc['run_attempt'],1,2**31-1))
    require(doc['action'] in {'deploy','view','stop','verify'})
    require(doc['cleanup'] in {'not_required','unknown','rollback_verified'})
    if doc['primary_failure'] is not None:failure(doc['primary_failure'],version)
    require(type(doc['secondary_failures']) is list and len(doc['secondary_failures'])<=8)
    require(not doc['secondary_failures'] or doc['primary_failure'] is not None)
    for row in doc['secondary_failures']:failure(row,version)
    if version==1:return
    diag=doc['build_diagnostics']
    keys(diag,('capabilities','required_options','classifications','projection_incomplete'))
    require(type(diag['projection_incomplete']) is bool)
    keys(diag['required_options'],OPTIONS)
    require(all(value in {'supported','unsupported','unknown'} for value in diag['required_options'].values()))
    caps=diag['capabilities'];require(type(caps) is list and len(caps) in (0,2))
    if caps:
        require([row.get('command') for row in caps if type(row) is dict]==list(COMMANDS))
        for row in caps:
            keys(row,('command','state','exit_status','output'))
            require(row['state'] in {'available','unavailable','unknown'})
            require(row['exit_status'] is None or integer(row['exit_status'],-255,255))
            output(row['output'])
            if row['state']=='available':require(row['exit_status']==0 and not row['output']['truncated'])
            if row['state']=='unavailable':require(row['exit_status'] not in (None,0) and not row['output']['truncated'])
        if caps[1]['state']=='available':require(set(diag['required_options'].values())=={'supported'})
        else:require(set(diag['required_options'].values())=={'unknown'})
    else:require(set(diag['required_options'].values())=={'unknown'})
    rows=diag['classifications'];require(type(rows) is list and len(rows)<=8)
    seen=set()
    for row in rows:
        keys(row,('classification','option'));kind=row['classification'];option=row['option']
        require(kind in CLASSES and option in {*OPTIONS,'unknown'})
        if kind=='named_context_unsupported':require(option=='build_context')
        elif kind=='build_option_unsupported':require(option in {'load','provenance','iidfile'})
        else:require(option=='unknown')
        require((kind,option) not in seen);seen.add((kind,option))


def consume(doc):
    result=dict(valid=False,evidence_schema=None,capabilities_collected=False,next_action='unknown_stop')
    try:validate(doc)
    except (ValueError,TypeError,KeyError,AttributeError,RecursionError):return result
    version=doc['schema_version'];result.update(valid=True,evidence_schema=version)
    if version==1:return result # no inference/backfill/mutation of legacy schema
    diag=doc['build_diagnostics'];caps=diag['capabilities']
    result['capabilities_collected']=len(caps)==2
    primary=doc['primary_failure']
    if (not primary or not caps or doc['secondary_failures'] or diag['projection_incomplete']
        or any(doc[key] is None for key in ('source_sha','run_id','run_attempt')) or doc['action']!='deploy'):
        return result
    if primary.get('output',{}).get('truncated'):return result
    if any(row['output']['truncated'] for row in caps):return result
    if primary['phase']=='build_preflight':
        if diag['classifications'] or doc['cleanup']!='not_required':return result
        if any(row['state']=='unknown' for row in caps):return result
        if primary['code']=='capability_missing' and any(row['state']=='unavailable' for row in caps):
            result['next_action']='host_capability_review'
        return result
    if (primary['phase'] not in {'prepare_contexts','image_build'} or primary['code']!='command_failed'
        or primary['exit_status'] in (None,0) or doc['cleanup']!='unknown'
        or any(row['state']!='available' for row in caps)
        or set(diag['required_options'].values())!={'supported'}):return result
    routes=set()
    for row in diag['classifications']:
        kind=row['classification']
        # Same buildx command cannot simultaneously have all required options
        # proven and claim the plugin/flag is absent. Do not choose a cause.
        if primary['phase']=='image_build' and kind in {'buildx_unavailable','build_option_unsupported','named_context_unsupported'}:
            return result
        if kind in {'build_option_unsupported','named_context_unsupported'}:routes.add('source_compatibility_review')
        elif kind in {'buildx_unavailable','daemon_unreachable','daemon_permission_denied','storage_exhausted','resource_exhausted'}:routes.add('host_capability_review')
        elif kind in {'public_dependency_fetch_failed','build_step_failed'}:routes.add('public_build_review')
        else:return result
    # More than one cause/signature is not a root-cause proof. Even same-route
    # conflicting observations stop instead of choosing a preferred match.
    if len(routes)==1 and len(diag['classifications'])==1:result['next_action']=routes.pop()
    return result


def unique(pairs):
    value={}
    for key,item in pairs:
        require(key not in value);value[key]=item
    return value


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('evidence',type=Path);args=parser.parse_args()
    try:
        with args.evidence.open('rb') as source:raw=source.read(LIMIT+1)
        require(len(raw)<=LIMIT)
        doc=json.loads(raw,object_pairs_hook=unique,parse_constant=lambda _:require(False))
        result=consume(doc)
    except (OSError,ValueError,TypeError,RecursionError):result=consume(None)
    print(json.dumps(result,sort_keys=True))
    return 0 if result['valid'] else 1


if __name__=='__main__':raise SystemExit(main())
