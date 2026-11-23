"""Fixed runner-local handoff; callers hold the existing target operation lock.

Actions transports an intent enum, never a meeting selector or authority. The
owner attests through Hermes; Join means both freshly present in the exact
staged occurrence and consenting to this one recording/transcription/output.
"""
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

from tools.target_admission import REPO, require
from teams_transcription.calendar_readback import validate_operation

TTL = 300
ACTIONS = ('preview', 'join-confirmed-present-and-media', 'view', 'output-disable', 'stop')
HELPER_LABEL = 'io.devflow.teams.private-browser'
HELPER_OWNER = 'io.devflow.owner'


def dispatch_identity(api, sha, run, attempt):
    row = api.request(f'/repos/{REPO}/actions/runs/{run}')[0]
    actor = row['actor']['id']
    require(row['id'] == run and row['run_attempt'] == attempt == 1
            and row['event'] == 'workflow_dispatch' and row['path'] == '.github/workflows/control.yml'
            and row['head_branch'] == 'main' and row['head_sha'] == sha
            and row['repository']['full_name'] == row['head_repository']['full_name'] == REPO
            and type(actor) is int and actor > 0 and row['triggering_actor']['id'] == actor,
            'control_dispatch_rejected')
    start = datetime.fromisoformat(row['created_at'].replace('Z', '+00:00')).timestamp()
    require(start <= time.time(), 'control_dispatch_rejected')
    return actor, start


def control_slot(c):
    path = c.root/'single-control.json'
    return c.read(path) if path.exists() or path.is_symlink() else {}


def helper_name(pending):
    nonce = pending['helper_nonce']
    require(re.fullmatch('[0-9a-f]{32}', nonce), 'operation_cleanup_unverified')
    return 'devflow-teams-transcription-private-browser-'+nonce


def tracked_browser(pending, row):
    labels = row.get('Config', {}).get('Labels') or {}
    return (pending.get('phase') == 'browser-cleanup'
            and row['Name'] == '/'+helper_name(pending)
            and labels.get(HELPER_OWNER) == REPO
            and labels.get(HELPER_LABEL) == pending['helper_nonce']
            and re.fullmatch('[0-9a-f]{64}', row['Id']) is not None
            and row['Id'] == pending.get('helper_id', row['Id']))


def clean_browser(c, slot, pending):
    c.failures.cleanup = 'unknown'
    name = helper_name(pending)
    folder = c.root/name

    def matching(expected_id):
        # A failed inspect is not proof of absence. Require a successful daemon
        # inventory, including stopped containers and full immutable IDs.
        rows = c.docker('container', 'ls', '--all', '--no-trunc', '--format', '{{.ID}} {{.Names}}')
        return [(cid, label) for cid, label in map(str.split, rows.splitlines())
                if label == name or cid == expected_id]

    try:
        found = matching(pending.get('helper_id'))
        if found:
            [(cid, label)] = found
            require(label == name and re.fullmatch('[0-9a-f]{64}', cid), 'operation_cleanup_unverified')
            [row] = json.loads(c.docker('container', 'inspect', cid))
            require(row['Id'] == cid and tracked_browser(pending, row), 'operation_cleanup_unverified')
            # Journal this ID before removal: a crash after rm can then verify
            # absence without mistaking an unacknowledged create for cleanup.
            atomic_slot(c, slot, {**pending, 'helper_id':cid})
            c.docker('rm', '-f', cid)
            require(not matching(cid), 'operation_cleanup_unverified')
        else:
            require('helper_id' in pending or not (folder/'creating').exists(), 'operation_cleanup_unverified')
        if folder.exists():
            shutil.rmtree(folder)
        atomic_slot(c, slot, {k:pending[k] for k in ('actor','source','container')} | dict(phase='consumed'))
    except Exception:
        raise ValueError('operation_cleanup_unverified') from None
    c.failures.cleanup = 'verified'


def browser(c, operation, args, ops_env, *, owner):
    from deploy.target_deploy import ROOT
    from deploy.target_diagnostics import CONTROL_CODES
    slot = c.root/'single-control.json'
    pending = {**owner, 'phase':'browser-cleanup', 'helper_nonce':uuid4().hex}
    folder = c.root/helper_name(pending)
    atomic_slot(c, slot, pending)
    c.failures.cleanup = 'unknown'
    folder.mkdir(mode=0o700)
    output = folder/'private.json'
    env = {k: os.environ[k] for k in ('PATH', 'HOME', 'GITHUB_SHA', 'GITHUB_RUN_ID', 'GITHUB_RUN_ATTEMPT')}
    env.update(DEVFLOW_OP=operation, DEVFLOW_OP_ARGS=json.dumps(args), DEVFLOW_OPS_ENV=ops_env,
               DEVFLOW_PRIVATE_RESULT=str(output), DEVFLOW_PRIVATE_HELPER=pending['helper_nonce'])
    # A canceled controller must not release its lock while the browser
    # shell / Docker CLI can still create or start this helper. Reconcile only
    # once the subprocess has returned; interruption leaves the journal intact.
    run = subprocess.run(['bash', str(ROOT/'.devflow/operate.sh')], cwd=ROOT, env=env,
                         pass_fds=(c.operation_lock,),
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    phase = 'control_preview' if operation == 'calendar-occurrences' else 'control_join-confirmed-present-and-media'
    try:
        with c.failures.stage(phase):
            if run.returncode:
                try:
                    code = c.read(output.parent/'projection/result.json')['code']
                except (OSError, ValueError, TypeError, KeyError):
                    code = None
                raise ValueError(code if isinstance(code, str) and code in CONTROL_CODES else 'control_browser_failed')
            return c.read(output)
    finally:
        with c.failures.stage('cleanup'):
            clean_browser(c, slot, pending)


def atomic_slot(c, path, value):
    # Publish the dispatch interlock before starting the private runtime call.
    with tempfile.NamedTemporaryFile(dir=c.root, prefix='control-', mode='w', delete=False) as out:
        name = out.name
        try:
            json.dump(value, out, allow_nan=False)
            out.flush()
            os.fsync(out.fileno())
            os.replace(name, path)
            directory = os.open(c.root, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(name).unlink(missing_ok=True)


def private_runtime(c, action, **fields):
    return c.runtime('bound-control', input=json.dumps(dict(operation=action, **fields)))


def run_control(c, action, sha, container, dispatch, ops_env):
    actor, submitted = dispatch
    slot = c.root/'single-control.json'
    saved = control_slot(c)
    owner = dict(actor=actor, source=sha, container=container)
    if saved.get('phase') == 'browser-cleanup':
        c.failures.cleanup = 'unknown'
        require(action not in {'preview','join-confirmed-present-and-media'}, 'control_slot_occupied')
        require(action == 'stop' and all(saved[key] == value for key, value in owner.items()),
                'control_slot_unavailable')
        clean_browser(c, slot, saved)
        return dict(ok=True, browser_cleanup_verified=True)
    now = time.time()
    if action == 'preview':
        require(not saved or saved['phase'] in {'cleaned','consumed'}
                or (saved['phase'] == 'preview' and now >= saved['expires_at']), 'control_slot_occupied')
        value = browser(c, 'calendar-occurrences', {}, ops_env, owner=owner)
        validate_operation(value)
        rows = [row for row in value['occurrences'] if row['joinable'] is True]
        require(value['ready'] and len(rows) == 1, 'control_unique_occurrence_required')
        issued = time.time()
        atomic_slot(c, slot, dict(phase='preview', actor=actor, source=sha, container=container,
                                selection=rows[0], issued_at=issued, expires_at=issued+TTL))
        return dict(ok=True, unique_occurrence_staged=True, confirmation_window_seconds=TTL)

    require(saved and saved['actor'] == actor and saved['source'] == sha and saved['container'] == container,
            'control_slot_unavailable')
    if action == 'join-confirmed-present-and-media':
        require(saved['phase'] == 'preview' and saved['issued_at'] <= submitted <= now < saved['expires_at']
                and now-submitted < TTL, 'control_confirmation_stale')
        selection = saved['selection']
        ident = selection['occurrence_id']
        # Consume the preview even on refusal. Never substitute a new occurrence
        # or replay a confirmation when a UI/read/runtime response is lost.
        atomic_slot(c, slot, dict(phase='consumed', actor=actor, source=sha, container=container))
        binding = browser(c, 'issue-binding', {k:v for k,v in selection.items() if k!='joinable'} |
                          dict(present=True, media=True), ops_env, owner=owner)
        request = binding.get('request_sha256')
        require(binding.get('ok') is True and binding['occurrence_id'] == ident
                and binding['revision'] == selection['revision'] + 1
                and binding.get('expires_in_seconds') == TTL
                and re.fullmatch('[a-f0-9]{64}', request or '')
                and abs(binding['expires_at'] - binding['issued_at'] - TTL) < .001,
                'binding_result_invalid')
        deadline = min(saved['expires_at'], submitted+TTL, binding['expires_at'])
        require(binding['issued_at'] <= time.time() < deadline, 'control_confirmation_stale')
        bound = dict(phase='dispatching', actor=actor, source=sha, container=container,
                     occurrence_id=ident, request_sha256=request)
        atomic_slot(c, slot, bound)
        result = private_runtime(c, 'join', id=ident, request_sha=request, revision=binding['revision'],
                                 not_after=deadline)
        session = result.get('session_id')
        sent = result.get('dispatch_at')
        require(type(sent) in (int, float) and binding['issued_at'] <= sent < deadline
                and re.fullmatch('[a-f0-9]{32}', session or ''), 'control_dispatch_unverified')
        atomic_slot(c, slot, {**bound, 'phase':'active', 'session_id':session})
        return dict(ok=True, join_dispatched=True, dispatch_at=sent,
                    issue_to_dispatch_ms=round(1000*(sent-binding['issued_at'])))

    if saved['phase'] == 'cleaned':
        require(action in {'view','stop'}, 'control_slot_unavailable')
        return saved['view'] if action == 'view' else dict(ok=True, cleanup_verified=True)
    require(saved['phase'] in {'dispatching','active'}, 'control_slot_unavailable')
    # The same bound view both authenticates the association and recovers a
    # lost Join response. It must never acquire a handle for another run.
    view = private_runtime(c, 'view', id=saved['occurrence_id'], request_sha=saved['request_sha256'])
    session = view['session_id']
    require(re.fullmatch('[a-f0-9]{32}', session or '') and saved.get('session_id', session) == session,
            'control_bound_view_required')
    if action == 'view':
        return project_view(view)
    if action == 'output-disable':
        result = private_runtime(c, action, id=session, request_sha=saved['request_sha256'])
        require(result.get('ok') is True, 'control_output_unverified')
        return dict(ok=True, output_disabled=True)
    require(action == 'stop', 'control_dispatch_rejected')
    c.failures.cleanup = 'unknown'
    result = private_runtime(c, 'stop', id=session, request_sha=saved['request_sha256'])
    require(result.get('ok') is True and result.get('cleanup_verified') is True, 'stop_cleanup_unverified')
    # Keep only the sanitized outcome once durable cleanup is verified.
    atomic_slot(c, slot, dict(phase='cleaned', actor=actor, source=sha, container=container,
                              view=dict(ok=True, authorized_view=True, cleanup_verified=True)))
    c.failures.cleanup = 'verified'
    return dict(ok=True, cleanup_verified=True)


def project_view(view):
    # There is no pass-through, including for new runtime fields.
    acceptance = view.get('acceptance') or {}
    state = view.get('run_state')
    def count(key):
        value = acceptance.get(key)
        return value if type(value) in (int,float) and 0 <= value < 10**15 else None
    return dict(ok=True, authorized_view=True,
                run_state=state if state in ('running', 'completed', 'failed', 'stopped', 'interrupted') else None,
                response_ready=view.get('response_ready') is True,
                egress_allows_target_host=view.get('egress_allows_target_host') is True,
                joined=acceptance.get('joined') is True, active_lease=view.get('active_lease') is True,
                target_calendar_claimed=acceptance.get('target_calendar_claimed') is True,
                summary_disabled=acceptance.get('summary_disabled') is True,
                capture_interlock=view.get('capture_interlock') is True,
                source_rx_packets=count('source_rx_packets'), B_tx_packets=count('B_tx_packets'),
                B_tx_frames=count('B_tx_frames'), B_tx_energy=count('B_tx_energy'),
                cleanup_verified=acceptance.get('cleanup_verified') is True)
