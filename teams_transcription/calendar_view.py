"""Read-only calendar and meeting workspace projections of authoritative SQLite data."""
import json
import re
from datetime import date, datetime, time, timezone as utc_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Listing deliberately never joins source, summary or Q&A text tables.
_MEETINGS = '''SELECT o.*, s.name AS series_name,
    (NULLIF(json_extract(o.value,'$.recurring_event_id'),'') IS NOT NULL
     OR NULLIF(json_extract(o.value,'$.series_id'),'') IS NOT NULL
     OR s.series_key != o.identity_key
     OR EXISTS (SELECT 1 FROM calendar_occurrences sibling
                WHERE sibling.series_id=o.series_id AND sibling.id!=o.id AND sibling.state!='deleted')) AS grouped,
    r.session_id, r.state AS run_state, rt.status_json,
    l.state AS lease_state, l.boot_id, l.expires_boottime
    FROM calendar_occurrences o JOIN meeting_series s ON s.id=o.series_id
    LEFT JOIN assistant_runs r ON r.session_id=(
        SELECT latest.session_id FROM assistant_runs latest
        WHERE latest.occurrence_id=o.id
        ORDER BY latest.started_at DESC, latest.rowid DESC LIMIT 1
    )
    LEFT JOIN session_runtime rt ON rt.meeting_id=r.session_id
    LEFT JOIN execution_leases l ON l.lease_id=r.session_id AND l.kind='capture' '''

_STATE_LABELS = {
    'pending':'未開始', 'needs_review':'待確認', 'cancelled':'已取消', 'skipped':'已略過',
    'running':'執行中', 'starting':'啟動中', 'preparing':'準備中', 'joining':'加入中',
    'connecting':'連線中', 'waiting_for_admission':'等待准入', 'joined':'已加入',
    'prepared':'準備完成', 'owner_confirmed':'已確認', 'rx_starting':'啟動收音中',
    'recording':'記錄中', 'draining':'停止整理中', 'stopped':'已停止',
    'completed':'已結束', 'failed':'執行失敗', 'interrupted':'執行中斷',
    'recorded_without_transcript':'已結束，沒有逐字來源', 'recorded':'已結束',
    'worker_lost':'執行狀態待確認',
}


def _zone(name):
    from .assistant_store import AssistantStoreError
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise AssistantStoreError('invalid_timezone') from None


def _instant(value):
    if not isinstance(value,str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _range(start, end, zone):
    from .assistant_store import AssistantStoreError
    try:
        if any(not isinstance(value,str) or re.fullmatch(r'\d{4}-\d{2}-\d{2}',value) is None
                for value in (start,end)):
            raise ValueError
        first, last = date.fromisoformat(start),date.fromisoformat(end)
        if last<=first:
            raise ValueError
        return tuple(datetime.combine(day,time.min,zone).astimezone(utc_timezone.utc).isoformat()
            for day in (first,last))
    except (ValueError, OverflowError):
        raise AssistantStoreError('invalid_calendar_range') from None


def _meeting(store, row, zone):
    value = json.loads(row['value'])
    status = json.loads(row['status_json']) if row['status_json'] else {}
    active = bool(row['run_state']=='running' and row['lease_state']=='active'
        and store._lease_time_valid(row))
    state = row['run_state'] or row['state']
    if active:
        state = status.get('state') or 'starting'
    label = _STATE_LABELS.get(state,'狀態待確認')
    if row['run_state']=='running' and not active:
        label = '執行狀態待確認'
    result = dict(id=row['id'],title=value.get('title') or '未命名會議',
        kind='series' if row['grouped'] else 'single',series_id=row['series_id'],
        series_name=row['series_name'],state=state,state_label=label,
        session_id=row['session_id'],is_live=bool(active and state=='recording'))
    from .teams_acceptance import is_owner_immediate,IMMEDIATE_MODE
    result['admission_mode']=IMMEDIATE_MODE if is_owner_immediate(store._occurrence(row)) else 'scheduled'
    result['prepared_at']=value.get('prepared_at') if result['admission_mode']==IMMEDIATE_MODE else None
    for edge in ('start','end'):
        instant = _instant(row[edge+'_at'])
        local = instant.astimezone(zone) if instant else None
        result[edge+'_at'] = local.isoformat() if local else None
        result[edge+'_date'] = local.date().isoformat() if local else None
        result[edge+'_time'] = local.strftime('%H:%M') if local and not value.get('all_day') else None
    return result


def calendar_view(store, start, end, timezone):
    zone = _zone(timezone)
    first, last = _range(start,end,zone)
    with store._locked() as db:
        rows = db.execute(_MEETINGS + '''WHERE o.state!='deleted' AND julianday(o.start_at)<julianday(?)
            AND (julianday(o.end_at)>julianday(?) OR
                 (o.end_at IS NULL AND julianday(o.start_at)>=julianday(?)))
            ORDER BY julianday(o.start_at),o.id''',(last,first,first))
        meetings = [_meeting(store,row,zone) for row in rows]
        series = {item['series_id']:item['series_name'] for item in meetings if item['kind']=='series'}
        undated = db.execute("SELECT COUNT(*) FROM calendar_occurrences WHERE state!='deleted' AND start_at IS NULL").fetchone()[0]
    return dict(meetings=meetings,series=[dict(id=ident,name=name) for ident,name in
        sorted(series.items(),key=lambda item:(item[1],item[0]))],undated_count=undated)


def _previous(store, db, row, run, zone):
    # Once a run starts, its frozen snapshot is exactly the history B received.
    cutoff=row['start_at'] or json.loads(row['value']).get('prepared_at')
    prior = json.loads(run['prior_json']) if run else store._prior_db(db,row['series_id'],cutoff) if cutoff else {}
    if not prior.get('available') or prior.get('series_id')!=row['series_id']:
        return None
    earlier = db.execute('''SELECT o.id,o.value,o.start_at FROM assistant_runs r
        JOIN calendar_occurrences o ON o.id=r.occurrence_id
        WHERE r.session_id=? AND o.series_id=?''',
        (prior.get('session_id'),row['series_id'])).fetchone()
    if earlier is None:
        return None
    summary = prior['summary']
    occurred = _instant(prior.get('occurred_at') or earlier['start_at'])
    return dict(session_id=prior['session_id'],occurrence_id=earlier['id'],
        title=json.loads(earlier['value']).get('title') or '未命名會議',
        date=occurred.astimezone(zone).date().isoformat() if occurred else None,
        conclusions=[dict(text=item['text'],sources=item.get('sources',[]))
            for item in (summary.get('決策') or summary.get('重點') or [])],summary=summary)



_SENTENCE_ENDINGS = frozenset('。！？.!?')


def _join_fragment(current, piece):
    if not current:
        return piece
    if not piece:
        return current
    if current[-1].isascii() and current[-1].isalnum() and piece[0].isascii() and piece[0].isalnum():
        return current + ' ' + piece
    return current + piece


def group_transcript_paragraphs(transcript, *, pause_seconds=4.0, soft_max_chars=260, hard_max_chars=500):
    """Project canonical transcript entries into readable paragraphs without mutating SQLite provenance."""
    paragraphs = []
    current = None
    last_instant = None
    for entry in transcript or ():
        piece = entry.get('text') or ''
        state = entry.get('source_state') or 'unknown'
        quality = entry.get('quality')
        ts = entry.get('timestamp')
        parsed = _instant(ts)
        split = current is None
        if not split:
            if state != current['source_state']:
                split = True
            elif parsed is not None and last_instant is not None:
                if abs((parsed - last_instant).total_seconds()) > pause_seconds:
                    split = True
            elif (parsed is None) != (last_instant is None):
                split = True
            elif len(current['text']) >= hard_max_chars:
                split = True
            elif (len(current['text']) >= soft_max_chars
                    and current['text'].rstrip() and current['text'].rstrip()[-1] in _SENTENCE_ENDINGS):
                split = True
        if split:
            current = {
                'id': entry['id'],
                'start_timestamp': ts,
                'end_timestamp': ts,
                'source_state': state,
                'quality': quality,
                'text': piece,
                'segment_count': 1,
                'source_ids': [entry['id']],
            }
            paragraphs.append(current)
        else:
            current['text'] = _join_fragment(current['text'], piece)
            if ts:
                if not current['start_timestamp']:
                    current['start_timestamp'] = ts
                current['end_timestamp'] = ts
            current['segment_count'] += 1
            current['source_ids'].append(entry['id'])
            if quality and quality != current['quality'] and current['quality'] == 'provider_final':
                current['quality'] = quality
        last_instant = parsed
    return paragraphs


def _auto_join_status(db, row):
    value = json.loads(row['value'])
    policy_row = db.execute('SELECT value FROM assistant_settings WHERE id=0').fetchone()
    scheduler_paused = bool(json.loads(policy_row[0]).get('scheduler_paused', True)) if policy_row else True
    calendar = db.execute('SELECT value FROM assistant_calendars WHERE id=?', (row['calendar_ref'],)).fetchone()
    cal = json.loads(calendar['value']) if calendar else None
    if value.get('status') == 'cancelled' or row['state'] == 'cancelled':
        return dict(enabled=False, ready=False, reason='cancelled', label='會議已取消，不會自動入會')
    if value.get('link_status') != 'valid' or value.get('all_day'):
        return dict(enabled=False, ready=False, reason='invalid_link', label='缺少有效 Teams 連結或為全天事件，無法自動入會')
    if row['calendar_ref']:
        if not cal or not cal.get('enabled'):
            return dict(enabled=False, ready=False, reason='calendar_disabled', label='所屬行事曆未啟用，不會自動入會')
        if not cal.get('recording_authorized'):
            return dict(enabled=False, ready=False, reason='recording_not_authorized',
                label='未開啟行事曆錄音授權，不會自動入會（請至設定開啟）')
    elif not value.get('recording_authorized'):
        return dict(enabled=False, ready=False, reason='recording_not_authorized',
            label='此手動場次未勾選錄音授權，不會自動入會')
    if scheduler_paused:
        return dict(enabled=True, ready=False, reason='scheduler_paused',
            label='已授權自動入會（全域自動排程目前暫停中）')
    if row['state'] != 'pending':
        return dict(enabled=True, ready=False, reason='already_processed',
            label='已授權自動入會（此場次已執行或已略過）')
    return dict(enabled=True, ready=True, reason='ready', label='已授權自動入會（時間到將自動加入）')


def _controls(store, db, row, meeting, status, job, source=None):
    from .meeting_store import TERMINAL
    value = json.loads(row['value'])
    calendar = db.execute('SELECT value FROM assistant_calendars WHERE id=?',(row['calendar_ref'],)).fetchone()
    policy = json.loads(calendar['value']) if calendar else None
    authorized = bool(value.get('recording_authorized') and
        (not row['calendar_ref'] or policy and policy.get('enabled') and policy.get('recording_authorized')))
    start, end = _instant(row['start_at']),_instant(row['end_at'])
    now = datetime.now(utc_timezone.utc).timestamp()
    join_window = bool(start and end and end.timestamp()>now
        and start.timestamp()-value.get('join_early_seconds',0)<=now
        and now<=start.timestamp()+value.get('max_lateness_seconds',0))
    busy = db.execute("SELECT 1 FROM execution_leases WHERE kind='capture' AND state='active' LIMIT 1").fetchone()
    stopping = db.execute('SELECT 1 FROM session_stop_requests WHERE meeting_id=?',(row['session_id'],)).fetchone()
    active = bool(row['run_state']=='running' and row['lease_state']=='active'
        and store._lease_time_valid(row) and not stopping and
        meeting['state'] not in {'stopped','completed','failed','draining','recorded','recorded_without_transcript'})
    response = status.get('response_status')
    speaking = (policy.get('enabled') and policy.get('recording_authorized') and policy.get('speaking_authorized')
        if policy else not row['calendar_ref'] and authorized and value.get('speaking_authorized'))
    interlocks = set(store._capture_interlocks_db(db))
    occ_runs = [item['session_id'] for item in db.execute(
        'SELECT session_id FROM assistant_runs WHERE occurrence_id=?',(row['id'],)).fetchall()]
    occ_running = bool(row['state']=='running' or row['run_state']=='running' or active)
    occ_interlocked = any(sid in interlocks for sid in occ_runs)
    occ_summary_busy = False
    if occ_runs:
        placeholders = ','.join('?' for _ in occ_runs)
        occ_summary_busy = bool(
            db.execute(f"SELECT 1 FROM summary_jobs WHERE session_id IN ({placeholders}) AND state='running' LIMIT 1",occ_runs).fetchone()
            or db.execute(f"SELECT 1 FROM postmeeting_jobs WHERE session_id IN ({placeholders}) AND state IN ('running','blocked') LIMIT 1",occ_runs).fetchone())
    has_sources = bool(source and source.get('sources'))
    has_summary = bool(source and source.get('summary'))
    can_retry_summary = bool(
        row['session_id'] and row['run_state'] in TERMINAL | {'interrupted'}
        and has_sources
        and (job is None or job['state'] != 'running')
        and (not has_summary or (job is not None and job['state'] == 'failed')))
    can_rejoin = bool(
        row['session_id'] and row['state'] in {'completed','stopped','failed','interrupted','skipped'}
        and authorized and value.get('link_status')=='valid' and not value.get('all_day')
        and value.get('status')!='cancelled' and not busy and not interlocks)
    can_delete = bool(
        row['state']!='deleted' and not occ_running and not occ_interlocked and not occ_summary_busy)
    return dict(
        join=bool(row['state']=='pending' and not row['session_id'] and authorized and join_window
            and value.get('link_status')=='valid' and not value.get('all_day')
            and not busy and not interlocks),
        rejoin=can_rejoin,
        skip=bool(not row['session_id'] and row['state'] in {'pending','needs_review'}),
        delete=can_delete,
        stop=active,
        output=bool(active and isinstance(response,dict) and response.get('state') not in {'failed','stopped'}
            and (speaking or response.get('output_enabled'))),
        retry_summary=can_retry_summary)


def _latest(values):
    instants = [(instant,value) for value in values if (instant := _instant(value)) is not None]
    return max(instants,key=lambda item:item[0])[1] if instants else None


def meeting_view(store, occurrence_id, timezone):
    from .assistant_store import AssistantStoreError
    zone = _zone(timezone)
    with store._locked() as db:
        row = db.execute(_MEETINGS+"WHERE o.id=? AND o.state!='deleted'",(occurrence_id,)).fetchone()
        if row is None:
            raise AssistantStoreError('record_not_found')
        meeting = _meeting(store,row,zone)
        session_id = row['session_id']
        run = db.execute('SELECT * FROM assistant_runs WHERE session_id=?',(session_id,)).fetchone() if session_id else None
        all_runs = [dict(session_id=item['session_id'],state=item['state'],
            state_label=_STATE_LABELS.get(item['state'],'狀態待確認'),
            started_at=item['started_at'],ended_at=item['ended_at'],end_reason=item['end_reason'])
            for item in db.execute('SELECT session_id,state,started_at,ended_at,end_reason FROM assistant_runs '
                'WHERE occurrence_id=? ORDER BY started_at DESC,rowid DESC',(occurrence_id,)).fetchall()]
        status = json.loads(row['status_json']) if row['status_json'] else {}
        source = store._publish(db,session_id,materialize=True) if session_id else None
        job = db.execute('SELECT state,error_code,updated_at FROM summary_jobs WHERE session_id=? '
            'ORDER BY created_at DESC,rowid DESC LIMIT 1',(session_id,)).fetchone() if session_id else None
        previous = _previous(store,db,row,run,zone)
        series_state = store._series_state_db(db,row['series_id'])
        task_workspace = store._task_workspace_db(db,row['series_id'],zone=timezone)
        latest_recorded = db.execute('SELECT o.start_at,r.session_id FROM calendar_occurrences o '
            "JOIN assistant_runs r ON r.occurrence_id=o.id WHERE o.series_id=? AND o.state!='deleted' AND r.ended_at IS NOT NULL "
            'ORDER BY julianday(o.start_at) DESC,r.ended_at DESC LIMIT 1',(row['series_id'],)).fetchone()
        postmeeting = store._postmeeting_jobs_db(db,session_id) if session_id else []
        postmeeting_summary = store._postmeeting_summary_db(db,session_id) if session_id else None
        tasks = [{key:task[key] for key in ('id','text','owner','date','status','origin_session_id')}
            for task in store._tasks_db(db,row['series_id'])]
        series_meetings = ([_meeting(store,other,zone) for other in db.execute(
            _MEETINGS+"WHERE o.series_id=? AND o.state!='deleted' ORDER BY julianday(o.start_at),o.id",(row['series_id'],))]
            if meeting['kind']=='series' else [])
        # Keep canonical source IDs, provenance and grouped stream order;
        # the calendar workspace only selects its display fields.
        transcript = [dict(id=entry['id'],text=entry['text'],timestamp=entry.get('timestamp'),
            source_state=entry['source_state'],quality=entry.get('quality'),
            revision=entry.get('revision',entry.get('source_revision')) or 0)
            for entry in source['sources']] if source else []
        transcript_paragraphs = group_transcript_paragraphs(transcript)
        snapshot = status.get('response_status')
        response = (dict(state=snapshot.get('state','unknown'),output_enabled=bool(snapshot.get('output_enabled')),
            output_muted=bool(snapshot.get('output_muted',True)),error_code=snapshot.get('error_code'))
            if isinstance(snapshot,dict) else None)
        if response is not None and isinstance(snapshot.get('question_activity'),dict):
            response['question_activity'] = {key:snapshot['question_activity'].get(key)
                for key in ('state','error_code')}
        detail = dict(meeting=meeting,session_id=session_id,run_state=row['run_state'],runs=all_runs,
            auto_join=_auto_join_status(db,row),
            response=response,previous=previous,tasks=tasks,series_meetings=series_meetings,
            series_state=series_state,task_workspace=task_workspace,postmeeting=postmeeting,
            latest_recorded_meeting=dict(latest_recorded) if latest_recorded else None,
            transcript=transcript,transcript_paragraphs=transcript_paragraphs,
            summary=postmeeting_summary or (source['summary'] if source else None),
            summary_job=dict(state=job['state'],error_code=job['error_code']) if job else None,
            source_version=source['version'] if source else None,
            completeness=source['completeness'] if source else None,
            controls=_controls(store,db,row,meeting,status,job,source=source),
            cleanup_review_required=bool(run and store._cleanup_review_metadata(
                db,run,row['status_json'],worker_status='unknown')['required']))
        observed = [status.get('updated_at'),status.get('created_at'),
            run['started_at'] if run else None,run['ended_at'] if run else None,
            job['updated_at'] if job else None]
        observed.extend(item['timestamp'] for item in transcript)
        observed.extend(item['updated_at'] for item in postmeeting)
        if series_state:
            observed.append(series_state['published_at'])
    # Public journal API owns legacy handling and association. Never synthesize
    # Q&A from transcript text; the separate lock also avoids nested flock.
    detail['qa'] = store.list_question_activity(session_id) if session_id else []
    observed.extend(item['updated_at'] for item in detail['qa'])
    detail['updated_at'] = _latest(observed)
    return detail
