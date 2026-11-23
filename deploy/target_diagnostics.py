"""Fixed metadata only. Public build output is untrusted and is never decoded.

No log/event text is accepted: MAX_EVENTS=0. stdout/stderr share byte and line
budgets. Line counters saturate; byte overflow or deadline kill the local process
group and reap it within a bounded wait. This is NOT proof a Docker daemon job
was removed; deployment cleanup remains unknown on failure.
"""
from contextlib import contextmanager
import os
import selectors
import signal
import subprocess
import time

MAX_BYTES=8388608
MAX_LINES=1024
MAX_LINE_BYTES=2048
MAX_EVENTS=0
MAX_SECONDARY=8
PHASES=frozenset({'prepare_contexts','image_build','audit','cleanup','unknown',
    'control_view','control_join-confirmed-present-and-media','control_preview','control_output-disable',
    'control_stop','control_prepare-home'})
CONTROL_CODES=frozenset({'control_dispatch_rejected','control_slot_occupied','control_slot_unavailable',
    'control_unique_occurrence_required','control_confirmation_stale','control_browser_failed',
    'control_dispatch_unverified','control_bound_view_required','control_output_unverified',
    'binding_result_invalid','binding_selection_changed','binding_form_required','pending_occurrence_required',
    'target_occurrence_not_authorized','occurrence_outside_join_window','operation_cleanup_unverified',
    'missing_ADMIN_PASSWORD','stop_cleanup_unverified'})
CODES=frozenset({'command_failed','command_timeout','output_limit','command_start_failed',
    'unknown','audit_failed','snapshot_failed','cleanup_failed','evidence_write_failed'}) | CONTROL_CODES
PUBLIC_PHASES=frozenset({'prepare_contexts','image_build'})


class PublicCommandFailure(ValueError):
    def __init__(self,phase,code,exit_status=None,output=None):
        super().__init__('target_public_command_failed')
        self.phase=phase;self.code=code;self.exit_status=exit_status;self.output=output


def public_command(args,*,phase,timeout,env):
    if phase not in PUBLIC_PHASES:raise ValueError('public_phase_rejected')
    # No stdin/private env, no shell, raw temporary files or communicate buffers.
    stats=dict(bytes=0,lines=0,max_line_bytes=0,events=MAX_EVENTS,truncated=False)
    sizes={};deadline=time.monotonic()+timeout
    try:
        process=subprocess.Popen(args,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,env=env,start_new_session=True)
    except OSError:
        raise PublicCommandFailure(phase,'command_start_failed',output=stats) from None
    code=None
    try:
        with selectors.DefaultSelector() as selector:
            assert process.stdout is not None and process.stderr is not None
            for stream in (process.stdout,process.stderr):
                os.set_blocking(stream.fileno(),False);selector.register(stream,selectors.EVENT_READ)
                sizes[stream]=0
            while selector.get_map():
                remaining=deadline-time.monotonic()
                if remaining<=0:code='command_timeout';break
                for key,_ in selector.select(min(remaining,0.1)):
                    chunk=os.read(key.fd,8192)
                    if not chunk:selector.unregister(key.fileobj);continue
                    # Projection is counters only; no text matching/decoding/logging.
                    available=MAX_BYTES-stats['bytes']
                    stats['bytes']+=min(len(chunk),available)
                    if len(chunk)>available:code='output_limit'
                    # Split at most one read chunk, not an unbounded line buffer.
                    parts=chunk.split(b'\n')
                    stats['lines']=min(MAX_LINES,stats['lines']+len(parts)-1)
                    if stats['lines']==MAX_LINES:stats['truncated']=True
                    for index,part in enumerate(parts):
                        length=(sizes[key.fileobj] if index==0 else 0)+len(part)
                        stats['max_line_bytes']=max(stats['max_line_bytes'],min(MAX_LINE_BYTES,length))
                        if length>MAX_LINE_BYTES:stats['truncated']=True
                        sizes[key.fileobj]=min(MAX_LINE_BYTES+1,length)
                    if code:break
                if code:break
            if code is None:
                try:process.wait(timeout=max(0.001,deadline-time.monotonic()))
                except subprocess.TimeoutExpired:code='command_timeout'
    except Exception:
        code='unknown'
    finally:
        # Descendants retaining pipes must not extend the deadline or survive a
        # failed local command. A Docker daemon is not in this process group.
        if code is not None:
            stats['truncated']=True
            try:os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:pass
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:pass
        for stream in (process.stdout,process.stderr):stream.close()
    if code is not None:raise PublicCommandFailure(phase,code,output=stats) from None
    if process.returncode:raise PublicCommandFailure(phase,'command_failed',process.returncode,stats)
    return ''


def bounded_exit(value):
    return value if type(value) is int and -255<=value<=255 else None


def failure(exc,phase='unknown',code='unknown'):
    """Only fixed enums and bounded numbers, never exception messages/classes."""
    row: dict = dict(phase=phase if phase in PHASES else 'unknown',code=code if code in CODES else 'unknown',exit_status=None)
    from deploy.target_runtime import SAFE_FAILURE_CODES
    if type(exc) is ValueError and len(exc.args)==1 and type(exc.args[0]) is str:
        if exc.args[0] in CONTROL_CODES:row['code']=exc.args[0]
        if exc.args[0] in {'target_runtime_rejected:'+code for code in SAFE_FAILURE_CODES}:
            from teams_transcription.control_errors import SAFE_CONTROL_FAILURE_CODES
            runtime_code=exc.args[0].removeprefix('target_runtime_rejected:')
            row['code']=runtime_code if runtime_code in SAFE_CONTROL_FAILURE_CODES else exc.args[0]
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
        return dict(schema_version=1,repository=repository,source_sha=sha,run_id=run,run_attempt=attempt,
            action=action,primary_failure=self.primary,secondary_failures=self.secondary,cleanup=self.cleanup)
