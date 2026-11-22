"""Bounded, authenticated single-meeting acceptance; no transport or credentials.

The host reserves two canonical ledger slots before Join. These durable per-run
events enforce at most one A and one B channel, including ambiguous RPC attempts.
They never substitute for the host ledger, operator authority, or verified cleanup.
"""
import json
import re

VERSION='teams-single-v1'
POLICY_EVENT='teams_acceptance_policy'
START_EVENT='teams_acceptance_start_attempt'
FINISHED_EVENT='teams_acceptance_work_finished'
IMMEDIATE_MODE='owner_immediate'


def is_owner_immediate(occurrence):
    """Only the explicit prepared manual contract may omit schedule times."""
    value=occurrence.get('local_preparation')
    if not isinstance(value,dict) or value.get('mode')!=IMMEDIATE_MODE:return False
    if (set(value)!={'mode','source_id','source_revision','request_sha256'}
            or not isinstance(value['source_id'],str) or not re.fullmatch('[0-9a-f]{32}',value['source_id'])
            or type(value['source_revision']) is not int or value['source_revision']<1
            or not isinstance(value['request_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['request_sha256'])
            or any(k not in occurrence for k in ('start_at','end_at','join_early_seconds','max_lateness_seconds'))
            or occurrence.get('calendar_ref') is not None or occurrence.get('start_at') is not None
            or occurrence.get('end_at') is not None or occurrence.get('join_early_seconds') is not None
            or occurrence.get('max_lateness_seconds') is not None):
        raise ValueError('teams_immediate_contract_rejected')
    return True


def validate_policy(value):
    if (not isinstance(value,dict) or set(value)!={'version','request_sha256','maximum_seconds','occurrence_revision'}
            or value.get('version')!=VERSION
            or not isinstance(value.get('request_sha256'),str)
            or not re.fullmatch('[0-9a-f]{64}',value['request_sha256'])
            or type(value.get('occurrence_revision')) is not int or value['occurrence_revision']<1
            or value['maximum_seconds'] is not None and
                (type(value['maximum_seconds']) is not int or not 1<=value['maximum_seconds']<=120)):
        raise ValueError('teams_acceptance_policy_rejected')
    return dict(value)


def policy_from_db(db,session_id):
    rows=db.execute('SELECT payload_json FROM session_events WHERE meeting_id=? AND kind=? LIMIT 2',
        (session_id,POLICY_EVENT)).fetchall()
    if not rows:return None
    if len(rows)!=1:raise ValueError('teams_acceptance_binding_rejected')
    value=json.loads(rows[0][0])
    # Recovery reads may see owner-controlled history. Admission stays unchanged.
    if isinstance(value,dict) and value.get('maximum_seconds') is None:
        if (set(value)!={'version','request_sha256','maximum_seconds','occurrence_revision'}
                or value.get('version')!=VERSION
                or not isinstance(value.get('request_sha256'),str)
                or not re.fullmatch('[0-9a-f]{64}',value['request_sha256'])
                or type(value.get('occurrence_revision')) is not int or value['occurrence_revision']<1):
            raise ValueError('teams_acceptance_policy_rejected')
        return dict(value)
    return validate_policy(value)


def session_budget(session,policy):
    """Persist before RPC under the existing capture lease and SQLite transaction."""
    from .storage import stamp
    def persist(kind,data):
        with session.store._locked(mutating=True,kind='capture',meeting_id=session.ident) as db, db:
            if policy_from_db(db,session.ident)!=policy:
                raise ValueError('teams_acceptance_binding_rejected')
            rows=db.execute('SELECT payload_json FROM session_events WHERE meeting_id=? AND kind=? LIMIT 5',
                (session.ident,START_EVENT)).fetchall()
            seen={(json.loads(row[0]).get('role'),json.loads(row[0]).get('stage')) for row in rows}
            key=(data['role'],data['stage'])
            if len(rows)!=len(seen) or len(rows)>=4 or key in seen \
                    or key[1]=='realtime' and (key[0],'thread') not in seen:
                raise ValueError('teams_acceptance_native_limit')
            if db.execute('SELECT 1 FROM session_events WHERE meeting_id=? AND kind=? LIMIT 1',
                    (session.ident,FINISHED_EVENT)).fetchone():
                raise ValueError('teams_acceptance_already_finished')
            db.execute('INSERT INTO session_events(meeting_id,kind,payload_json,created_at) VALUES(?,?,?,?)',
                (session.ident,kind,json.dumps(data,sort_keys=True),stamp()))
    return NativeStartBudget(policy,persist)


class NativeStartBudget:
    def __init__(self,policy,persist):
        self.policy=validate_policy(policy)
        self.persist=persist
        self.attempts=set()
        self.poisoned=False

    def claim(self,role,stage):
        if (self.poisoned or role not in ('A','B') or stage not in ('thread','realtime')
                or (role,stage) in self.attempts
                or stage=='realtime' and (role,'thread') not in self.attempts):
            raise ValueError('teams_acceptance_native_limit')
        self.attempts.add((role,stage))
        try:
            self.persist(START_EVENT,{'role':role,'stage':stage})
        except BaseException:
            self.poisoned=True
            raise


def receipt(db,session_id,status):
    policy=policy_from_db(db,session_id)
    if policy is None:return None
    attempts=set();complete=False
    rows=db.execute('SELECT kind,payload_json FROM session_events WHERE meeting_id=? AND kind IN (?,?) '
        'ORDER BY rowid LIMIT 7',(session_id,START_EVENT,FINISHED_EVENT)).fetchall()
    for row in rows:
        data=json.loads(row['payload_json'])
        if row['kind']==FINISHED_EVENT:
            if complete:raise ValueError('teams_acceptance_evidence_rejected')
            complete=True;continue
        key=(data.get('role'),data.get('stage'))
        if complete or key[0] not in ('A','B') or key[1] not in ('thread','realtime') or key in attempts \
                or key[1]=='realtime' and (key[0],'thread') not in attempts:
            raise ValueError('teams_acceptance_evidence_rejected')
        attempts.add(key)
    return {**policy,'session_id':session_id,
        'thread_attempts':sum(stage=='thread' for _,stage in attempts),
        'native_attempts':sum(stage=='realtime' for _,stage in attempts),
        'A_thread_attempted':('A','thread') in attempts,'B_thread_attempted':('B','thread') in attempts,
        'A_native_attempted':('A','realtime') in attempts,'B_native_attempted':('B','realtime') in attempts,
        'count_complete':complete,'cleanup_verified':complete and status.get('cleanup_status')=='cleaned',
        'summary_disabled':True}


def meeting_view(store,occurrence_id):
    """Authenticated admin projection; never materializes transcript/title/URL."""
    from .calendar_client import validate_teams_url
    from .target_consent import TARGET_CALENDAR_CLAIM_EVENT
    from urllib.parse import urlsplit
    with store._locked() as db:
        occurrence=store._occurrence(store._row(db,'calendar_occurrences','id',occurrence_id))
        run=db.execute('SELECT * FROM assistant_runs WHERE occurrence_id=?',(occurrence_id,)).fetchone()
        session_id=run['session_id'] if run else None
        row=db.execute('SELECT status_json FROM session_runtime WHERE meeting_id=?',(session_id,)).fetchone() if run else None
        status=json.loads(row[0]) if row else {}
        proof=receipt(db,session_id,status) if run else None
        active=bool(run and db.execute("SELECT 1 FROM execution_leases WHERE session_id=? AND kind='capture' AND state='active'",
            (session_id,)).fetchone())
        result={'protocol':VERSION,'occurrence_id':occurrence_id,'revision':occurrence['revision'],
            'admission_mode':IMMEDIATE_MODE if is_owner_immediate(occurrence) else 'scheduled',
            'occurrence_state':occurrence['state'],'start_at':occurrence['start_at'],'end_at':occurrence['end_at'],
            'max_lateness_seconds':occurrence.get('max_lateness_seconds'),
            'join_host':urlsplit(validate_teams_url(occurrence['join_url'])).hostname,
            'recording_authorized':occurrence.get('recording_authorized') is True,
            'speaking_authorized':occurrence.get('speaking_authorized') is True,
            'scheduler_paused':json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0]).get('scheduler_paused') is True,
            'capture_interlock':bool(store._capture_interlocks_db(db)),'active_lease':active,
            'session_id':session_id,'run_state':run['state'] if run else None,'acceptance':proof}
        prepared=occurrence.get('local_preparation')
        if isinstance(prepared,dict):result['preparation_request_sha256']=prepared.get('request_sha256')
        if proof is not None:
            if db.execute('SELECT 1 FROM session_events WHERE meeting_id=? AND kind=? LIMIT 1',
                    (session_id,TARGET_CALENDAR_CLAIM_EVENT)).fetchone():
                proof['target_calendar_claimed']=True
            proof['cleanup_verified']=proof['cleanup_verified'] and not active and run['state']!='running'
            proof['joined']=bool(db.execute("SELECT 1 FROM session_events WHERE meeting_id=? AND kind='joined' LIMIT 1",(session_id,)).fetchone())
            rx=status.get('rx_audio')
            if rx is None:rx={}
            transport=rx.get('live_transport')
            if transport is None:transport={}
            rx=transport.get('native_source') or {}
            tx=(status.get('response_status') or {}).get('progress',{}).get('tx') or {}
            def count(value):return value if type(value) in (int,float) and 0<=value<10**15 else None
            proof['source_rx_packets']=count(rx.get('packets_received'))
            proof['B_tx_packets']=count(tx.get('packets'))
            proof['B_tx_frames']=count(tx.get('frames'))
            proof['B_tx_energy']=count(tx.get('energy'))
            proof['remote_audibility_verified']=False
        return result
