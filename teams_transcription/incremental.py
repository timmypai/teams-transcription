"""Durable received text, independent of a provider's optional turn-final event.

Every chunk is an application boundary, never an inferred model boundary. Flat
V3 RPC has no source identity/audio clock: keep repeats and receipt order intact.
"""
import hashlib
import json
import re
from datetime import datetime, timezone

CHUNK_BYTES = 8192
CAPTURE_REASONS = {'provider_done', 'provider_closed', 'provider_error', 'application_stop'}


def text_chunks(text):
    # At most 8192 encoded bytes per yield, including astral Unicode. Preserve
    # whitespace and every character; never freeze or silently drop a suffix.
    start = 0
    while start < len(text):
        piece = text[start:start + CHUNK_BYTES]
        encoded = piece.encode('utf-8')
        if len(encoded) > CHUNK_BYTES:
            piece = encoded[:CHUNK_BYTES].decode('utf-8', errors='ignore')
        yield piece
        start += len(piece)


def utc(value):
    if not isinstance(value, str):
        raise ValueError('invalid_increment_timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('invalid_increment_timestamp')
    return parsed.astimezone(timezone.utc).isoformat()


class IncrementalStoreMixin:
    def _init_increments(self, db):
        db.executescript("""
          CREATE TABLE IF NOT EXISTS transcript_captures (
            meeting_id TEXT NOT NULL, capture_id TEXT NOT NULL,
            state TEXT NOT NULL, reason TEXT, ended_at TEXT,
            PRIMARY KEY(meeting_id,capture_id));
          CREATE TABLE IF NOT EXISTS transcript_increments (
            meeting_id TEXT NOT NULL, capture_id TEXT NOT NULL,
            receipt_sequence INTEGER NOT NULL, part_index INTEGER NOT NULL,
            text TEXT NOT NULL, received_at TEXT NOT NULL,
            source_event_id TEXT, source_revision INTEGER, source_sequence INTEGER,
            source_timestamp TEXT, late INTEGER NOT NULL, changed_version INTEGER NOT NULL,
            PRIMARY KEY(meeting_id,capture_id,receipt_sequence,part_index));
          CREATE INDEX IF NOT EXISTS transcript_increment_identity
            ON transcript_increments(meeting_id,source_event_id,source_revision,part_index);
        """)

    def _increment_value(self, value):
        from .meeting_store import MeetingStoreError
        required = {'capture_id', 'receipt_sequence', 'part_index', 'text', 'received_at'}
        optional = {'source_event_id', 'source_revision', 'source_sequence', 'source_timestamp'}
        if (not isinstance(value, dict) or not required <= set(value) or set(value)-required-optional
                or not isinstance(value['capture_id'], str)
                or not re.fullmatch('[0-9a-f]{32}', value['capture_id'])
                or type(value['receipt_sequence']) is not int or value['receipt_sequence'] < 1
                or type(value['part_index']) is not int or value['part_index'] < 0
                or not isinstance(value['text'], str) or not value['text']
                or len(value['text'].encode('utf-8')) > CHUNK_BYTES):
            raise MeetingStoreError('invalid_increment')
        source_id, revision = value.get('source_event_id'), value.get('source_revision')
        if ((source_id is None) != (revision is None)
                or source_id is not None and (not isinstance(source_id, str)
                    or not re.fullmatch('[A-Za-z0-9_.:-]{1,200}', source_id)
                    or type(revision) is not int or revision < 0)
                or value.get('source_sequence') is not None and
                    (type(value['source_sequence']) is not int or value['source_sequence'] < 0)):
            raise MeetingStoreError('invalid_increment_identity')
        try:
            at = utc(value['received_at'])
            source_at = utc(value['source_timestamp']) if value.get('source_timestamp') is not None else None
        except (ValueError, TypeError):
            raise MeetingStoreError('invalid_increment_timestamp') from None
        return {**value, 'received_at': at, 'source_event_id': source_id, 'source_revision': revision,
                'source_sequence': value.get('source_sequence'), 'source_timestamp': source_at}

    def record_increment(self, meeting_id, value, *, publish=True):
        """Commit one received message in full, even when it has many chunks.

        `parts` is a local batch envelope, never a provider RPC field. All parts
        share receipt identity/time and one SQLite transaction. Cancellation of
        the async owner cannot leave an unsubmitted suffix between callbacks.
        """
        from .meeting_store import MeetingStoreError, TERMINAL
        if isinstance(value, dict) and 'parts' in value:
            parts = value['parts']
            if (not isinstance(parts, list) or not parts or value.get('part_index') != 0
                    or value.get('text') != parts[0]
                    or any(not isinstance(t, str) or not t for t in parts)
                    or sum(len(t.encode('utf-8')) for t in parts) > 4 * 1024 * 1024):
                raise MeetingStoreError('invalid_increment_batch')
            base = {k: v for k, v in value.items() if k != 'parts'}
            values = [self._increment_value({**base, 'text': t, 'part_index': n})
                      for n, t in enumerate(parts)]
        else:
            values = [self._increment_value(value)]
        with self._locked(mutating=True, kind='capture', meeting_id=meeting_id) as db:
            meeting = self._meeting(db, meeting_id)
            inserts = []
            for value in values:
                key = (meeting_id, value['capture_id'], value['receipt_sequence'], value['part_index'])
                old = db.execute('SELECT * FROM transcript_increments WHERE meeting_id=? AND capture_id=? AND receipt_sequence=? AND part_index=?', key).fetchone()
                if old:
                    identity = ('text', 'source_event_id', 'source_revision', 'source_sequence', 'source_timestamp')
                    if any(old[k] != value[k] for k in identity):
                        raise MeetingStoreError('increment_receipt_conflict')
                    continue
                if value['source_event_id'] is not None:
                    known = db.execute('SELECT * FROM transcript_increments WHERE meeting_id=? AND source_event_id=? AND source_revision=? AND part_index=?',
                        (meeting_id, value['source_event_id'], value['source_revision'], value['part_index'])).fetchone()
                    if known:
                        if known['text'] != value['text']:
                            raise MeetingStoreError('increment_source_revision_conflict')
                        continue
                if meeting['status'] in TERMINAL:
                    raise MeetingStoreError('meeting_finalized')
                capture = db.execute('SELECT * FROM transcript_captures WHERE meeting_id=? AND capture_id=?', key[:2]).fetchone()
                inserts.append((*key, value['text'], value['received_at'], value['source_event_id'], value['source_revision'],
                    value['source_sequence'], value['source_timestamp'], int(bool(capture and capture['state'] == 'sealed')),
                    meeting['version'] + 1))
            with db:
                for entry in inserts:
                    db.execute("INSERT OR IGNORE INTO transcript_captures VALUES(?,?,'active',NULL,NULL)", entry[:2])
                    db.execute('INSERT INTO transcript_increments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)', entry)
                if inserts:
                    db.execute('UPDATE meetings SET version=version+1 WHERE meeting_id=?', (meeting_id,))
            # Source commits do not depend on any external file projection.
            return self._publish(db, meeting_id) if publish else {'saved': True, 'parts': len(values)}

    def end_capture(self, meeting_id, value, *, publish=True):
        from .meeting_store import MeetingStoreError, TERMINAL
        if (not isinstance(value, dict) or set(value) != {'capture_id', 'reason'}
                or not isinstance(value['capture_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['capture_id'])
                or value['reason'] not in CAPTURE_REASONS):
            raise MeetingStoreError('invalid_capture_end')
        with self._locked(mutating=True, kind='capture', meeting_id=meeting_id) as db:
            meeting = self._meeting(db, meeting_id)
            old = db.execute('SELECT * FROM transcript_captures WHERE meeting_id=? AND capture_id=?',
                             (meeting_id, value['capture_id'])).fetchone()
            if old and old['state'] == 'sealed':
                return self._publish(db, meeting_id) if publish else {'sealed': True}
            if meeting['status'] in TERMINAL:
                raise MeetingStoreError('meeting_finalized')
            with db:
                db.execute("INSERT OR REPLACE INTO transcript_captures VALUES(?,?,'sealed',?,?)",
                    (meeting_id, value['capture_id'], value['reason'], datetime.now(timezone.utc).isoformat()))
                db.execute('UPDATE meetings SET version=version+1 WHERE meeting_id=?', (meeting_id,))
            return self._publish(db, meeting_id) if publish else {'sealed': True}

    @staticmethod
    def _segment(row):
        row = dict(row)
        identity = f"{row['capture_id']}:{row['receipt_sequence']}:{row['part_index']}"
        return {**row, 'event_id': 'increment:' + hashlib.sha256(identity.encode()).hexdigest()[:32],
            'timestamp': row['received_at'], 'last_received_at': row['received_at'],
            'sequence': row['receipt_order'], 'late': bool(row['late']),
            'source_state': 'application_segment', 'provider_final': False, 'quality': 'unfinalized',
            'boundary': 'application_chunk', 'ordering': 'receipt_order', 'deduplication': 'source_identity_only'}

    def _iter_segments(self, db, meeting_id):
        # SQL filters current verified revisions without loading the audit
        # history or meeting text into RAM. Each yielded text is <=8192 bytes.
        rows = db.execute(
            'SELECT i.*,i.rowid AS receipt_order,c.state AS capture_state,c.reason AS capture_end_reason '
            'FROM transcript_increments i JOIN transcript_captures c USING(meeting_id,capture_id) '
            'WHERE i.meeting_id=? AND (i.source_event_id IS NULL OR NOT EXISTS '
            '(SELECT 1 FROM transcript_increments newer WHERE newer.meeting_id=i.meeting_id '
            'AND newer.source_event_id=i.source_event_id AND newer.source_revision>i.source_revision)) '
            'ORDER BY i.rowid', (meeting_id,))
        for row in rows:
            yield self._segment(row)

    def _increment_snapshot(self, db, meeting_id):
        raw = [dict(row) for row in db.execute(
            'SELECT i.*,i.rowid AS receipt_order,c.state AS capture_state,c.reason AS capture_end_reason '
            'FROM transcript_increments i JOIN transcript_captures c USING(meeting_id,capture_id) '
            'WHERE i.meeting_id=? ORDER BY i.rowid', (meeting_id,))]
        segments = list(self._iter_segments(db, meeting_id))
        captures = [dict(row) for row in db.execute('SELECT * FROM transcript_captures WHERE meeting_id=? ORDER BY rowid', (meeting_id,))]
        return segments, raw, captures
