"""Operator task actions and read-only reminders on the existing series ledger."""
import copy
import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timezone

from .meeting_store import canonical_json

CLOSED = {'completed','cancelled','superseded'}


def task_note(value):
    from .assistant_store import AssistantStoreError
    if not isinstance(value,str):
        raise AssistantStoreError('invalid_task_note')
    normalized = value.replace('\r\n','\n')
    if len(normalized)>2000:
        raise AssistantStoreError('task_note_too_long')
    if any(unicodedata.category(char)=='Cc' and char!='\n' for char in normalized):
        raise AssistantStoreError('task_note_control_character')
    if not normalized.strip():
        raise AssistantStoreError('task_note_required')
    return normalized.strip()


def instant(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed if parsed.tzinfo else None
    except (ValueError,TypeError,AttributeError):
        return None


def task_metadata(item, old, now):
    """The host owns acceptance/times; a model cannot submit human evidence."""
    tracked = ('text','owner','date','status')
    changed = old is None or any(old.get(key)!=item.get(key) for key in tracked)
    for key in ('commitment_kind','acceptance_evidence','completion_evidence','created_at',
            'last_changed_at','last_evidenced_at','last_operator_evidence'):
        if old and key in old:
            item[key] = copy.deepcopy(old[key])
    item.setdefault('commitment_kind','suggested')
    item.setdefault('created_at',now if old is None else None)
    if changed:
        item['last_changed_at'] = now
    item['last_evidenced_at'] = now
    if old and (old.get('owner')!=item['owner'] or old.get('text')!=item['text']):
        item['commitment_kind'] = 'suggested'
        item.pop('acceptance_evidence',None)
    return item


def revalidate_acceptance(item, source, session_id):
    """A correction invalidates dependent source evidence, not independent human intent."""
    evidence = item.get('acceptance_evidence') or {}
    previous_review = item.get('acceptance_review') or {}
    if evidence.get('kind')=='meeting_source':
        ref = evidence.get('source_ref') or {}
    elif (not evidence and item.get('commitment_kind')=='suggested'
            and previous_review.get('reason')=='source_corrected'
            and previous_review.get('session_id')==session_id):
        # A pending review still depends on this source after the invalid
        # acceptance was removed. Follow subsequent corrections without
        # restoring the promise or moving back to an older snapshot.
        if source['snapshot_revision']<=previous_review['snapshot_revision']:
            return item
        ref = previous_review.get('invalidated_source_ref') or {}
        evidence = previous_review.get('invalidated_evidence') or {}
    else:
        return item
    if ref.get('session_id')!=session_id:
        return item
    active = set(source.get('active_correction_ids',[]))
    corrected = {entry['correction_of'] for entry in source['entries']
        if entry['id'] in active and entry.get('correction_of')}
    entries = {entry['id']:entry for entry in source['entries']}
    entry = entries.get(ref.get('source_id'))
    invalid = (entry is None or ref['source_id'] in corrected
        or entry.get('correction_of') and ref['source_id'] not in active)
    if invalid:
        root = entry.get('correction_of') if entry else ref['source_id']
        root = root or ref['source_id']
        item = copy.deepcopy(item)
        review = dict(
            reason='source_corrected',invalidated_source_ref=ref,
            invalidated_evidence=evidence,session_id=session_id,
            source_sha256=source['source_sha256'],snapshot_revision=source['snapshot_revision'],
            active_correction_ids=sorted(active),replacement_sources=[dict(source_id=value['id'],
                text=value['text'],timestamp=value.get('timestamp'),source_state=value['source_state'],
                source_sha256=source['source_sha256'],source_version=source['source_version'],snapshot_revision=source['snapshot_revision'])
                for value in source['entries'] if value['id'] in active and value.get('correction_of')==root])
        audit_fields = ('source_sha256','snapshot_revision','active_correction_ids','replacement_sources')
        initial = previous_review.get('initial_invalidation')
        if not initial:
            audit = previous_review or review
            initial = {key:audit[key] for key in audit_fields}
        review['initial_invalidation'] = copy.deepcopy(initial)
        item.update(commitment_kind='suggested',acceptance_review=review)
        item.pop('acceptance_evidence',None)
        item['last_changed_at'] = datetime.now(timezone.utc).isoformat()
    return item


def source_evidence(value, source, cited, *, owner=None):
    from .assistant_store import AssistantStoreError
    if value is None:
        return None
    if (not isinstance(value,dict) or set(value)!={'source_id','quote'}
            or value['source_id'] not in cited or not isinstance(value['quote'],str)
            or not value['quote'].strip()):
        raise AssistantStoreError('invalid_task_evidence')
    entry = next((entry for entry in source['entries'] if entry['id']==value['source_id']),None)
    if (entry is None or value['quote'] not in entry['text']
            or owner is not None and (not owner or owner not in value['quote'])):
        raise AssistantStoreError('invalid_task_evidence')
    active = source.get('active_correction_ids')
    if active is not None:
        corrected = {entry['correction_of'] for entry in source['entries']
            if entry['id'] in active and entry.get('correction_of')}
        if value['source_id'] in corrected or entry.get('correction_of') and entry['id'] not in active:
            raise AssistantStoreError('superseded_task_evidence')
    return dict(kind='meeting_source',quote=value['quote'],source_id=value['source_id'])


def project_task(item, *, now, stale_days, zone):
    result = copy.deepcopy(item)
    reasons = []
    committed = item.get('commitment_kind')=='committed' and bool(item.get('acceptance_evidence'))
    if not item.get('owner'):
        reasons.append('未指定負責人')
    if not committed:
        reasons.append('尚無明確承接紀錄')
    if item.get('acceptance_review'):
        reasons.append('承接來源已訂正，需重新確認')
    due = None
    raw = item.get('date','')
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}',raw):
        try:
            due = date.fromisoformat(raw)
        except ValueError:
            pass
    if due and due<now.astimezone(zone).date() and item['status'] not in CLOSED:
        reasons.append('已逾期，請確認進度')
    elif raw and due is None:
        reasons.append('期限格式待確認')
    evidenced = instant(item.get('last_evidenced_at'))
    if evidenced is None:
        reasons.append('缺少最近確認時間')
    elif stale_days and (now-evidenced).total_seconds()>=stale_days*86400:
        reasons.append(f'超過 {stale_days} 天未確認')
    if item.get('completion_proposal'):
        reasons.append('來源提出結束此事項，仍待確認證據')
    label = {'completed':'已完成','cancelled':'已取消','superseded':'已被取代'}.get(item['status'])
    result.update(display_status=label or ('待確認' if reasons else '待處理'),
        confirmation_reasons=reasons,commitment_label='明確承諾' if committed else 'AI 建議／待確認',
        due_at=due.isoformat() if due else None)
    return result


class TaskWorkflowMixin:
    def _invalidate_series_acceptance_db(self, db, series_id, session_id, source, snapshot_id):
        state = self._series_state_db(db,series_id)
        if state is None:
            return
        changes = []
        for key in ('open_tasks','closed_tasks'):
            updated = []
            for old in state[key]:
                item = revalidate_acceptance(old,source,session_id)
                updated.append(item)
                if item!=old:
                    changes.append(dict(kind='task',action='updated',previous_status=old['status'],**item))
            state[key] = updated
        if changes:
            state.update(revision=state['revision']+1,changes=changes,
                publication_kind='source_correction',published_at=datetime.now(timezone.utc).isoformat(),
                correction_evidence=dict(session_id=session_id,snapshot_id=snapshot_id,
                    source_sha256=source['source_sha256'],snapshot_revision=source['snapshot_revision']))
            db.execute('INSERT INTO series_state_revisions VALUES(?,?,?,?)',
                (series_id,state['revision'],'source-invalidation:'+snapshot_id+':'+str(state['revision']),canonical_json(state)))

    @staticmethod
    def _init_task_workflow(db):
        db.execute('''CREATE TABLE IF NOT EXISTS task_actions(
            request_id TEXT PRIMARY KEY,digest TEXT NOT NULL,series_id TEXT NOT NULL,
            task_id TEXT NOT NULL,revision INTEGER NOT NULL,action_json TEXT NOT NULL,
            result_json TEXT NOT NULL,created_at TEXT NOT NULL)''')

    def _task_workspace_db(self, db, series_id, *, zone='Asia/Taipei', now=None):
        from zoneinfo import ZoneInfo
        state = self._series_state_db(db,series_id)
        policy = json.loads(db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()[0])
        stale_days = policy.get('task_stale_days',14)
        now = now or datetime.now(timezone.utc)
        if not state:
            return dict(tasks=[],closed_tasks=[],stale_days=stale_days,revision=0,changes=[])
        previous = self._series_state_db(db,series_id,state['revision']-1) or {}
        old_items = {item['id']:item for key in ('conclusions','closed_conclusions','open_tasks','closed_tasks')
            for item in previous.get(key,[])}
        changes = []
        for change in state['changes']:
            old = old_items.get(change.get('id'))
            differences = []
            if old:
                for key,label in (('text','內容'),('owner','責任人'),('date','期限'),('status','狀態')):
                    if key in change and old.get(key)!=change[key]:
                        differences.append(dict(field=key,label=label,before=old.get(key,''),after=change[key]))
            changes.append({**change,'differences':differences})
        return dict(tasks=[project_task(item,now=now,stale_days=stale_days,zone=ZoneInfo(zone))
                for item in state['open_tasks']],
            closed_tasks=[project_task(item,now=now,stale_days=stale_days,zone=ZoneInfo(zone))
                for item in state['closed_tasks']],
            stale_days=stale_days,revision=state['revision'],changes=changes)

    def task_workspace(self, series_id, *, zone='Asia/Taipei', now=None):
        with self._locked() as db:
            self._row(db,'meeting_series','id',series_id)
            return self._task_workspace_db(db,series_id,zone=zone,now=now)

    def task_workspace_snapshot(self, series_id, *, zone='Asia/Taipei', now=None):
        """Heading, memory, task projection, form CAS and history share one read lock."""
        with self._locked() as db:
            series = dict(self._row(db,'meeting_series','id',series_id))
            return dict(series=series,state=self._series_state_db(db,series_id),
                workspace=self._task_workspace_db(db,series_id,zone=zone,now=now),
                legacy_tasks=self._tasks_db(db,series_id),
                task_actions=[json.loads(row[0]) for row in db.execute('SELECT action_json '
                    'FROM task_actions WHERE series_id=? ORDER BY revision',(series_id,))])

    def task_action_history(self, series_id, task_id):
        with self._locked() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT action_json FROM task_actions '
                'WHERE series_id=? AND task_id=? ORDER BY revision',(series_id,task_id))]

    def apply_task_action(self, *, series_id, task_id, action, values, expected_revision,
            request_id, confirmed=False, note='', actor='admin'):
        from .assistant_store import AssistantStoreError, text
        if (action not in {'update','accept','complete','cancel'} or type(expected_revision) is not int
                or not isinstance(request_id,str) or re.fullmatch('[a-f0-9]{32}',request_id) is None
                or type(confirmed) is not bool or not isinstance(values,dict)
                or set(values)!={'text','owner','date'}):
            raise AssistantStoreError('invalid_task_action')
        normalized = {key:text(value,2000 if key=='text' else 256,empty=key!='text')
            for key,value in values.items()}
        note = task_note(note)
        actor = text(actor,128)
        if action in {'accept','complete','cancel'} and not confirmed:
            raise AssistantStoreError('task_confirmation_required')
        if action=='accept' and not normalized['owner']:
            raise AssistantStoreError('task_owner_required')
        payload = dict(series_id=series_id,task_id=task_id,action=action,values=normalized,
            expected_revision=expected_revision,request_id=request_id,confirmed=confirmed,note=note,actor=actor)
        digest = hashlib.sha256(canonical_json(payload).encode()).hexdigest()
        with self._locked() as db, db:
            replay = db.execute('SELECT digest,result_json FROM task_actions WHERE request_id=?',(request_id,)).fetchone()
            if replay:
                if replay['digest']!=digest:
                    raise AssistantStoreError('task_request_conflict')
                return json.loads(replay['result_json'])
            state = self._series_state_db(db,series_id)
            if not state or state['revision']!=expected_revision:
                raise AssistantStoreError('series_revision_changed')
            item = next((item for item in state['open_tasks'] if item['id']==task_id),None)
            if item is None:
                raise AssistantStoreError('task_not_open')
            now = datetime.now(timezone.utc).isoformat()
            updated = task_metadata({**item,**normalized},item,now)
            evidence = dict(kind='human_confirmation',actor=actor,note=note,created_at=now,request_id=request_id)
            updated['last_operator_evidence'] = {**evidence,'action':action}
            if action=='accept':
                updated.update(commitment_kind='committed',acceptance_evidence=evidence,last_changed_at=now)
                updated['acceptance_evidence'].update(basis_series_revision=state['revision'],
                    basis_source_refs=copy.deepcopy(item.get('source_refs',[])))
                updated.pop('acceptance_review',None)
            if action in {'complete','cancel'}:
                updated.update(status='completed' if action=='complete' else 'cancelled',
                    completion_evidence=evidence,last_changed_at=now)
            updated.pop('completion_proposal',None)
            revision = state['revision']+1
            state.update(revision=revision,publication_kind='human_task_action',published_at=now,
                changes=[dict(kind='task',action='updated',previous_status=item['status'],**updated)])
            state['open_tasks'] = [updated if old['id']==task_id else old for old in state['open_tasks']]
            if updated['status'] in CLOSED:
                state['open_tasks'] = [old for old in state['open_tasks'] if old['id']!=task_id]
                state['closed_tasks'].append(updated)
            audit = dict(**payload,revision=revision,evidence=evidence,created_at=now)
            db.execute('INSERT INTO series_state_revisions VALUES(?,?,?,?)',
                (series_id,revision,'human:'+request_id,canonical_json(state)))
            db.execute('INSERT INTO task_actions VALUES(?,?,?,?,?,?,?,?)',
                (request_id,digest,series_id,task_id,revision,canonical_json(audit),canonical_json(state),now))
            self._audit(db,actor,'task_'+action,task_id)
            return state
