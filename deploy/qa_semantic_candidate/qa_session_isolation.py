"""Opt-in synthetic QA experiment; serial fresh answer sessions, no daemon switch."""
import argparse
import asyncio
import hashlib
import json
import re
import tempfile
from pathlib import Path
from time import monotonic

from . import qa_probe
from .qa_content import CRITERION_VERSION
from .qa_rx_evidence import owned_cleanup_verified


def session_proof(result, private, previous):
    """Local lifecycle evidence only, never a model-consumption/memory ACK."""
    session = result.get('session', {})
    identity = session.get('thread_sha256')
    request = private.get('request', {})
    return (isinstance(identity, str) and re.fullmatch(r'[0-9a-f]{64}', identity) is not None
        and identity not in previous and session.get('start_attempted') is True
        and session.get('startup_completed') is True and session.get('realtime_start_issued') is True
        and request.get('model') == 'gpt-live-1-codex' and request.get('version') == 'v3'
        and request.get('includeStartupContext') is False and request.get('initialItems') == [])


async def run_isolated_probe(args):
    if not args.authorize_live:
        raise ValueError('explicit_live_authorization_required')
    private_path = getattr(args, 'private_result', None)
    if private_path is None:
        raise ValueError('private_result_path_required')
    if getattr(args, 'qa_start_stage', 'wake') != 'question':
        raise ValueError('isolated_qa_requires_explicit_question_stage')
    if getattr(args, 'peer_media_profile', 'legacy') not in {'legacy', 'trusted-none'}:
        raise ValueError('unknown_peer_media_profile')
    private_path = Path(private_path)
    evidence = {'schema_version':1, 'kind':'private_isolated_synthetic_qa_evidence',
        'sessions':[], 'control_receipt':None,
        'model_start_limit':2, 'same_auth_policy':'same inherited authorized Codex home; credentials not inspected or copied',
        'model_consumption_verified':False}
    qa_probe.write_private_result(private_path, evidence, create=True)
    started = monotonic()
    deadline = started + qa_probe.WORK_SECONDS
    checks, identities, completed = [], set(), []
    failure, secondary_failure, phase = None, None, 'synthetic_input'
    try:
        # One fixed waveform for both questions, including the existing phoneme preflight.
        question = await asyncio.to_thread(qa_probe.synthetic_pcm, qa_probe.QUESTION)
        with tempfile.TemporaryDirectory(prefix='isolated-native-qa-') as directory:
            for revision in (1, 2):  # Hard two-slot boundary; no retries or prewarming.
                remaining = deadline - monotonic()
                if remaining <= 0:
                    failure = {'phase':'session_budget','error_code':'work_deadline_exceeded',
                        'timeout_layer':'whole_work','timeout_seconds':qa_probe.WORK_SECONDS}
                    break
                phase = 'session_' + str(revision)
                row = {'session_number':revision, 'source_revision':revision,
                    'launch_elapsed_seconds':monotonic()-started, 'conservative_start_slot_reserved':True,
                    'question_pcm_sha256':hashlib.sha256(question).hexdigest(),
                    'work_budget_seconds':remaining, 'result':None, 'private_evidence':None,
                    'model_consumption_verified':False}
                evidence['sessions'].append(row)
                qa_probe.write_private_result(private_path, evidence)
                child_args = argparse.Namespace(**vars(args))
                child_args.private_result = Path(directory)/('session-'+str(revision)+'.json')
                child_args.session_mode = 'shared'
                result = await qa_probe._run_probe_session(child_args, revision=revision,
                    work_seconds=remaining, question_pcm=question, work_deadline=deadline)
                # Keep the returned first failure/cleanup before further receipt I/O.
                row.update(result=result,
                    completed_elapsed_seconds=monotonic()-started)
                completed.append(result)
                checks.extend(result['checks'])
                # Cleanup evidence is checked independently, not trusted from an accepted flag.
                if result.get('failure') is not None:
                    failure = {'phase':phase,'error_code':'session_probe_failed',
                        'child_failure':result['failure']}
                elif (result.get('schema_version')!=3 or result.get('content_criterion_version')!=CRITERION_VERSION):
                    failure={'phase':phase,'error_code':'session_criterion_unverified'}
                elif not owned_cleanup_verified(result.get('owned_cleanup')):
                    failure = {'phase':phase,'error_code':'session_cleanup_unverified'}
                private = json.loads(child_args.private_result.read_text())
                row['private_evidence'] = private
                if failure is None and not session_proof(result, private, identities):
                    failure = {'phase':phase,'error_code':'session_identity_unverified'}
                elif failure is None and (len(result['checks']) != 1
                        or result['checks'][0]['source_revision'] != revision):
                    failure = {'phase':phase,'error_code':'session_revision_unverified'}
                elif failure is None:
                    identities.add(result['session']['thread_sha256'])
                qa_probe.write_private_result(private_path, evidence)
                if failure is not None:
                    break  # Never launch after timeout, uncertain cleanup or bad identity.
            else:
                phase = 'checks_complete'
    except BaseException as error:
        observed_failure = {'phase':phase,'error_code':'isolated_probe_cancelled'
            if isinstance(error, asyncio.CancelledError) else 'isolated_probe_failed',
            'exception_type':type(error).__name__}
        if failure is None:
            failure = observed_failure
        else:
            secondary_failure = observed_failure
    complete = len(completed) == len(evidence['sessions'])
    clean = bool(completed) and complete and all(owned_cleanup_verified(r.get('owned_cleanup')) for r in completed)
    isolated = failure is None and len(identities) == 2 and len(completed) == 2
    result = {'schema_version':2, 'criterion_version':CRITERION_VERSION, 'qa_receipt_schema_version':3, 'kind':'isolated_synthetic_native_qa', 'session_mode':'isolated',
        'accepted':isolated and clean and all(r['accepted'] is True for r in completed),
        'failure':failure, 'secondary_failure':secondary_failure, 'last_phase':phase, 'checks':checks,
        'session_isolation_verified':isolated,
        'session_isolation_basis':'distinct locally observed thread hashes and serial verified cleanup; not provider memory or consumption ACK',
        'model_start_attempts':len(evidence['sessions']), 'model_start_limit':2,
        'native_start_count_complete':complete,
        'observed_native_start_attempts':sum(r.get('session', {}).get('start_attempted') is True for r in completed),
        'sessions':[{'session_number':row['session_number'], 'source_revision':row['source_revision'],
            'launch_elapsed_seconds':row['launch_elapsed_seconds'],
            'completed_elapsed_seconds':row.get('completed_elapsed_seconds'),
            'work_budget_seconds':row['work_budget_seconds'],
            'question_pcm_sha256':row['question_pcm_sha256'],
            'identity':(row['result'] or {}).get('session'),
            'context_deliveries':(row['private_evidence'] or {}).get('context_deliveries', []),
            'context_plans':(row['private_evidence'] or {}).get('context_plans', []),
            'owned_cleanup':(row['result'] or {}).get('owned_cleanup'),
            'model_consumption_verified':False} for row in evidence['sessions']],
        'owned_cleanup_verified':clean, 'work_limit_seconds':qa_probe.WORK_SECONDS,
        'cleanup_limit_seconds':qa_probe.CLEANUP_SECONDS,
        'model_consumption_verified':False, 'wake_validation':'skipped_unverified',
        'teams_verified':False, 'physical_tx_verified':False}
    evidence['control_receipt'] = result.copy()
    try:
        digest = qa_probe.write_private_result(private_path, evidence)
    except Exception as error:
        receipt_failure = {'phase':'private_receipt','error_code':'private_evidence_write_failed',
            'exception_type':type(error).__name__}
        result['accepted'] = False
        if result['failure'] is None:
            result['failure'] = receipt_failure
        elif result['secondary_failure'] is None:
            result['secondary_failure'] = receipt_failure
        result['private_evidence'] = {'schema_version':1,'written':False,
            'error_code':'private_evidence_write_failed'}
        return result
    result['private_evidence'] = {'schema_version':1,'written':True,'sha256':digest,
        'session_count':len(evidence['sessions'])}
    return result
