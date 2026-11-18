"""Bounded numeric evidence plus reviewed enums; never raw output or raw files.

Schema1 output.events=0 remains text-event accounting. Schema2 classifications
live separately (max8); line buffers max2048 each, shared byte/deadline budgets.
Killing/reaping the CLI does NOT prove daemon workload cleanup.
"""
from contextlib import contextmanager
import os
import selectors
import signal
import subprocess
import time
from typing import Any

from deploy.build_projection import Projection, COMMANDS, MAX_CLASSIFICATIONS, empty_diagnostics

MAX_BYTES=8388608
MAX_LINES=1024
MAX_LINE_BYTES=2048
MAX_EVENTS=0
MAX_SECONDARY=8
PHASES=frozenset({'build_preflight','prepare_contexts','image_build','audit','cleanup','unknown'})
CODES=frozenset({'command_failed','command_timeout','output_limit','command_start_failed',
    'capability_missing','capability_unknown','unknown','audit_failed','snapshot_failed','cleanup_failed','evidence_write_failed'})
PUBLIC_PHASES=frozenset({'build_preflight','prepare_contexts','image_build'})


class PublicResult(str):
    projection: Projection
    stats: dict
    exit_status: int
    def __new__(cls,projection,stats,exit_status):
        value=super().__new__(cls,'')
        value.projection=projection;value.stats=stats;value.exit_status=exit_status
        return value


class PublicCommandFailure(ValueError):
    def __init__(self,phase,code,exit_status=None,output=None,projection=None):
        super().__init__('target_public_command_failed')
        self.phase=phase;self.code=code;self.exit_status=exit_status;self.output=output;self.projection=projection


def public_command(args,*,phase,timeout,env,probe=None):
    if phase not in PUBLIC_PHASES:raise ValueError('public_phase_rejected')
    if (phase=='build_preflight')!=(probe in COMMANDS):raise ValueError('public_probe_rejected')
    stats: dict[str,Any]=dict(bytes=0,lines=0,max_line_bytes=0,events=MAX_EVENTS,truncated=False)
    projection=Projection(probe)
    sizes={};buffers={};deadline=time.monotonic()+timeout
    try:
        process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,env=env,start_new_session=True)
    except OSError:
        raise PublicCommandFailure(phase,'command_start_failed',output=stats,projection=projection) from None
    code=None
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            for number,stream in enumerate((process.stdout,process.stderr)):
                os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ,number)
                sizes[stream]=0;buffers[stream]=bytearray()
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:code='command_timeout';break
                for key,_ in selector.select(min(remaining,0.1)):
                    stream=key.fileobj;chunk=os.read(key.fd,8192)
                    if not chunk:
                        if sizes[stream]:projection.incomplete=True;projection.valid=False
                        selector.unregister(stream);continue
                    available=MAX_BYTES-stats['bytes']
                    stats['bytes']+=min(len(chunk),available)
                    if len(chunk)>available:code='output_limit'
                    # One bounded read only; no unbounded split/capture followed
                    # by truncation. Distinct streams cannot complete each other.
                    parts=chunk[:available].split(b'\n') # at most ONE 8192-byte read
                    for index,part in enumerate(parts):
                        sizes[stream]=min(MAX_LINE_BYTES+1,sizes[stream]+len(part))
                        stats['max_line_bytes']=max(stats['max_line_bytes'],min(MAX_LINE_BYTES,sizes[stream]))
                        if sizes[stream]<=MAX_LINE_BYTES:buffers[stream].extend(part)
                        else:stats['truncated']=True;buffers[stream].clear()
                        if index<len(parts)-1:
                            stats['lines']=min(MAX_LINES,stats['lines']+1)
                            if stats['lines']==MAX_LINES:stats['truncated']=True
                            if sizes[stream]<=MAX_LINE_BYTES and not stats['truncated']:
                                projection.line(bytes(buffers[stream]),key.data)
                            buffers[stream].clear();sizes[stream]=0
                    if code:break
                if code:break
            if code is None:
                try:process.wait(timeout=max(0.001,deadline-time.monotonic()))
                except subprocess.TimeoutExpired:code='command_timeout'
    except Exception:
        code='unknown'
    finally:
        if code is not None:
            stats['truncated']=True
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:pass
        for stream in (process.stdout,process.stderr):stream.close()
        projection.incomplete=projection.incomplete or stats['truncated']
    if code is not None:raise PublicCommandFailure(phase,code,output=stats,projection=projection) from None
    if process.returncode:raise PublicCommandFailure(phase,'command_failed',process.returncode,stats,projection)
    return PublicResult(projection,stats,0)


def bounded_exit(value):
    return value if type(value) is int and -255<=value<=255 else None


def failure(exc,phase='unknown',code='unknown'):
    """Only fixed enums and bounded numbers, never exception messages/classes."""
    row: dict = dict(phase=phase if phase in PHASES else 'unknown',code=code if code in CODES else 'unknown',exit_status=None)
    from deploy.target_runtime import SAFE_FAILURE_CODES
    if type(exc) is ValueError and len(exc.args)==1 and type(exc.args[0]) is str:
        if exc.args[0] in {'target_runtime_rejected:'+code for code in SAFE_FAILURE_CODES}:
            row['code']=exc.args[0]
    if isinstance(exc,PublicCommandFailure):
        row.update(phase=exc.phase if exc.phase in PUBLIC_PHASES else 'unknown',
            code=exc.code if exc.code in CODES else 'unknown',exit_status=bounded_exit(exc.exit_status))
        if isinstance(exc.output,dict):
            row['output']={k:min(limit,max(0,exc.output[k])) if type(exc.output.get(k)) is int else 0
                for k,limit in [('bytes',MAX_BYTES),('lines',MAX_LINES),('max_line_bytes',MAX_LINE_BYTES),('events',MAX_EVENTS)]}
            row['output']['truncated']=exc.output.get('truncated') is True
    return row


class Failures:
    def __init__(self):
        self.primary=None;self.secondary=[];self.cleanup='not_required';self.seen=[]
        self.build_diagnostics=empty_diagnostics()

    def observe(self,value):
        projection=getattr(value,'projection',None)
        if projection is None:return
        stats=value.output if isinstance(value,PublicCommandFailure) else value.stats
        exit_status=bounded_exit(value.exit_status)
        diag=self.build_diagnostics
        if projection.probe:
            row,options=projection.result(exit_status,stats)
            if len(diag['capabilities'])<2:diag['capabilities'].append(row)
            else:diag['projection_incomplete']=True
            if projection.probe=='buildx_build_help':diag['required_options']=options
        elif isinstance(value,PublicCommandFailure):
            # A successful earlier public command cannot explain a later failure.
            for row in projection.rows:
                if row not in diag['classifications']:
                    if len(diag['classifications'])<MAX_CLASSIFICATIONS:diag['classifications'].append(row)
                    else:diag['projection_incomplete']=True
        diag['projection_incomplete']=diag['projection_incomplete'] or projection.incomplete

    def record(self,exc,phase='unknown',code='unknown'):
        if any(exc is prior for prior in self.seen):return
        if len(self.seen)<MAX_SECONDARY+1:self.seen.append(exc)
        row=failure(exc,phase,code)
        if self.primary is None:self.primary=row
        elif len(self.secondary)<MAX_SECONDARY:self.secondary.append(row)

    @contextmanager
    def stage(self,phase):
        try:yield
        except Exception as exc:
            self.record(exc,phase);raise

    def document(self,repository,sha,run,attempt,action):
        return dict(schema_version=2,repository=repository,source_sha=sha,run_id=run,run_attempt=attempt,
            action=action,primary_failure=self.primary,secondary_failures=self.secondary,cleanup=self.cleanup,
            build_diagnostics=self.build_diagnostics)
