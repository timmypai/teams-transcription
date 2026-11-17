"""Synthetic legacy outbox rows for historical publication/lease regression only.

Production no longer enqueues or processes these jobs. Tests explicitly supply
historical data to retain publication, provenance and fencing coverage.
"""
import uuid

from teams_transcription.storage import stamp


def legacy_job(store,session_id,*,state='pending'):
    source=store.summary_source(session_id)
    with store._locked() as db,db:
        row=db.execute('SELECT id FROM summary_jobs WHERE session_id=?',(session_id,)).fetchone()
        if row:
            db.execute('UPDATE summary_jobs SET state=?,error_code=NULL WHERE id=?',(state,row['id']))
        else:
            now=stamp()
            db.execute('INSERT INTO summary_jobs VALUES(?,?,?,?,NULL,0,?,?)',
                (uuid.uuid4().hex,session_id,source['source_sha256'],state,now,now))
