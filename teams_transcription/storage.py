"""Private SQLite sessions; runtime directories are not context authority."""
import json
import os
import re
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from .meeting_store import MeetingStore, MeetingStoreError, canonical_json, source_id, atomic_text


def atomic_json(path, value):
    atomic_text(Path(path), json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def stamp():
    return datetime.now(timezone.utc).isoformat()


def process_signature(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()[19]
    except (OSError, IndexError):
        return None


class Session:
    def __init__(self, root, ident=None, *, lease=None):
        self.root = Path(root).resolve()
        self.ident = ident or uuid.uuid4().hex
        if not re.fullmatch(r'[0-9a-f]{32}', self.ident):
            raise ValueError('invalid_session_id')
        self.path = self.root / self.ident
        self.lease = lease
        self.store = MeetingStore(self.root, lease=lease)

    def create(self, config):
        if self.path.is_symlink() or not isinstance(config, dict):
            raise ValueError('invalid_session_config')
        self.path.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        prior = config.get('prior_context', {})
        self.store.create(self.ident, prior)
        status = {'id':self.ident,'state':'starting','created_at':stamp(),
            'provider':config['provider'],'source':config['source']}
        with self.store._locked(mutating=True, kind='capture', meeting_id=self.ident) as db:
            if db.execute('SELECT 1 FROM session_runtime WHERE meeting_id=?',(self.ident,)).fetchone():
                raise FileExistsError('session_already_exists')
            with db:
                stopped = db.execute('SELECT 1 FROM session_stop_requests WHERE meeting_id=?',(self.ident,)).fetchone()
                db.execute('INSERT INTO session_runtime VALUES(?,?,?,?)',
                    (self.ident,canonical_json(config),canonical_json(status),int(stopped is not None)))
        return self

    def _runtime(self):
        with self.store._locked() as db:
            row = db.execute('SELECT * FROM session_runtime WHERE meeting_id=?',(self.ident,)).fetchone()
            if row is None:
                raise MeetingStoreError('session_not_created')
            return dict(row)

    def config(self):
        return json.loads(self._runtime()['config_json'])

    def status(self):
        return json.loads(self._runtime()['status_json'])

    def observed_status(self):
        summary = self.store.summary_state(self.ident)
        value = self.status()
        for key in ('summary_model','summary_provider'):
            value.pop(key, None)
        value['summary_provider_requirement_met'] = bool(summary and summary['provider_requirement_met'])
        value['summary_required_provider'] = 'codex'
        if summary:
            value['summary_state'] = 'completed'
        if value['summary_provider_requirement_met']:
            value.update(summary_model=summary['model'], summary_provider='codex')
        if value.get('pid'):
            value['worker_alive'] = (process_signature(value['pid']) == value.get('process_signature')
                and value.get('process_signature') is not None)
            if not value['worker_alive'] and value['state'] in {
                    'preparing','joining','connecting','waiting_for_admission','joined','prepared',
                    'owner_confirmed','rx_starting','recording','draining'}:
                value['state'] = 'worker_lost'
        return value

    def update(self, **values):
        with self.store._locked(mutating=True, kind='capture', meeting_id=self.ident) as db:
            row = db.execute('SELECT status_json FROM session_runtime WHERE meeting_id=?',(self.ident,)).fetchone()
            if row is None:
                raise MeetingStoreError('session_not_created')
            current = json.loads(row['status_json'])
            current.update(values, updated_at=stamp())
            with db:
                db.execute('UPDATE session_runtime SET status_json=? WHERE meeting_id=?',
                    (canonical_json(current),self.ident))

    def event(self, kind, data=None):
        if not isinstance(kind,str) or not re.fullmatch(r'[A-Za-z0-9_./-]{1,96}',kind):
            raise ValueError('invalid_session_event')
        with self.store._locked(mutating=True, kind='capture', meeting_id=self.ident) as db:
            with db:
                db.execute('INSERT INTO session_events(meeting_id,kind,payload_json,created_at) VALUES(?,?,?,?)',
                    (self.ident,kind,canonical_json(data or {}),stamp()))

    def stop(self):
        with self.store._locked(mutating=True,kind='capture',meeting_id=self.ident) as db:
            with db:
                db.execute('INSERT OR IGNORE INTO session_stop_requests VALUES(?)',(self.ident,))
                db.execute('UPDATE session_runtime SET stop_requested=1 WHERE meeting_id=?',(self.ident,))

    def stopped(self):
        return bool(self._runtime()['stop_requested'])

    def append_text(self, text, elapsed_ms):
        entries = self.entries()
        ident = f'S{len(entries)+1:04d}'
        status = self.status()
        origin = datetime.fromisoformat(status.get('recording_started_at', status['created_at']))
        self.append_final({'event_id':ident,'revision':0,'text':text,
            'timestamp':(origin+timedelta(milliseconds=elapsed_ms)).isoformat(),'sequence':len(entries)+1})
        return next(entry for entry in self.entries() if entry['id']==ident)

    def append_final(self, event):
        return self.store.append_final(self.ident, event)

    def finalize_minutes(self, status, error_code=None):
        return self.store.finalize(self.ident, status, error_code)

    def summary_source(self):
        value = self.store.summary_source(self.ident)
        return {**value,'entries':self._store_entries(value)}

    def publish_summary(self, value, *, expected_version, expected_source_sha256, generation_mode):
        return self.store.publish_summary(self.ident,value,expected_version=expected_version,
            expected_source_sha256=expected_source_sha256,generation_mode=generation_mode)

    def save_provisional(self, value):
        return self.store.save_provisional(self.ident,value)

    def record_increment(self, value, *, publish=True):
        return self.store.record_increment(self.ident,value,publish=publish)

    def end_capture(self, value, *, publish=True):
        return self.store.end_capture(self.ident,value,publish=publish)

    def bind_final_alias(self, alias_id, event_id):
        return self.store.bind_final_alias(self.ident,alias_id,event_id)

    def entries(self, *, include_unfinalized=True):
        return self._store_entries(self.store.read(self.ident),include_unfinalized=include_unfinalized)

    def _store_entries(self, result, *, include_unfinalized=True):
        config, status = self.config(), self.status()
        created = datetime.fromisoformat(status.get('recording_started_at',status['created_at']))
        sources = list(result['events'])
        if include_unfinalized:
            sources.extend(result.get('segments',[]))
            sources.extend({**entry,'event_id':entry['provisional_id'],'timestamp':None,
                'source_state':'legacy_provisional','provider_final':False,
                'quality':'legacy_truncated' if entry['truncated'] else 'unfinalized',
                'ordering':'grouped_source_streams'}
                for entry in result.get('provisionals',[]))
        return [{'id':source_id(entry['event_id']),'event_id':entry['event_id'],
            'revision':entry.get('revision',entry.get('source_revision')),
            'received_offset_ms':max(0,round((datetime.fromisoformat(entry['timestamp'])-created).total_seconds()*1000))
                if entry.get('timestamp') else None,
            'text':entry['text'],'role':'user','provider':config['provider'],
            'source_state':entry.get('source_state','provider_final'),
            'provider_final':entry.get('provider_final',True),'quality':entry.get('quality','provider_final'),
            'timestamp':entry.get('timestamp'),'last_received_at':entry.get('last_received_at'),
            'source_timestamp':entry.get('source_timestamp'),'source_event_id':entry.get('source_event_id'),
            'source_revision':entry.get('source_revision'),'source_sequence':entry.get('source_sequence'),
            'ordering':entry.get('ordering','receipt_order'),'deduplication':'source_identity_only'}
            for entry in sources]


def list_sessions(root):
    store = MeetingStore(root)
    with store._locked() as db:
        ids = [row[0] for row in db.execute('SELECT meeting_id FROM session_runtime ORDER BY rowid DESC')]
    return [Session(root, ident) for ident in ids]
