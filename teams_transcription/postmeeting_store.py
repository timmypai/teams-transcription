"""Durable external handoff and revisioned series memory; no executor or timer.

All writes are host validated SQLite transactions. Original meeting sources remain
sealed. Jobs use separate leases so recovery cannot revive the retired summarizer.
"""
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone

from .meeting_store import canonical_json, source_id, TERMINAL
from .summary import SECTIONS, source_quality, validate_summary
from .task_workflow import TaskWorkflowMixin, task_metadata, source_evidence, revalidate_acceptance, instant

PIPELINE_VERSION = 'series-state-v2'
CLOSED = {'completed', 'cancelled', 'superseded'}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _digest(value):
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def _error(code):
    from .assistant_store import AssistantStoreError
    return AssistantStoreError(code)


class PostmeetingStoreMixin(TaskWorkflowMixin):
    @staticmethod
    def _init_postmeeting(db):
        db.executescript('''
          CREATE TABLE IF NOT EXISTS postmeeting_snapshots(
            id TEXT PRIMARY KEY,session_id TEXT NOT NULL,occurrence_id TEXT NOT NULL,
            series_id TEXT NOT NULL,source_sha256 TEXT NOT NULL,source_version INTEGER NOT NULL,
            snapshot_revision INTEGER NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL,
            UNIQUE(session_id,source_sha256),UNIQUE(session_id,snapshot_revision));
          CREATE TABLE IF NOT EXISTS postmeeting_jobs(
            id TEXT PRIMARY KEY,snapshot_id TEXT NOT NULL,session_id TEXT NOT NULL,
            series_id TEXT NOT NULL,pipeline_version TEXT NOT NULL,state TEXT NOT NULL,
            error_code TEXT,attempts INTEGER NOT NULL DEFAULT 0,generation INTEGER NOT NULL DEFAULT 0,
            owner TEXT,token TEXT,boot_id TEXT,expires_boottime REAL,prior_revision INTEGER,
            prior_json TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
            UNIQUE(session_id,snapshot_id,pipeline_version));
          CREATE TABLE IF NOT EXISTS postmeeting_attempts(
            job_id TEXT NOT NULL,generation INTEGER NOT NULL,owner TEXT NOT NULL,
            token_sha256 TEXT NOT NULL,cleanup_state TEXT NOT NULL,cleanup_error_code TEXT,
            updated_at TEXT NOT NULL,PRIMARY KEY(job_id,generation));
          CREATE TABLE IF NOT EXISTS postmeeting_publications(
            job_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,series_id TEXT NOT NULL,
            series_revision INTEGER,summary TEXT NOT NULL,result TEXT NOT NULL,
            receipt TEXT NOT NULL,published_at TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS series_state_revisions(
            series_id TEXT NOT NULL,revision INTEGER NOT NULL,job_id TEXT NOT NULL UNIQUE,
            state_json TEXT NOT NULL,PRIMARY KEY(series_id,revision));
          CREATE TABLE IF NOT EXISTS postmeeting_notifications(
            id TEXT PRIMARY KEY,job_id TEXT NOT NULL UNIQUE,series_id TEXT NOT NULL,
            session_id TEXT NOT NULL,created_at TEXT NOT NULL,read_at TEXT);
        ''')
        TaskWorkflowMixin._init_task_workflow(db)
        # Preserve the identity of any current pre-ledger attempt on upgrade.
        # A report can record risk after expiry, never regain result authority.
        with db:
            for job in db.execute("SELECT * FROM postmeeting_jobs WHERE state='running' AND token IS NOT NULL").fetchall():
                db.execute('INSERT OR IGNORE INTO postmeeting_attempts VALUES(?,?,?,?,?,?,?)',
                    (job['id'],job['generation'],job['owner'],_digest(job['token']),'pending',None,_now()))

    @staticmethod
    def _postmeeting_interlocked_db(db):
        return bool(db.execute("SELECT 1 WHERE EXISTS (SELECT 1 FROM postmeeting_jobs WHERE state='blocked') "
            "OR EXISTS (SELECT 1 FROM postmeeting_attempts WHERE cleanup_state='unknown')").fetchone())

    def _create_postmeeting_snapshot_db(self, db, session_id):
        # Source-only path: do not invoke the retired summary materializer.
        meeting = self._meeting(db,session_id)
        if meeting['status'] not in TERMINAL:
            return None
        events = list(self._final_events(db,session_id))
        segments = list(self._iter_segments(db,session_id))
        provisionals = list(self._legacy_provisionals(db,session_id))
        entries = [{**entry,'id':source_id(entry['event_id'])} for entry in [*events,*segments]]
        entries.extend({**entry,'id':source_id(entry['provisional_id']),
            'source_state':'legacy_provisional','provider_final':False,
            'quality':'legacy_truncated' if entry['truncated'] else 'unfinalized',
            'ordering':'grouped_source_streams'} for entry in provisionals)
        if not entries:
            return None
        source = dict(sha256=_digest(dict(session_id=session_id,events=events,segments=segments,
            provisionals=provisionals)),version=meeting['version'],sources=entries,
            transcript_state=self._transcript_state(db,session_id,meeting),
            status=meeting['status'],error_code=meeting['error_code'])
        old = db.execute('SELECT id FROM postmeeting_snapshots WHERE session_id=? AND source_sha256=?',
            (session_id,source['sha256'])).fetchone()
        if old:
            return old['id']
        run = self._row(db,'assistant_runs','session_id',session_id)
        occurrence = self._row(db,'calendar_occurrences','id',run['occurrence_id'])
        revision = db.execute('SELECT COALESCE(MAX(snapshot_revision),0)+1 FROM postmeeting_snapshots '
            'WHERE session_id=?',(session_id,)).fetchone()[0]
        snapshot_id = _digest([session_id,source['sha256']])
        payload = dict(source_sha256=source['sha256'],source_version=source['version'],
            snapshot_revision=revision,entries=source['sources'],quality=source_quality(source['sources']),
            transcript_state=source['transcript_state'],capture_status=source['status'],
            capture_error_code=source['error_code'],occurred_at=occurrence['start_at'] or run['started_at'],
            ended_at=run['ended_at'],kind='terminal_snapshot',base_snapshot_id=None)
        db.execute('INSERT INTO postmeeting_snapshots VALUES(?,?,?,?,?,?,?,?,?)',
            (snapshot_id,session_id,occurrence['id'],occurrence['series_id'],source['sha256'],
             source['version'],revision,canonical_json(payload),_now()))
        return self._enqueue_postmeeting_db(db,snapshot_id)

    @staticmethod
    def _enqueue_postmeeting_db(db, snapshot_id, pipeline_version=PIPELINE_VERSION):
        snapshot = db.execute('SELECT * FROM postmeeting_snapshots WHERE id=?',(snapshot_id,)).fetchone()
        key = _digest([snapshot['session_id'],snapshot['source_sha256'],pipeline_version])
        now = _now()
        db.execute('INSERT OR IGNORE INTO postmeeting_jobs '
            '(id,snapshot_id,session_id,series_id,pipeline_version,state,created_at,updated_at) '
            "VALUES(?,?,?,?,?,'ready',?,?)",(key,snapshot_id,snapshot['session_id'],snapshot['series_id'],pipeline_version,now,now))
        return snapshot_id

    @staticmethod
    def _postmeeting_jobs_db(db, session_id):
        return [dict(row) for row in db.execute('SELECT j.id,j.state,j.error_code,s.source_sha256,'
            's.source_version,s.snapshot_revision,j.updated_at FROM postmeeting_jobs j '
            'JOIN postmeeting_snapshots s ON s.id=j.snapshot_id WHERE j.session_id=? '
            'ORDER BY s.snapshot_revision DESC,j.created_at DESC',(session_id,))]

    def list_postmeeting_jobs(self, session_id):
        with self._locked() as db:
            self._meeting(db,session_id)
            return self._postmeeting_jobs_db(db,session_id)

    @staticmethod
    def _series_state_db(db, series_id, revision=None):
        row = db.execute('SELECT state_json FROM series_state_revisions WHERE series_id=? '
            + ('AND revision=?' if revision is not None else 'ORDER BY revision DESC LIMIT 1'),
            (series_id,revision) if revision is not None else (series_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def series_state(self, series_id, revision=None):
        """One complete pinned read. Caller retains revision for the whole question."""
        with self._locked() as db:
            self._row(db,'meeting_series','id',series_id)
            return self._series_state_db(db,series_id,revision)

    @staticmethod
    def _postmeeting_summary_db(db, session_id):
        row = db.execute('SELECT p.summary FROM postmeeting_publications p '
            'JOIN postmeeting_jobs j ON j.id=p.job_id JOIN postmeeting_snapshots s ON s.id=j.snapshot_id '
            'WHERE p.session_id=? ORDER BY s.snapshot_revision DESC,p.rowid DESC LIMIT 1',(session_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def claim_postmeeting(self, owner, lease_seconds=90):
        from .assistant_store import text
        owner = text(owner,128)
        if type(lease_seconds) is not int or not 1<=lease_seconds<=300:
            raise _error('invalid_postmeeting_lease')
        with self._locked() as db, db:
            if (self.root / '.deployment-maintenance').exists():
                return None
            # Unknown provider cleanup belongs to this execution root, not just
            # one source snapshot. Corrections and other series cannot bypass it.
            if self._postmeeting_interlocked_db(db):
                return None
            boot, now = self._lease_clock()
            db.execute("UPDATE postmeeting_jobs SET state='ready',error_code='lease_expired',token=NULL "
                "WHERE state='running' AND (boot_id!=? OR expires_boottime<=?)",(boot,now))
            job = db.execute("SELECT j.* FROM postmeeting_jobs j JOIN postmeeting_snapshots s ON s.id=j.snapshot_id "
                "WHERE j.state='ready' AND NOT EXISTS (SELECT 1 FROM postmeeting_jobs active "
                "WHERE active.series_id=j.series_id AND active.state='running') "
                'ORDER BY j.created_at,s.snapshot_revision,j.id LIMIT 1').fetchone()
            if job is None:
                return None
            prior = self._series_state_db(db,job['series_id']) or self._legacy_postmeeting_prior_db(db,job['series_id'])
            token = secrets.token_hex(32)
            generation = job['generation']+1
            db.execute("UPDATE postmeeting_jobs SET state='running',error_code=NULL,attempts=attempts+1,"
                'generation=?,owner=?,token=?,boot_id=?,expires_boottime=?,prior_revision=?,prior_json=?,updated_at=? WHERE id=?',
                (generation,owner,token,boot,now+lease_seconds,prior['revision'],canonical_json(prior),_now(),job['id']))
            db.execute('INSERT INTO postmeeting_attempts VALUES(?,?,?,?,?,?,?)',
                (job['id'],generation,owner,_digest(token),'pending',None,_now()))
            return dict(id=job['id'],owner=owner,token=token,generation=generation)

    def _postmeeting_lease_db(self, db, lease):
        row = db.execute('SELECT * FROM postmeeting_jobs WHERE id=?',(lease.get('id'),)).fetchone()
        boot, now = self._lease_clock()
        if (row is None or row['state']!='running' or row['boot_id']!=boot or row['expires_boottime']<=now
                or any(row[key]!=lease.get(key) for key in ('owner','token','generation'))):
            raise _error('stale_postmeeting_lease')
        return row

    def heartbeat_postmeeting(self, lease, lease_seconds=90):
        if type(lease_seconds) is not int or not 1<=lease_seconds<=300:
            raise _error('invalid_postmeeting_lease')
        with self._locked() as db, db:
            self._postmeeting_lease_db(db,lease)
            if self._postmeeting_interlocked_db(db):
                raise _error('postmeeting_cleanup_interlock')
            _, now = self._lease_clock()
            db.execute('UPDATE postmeeting_jobs SET expires_boottime=?,updated_at=? WHERE id=?',
                (now+lease_seconds,_now(),lease['id']))

    def postmeeting_input(self, lease):
        with self._locked() as db:
            job = self._postmeeting_lease_db(db,lease)
            source = self._row(db,'postmeeting_snapshots','id',job['snapshot_id'])
            return self._postmeeting_input_db(job,source)

    def fail_postmeeting(self, lease, code):
        if not isinstance(code,str) or re.fullmatch('[a-z0-9_]{1,80}',code) is None:
            raise _error('invalid_error_code')
        with self._locked() as db, db:
            if code.endswith('cleanup_unknown'):
                # Safety reports bind to the original attempt, even after time
                # expiry or takeover. They cannot mutate a newer lease/result.
                attempt = db.execute('SELECT * FROM postmeeting_attempts WHERE job_id=? AND generation=?',
                    (lease.get('id'),lease.get('generation'))).fetchone()
                if (attempt is None or attempt['owner']!=lease.get('owner')
                        or not secrets.compare_digest(attempt['token_sha256'],_digest(lease.get('token')))
                        or attempt['cleanup_state']=='completed'):
                    raise _error('stale_postmeeting_lease')
                db.execute("UPDATE postmeeting_attempts SET cleanup_state='unknown',cleanup_error_code=?,updated_at=? "
                    'WHERE job_id=? AND generation=?',(code,_now(),lease['id'],lease['generation']))
                db.execute("UPDATE postmeeting_jobs SET state='blocked',error_code=?,token=NULL,updated_at=? "
                    "WHERE id=? AND generation=? AND state IN ('running','ready')",
                    (code,_now(),lease['id'],lease['generation']))
                return
            self._postmeeting_lease_db(db,lease)
            db.execute("UPDATE postmeeting_jobs SET state='ready',error_code=?,token=NULL,updated_at=? WHERE id=?",
                (code,_now(),lease['id']))
            db.execute("UPDATE postmeeting_attempts SET cleanup_state='completed',updated_at=? "
                'WHERE job_id=? AND generation=?',(_now(),lease['id'],lease['generation']))

    @staticmethod
    def _merge_items(prior, updates, *, series_id, source, task, session_id, occurrence_id):
        """Omission carries. Every explicit change requires current snapshot citations."""
        statuses = {'open','deferred',*CLOSED} if task else {'active','cancelled','superseded'}
        fields = {'id','text','status','sources'} | ({'owner','date'} if task else set())
        optional = {'acceptance_evidence','completion_evidence'} if task else set()
        if not isinstance(updates,list):
            raise _error('invalid_postmeeting_result')
        original = {item['id']:dict(item) for item in prior}
        by_id = {ident:revalidate_acceptance(item,source,session_id) if task else dict(item)
            for ident,item in original.items()}
        seen, changes = set(), []
        source_ids = {entry['id'] for entry in source['entries']}
        for value in updates:
            if (not isinstance(value,dict) or not fields<=set(value) or set(value)-fields-optional or value['status'] not in statuses
                    or not isinstance(value['text'],str) or not value['text'].strip()
                    or task and any(not isinstance(value[key],str) for key in ('owner','date'))
                    or not isinstance(value['sources'],list) or not value['sources']
                    or any(not isinstance(ident,str) or ident not in source_ids for ident in value['sources'])):
                raise _error('invalid_postmeeting_result')
            ident = value['id']
            if ident is not None and (not isinstance(ident,str) or ident not in by_id):
                raise _error('unknown_series_item')
            if ident is None:
                if value['status'] not in ({'open','deferred'} if task else {'active'}):
                    raise _error('new_item_requires_active_status')
                keys = ('text','owner','date') if task else ('text',)
                signature = {key:value[key].strip() for key in keys}
                matches = [item for item in by_id.values()
                    if {key:item[key].strip() for key in keys} == signature]
                if any(item['status'] not in CLOSED or
                        (item.get('origin_session_id') or item.get('source_refs',[{}])[0].get('session_id')) == session_id
                        for item in matches):
                    raise _error('existing_item_requires_stable_id')
                if task and any(item['status'] not in CLOSED and item['text'].strip()==value['text'].strip()
                        for item in by_id.values()):
                    raise _error('existing_item_requires_stable_id')
                # A later occurrence may explicitly create a new work cycle.
                # Existing IDs remain unchanged for carry/update/terminal states.
                ident = ('task-' if task else 'decision-') + _digest([series_id,occurrence_id,signature])[:32]
                if ident in by_id:
                    raise _error('existing_item_requires_stable_id')
            if ident in seen:
                raise _error('duplicate_series_item')
            seen.add(ident)
            old = by_id.get(ident)
            if task and old:
                # Empty model fields mean not stated, never evidence to erase
                # a known owner/deadline. Explicit clearing is an audited operator action.
                value = {**value,**{key:value[key].strip() or old[key] for key in ('owner','date')}}
            if old and old['status'] in CLOSED and value['status']!=old['status']:
                raise _error('closed_item_cannot_reopen')
            refs = [dict(session_id=session_id,occurrence_id=occurrence_id,
                source_sha256=source['source_sha256'],source_version=source['source_version'],
                snapshot_revision=source['snapshot_revision'],source_id=source_id)
                for source_id in dict.fromkeys(value['sources'])]
            previous_refs = old.get('source_refs',[]) if old else []
            item = {**value,'id':ident,
                'origin_session_id':old.get('origin_session_id',previous_refs[0]['session_id'] if previous_refs else session_id) if old else session_id,
                'source_refs':previous_refs+[ref for ref in refs if ref not in previous_refs]}
            if task:
                accepted = source_evidence(value.get('acceptance_evidence'),source,value['sources'],owner=value['owner'])
                completed = source_evidence(value.get('completion_evidence'),source,value['sources'])
                if value['status'] in CLOSED and not completed and not (old and old['status'] in CLOSED):
                    item['status'] = old['status'] if old else 'open'
                    item['completion_proposal'] = dict(requested_status=value['status'],source_refs=refs)
                task_metadata(item,old,_now())
                if old and old.get('acceptance_review'):
                    item['acceptance_review'] = old['acceptance_review']
                previous_evidenced = instant(old.get('last_evidenced_at')) if old else None
                # Republishing an older meeting cannot age newer operator or
                # meeting evidence. Compare instants, including timezone offsets.
                item['last_evidenced_at'] = (old['last_evidenced_at']
                    if previous_evidenced and previous_evidenced>instant(source['occurred_at'])
                    else source['occurred_at'])
                if accepted and (item.get('acceptance_evidence') or {}).get('kind')!='human_confirmation':
                    item.update(commitment_kind='committed',acceptance_evidence={**accepted,'source_ref':next(ref for ref in refs if ref['source_id']==accepted['source_id'])})
                    item.pop('acceptance_review',None)
                if completed:
                    item['completion_evidence'] = {**completed,'source_ref':next(ref for ref in refs if ref['source_id']==completed['source_id'])}
                if completed:
                    item.pop('completion_proposal',None)
            by_id[ident] = item
        for ident,item in by_id.items():
            old = original.get(ident)
            compared = ('text','status','owner','date','commitment_kind','acceptance_evidence','acceptance_review') if task else ('text','status')
            if old is None or any(old.get(key)!=item.get(key) for key in compared):
                if task:
                    item['last_changed_at'] = _now()
                changes.append(dict(kind='task' if task else 'conclusion',
                    action='created' if old is None else 'updated',
                    previous_status=old['status'] if old else None,**item))
        return list(by_id.values()), changes

    def publish_postmeeting(self, lease, completed):
        """Only the restricted adapter's completed receipt enters this host seam.

        This is not an HTTP/SQL import endpoint. Adapter metadata is derived from
        actual bound RPC notifications; model output cannot set it.
        """
        from .postmeeting_adapter import CompletedPostmeeting
        from .summary import validate_codex_summary_model
        if not isinstance(completed,CompletedPostmeeting):
            raise _error('postmeeting_completion_required')
        validate_codex_summary_model(completed.model)
        if not completed.model or not completed.thread_id or not completed.turn_id:
            raise _error('postmeeting_completion_required')
        result = completed.result
        if not isinstance(result,dict) or set(result)!={'summary','conclusions','tasks'}:
            raise _error('invalid_postmeeting_result')
        with self._locked() as db, db:
            job = self._postmeeting_lease_db(db,lease)
            if self._postmeeting_interlocked_db(db):
                raise _error('postmeeting_cleanup_interlock')
            snapshot = self._row(db,'postmeeting_snapshots','id',job['snapshot_id'])
            source = json.loads(snapshot['payload'])
            if completed.input_sha256 != _digest(self._postmeeting_input_db(job,snapshot)):
                raise _error('postmeeting_input_changed')
            validate_summary(result['summary'],source['entries'])
            prior = json.loads(job['prior_json'])
            latest = self._series_state_db(db,job['series_id'])
            if (latest['revision'] if latest else 0)!=job['prior_revision']:
                raise _error('series_revision_changed')
            # Older snapshots may publish their meeting history, never current memory.
            newest = db.execute('SELECT MAX(snapshot_revision) FROM postmeeting_snapshots WHERE session_id=?',
                (job['session_id'],)).fetchone()[0]
            historical = (snapshot['snapshot_revision']<newest or latest is not None
                and datetime.fromisoformat(source['occurred_at'].replace('Z','+00:00'))
                < datetime.fromisoformat(latest['occurred_at'].replace('Z','+00:00')))
            decisions, decision_changes = self._merge_items(
                [*prior.get('conclusions',[]),*prior.get('closed_conclusions',[])],result['conclusions'],
                series_id=job['series_id'],source=source,task=False,
                session_id=job['session_id'],occurrence_id=snapshot['occurrence_id'])
            tasks, task_changes = self._merge_items(
                [*prior.get('open_tasks',[]),*prior.get('closed_tasks',[])],result['tasks'],
                series_id=job['series_id'],source=source,task=True,
                session_id=job['session_id'],occurrence_id=snapshot['occurrence_id'])
            revision = None if historical else job['prior_revision']+1
            now = _now()
            summary = {**result['summary'],'source_quality':source['quality'],
                'postmeeting_state':dict(job_id=job['id'],source_sha256=snapshot['source_sha256'],
                    source_version=snapshot['source_version'],snapshot_revision=snapshot['snapshot_revision'],
                    model=completed.model,published_at=now,advances_latest=not historical)}
            if not historical:
                state = dict(series_id=job['series_id'],revision=revision,session_id=job['session_id'],
                    occurrence_id=snapshot['occurrence_id'],occurred_at=source['occurred_at'],
                    source_sha256=snapshot['source_sha256'],source_version=snapshot['source_version'],
                    snapshot_revision=snapshot['snapshot_revision'],summary=summary,
                    conclusions=[item for item in decisions if item['status']=='active'],
                    closed_conclusions=[item for item in decisions if item['status']!='active'],
                    open_tasks=[item for item in tasks if item['status'] not in CLOSED],
                    closed_tasks=[item for item in tasks if item['status'] in CLOSED],
                    changes=decision_changes+task_changes,published_at=now)
                db.execute('INSERT INTO series_state_revisions VALUES(?,?,?,?)',
                    (job['series_id'],revision,job['id'],canonical_json(state)))
            receipt = dict(model=completed.model,thread_id=completed.thread_id,turn_id=completed.turn_id)
            db.execute('INSERT INTO postmeeting_publications VALUES(?,?,?,?,?,?,?,?)',
                (job['id'],job['session_id'],job['series_id'],revision,canonical_json(summary),canonical_json(result),
                 canonical_json(receipt),now))
            db.execute("UPDATE postmeeting_jobs SET state='published',token=NULL,updated_at=? WHERE id=?",(now,job['id']))
            db.execute("UPDATE postmeeting_attempts SET cleanup_state='completed',updated_at=? "
                'WHERE job_id=? AND generation=?',(now,job['id'],lease['generation']))
            db.execute('INSERT INTO postmeeting_notifications VALUES(?,?,?,?,?,NULL)',
                (job['id'],job['id'],job['series_id'],job['session_id'],now))
            return dict(job_id=job['id'],series_revision=revision,advances_latest=not historical)

    def list_postmeeting_notifications(self, *, unread_only=True):
        with self._locked() as db:
            return [dict(row) for row in db.execute('SELECT * FROM postmeeting_notifications '
                + ('WHERE read_at IS NULL ' if unread_only else '')+'ORDER BY created_at,id')]

    def acknowledge_postmeeting_notification(self, notification_id):
        with self._locked() as db, db:
            row = self._row(db,'postmeeting_notifications','id',notification_id)
            if row['read_at'] is None:
                now = _now()
                db.execute('UPDATE postmeeting_notifications SET read_at=? WHERE id=?',(now,notification_id))
                db.execute("UPDATE postmeeting_jobs SET state='notified',updated_at=? WHERE id=?",(now,row['job_id']))

    def add_postmeeting_correction(self, session_id, corrections, *, reason):
        """Explicit host correction: append a snapshot, never rewrite sealed source.

        Each correction cites an existing source ID and retains its own timestamp
        and revision. Repeating identical submission returns the same snapshot.
        """
        from .assistant_store import text, timestamp
        reason = text(reason,1000)
        if not isinstance(corrections,list) or not corrections:
            raise _error('invalid_source_correction')
        with self._locked() as db, db:
            history = db.execute('SELECT * FROM postmeeting_snapshots WHERE session_id=? '
                'ORDER BY snapshot_revision',(session_id,)).fetchall()
            if not history:
                raise _error('terminal_snapshot_required')
            base = history[0]
            payload = json.loads(base['payload'])
            ids = {entry['id'] for entry in payload['entries']}
            additions = []
            for correction in corrections:
                if (not isinstance(correction,dict) or set(correction)!={'correction_of','text','timestamp','source_revision'}
                        or correction['correction_of'] not in ids
                        or type(correction['source_revision']) is not int or correction['source_revision']<1):
                    raise _error('invalid_source_correction')
                value = {**correction,'text':text(correction['text'],200000),
                    'timestamp':timestamp(correction['timestamp'])}
                additions.append(dict(**value,id='C'+_digest([base['id'],value])[:24],
                    source_state='application_correction',provider_final=False,quality='unfinalized',
                    ordering='receipt_order'))
            request_sha = _digest([base['source_sha256'],additions,reason])
            entries = list(payload['entries'])
            known_ids = {entry['id'] for entry in entries}
            def accumulate(entry):
                if entry['id'] in known_ids:
                    return
                # Application submission order is explicit, not a guessed provider clock.
                supersedes = [prior['id'] for prior in entries
                    if prior.get('correction_of') == entry['correction_of']]
                entries.append({**entry,'supersedes':supersedes})
                known_ids.add(entry['id'])
            for row in history[1:]:
                previous = json.loads(row['payload'])
                # Old snapshots used the request digest as source SHA. Recognize
                # them too; exact replay never creates new model work after later edits.
                if previous.get('correction_request_sha256',row['source_sha256']) == request_sha:
                    # Replaying an older request retains its identity, but must
                    # never revalidate current evidence against an obsolete snapshot.
                    newest_row = history[-1]
                    self._invalidate_series_acceptance_db(db,base['series_id'],session_id,
                        json.loads(newest_row['payload']),newest_row['id'])
                    return row['id']
                for entry in previous['entries']:
                    if entry.get('source_state') == 'application_correction':
                        accumulate(entry)
            for entry in additions:
                accumulate(entry)
            revision = history[-1]['snapshot_revision']+1
            sha = _digest([base['source_sha256'],entries,reason,request_sha])
            superseded = {ident for entry in entries for ident in entry.get('supersedes',[])}
            payload.update(source_sha256=sha,snapshot_revision=revision,entries=entries,
                quality=source_quality(entries),kind='correction_snapshot',base_snapshot_id=base['id'],
                parent_snapshot_id=history[-1]['id'],correction_request_sha256=request_sha,
                active_correction_ids=[entry['id'] for entry in entries
                    if entry.get('source_state')=='application_correction' and entry['id'] not in superseded],
                correction_reason=reason)
            ident = _digest([session_id,sha])
            db.execute('INSERT INTO postmeeting_snapshots VALUES(?,?,?,?,?,?,?,?,?)',
                (ident,session_id,base['occurrence_id'],base['series_id'],sha,base['source_version'],
                 revision,canonical_json(payload),_now()))
            self._enqueue_postmeeting_db(db,ident)
            self._invalidate_series_acceptance_db(db,base['series_id'],session_id,payload,ident)
            return ident

    @staticmethod
    def _postmeeting_input_db(job, source):
        return dict(job_id=job['id'],session_id=job['session_id'],series_id=job['series_id'],
            occurrence_id=source['occurrence_id'],pipeline_version=job['pipeline_version'],
            source=json.loads(source['payload']),prior=json.loads(job['prior_json']))

    def _legacy_postmeeting_prior_db(self, db, series_id):
        """Bootstrap only once from validated existing history and all task states."""
        prior = self._prior_db(db,series_id,'9999-12-31T23:59:59+00:00')
        tasks = self._tasks_db(db,series_id)
        decisions = []
        for item in prior.get('summary',{}).get('決策',[]):
            decisions.append(dict(id='legacy-decision-'+_digest([series_id,item['text']])[:32],
                text=item['text'],status='active',sources=item.get('sources',[]),
                source_refs=[dict(session_id=prior.get('session_id'),summary_revision=prior.get('revision'),
                    source_id=ident,provenance='legacy_summary') for ident in item.get('sources',[])]))
        return dict(revision=0,conclusions=decisions,closed_conclusions=[],
            open_tasks=[item for item in tasks if item['status'] not in CLOSED],
            closed_tasks=[item for item in tasks if item['status'] in CLOSED])

    def list_postmeeting_snapshots(self, session_id):
        """Private source history, including append-only corrections, never notifications."""
        with self._locked() as db:
            self._meeting(db,session_id)
            return [dict(id=row['id'],created_at=row['created_at'],**json.loads(row['payload']))
                for row in db.execute('SELECT * FROM postmeeting_snapshots WHERE session_id=? '
                    'ORDER BY snapshot_revision',(session_id,))]

    def series_question_context(self, session_id, *, revision=None):
        """Pinned current source + latest series memory for one application question.

        The B owner must call this once per question and retain the returned value.
        This seam does not change the frozen admission prior or audio runtime.
        """
        with self._locked() as db:
            run = self._row(db,'assistant_runs','session_id',session_id)
            occurrence = self._row(db,'calendar_occurrences','id',run['occurrence_id'])
            latest = self._series_state_db(db,occurrence['series_id'],revision)
            if revision is not None and latest is None:
                raise _error('series_revision_not_found')
            current = self._publish(db,session_id,materialize=True)
            current.pop('prior_context',None)
            return dict(session_id=session_id,occurrence_id=occurrence['id'],series_id=occurrence['series_id'],
                series_revision=latest['revision'] if latest else 0,current=current,series_state=latest)

    def read_question_snapshot(self, session_id):
        """B's one-lock source + bound series read; admission prior is immutable.

        Standalone sources without a Calendar run retain their legacy context.
        Series binding comes only from the run's occurrence, never model input.
        """
        with self._locked() as db:
            current = self._publish(db,session_id,materialize=True)
            binding = db.execute('SELECT o.series_id FROM assistant_runs r '
                'JOIN calendar_occurrences o ON o.id=r.occurrence_id WHERE r.session_id=?',
                (session_id,)).fetchone()
            if binding:
                state = self._series_state_db(db,binding['series_id'])
                current['series_context'] = dict(series_id=binding['series_id'],
                    revision=state['revision'] if state else 0,state=state)
            return current
