"""Test-only phase barriers and private, content-free failure receipts."""
import asyncio
import contextlib
import json
import os
from pathlib import Path
import tempfile
import time

def session_events(session):
    """Read actual scoped SQLite diagnostics, not optional projection files."""
    with session.store._locked() as db:
        return [{**json.loads(row['payload_json']),'type':row['kind']} for row in db.execute(
            'SELECT kind,payload_json FROM session_events WHERE meeting_id=? ORDER BY rowid',(session.ident,))]



class LifecycleProbe:
    def __init__(self):
        self.phase = 'setup'
        self.started = time.monotonic()
        self.evidence_path = None

    async def wait(self, awaitable, phase, timeout=5):
        self.phase = phase
        return await asyncio.wait_for(awaitable, timeout)

    @contextlib.contextmanager
    def record_failure(self, session, fixture):
        try:
            yield
        except BaseException as exc:
            try:
                self._save(session, fixture, exc)
            except Exception as diagnostic_error:
                exc.add_note('Safe lifecycle evidence unavailable: ' + type(diagnostic_error).__name__)
            raise

    def _save(self, session, fixture, exc):
        # Never copy config, transcript, event payloads, URL, audio or the
        # exception message. The receipt outlives TemporaryDirectory.
        status = session.status()
        phases = []
        allowed = {'prepare', 'browser_open', 'meeting_join', 'live_start',
                   'prepared', 'owner_wait', 'rx_start', 'rx_capture',
                   'rx_drain', 'live_drain', 'summary'}
        for event in session_events(session):
            if event.get('type') == 'phase' and event.get('phase') in allowed:
                phases.append(event['phase'])
        provider = getattr(fixture, 'provider', None)
        rpc = getattr(provider, 'rpc', None)
        child = getattr(rpc, 'process', None)
        queue = getattr(rpc, 'notifications', None)
        receiver = getattr(provider, 'receiver', None)
        codes = {'rx_stopped', 'rx_cancelled', 'rx_cleanup_unknown',
                 'rx_total_deadline_exceeded', 'rx_owner_ready_expired',
                 'final_persistence_failed', 'transcript_unavailable'}
        test_phases = {'setup', 'wait_owner_ready', 'wait_capture', 'wait_disk_entry',
                       'wait_queue_full', 'owner_stop', 'wait_receiver_stop', 'wait_runner',
                       'verify_cleanup', 'wait_late_write', 'run_sample_cap', 'wait_initial_push',
                       'wait_packet_consumed', 'assert_sample_cap'}
        value = {'schema_version': 1, 'test_phase': self.phase if self.phase in test_phases else 'other',
                 'elapsed_seconds': time.monotonic() - self.started,
                 'exception_type': type(exc).__name__, 'worker_phases': phases[-16:],
                 'state': status.get('state') if status.get('state') in
                     {'starting', 'preparing', 'prepared', 'recording', 'draining',
                      'stopped', 'failed', 'completed', 'recorded_without_transcript'} else 'other',
                 'error_code': status.get('error') if status.get('error') in codes else 'other',
                 'cleanup_status': status.get('cleanup_status') if status.get('cleanup_status') in
                     {'cleaned', 'unknown'} else 'pending',
                 'joins': fixture.joins, 'captures': fixture.captures,
                 'input_mode':'native_media' if getattr(provider, 'media_source', None) is not None else 'pcm',
                 'pushes': fixture.pushes, 'pcm_pushed_samples': sum(fixture.push_samples),
                 'child_alive': child is not None and child.returncode is None,
                 'receiver_done': receiver.done() if receiver is not None else None,
                 'queue_depth': queue.qsize() if queue is not None else None,
                 'rpc_pending_requests': len(getattr(rpc, 'pending', {}))}
        folder = Path(tempfile.mkdtemp(prefix='meeting-test-failure-'))
        self.evidence_path = folder / 'diagnostic.json'
        fd = os.open(self.evidence_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as output:
            json.dump(value, output, indent=2)
        exc.add_note('Safe lifecycle failure evidence: ' + str(self.evidence_path))
