"""SQLite source authority, structured context and atomic derived summaries.

Provider text and provenance remain immutable after source sealing. Models do
not write SQL. Context and summary publication never require meeting files.
"""
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from functools import cache
from datetime import datetime, timezone
from pathlib import Path
from .provisional import MAX_PROVISIONAL_BYTES, MAX_PENDING_PROVISIONALS
from .incremental import IncrementalStoreMixin
import time


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def source_id(value):
    return value if re.fullmatch(r'S\d{4,}', value) else 'S' + hashlib.sha256(value.encode()).hexdigest()[:16]

SCHEMA_VERSION = 1
TERMINAL = {"completed", "failed", "stopped"}


class MeetingStoreError(ValueError):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


@cache
def _boot_id():
    try:
        return str(uuid.UUID(Path('/proc/sys/kernel/random/boot_id').read_text().strip()))
    except (OSError, ValueError):
        raise MeetingStoreError('lease_clock_unavailable') from None


def atomic_text(path, text):
    path = Path(path)
    fd, temporary = tempfile.mkstemp(prefix=".minutes-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)




class MeetingStore(IncrementalStoreMixin):
    def __init__(self, root, *, lease=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lease = dict(lease) if lease is not None else None
        with self._locked() as db:
            db.executescript("""
              CREATE TABLE IF NOT EXISTS meetings (
                meeting_id TEXT PRIMARY KEY, prior_minutes TEXT NOT NULL,
                status TEXT NOT NULL, version INTEGER NOT NULL, error_code TEXT,
                summary TEXT NOT NULL DEFAULT '');
              CREATE TABLE IF NOT EXISTS finals (
                meeting_id TEXT NOT NULL, event_id TEXT NOT NULL, revision INTEGER NOT NULL,
                text TEXT NOT NULL, timestamp TEXT NOT NULL, sequence INTEGER,
                changed_version INTEGER NOT NULL, PRIMARY KEY(meeting_id,event_id));
              CREATE TABLE IF NOT EXISTS revisions (
                meeting_id TEXT NOT NULL, event_id TEXT NOT NULL, revision INTEGER NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(meeting_id,event_id,revision));
              CREATE TABLE IF NOT EXISTS final_aliases (
                meeting_id TEXT NOT NULL, alias_id TEXT NOT NULL, event_id TEXT NOT NULL,
                PRIMARY KEY(meeting_id,alias_id));
              CREATE TABLE IF NOT EXISTS provisionals (
                meeting_id TEXT NOT NULL, provisional_id TEXT NOT NULL,
                revision INTEGER NOT NULL, text TEXT NOT NULL, truncated INTEGER NOT NULL,
                resolved INTEGER NOT NULL, PRIMARY KEY(meeting_id,provisional_id));
              CREATE TABLE IF NOT EXISTS summary_revisions (
                meeting_id TEXT NOT NULL, revision INTEGER NOT NULL,
                summary_json TEXT NOT NULL, markdown TEXT NOT NULL, metadata TEXT NOT NULL,
                PRIMARY KEY(meeting_id,revision));
              CREATE TABLE IF NOT EXISTS summary_publication_recovery (
                meeting_id TEXT PRIMARY KEY, target_revision INTEGER NOT NULL,
                old_json TEXT, old_markdown TEXT, old_status TEXT);
              CREATE TABLE IF NOT EXISTS question_activity (
                capture_order INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL, activity_id TEXT NOT NULL,
                thread_id TEXT NOT NULL, kind TEXT NOT NULL,
                request_id TEXT, turn_id TEXT, text TEXT NOT NULL,
                state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                UNIQUE(meeting_id,activity_id));
              CREATE INDEX IF NOT EXISTS question_activity_meeting
                ON question_activity(meeting_id,capture_order);
            """)
            db.executescript("""
              CREATE TABLE IF NOT EXISTS session_runtime (
                meeting_id TEXT PRIMARY KEY, config_json TEXT NOT NULL,
                status_json TEXT NOT NULL, stop_requested INTEGER NOT NULL DEFAULT 0);
              CREATE TABLE IF NOT EXISTS session_stop_requests (
                meeting_id TEXT PRIMARY KEY);
              CREATE TABLE IF NOT EXISTS session_events (
                event_seq INTEGER PRIMARY KEY AUTOINCREMENT, meeting_id TEXT NOT NULL,
                kind TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
              CREATE TABLE IF NOT EXISTS execution_leases (
                lease_id TEXT PRIMARY KEY, kind TEXT NOT NULL, session_id TEXT NOT NULL,
                owner TEXT NOT NULL, generation INTEGER NOT NULL, token TEXT NOT NULL,
                expires_at REAL NOT NULL, state TEXT NOT NULL,
                boot_id TEXT NOT NULL DEFAULT '', expires_boottime REAL NOT NULL DEFAULT 0);
              CREATE UNIQUE INDEX IF NOT EXISTS capture_single_slot ON execution_leases(kind)
                WHERE kind='capture' AND state='active';
              CREATE UNIQUE INDEX IF NOT EXISTS summary_single_slot ON execution_leases(kind)
                WHERE kind='summary' AND state='active';
              CREATE TABLE IF NOT EXISTS source_migrations (
                version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL);
            """)
            lease_columns = {row[1] for row in db.execute('PRAGMA table_info(execution_leases)')}
            if not {'boot_id', 'expires_boottime'} <= lease_columns:
                # Legacy wall deadlines cannot be translated safely after clock changes.
                # Empty boot identity fences them until normal recovery seals the run.
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    if 'boot_id' not in lease_columns:
                        db.execute("ALTER TABLE execution_leases ADD COLUMN boot_id TEXT NOT NULL DEFAULT ''")
                    if 'expires_boottime' not in lease_columns:
                        db.execute('ALTER TABLE execution_leases ADD COLUMN expires_boottime REAL NOT NULL DEFAULT 0')
            if not db.execute('SELECT 1 FROM source_migrations WHERE version=2').fetchone():
                self._migrate_summary_format(db)
                with db:
                    db.execute('INSERT INTO source_migrations VALUES(2,?)',
                        (datetime.now(timezone.utc).isoformat(),))
            self._init_increments(db)
            if "summary" not in {row[1] for row in db.execute("PRAGMA table_info(meetings)")}:
                with db:
                    db.execute("ALTER TABLE meetings ADD COLUMN summary TEXT NOT NULL DEFAULT ''")

    @contextlib.contextmanager
    def _locked(self, *, mutating=False, kind=None, meeting_id=None):
        fd = os.open(self.root / ".meeting-store.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self.root / ".meeting-store.sqlite3"
            db = sqlite3.connect(path, timeout=30)
            os.chmod(path, 0o600)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            try:
                if mutating and self.lease is not None:
                    self._check_lease(db, kind, meeting_id)
                yield db
            finally:
                db.close()
        finally:
            os.close(fd)

    @staticmethod
    def _lease_clock():
        try:
            # Includes actual suspend time, but is unaffected by wall-clock correction.
            return _boot_id(), time.clock_gettime(time.CLOCK_BOOTTIME)
        except (AttributeError, OSError):
            raise MeetingStoreError('lease_clock_unavailable') from None

    @classmethod
    def _lease_time_valid(cls, row):
        boot_id, now = cls._lease_clock()
        return row['boot_id'] == boot_id and row['expires_boottime'] > now

    def _check_lease(self, db, kind=None, meeting_id=None):
        lease = self.lease
        row = db.execute('SELECT * FROM execution_leases WHERE lease_id=?',
                         (lease.get('id'),)).fetchone()
        if (row is None or row['state'] != 'active' or not self._lease_time_valid(row)
                or row['boot_id'] != lease.get('boot_id')
                or row['owner'] != lease.get('owner') or row['generation'] != lease.get('generation')
                or row['token'] != lease.get('token') or row['session_id'] != lease.get('session_id')
                or meeting_id != row['session_id'] or kind is not None and row['kind'] != kind):
            raise MeetingStoreError('stale_execution_lease')

    def _validate_id(self, meeting_id):
        if not isinstance(meeting_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', meeting_id):
            raise MeetingStoreError('invalid_meeting_id')
        path = self.root / meeting_id
        if path.is_symlink() or path.resolve().parent != self.root:
            raise MeetingStoreError('invalid_meeting_path')

    @staticmethod
    def _prior_context(raw):
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            return {'legacy_text':raw, 'quality':'historical_unverified'}
        return value if isinstance(value, dict) else {'legacy_text':raw, 'quality':'historical_unverified'}

    def _migrate_summary_format(self, db):
        for row in db.execute('SELECT * FROM summary_revisions').fetchall():
            try:
                payload = json.loads(row['summary_json'])
                metadata = json.loads(row['metadata'])
                if metadata.get('format')=='json-v1':
                    continue
                value = {key:item for key,item in payload.items() if key != 'summary_state'}
                metadata['sha256'] = hashlib.sha256(canonical_json(value).encode()).hexdigest()
                metadata['format'] = 'json-v1'
                from .source_contract import SummaryState
                metadata = SummaryState.model_validate(metadata).model_dump()
            except (ValueError, TypeError, KeyError):
                continue
            with db:
                db.execute('UPDATE summary_revisions SET summary_json=?,metadata=? WHERE meeting_id=? AND revision=?',
                    (canonical_json({**value,'summary_state':metadata}),canonical_json(metadata),
                     row['meeting_id'],row['revision']))

    def _meeting(self, db, meeting_id):
        self._validate_id(meeting_id)
        row = db.execute("SELECT * FROM meetings WHERE meeting_id=?", (meeting_id,)).fetchone()
        if row is None:
            raise MeetingStoreError("meeting_not_found")
        return row

    def _final_events(self, db, meeting_id):
        for value in db.execute(
                "SELECT event_id,revision,text,timestamp,sequence,changed_version FROM finals "
                "WHERE meeting_id=? ORDER BY sequence IS NULL,sequence,rowid", (meeting_id,)):
            # v1 allocates `sequence` locally when the provider omitted it.
            # The original revision payload retains that omission; do not label
            # a local allocation as a provider/audio sequence.
            original = db.execute('SELECT payload FROM revisions WHERE meeting_id=? AND event_id=? ORDER BY revision LIMIT 1',
                                  (meeting_id, value['event_id'])).fetchone()
            source_sequence = json.loads(original['payload']).get('sequence') if original else None
            yield {**dict(value), "source_state": "provider_final", "provider_final": True,
                   "quality": "provider_final", "source_sequence": source_sequence,
                   "ordering": "provider_sequence_then_receipt" if source_sequence is not None else "receipt_order"}

    def _legacy_provisionals(self, db, meeting_id):
        for value in db.execute(
                'SELECT provisional_id,revision,text,truncated FROM provisionals '
                'WHERE meeting_id=? AND resolved=0 ORDER BY rowid', (meeting_id,)):
            yield dict(value)

    def _transcript_state(self, db, meeting_id, row):
        finals = db.execute('SELECT COUNT(*) FROM finals WHERE meeting_id=?', (meeting_id,)).fetchone()[0]
        legacy = db.execute('SELECT COUNT(*) FROM provisionals WHERE meeting_id=? AND resolved=0', (meeting_id,)).fetchone()[0]
        segments = db.execute(
            'SELECT COUNT(*) FROM transcript_increments i WHERE i.meeting_id=? '
            'AND (i.source_event_id IS NULL OR NOT EXISTS ('
            'SELECT 1 FROM transcript_increments newer WHERE newer.meeting_id=i.meeting_id '
            'AND newer.source_event_id=i.source_event_id AND newer.source_revision>i.source_revision))',
            (meeting_id,)).fetchone()[0]
        # Source groups are deliberately not presented as a global chronology.
        # Legacy preview rows lack receipt metadata even when they are alone.
        source_sequences = sum(e['source_sequence'] is not None for e in self._final_events(db, meeting_id))
        ordering = ('grouped_source_streams' if legacy or finals and segments or 0 < source_sequences < finals else
                    'provider_sequence_then_receipt' if finals and source_sequences == finals else 'receipt_order')
        return {"saved": bool(finals or segments or legacy), "provider_final_count": finals,
                "application_segment_count": segments, "unfinalized_segment_count": segments + legacy,
                "meeting_ended": row['status'] in TERMINAL, "ordering": ordering,
                "deduplication": "source_identity_only",
                "quality": "includes_unfinalized" if segments or legacy else "provider_final_only" if finals else "no_text"}

    def _publish(self, db, meeting_id, materialize=False):
        row = self._meeting(db, meeting_id)
        state = self._transcript_state(db, meeting_id, row)
        result = {'schema_version':SCHEMA_VERSION,'meeting_id':meeting_id,
            'version':row['version'],'status':row['status'],'terminal':row['status'] in TERMINAL,
            'error_code':row['error_code'],'ledger_version':2,'transcript_state':state,
            'finalized':row['status'] in TERMINAL,
            'completeness':'unavailable' if not state['saved'] else
                'partial' if state['unfinalized_segment_count'] or row['status'] in {'active','failed'} else 'complete'}
        head = db.execute('SELECT summary_json FROM summary_revisions WHERE meeting_id=? '
            'ORDER BY revision DESC LIMIT 1', (meeting_id,)).fetchone()
        result['summary'] = json.loads(head['summary_json']) if head else None
        summary_state = self._summary_state(db, meeting_id)
        if summary_state:
            result['summary_state'] = summary_state
        if materialize:
            segments, increments, captures = self._increment_snapshot(db, meeting_id)
            events = list(self._final_events(db, meeting_id))
            provisionals = list(self._legacy_provisionals(db, meeting_id))
            sources = [{**entry,'id':source_id(entry['event_id'])} for entry in [*events,*segments]]
            sources.extend({**entry,'id':source_id(entry['provisional_id']),
                'source_state':'legacy_provisional','provider_final':False,
                'quality':'legacy_truncated' if entry['truncated'] else 'unfinalized',
                'ordering':'grouped_source_streams'} for entry in provisionals)
            result.update(prior_context=self._prior_context(row['prior_minutes']),events=events,
                segments=segments,provisionals=provisionals,increments=increments,captures=captures,sources=sources)
            result['sha256'] = self._summary_source(db, meeting_id)['source_sha256']
        return result

    def _summary_source(self, db, meeting_id):
        row = self._meeting(db, meeting_id)
        segments = list(self._iter_segments(db, meeting_id))
        source = dict(meeting_id=meeting_id, prior_minutes=row['prior_minutes'],
                      events=list(self._final_events(db, meeting_id)), segments=segments,
                      provisionals=list(self._legacy_provisionals(db, meeting_id)))
        digest = hashlib.sha256(json.dumps(source, ensure_ascii=False,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        return {'meeting_id':meeting_id,'prior_context':self._prior_context(row['prior_minutes']),
                'events':source['events'],'segments':segments,'provisionals':source['provisionals'],
                'version':row['version'],'terminal':row['status'] in TERMINAL,
                'source_sha256':digest}

    def _summary_state(self, db, meeting_id):
        from .source_contract import SummaryState
        head = db.execute('SELECT metadata FROM summary_revisions WHERE meeting_id=? '
                          'ORDER BY revision DESC LIMIT 1', (meeting_id,)).fetchone()
        return SummaryState.model_validate(json.loads(head['metadata'])).model_dump() if head else None

    def summary_state(self, meeting_id):
        """Read authoritative derived metadata without materializing source text."""
        with self._locked() as db:
            self._meeting(db, meeting_id)
            return self._summary_state(db, meeting_id)

    def transcript_state(self, meeting_id):
        with self._locked() as db:
            return self._transcript_state(db,meeting_id,self._meeting(db,meeting_id))

    @staticmethod
    def _summary_status(status, metadata):
        status = dict(status)
        for key in ('summary_error', 'summary_model', 'summary_provider'):
            status.pop(key, None)
        status.update(summary_state='completed', summary_required_provider='codex',
            summary_provider_requirement_met=metadata['provider_requirement_met'],
            updated_at=metadata['published_at'])
        if metadata['provider_requirement_met']:
            status.update(summary_provider='codex', summary_model=metadata['model'])
        return status

    def summary_source(self, meeting_id):
        """Capture one scoped source snapshot before invoking the model."""
        with self._locked() as db:
            return self._summary_source(db, meeting_id)

    def list_question_activity(self, meeting_id):
        """Private B observations, never transcript or summary source material."""
        with self._locked() as db:
            self._meeting(db, meeting_id)
            return [dict(row) for row in db.execute(
                'SELECT activity_id AS id,kind,request_id,turn_id,text,state,created_at,updated_at '
                'FROM question_activity WHERE meeting_id=? ORDER BY capture_order', (meeting_id,))]

    def _save_question_activity(self, meeting_id, *, activity_id, thread_id, kind,
                                request_id, turn_id, text, state, append=False):
        if (not all(isinstance(value, str) and value for value in (activity_id, thread_id))
                or not isinstance(text, str) or not text
                or kind not in {'question', 'answer'}
                or state not in {'admitted', 'delta_unassociated', 'final_unassociated'}
                or kind == 'question' and (state != 'admitted'
                    or not isinstance(request_id, str) or not request_id or turn_id is not None)
                or kind == 'answer' and (state == 'admitted' or request_id is not None)
                or turn_id is not None and (not isinstance(turn_id, str) or not turn_id)
                or append and state != 'delta_unassociated'):
            raise MeetingStoreError('invalid_question_activity')
        with self._locked(mutating=True, kind='capture', meeting_id=meeting_id) as db:
            if self._meeting(db, meeting_id)['status'] in TERMINAL:
                raise MeetingStoreError('meeting_finalized')
            old = db.execute('SELECT * FROM question_activity WHERE meeting_id=? AND activity_id=?',
                (meeting_id, activity_id)).fetchone()
            now = datetime.now(timezone.utc).isoformat()
            if old is not None:
                if ((old['thread_id'], old['kind'], old['request_id']) != (thread_id, kind, request_id)
                        or old['turn_id'] is not None and turn_id is not None and old['turn_id'] != turn_id):
                    raise MeetingStoreError('question_activity_identity_conflict')
                if old['state'] == 'final_unassociated' and state == 'delta_unassociated':
                    return False  # An out-of-order delta cannot downgrade the final.
                if kind == 'question':
                    if old['text'] != text:
                        raise MeetingStoreError('question_activity_identity_conflict')
                    return True
                with db:
                    db.execute('UPDATE question_activity SET text=?,state=?,turn_id=?,updated_at=? '
                        'WHERE meeting_id=? AND activity_id=?',
                        (old['text'] + text if append else text, state, turn_id or old['turn_id'],
                         now, meeting_id, activity_id))
            else:
                with db:
                    db.execute('INSERT INTO question_activity '
                        '(meeting_id,activity_id,thread_id,kind,request_id,turn_id,text,state,created_at,updated_at) '
                        'VALUES(?,?,?,?,?,?,?,?,?,?)',
                        (meeting_id, activity_id, thread_id, kind, request_id, turn_id, text, state, now, now))
            return True


    def publish_summary(self, meeting_id, value, *, expected_version,
                        expected_source_sha256, generation_mode):
        """Atomically publish a validated canonical JSON revision, never files."""
        from .summary import SECTIONS, source_quality, validate_summary, validate_codex_summary_model
        from .source_contract import SummaryState
        if generation_mode not in {'codex','mock'}:
            raise MeetingStoreError('unsupported_summary_provider')
        if type(expected_version) is not int or expected_version < 0:
            raise MeetingStoreError('invalid_result_summary')
        with self._locked(mutating=True, kind='summary', meeting_id=meeting_id) as db:
            source = self._summary_source(db, meeting_id)
            if not source['terminal']:
                raise MeetingStoreError('summary_requires_terminal')
            if source['version'] != expected_version or source['source_sha256'] != expected_source_sha256:
                raise MeetingStoreError('snapshot_changed')
            entries = []
            for event in [*source['events'],*source['segments'],*source['provisionals']]:
                ident = event.get('event_id',event.get('provisional_id'))
                if 'provisional_id' in event:
                    event = {**event,'source_state':'legacy_provisional','provider_final':False,
                        'quality':'legacy_truncated' if event['truncated'] else 'unfinalized',
                        'ordering':'grouped_source_streams','timestamp':None}
                entries.append({**event,'id':source_id(ident)})
            if not entries:
                raise MeetingStoreError('summary_source_empty')
            fields = {*SECTIONS,'source_quality'} | ({'model'} if generation_mode=='codex' else set())
            if not isinstance(value,dict) or set(value) != fields:
                raise MeetingStoreError('invalid_result_summary')
            model = value.get('model')
            if generation_mode=='codex':
                validate_codex_summary_model(model)
                if model is None:
                    raise MeetingStoreError('unsupported_summary_model')
            validate_summary({key:value[key] for key in SECTIONS}, entries)
            if canonical_json(value['source_quality']) != canonical_json(source_quality(entries)):
                raise MeetingStoreError('invalid_result_summary')
            raw = canonical_json(value)
            if len(raw.encode()) > 2097152:
                raise MeetingStoreError('invalid_result_summary')
            revision = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM summary_revisions '
                'WHERE meeting_id=?',(meeting_id,)).fetchone()[0]
            metadata = SummaryState.model_validate(dict(schema_version=1,artifact_kind='derived_summary',
                revision=revision,published_at=datetime.now(timezone.utc).isoformat(),
                source_version=expected_version,source_sha256=source['source_sha256'],
                sha256=hashlib.sha256(raw.encode()).hexdigest(),source_count=len(entries),
                generation_mode=generation_mode,model=model,
                provider_requirement_met=generation_mode=='codex')).model_dump()
            with db:
                db.execute('INSERT INTO summary_revisions VALUES(?,?,?,?,?)',
                    (meeting_id,revision,canonical_json({**value,'summary_state':metadata}),'',canonical_json(metadata)))
                db.execute('UPDATE meetings SET summary=?,version=version+1 WHERE meeting_id=?',(raw,meeting_id))
                runtime = db.execute('SELECT status_json FROM session_runtime WHERE meeting_id=?',(meeting_id,)).fetchone()
                if runtime:
                    status = self._summary_status(json.loads(runtime['status_json']), metadata)
                    db.execute('UPDATE session_runtime SET status_json=? WHERE meeting_id=?',(canonical_json(status),meeting_id))
                if generation_mode == 'codex' and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_runs'").fetchone():
                    if db.execute('SELECT 1 FROM assistant_runs WHERE session_id=?',(meeting_id,)).fetchone():
                        from .assistant_store import AssistantStore
                        AssistantStore._roll_tasks(db,meeting_id,value)
                        db.execute("UPDATE summary_jobs SET state='published',error_code=NULL,updated_at=? "
                            'WHERE session_id=? AND source_sha256=?',
                            (metadata['published_at'],meeting_id,source['source_sha256']))
            return self._publish(db, meeting_id)

    def save_provisional(self, meeting_id, value):
        required = {'provisional_id','revision','text','truncated'}
        if (not isinstance(value,dict) or not required <= set(value) or set(value)-required-{'resolved','final_event_id'}
                or not isinstance(value['provisional_id'],str)
                or not re.fullmatch(r'provisional:[0-9a-f]{32}',value['provisional_id'])
                or type(value['revision']) is not int or value['revision']<1
                or not isinstance(value['text'],str) or len(value['text'].encode())>MAX_PROVISIONAL_BYTES
                or type(value['truncated']) is not bool or type(value.get('resolved',False)) is not bool
                or value.get('resolved',False) and value['text']):
            raise MeetingStoreError('invalid_provisional')
        resolved = value.get('resolved',False)
        if (resolved and (not isinstance(value.get('final_event_id'),str)
                          or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}',value['final_event_id']))
                or not resolved and 'final_event_id' in value):
            raise MeetingStoreError('invalid_provisional_final')
        with self._locked(mutating=True, kind='capture', meeting_id=meeting_id) as db:
            meeting = self._meeting(db,meeting_id)
            old = db.execute('SELECT * FROM provisionals WHERE meeting_id=? AND provisional_id=?',
                             (meeting_id,value['provisional_id'])).fetchone()
            if old and value['revision']<=old['revision']:
                if value['revision']==old['revision'] and (value['text'],int(value['truncated']),int(resolved)) != (old['text'],old['truncated'],old['resolved']):
                    raise MeetingStoreError('provisional_revision_conflict')
                return self._publish(db,meeting_id)
            if meeting['status'] in TERMINAL:
                raise MeetingStoreError('meeting_finalized')
            if resolved and not db.execute('SELECT 1 FROM finals WHERE meeting_id=? AND event_id=?',
                                           (meeting_id,value['final_event_id'])).fetchone():
                raise MeetingStoreError('provisional_final_missing')
            if old and old['resolved']:
                raise MeetingStoreError('provisional_already_resolved')
            if not old and (resolved or db.execute('SELECT COUNT(*) FROM provisionals WHERE meeting_id=? AND resolved=0',
                                                 (meeting_id,)).fetchone()[0]>=MAX_PENDING_PROVISIONALS):
                raise MeetingStoreError('provisional_capacity_or_identity')
            with db:
                db.execute('INSERT OR REPLACE INTO provisionals VALUES(?,?,?,?,?,?)',
                           (meeting_id,value['provisional_id'],value['revision'],value['text'],int(value['truncated']),int(resolved)))
                db.execute('UPDATE meetings SET version=version+1 WHERE meeting_id=?',(meeting_id,))
            return self._publish(db,meeting_id)

    def create(self, meeting_id, prior_context=None):
        self._validate_id(meeting_id)
        prior_context = {} if prior_context is None else prior_context
        if not isinstance(prior_context,dict):
            raise MeetingStoreError('invalid_prior_context')
        raw = canonical_json(prior_context)
        with self._locked(mutating=True, kind='capture', meeting_id=meeting_id) as db:
            old = db.execute('SELECT * FROM meetings WHERE meeting_id=?',(meeting_id,)).fetchone()
            if old and self._prior_context(old['prior_minutes']) != prior_context:
                raise MeetingStoreError('meeting_exists_conflict')
            with db:
                db.execute("INSERT OR IGNORE INTO meetings(meeting_id,prior_minutes,status,version,error_code) VALUES(?,?,'active',1,NULL)",
                    (meeting_id,raw))
            return self._publish(db,meeting_id)

    def _event(self, event):
        required = {"event_id", "revision", "text", "timestamp"}
        if not isinstance(event, dict) or not required <= set(event) or set(event) - required - {"sequence", "source_role"}:
            raise MeetingStoreError("invalid_final_event")
        if (not isinstance(event["event_id"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", event["event_id"])
                or type(event["revision"]) is not int or event["revision"] < 0
                or not isinstance(event["text"], str) or not event["text"].strip()
                or len(event["text"].encode()) > 65536 or event.get("source_role", "user") != "user"
                or (event.get("sequence") is not None and (type(event["sequence"]) is not int or event["sequence"] < 0))):
            raise MeetingStoreError("invalid_final_event")
        try:
            timestamp = datetime.fromisoformat(event["timestamp"].replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise MeetingStoreError("invalid_final_timestamp") from None
        return {"event_id": event["event_id"], "revision": event["revision"],
                "text": event["text"].strip(), "timestamp": timestamp.astimezone(timezone.utc).isoformat(),
                "sequence": event.get("sequence")}

    def append_final(self, meeting_id, event):
        event = self._event(event)
        payload = json.dumps(event, sort_keys=True, ensure_ascii=False)
        with self._locked(mutating=True, kind='capture', meeting_id=meeting_id) as db:
            row = self._meeting(db, meeting_id)
            old_revision = db.execute("SELECT payload FROM revisions WHERE meeting_id=? AND event_id=? AND revision=?",
                (meeting_id, event["event_id"], event["revision"])).fetchone()
            if old_revision:
                # Identity + revision bind the speech text. Receipt timestamps
                # and locally assigned sequence can change across reconnects;
                # their first durable values remain the ordering authority.
                if json.loads(old_revision["payload"])["text"] != event["text"]:
                    raise MeetingStoreError("revision_conflict")
                return self._publish(db, meeting_id)
            if row["status"] in TERMINAL:
                raise MeetingStoreError("meeting_finalized")
            old = db.execute("SELECT * FROM finals WHERE meeting_id=? AND event_id=?",
                             (meeting_id, event["event_id"])).fetchone()
            with db:
                db.execute("INSERT INTO revisions VALUES(?,?,?,?)", (meeting_id, event["event_id"], event["revision"], payload))
                if old is None or event["revision"] > old["revision"]:
                    version = row["version"] + 1
                    timestamp = old["timestamp"] if old is not None else event["timestamp"]
                    sequence = old["sequence"] if old is not None else event["sequence"]
                    if old is None and sequence is None:
                        sequence = db.execute("SELECT COALESCE(MAX(sequence),0) FROM finals WHERE meeting_id=?", (meeting_id,)).fetchone()[0]
                        # Keep older v1 rows without sequence in their existing
                        # timestamp order before allocating new receipt order.
                        for unsequenced in db.execute("SELECT event_id FROM finals WHERE meeting_id=? AND sequence IS NULL ORDER BY timestamp,event_id", (meeting_id,)).fetchall():
                            sequence += 1
                            db.execute("UPDATE finals SET sequence=? WHERE meeting_id=? AND event_id=?", (sequence, meeting_id, unsequenced["event_id"]))
                        sequence += 1
                    db.execute("INSERT OR REPLACE INTO finals VALUES(?,?,?,?,?,?,?)",
                        (meeting_id, event["event_id"], event["revision"], event["text"], timestamp, sequence, version))
                    db.execute("UPDATE meetings SET version=? WHERE meeting_id=?", (version, meeting_id))
            return self._publish(db, meeting_id)

    def get_final(self, meeting_id, event_id):
        """Meeting-bound durable state for local provider normalization only."""
        with self._locked() as db:
            self._meeting(db, meeting_id)
            alias = db.execute('SELECT event_id FROM final_aliases WHERE meeting_id=? AND alias_id=?', (meeting_id, event_id)).fetchone()
            target = alias['event_id'] if alias else event_id
            value = db.execute("SELECT event_id,revision,text,timestamp,sequence,changed_version FROM finals WHERE meeting_id=? AND event_id=?", (meeting_id, target)).fetchone()
            if value is None:
                return None
            return {**dict(value), **({'alias_event_id':event_id} if alias else {})}

    def active_meeting_ids(self):
        """Internal restart reconciliation without rewriting historical artifacts."""
        with self._locked() as db:
            return {row[0] for row in db.execute("SELECT meeting_id FROM meetings WHERE status='active'")}

    def bind_final_alias(self, meeting_id, alias_id, event_id):
        """Persist a confirmed same-source flat/canonical handover, never text dedup."""
        if any(not isinstance(v, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,200}', v) for v in (alias_id, event_id)):
            raise MeetingStoreError('invalid_final_alias')
        with self._locked(mutating=True, kind='capture', meeting_id=meeting_id) as db:
            meeting = self._meeting(db, meeting_id)
            existing = db.execute('SELECT event_id FROM final_aliases WHERE meeting_id=? AND alias_id=?', (meeting_id, alias_id)).fetchone()
            if existing:
                if existing['event_id'] != event_id:
                    raise MeetingStoreError('final_alias_conflict')
                return
            if meeting['status'] in TERMINAL:
                raise MeetingStoreError('meeting_finalized')
            if not db.execute('SELECT 1 FROM finals WHERE meeting_id=? AND event_id=?', (meeting_id, event_id)).fetchone():
                raise MeetingStoreError('final_alias_target_missing')
            if alias_id != event_id and db.execute('SELECT 1 FROM finals WHERE meeting_id=? AND event_id=?', (meeting_id, alias_id)).fetchone():
                raise MeetingStoreError('final_alias_conflict')
            with db:
                db.execute('INSERT INTO final_aliases VALUES(?,?,?)', (meeting_id, alias_id, event_id))

    def read(self, meeting_id):
        with self._locked() as db:
            return self._publish(db,meeting_id,materialize=True)


    def finalize(self, meeting_id, status='completed', error_code=None):
        if status not in TERMINAL or (error_code is not None and (
                not isinstance(error_code,str) or not re.fullmatch(r'[a-z0-9_]{1,80}',error_code))):
            raise MeetingStoreError('invalid_result_status')
        with self._locked(mutating=True, kind='capture', meeting_id=meeting_id) as db:
            row = self._meeting(db,meeting_id)
            if row['status'] in TERMINAL and (row['status'],row['error_code']) != (status,error_code):
                raise MeetingStoreError('result_conflict')
            with db:
                if row['status'] not in TERMINAL:
                    db.execute('UPDATE meetings SET status=?,error_code=?,version=version+1 WHERE meeting_id=?',
                        (status,error_code,meeting_id))
                db.execute("UPDATE transcript_captures SET state='sealed',reason=COALESCE(reason,'application_stop'),"
                    'ended_at=COALESCE(ended_at,?) WHERE meeting_id=?',
                    (datetime.now(timezone.utc).isoformat(),meeting_id))
                if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assistant_runs'").fetchone():
                    if db.execute('SELECT 1 FROM assistant_runs WHERE session_id=?',(meeting_id,)).fetchone():
                        from .assistant_store import AssistantStore
                        AssistantStore._enqueue_summary(db,self,meeting_id)
            return self._publish(db,meeting_id)

