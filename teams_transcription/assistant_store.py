"""Calendar identity, leased execution, glossary and memory on the source SQLite."""
import hashlib
import json
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timezone

from .meeting_store import MeetingStore, TERMINAL, canonical_json
from .storage import process_signature
from .postmeeting_store import PostmeetingStoreMixin
from .target_consent import TARGET_CALENDAR_CLAIM_EVENT, TargetConsentMixin


class AssistantStoreError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        return parsed.astimezone(timezone.utc).isoformat()
    except (AttributeError, ValueError, TypeError):
        raise AssistantStoreError('invalid_timestamp') from None


def text(value, maximum=200, *, empty=False):
    if (not isinstance(value,str) or len(value)>maximum or not empty and not value.strip()
            or any(ord(char)<32 for char in value)):
        raise AssistantStoreError('invalid_text')
    return value.strip()


def boolean(value):
    if type(value) is not bool:
        raise AssistantStoreError('invalid_policy_boolean')
    return value


def task_signature(item):
    return hashlib.sha256(canonical_json({key:item[key] for key in ('text','owner','date')}).encode()).hexdigest()


class AssistantStore(TargetConsentMixin, PostmeetingStoreMixin, MeetingStore):
    def __init__(self, root):
        super().__init__(root)
        with self._locked() as db:
            db.executescript('''
              CREATE TABLE IF NOT EXISTS assistant_settings(id INTEGER PRIMARY KEY CHECK(id=0),value TEXT NOT NULL);
              INSERT OR IGNORE INTO assistant_settings VALUES(0,'{"scheduler_paused":true}');
              CREATE TABLE IF NOT EXISTS assistant_calendars(id TEXT PRIMARY KEY,account_id TEXT NOT NULL,
                calendar_id TEXT NOT NULL,value TEXT NOT NULL,revision INTEGER NOT NULL,UNIQUE(account_id,calendar_id));
              CREATE TABLE IF NOT EXISTS meeting_series(id TEXT PRIMARY KEY,scope TEXT NOT NULL,
                series_key TEXT NOT NULL UNIQUE,name TEXT NOT NULL,created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS calendar_occurrences(id TEXT PRIMARY KEY,calendar_ref TEXT,
                series_id TEXT NOT NULL,event_id TEXT NOT NULL,identity_key TEXT NOT NULL UNIQUE,value TEXT NOT NULL,
                start_at TEXT,end_at TEXT,state TEXT NOT NULL,revision INTEGER NOT NULL,last_seen TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS upcoming_occurrences ON calendar_occurrences(state,start_at);
              CREATE TABLE IF NOT EXISTS assistant_runs(session_id TEXT PRIMARY KEY,occurrence_id TEXT NOT NULL,
                started_at TEXT NOT NULL,ended_at TEXT,state TEXT NOT NULL,end_reason TEXT,error_code TEXT,
                prior_json TEXT NOT NULL,glossary_json TEXT NOT NULL);
              CREATE INDEX IF NOT EXISTS assistant_runs_occurrence_started ON assistant_runs(occurrence_id,started_at DESC);
              CREATE TABLE IF NOT EXISTS capture_cleanup_reviews(session_id TEXT PRIMARY KEY,
                review_stamp TEXT NOT NULL,reviewed_at TEXT NOT NULL,actor TEXT NOT NULL,
                resources_stopped INTEGER NOT NULL CHECK(resources_stopped=1),
                assistant_left INTEGER NOT NULL CHECK(assistant_left=1));
              CREATE TABLE IF NOT EXISTS summary_jobs(id TEXT PRIMARY KEY,session_id TEXT NOT NULL,
                source_sha256 TEXT NOT NULL,state TEXT NOT NULL,error_code TEXT,attempts INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(session_id,source_sha256));
              CREATE TABLE IF NOT EXISTS glossary_entries(id TEXT PRIMARY KEY,kind TEXT NOT NULL,canonical TEXT NOT NULL,
                aliases TEXT NOT NULL,pronunciation TEXT NOT NULL,definition TEXT NOT NULL,scope TEXT NOT NULL,
                series_id TEXT,status TEXT NOT NULL,revision INTEGER NOT NULL,updated_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS glossary_version(id INTEGER PRIMARY KEY CHECK(id=0),version INTEGER NOT NULL);
              INSERT OR IGNORE INTO glossary_version VALUES(0,0);
              CREATE TABLE IF NOT EXISTS memory_tasks(id TEXT PRIMARY KEY,series_id TEXT NOT NULL,signature TEXT NOT NULL,
                text TEXT NOT NULL,owner TEXT NOT NULL,date TEXT NOT NULL,status TEXT NOT NULL,
                origin_session_id TEXT NOT NULL,source_refs TEXT NOT NULL,updated_at TEXT NOT NULL,
                UNIQUE(series_id,signature));
              CREATE TABLE IF NOT EXISTS assistant_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,
                actor TEXT NOT NULL,action TEXT NOT NULL,target TEXT NOT NULL,created_at TEXT NOT NULL);
            ''')

            self._init_postmeeting(db)
            self._migrate_calendar_identity_db(db)
            self._migrate_assistant_runs_rejoin_db(db)

    @staticmethod
    def _migrate_assistant_runs_rejoin_db(db):
        """Allow explicit operator rejoin runs while preserving every prior sealed run."""
        row = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='assistant_runs'").fetchone()
        if not row or not row['sql'] or 'UNIQUE' not in row['sql'].upper():
            return
        with db:
            db.execute('''CREATE TABLE assistant_runs_v2(session_id TEXT PRIMARY KEY,occurrence_id TEXT NOT NULL,
                started_at TEXT NOT NULL,ended_at TEXT,state TEXT NOT NULL,end_reason TEXT,error_code TEXT,
                prior_json TEXT NOT NULL,glossary_json TEXT NOT NULL)''')
            db.execute('''INSERT INTO assistant_runs_v2
                SELECT session_id,occurrence_id,started_at,ended_at,state,end_reason,error_code,prior_json,glossary_json
                FROM assistant_runs''')
            db.execute('DROP TABLE assistant_runs')
            db.execute('ALTER TABLE assistant_runs_v2 RENAME TO assistant_runs')
            db.execute('CREATE INDEX IF NOT EXISTS assistant_runs_occurrence_started ON assistant_runs(occurrence_id,started_at DESC)')

    @staticmethod
    def _migrate_calendar_identity_db(db):
        """Replace old local calendar keys while retaining public series/occurrence IDs."""
        db.execute('CREATE TABLE IF NOT EXISTS calendar_identity_migrations(version INTEGER PRIMARY KEY)')
        if db.execute('SELECT 1 FROM calendar_identity_migrations WHERE version=1').fetchone():
            return
        with db:
            for calendar in db.execute('SELECT id,account_id,calendar_id FROM assistant_calendars').fetchall():
                for table, key in (('meeting_series','series_key'),('calendar_occurrences','identity_key')):
                    for row in db.execute(f'SELECT id,{key} FROM {table}').fetchall():
                        try:
                            parts = json.loads(row[key])
                        except (ValueError,TypeError):
                            continue
                        if (isinstance(parts,list) and len(parts)>=3
                                and parts[:2]==[calendar['account_id'],calendar['id']]):
                            parts[1] = calendar['calendar_id']
                            db.execute(f'UPDATE {table} SET {key}=? WHERE id=?',(canonical_json(parts),row['id']))
            db.execute('INSERT INTO calendar_identity_migrations VALUES(1)')

    @staticmethod
    def _audit(db, actor, action, target):
        db.execute('INSERT INTO assistant_audit(actor,action,target,created_at) VALUES(?,?,?,?)',
            (text(actor,128),text(action,80),text(target,200),utc_now()))

    @staticmethod
    def _row(db, table, key, ident):
        row = db.execute(f'SELECT * FROM {table} WHERE {key}=?',(ident,)).fetchone()
        if row is None:
            raise AssistantStoreError('record_not_found')
        return row

    def settings(self):
        with self._locked() as db:
            return json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0])

    def update_settings(self, values, actor='admin'):
        allowed = {'scheduler_paused', 'task_stale_days', 'screen_context_mode', 'screen_include_in_summary'}
        if not isinstance(values,dict) or set(values)-allowed:
            raise AssistantStoreError('invalid_settings')
        values = dict(values)
        if 'scheduler_paused' in values:
            values['scheduler_paused'] = boolean(values['scheduler_paused'])
        if 'task_stale_days' in values and (type(values['task_stale_days']) is not int
                or not 0<=values['task_stale_days']<=365):
            raise AssistantStoreError('invalid_settings')
        if 'screen_context_mode' in values and values['screen_context_mode'] not in {'off', 'on_demand', 'watch_and_cache'}:
            raise AssistantStoreError('invalid_screen_policy')
        if 'screen_include_in_summary' in values:
            if type(values['screen_include_in_summary']) is not bool:
                raise AssistantStoreError('invalid_screen_policy')
        with self._locked() as db, db:
            current = json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0])
            current.update(values)
            db.execute('UPDATE assistant_settings SET value=? WHERE id=0',(canonical_json(current),))
            self._audit(db,actor,'settings_changed','settings')
            return current

    def save_calendar(self, values, actor='admin', *, read_only=False):
        fields = {'id','account_id','calendar_id','name','enabled','recording_authorized','speaking_authorized',
            'join_early_seconds','max_lateness_seconds','revision'}
        if not isinstance(values,dict) or set(values)-fields:
            raise AssistantStoreError('invalid_calendar')
        if read_only and (set(values)!={'id','revision','enabled'} or type(values['revision']) is not int
                or type(values['enabled']) is not bool):
            raise AssistantStoreError('invalid_calendar')
        ident = values.get('id') or uuid.uuid4().hex
        with self._locked() as db:
            if read_only:
                # Same lock/transaction boundary as the write and Join claim.
                policy = json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0])
                active = db.execute("SELECT EXISTS(SELECT 1 FROM execution_leases WHERE state='active')").fetchone()[0]
                if policy.get('scheduler_paused') is not True or active:
                    raise AssistantStoreError('calendar_settings_not_quiescent')
                if ident=='primary':
                    if values['revision']!=0 or db.execute('SELECT 1 FROM assistant_calendars WHERE '
                            "account_id='default' AND calendar_id='primary'").fetchone():
                        raise AssistantStoreError('revision_conflict')
                    ident = uuid.uuid4().hex
                    values = {**values,'id':ident,'account_id':'default','calendar_id':'primary','name':'主要行事曆'}
                elif not db.execute('SELECT 1 FROM assistant_calendars WHERE id=?',(ident,)).fetchone():
                    raise AssistantStoreError('record_not_found')
            old = db.execute('SELECT * FROM assistant_calendars WHERE id=?',(ident,)).fetchone()
            current = json.loads(old['value']) if old else {'enabled':False,'recording_authorized':False,
                'speaking_authorized':False,'join_early_seconds':300,'max_lateness_seconds':300,'account_id':'default'}
            if old and values.get('revision') != old['revision']:
                raise AssistantStoreError('revision_conflict')
            if old and any(key in values and values[key]!=current[key] for key in ('account_id','calendar_id')):
                raise AssistantStoreError('calendar_identity_immutable')
            current.update({key:value for key,value in values.items() if key!='revision'})
            current.update(id=ident,account_id=text(current['account_id'],128),
                calendar_id=text(current.get('calendar_id'),512),name=text(current.get('name'),200))
            for key in ('enabled','recording_authorized','speaking_authorized'):
                current[key] = boolean(current[key])
            for key,bound in (('join_early_seconds',600),('max_lateness_seconds',1800)):
                if type(current[key]) is not int or not 0<=current[key]<=bound:
                    raise AssistantStoreError('invalid_join_window')
            revision = old['revision']+1 if old else 1
            with db:
                try:
                    db.execute('INSERT INTO assistant_calendars VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                        'account_id=excluded.account_id,calendar_id=excluded.calendar_id,value=excluded.value,revision=excluded.revision',
                        (ident,current['account_id'],current['calendar_id'],canonical_json(current),revision))
                except Exception as error:
                    import sqlite3
                    if isinstance(error,sqlite3.IntegrityError):
                        raise AssistantStoreError('calendar_already_configured') from None
                    raise
                self._audit(db,actor,'calendar_saved',ident)
            return {**current,'revision':revision}

    def list_calendars(self):
        with self._locked() as db:
            return [{**json.loads(row['value']),'revision':row['revision']} for row in
                db.execute('SELECT * FROM assistant_calendars ORDER BY rowid')]

    def get_calendar(self, ident):
        with self._locked() as db:
            row = self._row(db,'assistant_calendars','id',ident)
            return {**json.loads(row['value']),'revision':row['revision']}

    @staticmethod
    def _series(db, scope, key, name):
        row = db.execute('SELECT id FROM meeting_series WHERE series_key=?',(key,)).fetchone()
        if row:
            return row[0]
        ident = uuid.uuid4().hex
        db.execute('INSERT INTO meeting_series VALUES(?,?,?,?,?)',(ident,scope,key,name,utc_now()))
        return ident

    @staticmethod
    def _occurrence(row):
        return {**json.loads(row['value']),'id':row['id'],'calendar_ref':row['calendar_ref'],
            'series_id':row['series_id'],'state':row['state'],'revision':row['revision']}

    def sync_occurrences(self, calendar_id, events, observed_at=None):
        if not isinstance(events,list):
            raise AssistantStoreError('invalid_calendar_events')
        observed_at = timestamp(observed_at or utc_now())
        with self._locked() as db, db:
            calendar_row = self._row(db,'assistant_calendars','id',calendar_id)
            calendar = json.loads(calendar_row['value'])
            changed = []
            for event in events:
                event_id = text(event.get('event_id'),1024)
                if event.get('status') == 'cancelled' and not event.get('recurring_event_id'):
                    known = db.execute('SELECT value FROM calendar_occurrences WHERE calendar_ref=? AND event_id=?',
                        (calendar_id,event_id)).fetchone()
                    if known:
                        prior = json.loads(known['value'])
                        event = {**event,'recurring_event_id':prior.get('recurring_event_id'),
                            'original_start':prior.get('original_start'),'title':prior['title']}
                recurring = event.get('recurring_event_id')
                if recurring:
                    parent = text(recurring,1024)
                    original = text(event.get('original_start'),512)
                    key = canonical_json([calendar['account_id'],calendar['calendar_id'],parent,original])
                    series_key = canonical_json([calendar['account_id'],calendar['calendar_id'],parent])
                else:
                    key = canonical_json([calendar['account_id'],calendar['calendar_id'],event_id])
                    series_key = key
                old = db.execute('SELECT * FROM calendar_occurrences WHERE identity_key=?',(key,)).fetchone()
                ident = old['id'] if old else uuid.uuid4().hex
                name = text(event.get('title') or '未命名會議',400)
                series = old['series_id'] if old else self._series(db,calendar['account_id'],series_key,name)
                value = {key:item for key,item in event.items()
                    if key not in {'target_control_consent','target_speaking_revoked'}}
                value.update(event_id=event_id,title=name,
                    recording_authorized=calendar['recording_authorized'],speaking_authorized=calendar['speaking_authorized'],
                    join_early_seconds=calendar['join_early_seconds'],max_lateness_seconds=calendar['max_lateness_seconds'])
                previous = json.loads(old['value']) if old else None
                if previous and event.get('updated_at') and previous.get('updated_at') and event['updated_at']<previous['updated_at']:
                    continue
                # Calendar rereads cannot erase current-target local consent or
                # restore speaking after an explicit per-occurrence denial.
                if previous and previous.get('target_speaking_revoked') is True:
                    value.update(speaking_authorized=False,target_speaking_revoked=True)
                if previous and {k:v for k,v in previous.items() if k!='target_control_consent'}==value:
                    if 'target_control_consent' in previous:
                        value['target_control_consent']=previous['target_control_consent']
                runnable = (value.get('status')=='confirmed' and not value.get('all_day')
                    and value.get('link_status')=='valid' and value.get('start_at') and value.get('end_at'))
                state = 'cancelled' if value.get('status')=='cancelled' else 'pending' if runnable else 'needs_review'
                if old and old['state'] == 'deleted':
                    state = 'deleted'
                elif old and old['state'] in {'running','completed','failed','interrupted','skipped'}:
                    state = old['state'] if value.get('status')!='cancelled' else 'cancelled'
                revision = old['revision']+1 if old and previous!=value else old['revision'] if old else 1
                db.execute('INSERT INTO calendar_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?) '
                    'ON CONFLICT(id) DO UPDATE SET value=excluded.value,start_at=excluded.start_at,end_at=excluded.end_at,'
                    'state=excluded.state,revision=excluded.revision,last_seen=excluded.last_seen',
                    (ident,calendar_id,series,event_id,key,canonical_json(value),value.get('start_at'),value.get('end_at'),
                     state,revision,observed_at))
                changed.append(self._occurrence(self._row(db,'calendar_occurrences','id',ident)))
            return changed

    def list_occurrences(self, limit=100):
        with self._locked() as db:
            return [self._occurrence(row) for row in db.execute(
                "SELECT * FROM calendar_occurrences WHERE state!='deleted' ORDER BY start_at DESC,rowid DESC LIMIT ?",
                (min(max(int(limit),1),1000),))]

    def get_occurrence(self, ident):
        with self._locked() as db:
            return self._occurrence(self._row(db,'calendar_occurrences','id',ident))

    def calendar_view(self, start, end, timezone='Asia/Taipei'):
        """Project every overlapping occurrence without loading meeting sources."""
        from .calendar_view import calendar_view
        return calendar_view(self,start,end,timezone)

    def meeting_view(self, occurrence_id, timezone='Asia/Taipei'):
        """Project one occurrence's own sources, frozen prior and current tasks."""
        from .calendar_view import meeting_view
        return meeting_view(self,occurrence_id,timezone)

    def due_occurrences(self, now=None):
        now = time.time() if now is None else float(now)
        threshold = datetime.fromtimestamp(now+600,timezone.utc).isoformat()
        with self._locked() as db:
            rows = db.execute("SELECT o.* FROM calendar_occurrences o LEFT JOIN assistant_calendars c ON c.id=o.calendar_ref "
                "WHERE o.state='pending' AND o.start_at<=? AND json_extract(o.value,'$.recording_authorized')=1 "
                "AND (o.calendar_ref IS NULL OR json_extract(c.value,'$.enabled')=1 "
                "AND json_extract(c.value,'$.recording_authorized')=1) "
                "AND (julianday(o.end_at)-2440587.5)*86400>? "
                "AND (julianday(o.start_at)-2440587.5)*86400-json_extract(o.value,'$.join_early_seconds')<=? "
                "AND (julianday(o.start_at)-2440587.5)*86400+json_extract(o.value,'$.max_lateness_seconds')>=? "
                "ORDER BY o.start_at,o.id LIMIT 100",(threshold,now,now,now))
            return [self._occurrence(row) for row in rows]

    def audit_control(self, actor, action, target):
        with self._locked() as db, db:
            self._audit(db,actor,action,target)

    @staticmethod
    def _cleanup_review_stamp(row, status_json):
        # Bind the entire persisted status, not only the fields displayed by the UI.
        value = {key:row[key] for key in ('session_id','state','ended_at','end_reason','error_code')}
        value['status_json'] = status_json
        return hashlib.sha256(canonical_json(value).encode()).hexdigest()

    @staticmethod
    def _cleanup_review_affected(row, status_json, *, reviewed_before=False):
        status = json.loads(status_json) if status_json else {}
        return (reviewed_before or row['end_reason']=='service_interrupted' or row['error_code']=='capture_cleanup_unknown'
            or status.get('cleanup_status')=='unknown')

    @classmethod
    def _cleanup_review_eligible(cls, row, status_json, *, reviewed_before=False):
        return (row['state'] in TERMINAL|{'interrupted'} and row['ended_at'] is not None
            and cls._cleanup_review_affected(row,status_json,reviewed_before=reviewed_before))

    @staticmethod
    def _cleanup_worker_status(status_json):
        status = json.loads(status_json) if status_json else {}
        pid, signature = status.get('pid'),status.get('process_signature')
        if (type(pid) is not int or not 0<pid<=2147483647 or not isinstance(signature,str)
                or re.fullmatch(r'[0-9]{1,40}',signature) is None):
            return 'unknown'
        try:
            os.kill(pid,0)
        except ProcessLookupError:
            return 'gone'
        except OSError:
            return 'unknown'
        try:
            current = process_signature(pid)
        except (OSError,ValueError):
            return 'unknown'
        if isinstance(current,str) and re.fullmatch(r'[0-9]{1,40}',current) is not None:
            return 'alive' if current==signature else 'gone'
        # It may have exited between the liveness check and reading /proc.
        try:
            os.kill(pid,0)
        except ProcessLookupError:
            return 'gone'
        except OSError:
            pass
        return 'unknown'

    @classmethod
    def _cleanup_review_metadata(cls, db, row, status_json, *, worker_status=None):
        stamp = cls._cleanup_review_stamp(row,status_json)
        review = db.execute('SELECT * FROM capture_cleanup_reviews WHERE session_id=?',
            (row['session_id'],)).fetchone()
        reviewed = (cls._cleanup_review_eligible(row,status_json,reviewed_before=review is not None)
            and review is not None and review['review_stamp']==stamp)
        return {'required':bool(cls._cleanup_review_affected(row,status_json,reviewed_before=review is not None) and not reviewed),
            'stamp':stamp,'reviewed_at':review['reviewed_at'] if reviewed else None,
            'actor':review['actor'] if reviewed else None,
            'worker_status':worker_status if worker_status is not None else cls._cleanup_worker_status(status_json)}

    @classmethod
    def _capture_interlocks_db(cls, db):
        rows = db.execute(
            "SELECT r.*,s.status_json,c.review_stamp FROM assistant_runs r "
            "LEFT JOIN session_runtime s ON s.meeting_id=r.session_id "
            "LEFT JOIN capture_cleanup_reviews c ON c.session_id=r.session_id "
            "WHERE r.end_reason='service_interrupted' OR r.error_code='capture_cleanup_unknown' "
            "OR json_extract(s.status_json,'$.cleanup_status')='unknown' OR c.session_id IS NOT NULL")
        return [row['session_id'] for row in rows if not (
            cls._cleanup_review_eligible(row,row['status_json'],reviewed_before=row['review_stamp'] is not None)
            and row['review_stamp']==cls._cleanup_review_stamp(row,row['status_json']))]

    def capture_interlocks(self):
        with self._locked() as db:
            return self._capture_interlocks_db(db)

    def review_capture_cleanup(self, session_id, expected_stamp, *, resources_stopped, assistant_left, actor='admin'):
        if resources_stopped is not True or assistant_left is not True:
            raise AssistantStoreError('cleanup_review_confirmations_required')
        if not isinstance(expected_stamp,str) or re.fullmatch(r'[0-9a-f]{64}',expected_stamp) is None:
            raise AssistantStoreError('cleanup_review_stale')
        actor = text(actor,128)
        with self._locked() as db, db:
            # Acquire SQLite's write lock before reading any state used to authorize
            # the attestation, including protection from non-flock SQLite writers.
            db.execute('BEGIN IMMEDIATE')
            row = self._row(db,'assistant_runs','session_id',session_id)
            runtime = db.execute('SELECT status_json FROM session_runtime WHERE meeting_id=?',(session_id,)).fetchone()
            status_json = runtime['status_json'] if runtime else None
            review = db.execute('SELECT review_stamp FROM capture_cleanup_reviews WHERE session_id=?',
                (session_id,)).fetchone()
            if (not self._cleanup_review_eligible(row,status_json,reviewed_before=review is not None)
                    or self._meeting(db,session_id)['status'] not in TERMINAL):
                raise AssistantStoreError('cleanup_review_not_eligible')
            if db.execute("SELECT 1 FROM execution_leases WHERE kind='capture' AND state='active'").fetchone():
                raise AssistantStoreError('meeting_slot_busy')
            stamp = self._cleanup_review_stamp(row,status_json)
            if not secrets.compare_digest(expected_stamp,stamp):
                raise AssistantStoreError('cleanup_review_stale')
            worker_status = self._cleanup_worker_status(status_json)
            if worker_status!='gone':
                raise AssistantStoreError('cleanup_review_worker_'+worker_status)
            if review is None or review['review_stamp']!=stamp:
                db.execute('INSERT INTO capture_cleanup_reviews VALUES(?,?,?,?,1,1) '
                    'ON CONFLICT(session_id) DO UPDATE SET review_stamp=excluded.review_stamp,'
                    'reviewed_at=excluded.reviewed_at,actor=excluded.actor,resources_stopped=1,assistant_left=1',
                    (session_id,stamp,utc_now(),actor))
                self._audit(db,actor,'capture_cleanup_reviewed',session_id)
            policy = json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0])
            if not policy['scheduler_paused']:
                policy['scheduler_paused'] = True
                db.execute('UPDATE assistant_settings SET value=? WHERE id=0',(canonical_json(policy),))
            return self._cleanup_review_metadata(db,row,status_json,worker_status=worker_status)

    def skip_occurrence(self, ident, actor='admin'):
        with self._locked() as db, db:
            row = self._row(db,'calendar_occurrences','id',ident)
            if row['state'] not in {'pending','needs_review'}:
                raise AssistantStoreError('occurrence_not_pending')
            db.execute("UPDATE calendar_occurrences SET state='skipped',revision=revision+1 WHERE id=?",(ident,))
            self._audit(db,actor,'occurrence_skipped',ident)

    def delete_occurrence(self, ident, actor='admin'):
        """Remove a manual meeting or tombstone a synced Google Calendar occurrence.

        Never deletes an active capture, an unreviewed cleanup interlock, or an
        occurrence with an active summary/postmeeting job.
        """
        with self._locked() as db, db:
            row = self._row(db,'calendar_occurrences','id',ident)
            if row['state'] == 'deleted':
                raise AssistantStoreError('record_not_found')
            runs = db.execute('SELECT session_id,state FROM assistant_runs WHERE occurrence_id=?',(ident,)).fetchall()
            session_ids = [run['session_id'] for run in runs]
            if row['state'] == 'running' or any(run['state'] == 'running' for run in runs):
                raise AssistantStoreError('meeting_running_cannot_delete')
            if session_ids:
                placeholders = ','.join('?' for _ in session_ids)
                if db.execute(f"SELECT 1 FROM execution_leases WHERE session_id IN ({placeholders}) AND state='active' LIMIT 1",
                        session_ids).fetchone():
                    raise AssistantStoreError('meeting_running_cannot_delete')
                interlocks = set(self._capture_interlocks_db(db))
                if any(sid in interlocks for sid in session_ids):
                    raise AssistantStoreError('capture_cleanup_review_required')
                if db.execute(f"SELECT 1 FROM summary_jobs WHERE session_id IN ({placeholders}) AND state='running' LIMIT 1",
                        session_ids).fetchone():
                    raise AssistantStoreError('summary_already_running')
                if db.execute(f"SELECT 1 FROM postmeeting_jobs WHERE session_id IN ({placeholders}) AND state IN ('running','blocked') LIMIT 1",
                        session_ids).fetchone():
                    raise AssistantStoreError('postmeeting_active_cannot_delete')
            if row['calendar_ref'] is not None:
                db.execute("UPDATE calendar_occurrences SET state='deleted',revision=revision+1 WHERE id=?",(ident,))
                self._audit(db,actor,'occurrence_deleted',ident)
                return {'deleted':True,'id':ident,'tombstoned':True}
            if session_ids:
                placeholders = ','.join('?' for _ in session_ids)
                job_ids = [item['id'] for item in db.execute(
                    f'SELECT id FROM postmeeting_jobs WHERE session_id IN ({placeholders})',session_ids).fetchall()]
                if job_ids:
                    jph = ','.join('?' for _ in job_ids)
                    for table in ('postmeeting_attempts','postmeeting_publications','postmeeting_notifications','series_state_revisions'):
                        db.execute(f'DELETE FROM {table} WHERE job_id IN ({jph})',job_ids)
                for table in ('postmeeting_notifications','postmeeting_publications','postmeeting_jobs',
                        'postmeeting_snapshots','summary_jobs','capture_cleanup_reviews','execution_leases','assistant_runs'):
                    db.execute(f'DELETE FROM {table} WHERE session_id IN ({placeholders})',session_ids)
                for table in ('summary_revisions','summary_publication_recovery','question_activity',
                        'screen_observations',
                        'session_runtime','session_stop_requests','session_events','transcript_increments',
                        'transcript_captures','finals','revisions','final_aliases','provisionals','meetings'):
                    db.execute(f'DELETE FROM {table} WHERE meeting_id IN ({placeholders})',session_ids)
            db.execute('DELETE FROM calendar_occurrences WHERE id=?',(ident,))
            series = db.execute('SELECT scope FROM meeting_series WHERE id=?',(row['series_id'],)).fetchone()
            if series and series['scope'] == 'manual':
                remaining = db.execute('SELECT 1 FROM calendar_occurrences WHERE series_id=? LIMIT 1',
                    (row['series_id'],)).fetchone()
                if remaining is None:
                    db.execute('DELETE FROM memory_tasks WHERE series_id=?',(row['series_id'],))
            self._audit(db,actor,'occurrence_deleted',ident)
            return {'deleted':True,'id':ident,'tombstoned':False}

    def list_series(self):
        with self._locked() as db:
            return [dict(row) for row in db.execute('SELECT * FROM meeting_series ORDER BY name,id')]

    def get_series(self, ident):
        with self._locked() as db:
            return dict(self._row(db,'meeting_series','id',ident))

    def assign_series(self, occurrence_id, series_id, actor='admin'):
        with self._locked() as db, db:
            row = self._row(db,'calendar_occurrences','id',occurrence_id)
            series = self._row(db,'meeting_series','id',series_id)
            if row['calendar_ref']:
                calendar = self._row(db,'assistant_calendars','id',row['calendar_ref'])
                if calendar['account_id'] != series['scope']:
                    raise AssistantStoreError('series_scope_mismatch')
            if db.execute('SELECT 1 FROM assistant_runs WHERE occurrence_id=?',(occurrence_id,)).fetchone():
                raise AssistantStoreError('occurrence_already_executed')
            db.execute('UPDATE calendar_occurrences SET series_id=?,revision=revision+1 WHERE id=?',(series_id,occurrence_id))
            self._audit(db,actor,'series_assigned',occurrence_id)

    def create_manual_occurrence(self, values, actor='admin'):
        from .calendar_client import validate_teams_url
        fields = {'title','join_url','start_at','end_at','series_id','recording_authorized','speaking_authorized'}
        if not isinstance(values,dict) or set(values)-fields:
            raise AssistantStoreError('invalid_manual_occurrence')
        url = validate_teams_url(text(values.get('join_url'),8192))
        start, end = timestamp(values.get('start_at')), timestamp(values.get('end_at'))
        if end<=start:
            raise AssistantStoreError('invalid_meeting_window')
        name = text(values.get('title'),400)
        ident = uuid.uuid4().hex
        value = dict(values)
        value.update(event_id='manual-'+ident,join_url=url,start_at=start,end_at=end,title=name,
            status='confirmed',all_day=False,link_status='valid',join_early_seconds=0,max_lateness_seconds=1800,
            recording_authorized=boolean(values.get('recording_authorized',False)),
            speaking_authorized=boolean(values.get('speaking_authorized',False)))
        with self._locked() as db, db:
            series = values.get('series_id')
            if series:
                self._row(db,'meeting_series','id',series)
            else:
                series = self._series(db,'manual','manual-'+ident,name)
            db.execute('INSERT INTO calendar_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (ident,None,series,value['event_id'],'manual-'+ident,canonical_json(value),start,end,'pending',1,utc_now()))
            self._audit(db,actor,'manual_occurrence_created',ident)
            return self._occurrence(self._row(db,'calendar_occurrences','id',ident))

    def prepare_owned_occurrence(self, fields, actor='local-teams-prepare'):
        """Clone one approved terminal manual target, atomically and idempotently.

        No arbitrary URL, consent, clock-derived meeting window or Join. The host
        supplies a source-bound immediate mode and root-authority request hash.
        """
        required={'source_id','source_revision','request_sha256','mode'}
        if (not isinstance(fields,dict) or set(fields)!=required
                or not isinstance(fields['source_id'],str) or not re.fullmatch('[0-9a-f]{32}',fields['source_id'])
                or type(fields['source_revision']) is not int or fields['source_revision']<1
                or not isinstance(fields['request_sha256'],str) or not re.fullmatch('[0-9a-f]{64}',fields['request_sha256'])
                or fields['mode']!='owner_immediate'):
            raise AssistantStoreError('teams_preparation_rejected')
        binding=dict(fields)
        key='local-prepare-'+fields['request_sha256']
        with self._locked() as db,db:
            policy=json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0])
            if (not (self.root/'.deployment-maintenance').exists() or policy.get('scheduler_paused') is not True
                    or self._capture_interlocks_db(db)
                    or db.execute("SELECT 1 FROM execution_leases WHERE kind='capture' AND state='active'").fetchone()):
                raise AssistantStoreError('teams_preparation_not_quiescent')
            row=self._row(db,'calendar_occurrences','id',fields['source_id'])
            source=self._occurrence(row)
            if (source['calendar_ref'] is not None or source['revision']!=fields['source_revision']
                    or source['state'] not in {'completed','failed'} or source.get('status')!='confirmed'
                    or source.get('recording_authorized') is not True or source.get('speaking_authorized') is not True):
                raise AssistantStoreError('teams_preparation_source_rejected')
            previous=db.execute('SELECT * FROM calendar_occurrences WHERE identity_key=?',(key,)).fetchone()
            if previous:
                target=self._occurrence(previous)
                if target.get('local_preparation')!=binding or target['state']!='pending' or target['revision']!=1:
                    raise AssistantStoreError('teams_preparation_already_used')
                return {'prepared':True,'occurrence_id':target['id'],'revision':1,'request_sha256':fields['request_sha256']}
            from .calendar_client import validate_teams_url
            validate_teams_url(source['join_url'])
            ident=uuid.uuid4().hex
            value=json.loads(row['value'])
            value.update(event_id='manual-'+ident,start_at=None,end_at=None,status='confirmed',
                join_early_seconds=None,max_lateness_seconds=None,local_preparation=binding,
                prepared_at=utc_now())  # creation metadata, never participant presence
            db.execute('INSERT INTO calendar_occurrences VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (ident,None,source['series_id'],value['event_id'],key,canonical_json(value),None,None,'pending',1,utc_now()))
            self._audit(db,actor,'owned_occurrence_prepared',ident)
            return {'prepared':True,'occurrence_id':ident,'revision':1,'request_sha256':fields['request_sha256']}

    @classmethod
    def _verify_lease(cls, db, lease, kind):
        row = db.execute('SELECT * FROM execution_leases WHERE lease_id=?',(lease.get('id'),)).fetchone()
        if (row is None or row['kind']!=kind or row['state']!='active' or not cls._lease_time_valid(row)
                or row['boot_id']!=lease.get('boot_id')
                or row['owner']!=lease.get('owner') or row['generation']!=lease.get('generation')
                or row['token']!=lease.get('token') or row['session_id']!=lease.get('session_id')):
            raise AssistantStoreError('stale_execution_lease')
        return row

    def _prior_db(self, db, series_id, before_at):
        from .history_context import validate_history
        rows = db.execute('SELECT r.session_id,COALESCE(o.start_at,r.started_at) AS start_at,s.summary_json,s.metadata,m.status AS capture_status,'
            'm.error_code AS capture_error_code,r.end_reason FROM assistant_runs r '
            'JOIN calendar_occurrences o ON o.id=r.occurrence_id JOIN summary_revisions s ON s.meeting_id=r.session_id '
            'JOIN meetings m ON m.meeting_id=r.session_id '
            'WHERE o.series_id=? AND COALESCE(o.start_at,r.started_at)<? ORDER BY COALESCE(o.start_at,r.started_at) DESC,s.revision DESC',
            (series_id,before_at))
        for row in rows:
            metadata = json.loads(row['metadata'])
            if not metadata.get('provider_requirement_met'):
                continue
            value = json.loads(row['summary_json'])
            tasks = self._tasks_db(db,series_id)
            by_signature = {task['signature']:task for task in tasks}
            summary = {key:value[key] for key in ('重點','決策','待辦','未決問題')}
            annotated_tasks = []
            for item in summary['待辦']:
                task = by_signature.get(task_signature(item))
                annotated_tasks.append({**item,**({'task_id':task['id'],'task_status':task['status']} if task else {})})
            summary['待辦'] = annotated_tasks
            return validate_history({'available':True,'series_id':series_id,'session_id':row['session_id'],
                'occurred_at':row['start_at'],'revision':metadata['revision'],'sha256':metadata['sha256'],
                'summary':summary,
                'source_quality':value['source_quality'],'capture_status':row['capture_status'],
                'capture_error_code':row['capture_error_code'],'end_reason':row['end_reason'],
                'open_tasks':tasks})
        return validate_history({'available':False,'series_id':series_id,'open_tasks':self._tasks_db(db,series_id)})

    def prior_context(self, series_id, before_at):
        with self._locked() as db:
            self._row(db,'meeting_series','id',series_id)
            return self._prior_db(db,series_id,timestamp(before_at))

    def claim_run(self, occurrence_id, owner, lease_seconds=30, *, acceptance=None, target_control=False, allow_rejoin=False):
        from .teams_acceptance import validate_policy, POLICY_EVENT
        if acceptance is not None:acceptance=validate_policy(acceptance)
        if allow_rejoin and (acceptance is not None or target_control):
            raise AssistantStoreError('occurrence_not_rejoinable')
        owner = text(owner,128)
        with self._locked() as db, db:
            if (self.root / '.deployment-maintenance').exists():
                raise AssistantStoreError('deployment_maintenance')
            if acceptance is not None:
                policy=json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0])
                if policy.get('scheduler_paused') is not True:
                    raise AssistantStoreError('teams_acceptance_scheduler_must_pause')
                if db.execute("SELECT 1 FROM session_events WHERE kind=? AND json_extract(payload_json,'$.request_sha256')=? LIMIT 1",
                        (POLICY_EVENT,acceptance['request_sha256'])).fetchone():
                    raise AssistantStoreError('teams_acceptance_already_claimed')
            occurrence = self._occurrence(self._row(db,'calendar_occurrences','id',occurrence_id))
            if occurrence['state'] == 'deleted':
                raise AssistantStoreError('record_not_found')
            if occurrence.get('local_preparation') is not None and acceptance is None:
                raise AssistantStoreError('teams_preparation_acceptance_required')
            from .teams_acceptance import is_owner_immediate
            if is_owner_immediate(occurrence) and acceptance['maximum_seconds'] is not None:
                raise AssistantStoreError('teams_owner_stop_policy_required')
            if acceptance is not None and occurrence['revision']!=acceptance['occurrence_revision']:
                raise AssistantStoreError('teams_acceptance_revision_changed')
            calendar_consent = self._validate_target_consent_db(db,occurrence,acceptance) if target_control else None
            if allow_rejoin:
                if (occurrence['state'] not in {'completed','stopped','failed','interrupted','skipped'}
                        or occurrence.get('status')=='cancelled'
                        or occurrence.get('link_status')!='valid'
                        or occurrence.get('all_day')):
                    raise AssistantStoreError('occurrence_not_rejoinable')
            elif occurrence['state']!='pending':
                raise AssistantStoreError('occurrence_not_ready')
            if calendar_consent is None:
                if not occurrence.get('recording_authorized'):
                    raise AssistantStoreError('recording_authorization_required')
                if acceptance is not None and not occurrence.get('speaking_authorized'):
                    raise AssistantStoreError('speaking_authorization_required')
                if occurrence['calendar_ref']:
                    calendar = json.loads(self._row(db,'assistant_calendars','id',occurrence['calendar_ref'])['value'])
                    if not calendar['enabled'] or not calendar['recording_authorized']:
                        raise AssistantStoreError('calendar_not_authorized')
                    if target_control and not calendar.get('speaking_authorized'):
                        raise AssistantStoreError('speaking_authorization_required')
            if db.execute("SELECT 1 FROM execution_leases WHERE kind='capture' AND state='active'").fetchone():
                raise AssistantStoreError('meeting_slot_busy')
            if self._capture_interlocks_db(db):
                raise AssistantStoreError('capture_cleanup_review_required')
            if not allow_rejoin and db.execute('SELECT 1 FROM assistant_runs WHERE occurrence_id=?',(occurrence_id,)).fetchone():
                raise AssistantStoreError('occurrence_already_executed')
            session_id = uuid.uuid4().hex
            prior = self._prior_db(db,occurrence['series_id'],occurrence['start_at'] or utc_now())
            glossary = self._glossary_snapshot_db(db,occurrence['series_id'])
            token = secrets.token_hex(32)
            boot_id, now = self._lease_clock()
            lease = {'id':session_id,'session_id':session_id,'occurrence_id':occurrence_id,
                'kind':'capture','owner':owner,'generation':1,'token':token,
                'expires_at':time.time()+lease_seconds,
                'boot_id':boot_id,'expires_boottime':now+lease_seconds}
            db.execute('INSERT INTO execution_leases '
                '(lease_id,kind,session_id,owner,generation,token,expires_at,state,boot_id,expires_boottime) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (session_id,'capture',session_id,owner,1,token,lease['expires_at'],'active',
                 boot_id,lease['expires_boottime']))
            db.execute('INSERT INTO assistant_runs VALUES(?,?,?,NULL,?,NULL,NULL,?,?)',
                (session_id,occurrence_id,utc_now(),'running',canonical_json(prior),canonical_json(glossary)))
            db.execute("UPDATE calendar_occurrences SET state='running',revision=revision+1 WHERE id=?",(occurrence_id,))
            db.execute("INSERT INTO meetings(meeting_id,prior_minutes,status,version,error_code) VALUES(?,?,'active',1,NULL)",
                (session_id,canonical_json(prior)))
            if acceptance is not None:
                db.execute('INSERT INTO session_events(meeting_id,kind,payload_json,created_at) VALUES(?,?,?,?)',
                    (session_id,POLICY_EVENT,canonical_json(acceptance),utc_now()))
                lease['acceptance_occurrence']=occurrence
            if calendar_consent is not None:
                db.execute('INSERT INTO session_events(meeting_id,kind,payload_json,created_at) VALUES(?,?,?,?)',
                    (session_id,TARGET_CALENDAR_CLAIM_EVENT,'{}',utc_now()))
                lease['target_calendar_consent'] = calendar_consent
            return lease

    def heartbeat_run(self, lease, lease_seconds=30):
        with self._locked() as db, db:
            self._verify_lease(db,lease,'capture')
            _, now = self._lease_clock()
            db.execute('UPDATE execution_leases SET expires_at=?,expires_boottime=? WHERE lease_id=?',
                (time.time()+lease_seconds,now+lease_seconds,lease['id']))

    def request_stop(self, session_id, actor='admin'):
        with self._locked() as db, db:
            row = self._row(db,'assistant_runs','session_id',session_id)
            if row['state']!='running':
                raise AssistantStoreError('meeting_not_running')
            db.execute('UPDATE session_runtime SET stop_requested=1 WHERE meeting_id=?',(session_id,))
            db.execute('INSERT OR IGNORE INTO session_stop_requests VALUES(?)',(session_id,))
            self._audit(db,actor,'meeting_stop_requested',session_id)

    def stop_requested(self, lease):
        with self._locked() as db:
            self._verify_lease(db,lease,'capture')
            row = db.execute('SELECT 1 FROM session_stop_requests WHERE meeting_id=?',(lease['session_id'],)).fetchone()
            return row is not None

    def _seal_run(self, db, session_id, status, end_reason, error_code):
        row = self._row(db,'assistant_runs','session_id',session_id)
        meeting = self._meeting(db,session_id)
        if meeting['status'] not in TERMINAL:
            db.execute('UPDATE meetings SET status=?,error_code=?,version=version+1 WHERE meeting_id=?',
                (status,error_code,session_id))
        db.execute("UPDATE transcript_captures SET state='sealed',reason=COALESCE(reason,'application_stop'),"
            'ended_at=COALESCE(ended_at,?) WHERE meeting_id=?',(utc_now(),session_id))
        db.execute('UPDATE assistant_runs SET ended_at=?,state=?,end_reason=?,error_code=? WHERE session_id=?',
            (utc_now(),'interrupted' if end_reason=='service_interrupted' else status,end_reason,error_code,session_id))
        db.execute('UPDATE calendar_occurrences SET state=?,revision=revision+1 WHERE id=?',
            ('interrupted' if end_reason=='service_interrupted' else 'completed' if status in {'completed','stopped'} else 'failed',row['occurrence_id']))
        if end_reason != 'service_interrupted':
            self._enqueue_summary(db,self,session_id)
        self._create_postmeeting_snapshot_db(db,session_id)

    @staticmethod
    def _enqueue_summary(db, store, session_id):
        # Server-side post-meeting Codex summary re-authorized by the operator on 2026-11-23.
        # Only enqueue for ordinary sealed runs with transcript entries (never acceptance-budgeted runs).
        from .teams_acceptance import POLICY_EVENT
        if db.execute('SELECT 1 FROM session_events WHERE meeting_id=? AND kind=? LIMIT 1',
                (session_id, POLICY_EVENT)).fetchone():
            return None
        source = store._summary_source(db,session_id)
        if not source['terminal'] or not (source['events'] or source['segments'] or source['provisionals']):
            return None
        now = utc_now()
        db.execute(
            "INSERT OR IGNORE INTO summary_jobs(id,session_id,source_sha256,state,error_code,attempts,created_at,updated_at) "
            "VALUES(?,?,?,'pending',NULL,0,?,?)",
            (uuid.uuid4().hex,session_id,source['source_sha256'],now,now))
        return source['source_sha256']

    def finish_run(self, lease, status, end_reason, error_code=None):
        if status not in TERMINAL or not re.fullmatch(r'[a-z0-9_]{1,80}',end_reason):
            raise AssistantStoreError('invalid_run_outcome')
        if error_code is not None and not re.fullmatch(r'[a-z0-9_]{1,80}',error_code):
            raise AssistantStoreError('invalid_error_code')
        with self._locked() as db, db:
            self._verify_lease(db,lease,'capture')
            self._seal_run(db,lease['session_id'],status,end_reason,error_code)
            db.execute("UPDATE execution_leases SET state='completed' WHERE lease_id=?",(lease['id'],))

    def recover_expired(self, now_boottime=None):
        with self._locked() as db, db:
            boot_id, now = self._lease_clock()
            if now_boottime is not None:
                now = float(now_boottime)
            leases = db.execute("SELECT * FROM execution_leases WHERE state='active' "
                'AND (boot_id!=? OR expires_boottime<=?)',(boot_id,now)).fetchall()
            for lease in leases:
                if lease['kind']=='capture':
                    self._seal_run(db,lease['session_id'],'failed','service_interrupted','service_interrupted')
                else:
                    db.execute("UPDATE summary_jobs SET state='pending',error_code='summary_interrupted',updated_at=? WHERE id=?",
                        (utc_now(),lease['lease_id']))
                db.execute("UPDATE execution_leases SET state='expired' WHERE lease_id=?",(lease['lease_id'],))
            return len(leases)

    def get_run(self, session_id):
        with self._locked() as db:
            row = dict(self._row(db,'assistant_runs','session_id',session_id))
            occurrence = self._occurrence(self._row(db,'calendar_occurrences','id',row['occurrence_id']))
            runtime = db.execute('SELECT status_json FROM session_runtime WHERE meeting_id=?',(session_id,)).fetchone()
            row.update(occurrence=occurrence,prior_context=json.loads(row.pop('prior_json')),
                glossary_snapshot=json.loads(row.pop('glossary_json')),status=json.loads(runtime[0]) if runtime else {})
            from .teams_acceptance import policy_from_db
            row['acceptance_policy']=policy_from_db(db,session_id)
            row['source'] = self._publish(db,session_id)
            row['cleanup_review'] = self._cleanup_review_metadata(db,row,runtime[0] if runtime else None)
            return row

    def list_runs(self, limit=100):
        with self._locked() as db:
            ids = [row[0] for row in db.execute(
                "SELECT r.session_id FROM assistant_runs r JOIN calendar_occurrences o ON o.id=r.occurrence_id "
                "WHERE o.state!='deleted' ORDER BY r.started_at DESC LIMIT ?",
                (min(max(int(limit),1),1000),))]
        return [self.get_run(ident) for ident in ids]

    def get_run_config(self, session_id):
        with self._locked() as db:
            row = self._row(db,'session_runtime','meeting_id',session_id)
            return json.loads(row['config_json'])

    def update_run_config(self, session_id, config, lease):
        with self._locked() as db, db:
            self._verify_lease(db,lease,'capture')
            if lease['session_id']!=session_id:
                raise AssistantStoreError('wrong_session_scope')
            db.execute('UPDATE session_runtime SET config_json=? WHERE meeting_id=?',(canonical_json(config),session_id))

    def save_glossary(self, values, actor='admin'):
        fields = {'id','kind','canonical','aliases','pronunciation','definition','scope','series_id','status','revision'}
        if not isinstance(values,dict) or set(values)-fields:
            raise AssistantStoreError('invalid_glossary_entry')
        ident = values.get('id') or uuid.uuid4().hex
        with self._locked() as db:
            old = db.execute('SELECT * FROM glossary_entries WHERE id=?',(ident,)).fetchone()
            current = dict(old) if old else {'kind':'term','aliases':[],'pronunciation':'','definition':'',
                'scope':'global','series_id':None,'status':'pending'}
            if old:
                if values.get('revision')!=old['revision']:
                    raise AssistantStoreError('revision_conflict')
                current['aliases'] = json.loads(current['aliases'])
            current.update(values)
            if current['kind'] not in {'person','term','abbreviation'} or current['status'] not in {'pending','active','disabled'}:
                raise AssistantStoreError('invalid_glossary_entry')
            canonical = text(current.get('canonical'),160)
            aliases = current['aliases']
            if not isinstance(aliases,list) or len(aliases)>20:
                raise AssistantStoreError('invalid_glossary_aliases')
            aliases = list(dict.fromkeys(text(alias,160) for alias in aliases))
            if current['scope'] not in {'global','series'} or (current['scope']=='series')!=(current.get('series_id') is not None):
                raise AssistantStoreError('invalid_glossary_scope')
            if current.get('series_id'):
                self._row(db,'meeting_series','id',current['series_id'])
            revision = old['revision']+1 if old else 1
            pronunciation = text(current.get('pronunciation',''),160,empty=True)
            definition = text(current.get('definition',''),600,empty=True)
            with db:
                db.execute('INSERT INTO glossary_entries VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET '
                    'kind=excluded.kind,canonical=excluded.canonical,aliases=excluded.aliases,pronunciation=excluded.pronunciation,'
                    'definition=excluded.definition,scope=excluded.scope,series_id=excluded.series_id,status=excluded.status,'
                    'revision=excluded.revision,updated_at=excluded.updated_at',
                    (ident,current['kind'],canonical,canonical_json(aliases),pronunciation,definition,current['scope'],
                     current.get('series_id'),current['status'],revision,utc_now()))
                db.execute('UPDATE glossary_version SET version=version+1 WHERE id=0')
                self._audit(db,actor,'glossary_saved',ident)
            return self._glossary_row(self._row(db,'glossary_entries','id',ident))

    @staticmethod
    def _glossary_row(row):
        return {**dict(row),'aliases':json.loads(row['aliases'])}

    def get_glossary(self, ident):
        with self._locked() as db:
            return self._glossary_row(self._row(db,'glossary_entries','id',ident))

    def list_glossary(self, scope=None):
        with self._locked() as db:
            rows = [self._glossary_row(row) for row in db.execute('SELECT * FROM glossary_entries ORDER BY canonical,id')]
        if scope:
            rows = [row for row in rows if row['scope']=='global' or row['series_id']==scope]
        for row in rows:
            names = {row['canonical'].casefold(),*(alias.casefold() for alias in row['aliases'])}
            row['conflicts'] = [other['id'] for other in rows if other['id']!=row['id'] and
                (other['scope']=='global' or row['scope']=='global' or other['series_id']==row['series_id']) and
                names & {other['canonical'].casefold(),*(alias.casefold() for alias in other['aliases'])}]
        return rows

    def set_glossary_status(self, ident, status, actor='admin'):
        row = self.get_glossary(ident)
        fields = {key:row[key] for key in ('id','kind','canonical','aliases','pronunciation','definition','scope','series_id','revision')}
        return self.save_glossary({**fields,'status':status},actor)

    def _glossary_snapshot_db(self, db, series_id):
        version = db.execute('SELECT version FROM glossary_version WHERE id=0').fetchone()[0]
        rows = db.execute("SELECT * FROM glossary_entries WHERE status='active' AND (scope='global' OR series_id=?) "
            "ORDER BY scope='series' DESC,updated_at DESC,id",(series_id,)).fetchall()
        entries = []
        for row in rows:
            entry = {key:row[key] for key in ('id','kind','canonical','pronunciation','definition','revision')}
            entry['aliases'] = json.loads(row['aliases'])
            entries.append(entry)
        raw = canonical_json(entries)
        return {'version':version,'sha256':hashlib.sha256(raw.encode()).hexdigest(),'entries':entries,
            'total_relevant':len(rows),'omitted_count':len(rows)-len(entries),'truncated':len(rows)!=len(entries)}

    def glossary_snapshot(self, series_id):
        with self._locked() as db:
            return self._glossary_snapshot_db(db,series_id)

    def claim_summary(self, owner, lease_seconds=90):
        owner = text(owner,128)
        with self._locked() as db, db:
            if (self.root / '.deployment-maintenance').exists():
                return None
            if db.execute("SELECT 1 FROM execution_leases WHERE kind='summary' AND state='active'").fetchone():
                return None
            job = db.execute("SELECT * FROM summary_jobs WHERE state='pending' ORDER BY created_at,id LIMIT 1").fetchone()
            if job is None:
                return None
            old = db.execute('SELECT generation FROM execution_leases WHERE lease_id=?',(job['id'],)).fetchone()
            generation = old[0]+1 if old else 1
            boot_id, now = self._lease_clock()
            lease = {'id':job['id'],'session_id':job['session_id'],'kind':'summary','owner':owner,
                'generation':generation,'token':secrets.token_hex(32),'expires_at':time.time()+lease_seconds,
                'boot_id':boot_id,'expires_boottime':now+lease_seconds,
                'source_sha256':job['source_sha256']}
            db.execute('INSERT OR REPLACE INTO execution_leases '
                '(lease_id,kind,session_id,owner,generation,token,expires_at,state,boot_id,expires_boottime) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (lease['id'],'summary',lease['session_id'],owner,generation,lease['token'],
                 lease['expires_at'],'active',boot_id,lease['expires_boottime']))
            db.execute("UPDATE summary_jobs SET state='running',attempts=attempts+1,error_code=NULL,updated_at=? WHERE id=?",
                (utc_now(),job['id']))
            return lease

    def heartbeat_summary(self, lease, lease_seconds=90):
        with self._locked() as db, db:
            self._verify_lease(db,lease,'summary')
            _, now = self._lease_clock()
            db.execute('UPDATE execution_leases SET expires_at=?,expires_boottime=? WHERE lease_id=?',
                (time.time()+lease_seconds,now+lease_seconds,lease['id']))

    @staticmethod
    def _roll_tasks(db, session_id, summary):
        run = AssistantStore._row(db,'assistant_runs','session_id',session_id)
        occurrence = AssistantStore._row(db,'calendar_occurrences','id',run['occurrence_id'])
        for item in summary['待辦']:
            signature = task_signature(item)
            existing = db.execute('SELECT * FROM memory_tasks WHERE series_id=? AND signature=?',
                (occurrence['series_id'],signature)).fetchone()
            refs = [{'session_id':session_id,'source_id':ident} for ident in item['sources']]
            if existing:
                all_refs = json.loads(existing['source_refs'])
                all_refs.extend(ref for ref in refs if ref not in all_refs)
                db.execute('UPDATE memory_tasks SET source_refs=?,updated_at=? WHERE id=?',
                    (canonical_json(all_refs),utc_now(),existing['id']))
            else:
                db.execute('INSERT INTO memory_tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (uuid.uuid4().hex,occurrence['series_id'],signature,item['text'],item['owner'],item['date'],
                     'open',session_id,canonical_json(refs),utc_now()))

    def finish_summary(self, lease, error_code=None):
        if error_code is not None and not re.fullmatch(r'[a-z0-9_]{1,80}',error_code):
            raise AssistantStoreError('invalid_error_code')
        with self._locked() as db, db:
            self._verify_lease(db,lease,'summary')
            metadata = self._summary_state(db,lease['session_id'])
            if error_code is None:
                if metadata is None or metadata['source_sha256']!=lease['source_sha256'] or not metadata['provider_requirement_met']:
                    raise AssistantStoreError('summary_not_published')
            db.execute('UPDATE summary_jobs SET state=?,error_code=?,updated_at=? WHERE id=?',
                ('published' if error_code is None else 'failed',error_code,utc_now(),lease['id']))
            db.execute("UPDATE execution_leases SET state='completed' WHERE lease_id=?",(lease['id'],))

    def retry_summary(self, session_id, actor='admin'):
        with self._locked() as db, db:
            self._row(db,'assistant_runs','session_id',session_id)
            source = self._summary_source(db,session_id)
            if not source['terminal'] or not (source['events'] or source['segments'] or source['provisionals']):
                raise AssistantStoreError('summary_source_unavailable')
            if db.execute(
                    "SELECT 1 FROM execution_leases WHERE kind='summary' AND session_id=? AND state='active'",
                    (session_id,)).fetchone():
                raise AssistantStoreError('summary_already_running')
            now = utc_now()
            existing = db.execute(
                'SELECT id FROM summary_jobs WHERE session_id=? AND source_sha256=?',
                (session_id,source['source_sha256'])).fetchone()
            if existing:
                db.execute(
                    "UPDATE summary_jobs SET state='pending',error_code=NULL,updated_at=? WHERE id=?",
                    (now,existing['id']))
            else:
                db.execute(
                    "INSERT INTO summary_jobs(id,session_id,source_sha256,state,error_code,attempts,created_at,updated_at) "
                    "VALUES(?,?,?,'pending',NULL,0,?,?)",
                    (uuid.uuid4().hex,session_id,source['source_sha256'],now,now))
            self._audit(db,actor,'summary_retry_requested',session_id)
            return {'session_id':session_id,'state':'pending','source_sha256':source['source_sha256']}

    def list_summary_jobs(self, limit=100):
        with self._locked() as db:
            return [dict(row) for row in db.execute('SELECT * FROM summary_jobs ORDER BY created_at DESC LIMIT ?',
                (min(max(int(limit),1),1000),))]

    @staticmethod
    def _tasks_db(db, series_id):
        return [{**dict(row),'source_refs':json.loads(row['source_refs'])} for row in
            db.execute('SELECT * FROM memory_tasks WHERE series_id=? ORDER BY updated_at DESC',(series_id,))]

    def list_tasks(self, series_id):
        with self._locked() as db:
            self._row(db,'meeting_series','id',series_id)
            return self._tasks_db(db,series_id)

    def update_task(self, task_id, status, actor='admin'):
        if status not in {'open','completed','deferred'}:
            raise AssistantStoreError('invalid_task_status')
        with self._locked() as db, db:
            self._row(db,'memory_tasks','id',task_id)
            db.execute('UPDATE memory_tasks SET status=?,updated_at=? WHERE id=?',(status,utc_now(),task_id))
            self._audit(db,actor,'task_status_changed',task_id)

    def audit(self, limit=100):
        with self._locked() as db:
            return [dict(row) for row in db.execute('SELECT * FROM assistant_audit ORDER BY id DESC LIMIT ?',
                (min(max(int(limit),1),1000),))]
