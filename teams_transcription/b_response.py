"""Host-bound structured SQLite context reader and exact source query tool."""
import inspect
import hashlib
import json
import re
import unicodedata


GREETING = "我在，有什麼問題？"
NO_DATA = "本場會議紀錄沒有足夠資料，無法回答這個問題。"


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _original_span(text, start, end):
    """Map casefold offsets back to original character offsets."""
    offset, original_start = 0, None
    for index, character in enumerate(text):
        after = offset + len(character.casefold())
        if original_start is None and after > start:
            original_start = index
        if after >= end:
            return original_start, index + 1
        offset = after
    raise ValueError("Invalid folded source range")


def _query_terms(value):
    query = unicodedata.normalize("NFKC", value).casefold()
    # Preserve explicit needles rather than splitting their identifiers
    # into common words that may only occur in an unrelated line prefix.
    literals = set(re.findall(r"[a-z0-9]+(?:[_.:\-/][a-z0-9]+)+", query))
    for expression in (r'"([^"\n]{2,})"', r"'([^'\n]{2,})'",
                       r"「([^」\n]{2,})」", r"“([^”\n]{2,})”"):
        literals.update(re.findall(expression, query))
    terms = set(re.findall(r"[a-z0-9]{2,}|[\u3400-\u9fff]{2,}", query))
    terms.update(literals)
    for term in tuple(terms):
        if re.fullmatch(r"[\u3400-\u9fff]+", term):
            terms.update(term[i:i+2] for i in range(len(term)-1))
    return query, literals, terms


def _receipt_boundary_match(segments, needles, meeting_id):
    """Search a bounded suffix/prefix of known adjacent receipt parts only.

    Keep one best match, not a meeting-wide concatenation. Tiny adjacent parts
    may span several boundaries, so the rolling suffix retains query length.
    Neither Markdown headings nor provider/final/legacy text prove adjacency.
    """
    if not isinstance(segments, list):
        raise ValueError("Invalid application segments")
    for segment in segments:
        if (not isinstance(segment, dict) or segment.get('meeting_id') != meeting_id
                or segment.get('source_state') != 'application_segment'
                or segment.get('provider_final') is not False or segment.get('ordering') != 'receipt_order'
                or not isinstance(segment.get('event_id'), str)
                or not re.fullmatch(r'increment:[0-9a-f]{32}', segment['event_id'])
                or not isinstance(segment.get('capture_id'), str)
                or not re.fullmatch(r'[0-9a-f]{32}', segment['capture_id'])
                or type(segment.get('receipt_sequence')) is not int or segment['receipt_sequence'] < 1
                or type(segment.get('part_index')) is not int or segment['part_index'] < 0
                or not isinstance(segment.get('text'), str) or not segment['text']
                or len(segment['text'].encode()) > 8192):
            raise ValueError("Invalid scoped application segment")
    needles = list(dict.fromkeys(needle for needle in needles if needle))
    if not needles:
        return None
    radius = max(map(len, needles)) - 1
    previous, suffix, best, best_priority = None, [], None, len(needles)
    for segment in sorted(segments, key=lambda e: (e['capture_id'], e['receipt_sequence'], e['part_index'])):
        key = (segment['capture_id'], segment['receipt_sequence'],
               segment.get('source_event_id'), segment.get('source_revision'))
        if previous is None or key != previous[0] or segment['part_index'] != previous[1] + 1:
            suffix = []
        prefix = segment['text'][:radius]
        window = [*suffix, (segment, 0, prefix)]
        text = ''.join(piece for _, _, piece in window)
        boundary = sum(len(piece.casefold()) for _, _, piece in suffix)
        folded = text.casefold()
        if boundary:
            for priority, needle in enumerate(needles[:best_priority]):
                start = folded.find(needle)
                while 0 <= start < boundary:
                    end = start + len(needle)
                    if end > boundary:
                        raw_start, raw_end = _original_span(text, start, end)
                        refs, offset = [], 0
                        for source, source_start, piece in window:
                            lo, hi = max(raw_start, offset), min(raw_end, offset + len(piece))
                            if lo < hi:
                                refs.append({'source_id': source['event_id'], 'part_index': source['part_index'],
                                    'char_start': source_start + lo - offset,
                                    'char_end': source_start + hi - offset, 'source_chars': len(source['text'])})
                            offset += len(piece)
                        best = {'needle': needle, 'capture_id': segment['capture_id'],
                            'receipt_sequence': segment['receipt_sequence'], 'source_state': 'application_segment',
                            'provider_final': False, 'ordering': 'receipt_order',
                            'quality': 'unfinalized',
                            'provenance': 'stored_receipt_parts', 'sources': refs}
                        best_priority = priority
                        if priority == 0:
                            return best
                        break
                    start = folded.find(needle, start + 1)
                if best_priority == priority:
                    break
        # Retain at most radius original characters, with exact source offsets.
        suffix = [*suffix, (segment, 0, segment['text'])]
        excess = sum(len(piece) for _, _, piece in suffix) - radius
        while suffix and excess > 0:
            source, source_start, piece = suffix[0]
            if len(piece) <= excess:
                suffix.pop(0)
                excess -= len(piece)
            else:
                suffix[0] = (source, source_start + excess, piece[excess:])
                excess = 0
        previous = key, segment['part_index']
    return best


def _series_records(context):
    """Project only current conclusions, unfinished tasks and necessary changes."""
    from .meeting_store import canonical_json
    if (not isinstance(context,dict) or set(context)!={'series_id','revision','state'}
            or not isinstance(context['series_id'],str) or not context['series_id']
            or type(context['revision']) is not int or context['revision']<0):
        raise ValueError('Invalid bound series context')
    state = context['state']
    identity = dict(series_id=context['series_id'],revision=context['revision'],sha256='')
    if state is None:
        if context['revision']!=0:
            raise ValueError('Missing series revision')
        return identity,None
    if (not isinstance(state,dict) or state.get('series_id')!=context['series_id']
            or state.get('revision')!=context['revision'] or context['revision']<1
            or any(not isinstance(state.get(key),list) for key in ('conclusions','open_tasks','changes'))):
        raise ValueError('Mismatched series revision')
    identity['sha256'] = hashlib.sha256(canonical_json(state).encode()).hexdigest()
    records, seen = [],set()
    def add(item, kind, status):
        if (not isinstance(item,dict) or not isinstance(item.get('id'),str) or not item['id']
                or not isinstance(item.get('text'),str) or not item['text']
                or item.get('status') not in status or not isinstance(item.get('source_refs'),list)
                or not item['source_refs']):
            raise ValueError('Invalid series item')
        # A changed current item appears once; completed/removed items appear as changes.
        if item['id'] in seen:
            raise ValueError('Duplicate current series item')
        seen.add(item['id'])
        ident = ('task:' if kind=='series_task' else 'conclusion:' if kind=='series_conclusion'
            else 'change:')+item['id']
        meta = dict(kind=kind,series_id=context['series_id'],series_revision=context['revision'],
            status=item['status'])
        meta.update({key:item[key] for key in ('owner','date','action','previous_status',
            'commitment_kind','acceptance_evidence','completion_evidence','completion_proposal',
            'last_changed_at','last_evidenced_at','last_operator_evidence','acceptance_review') if key in item})
        if kind=='series_task':
            meta['commitment_kind'] = item.get('commitment_kind','suggested')
            meta['commitment_note'] = '明確承諾' if item.get('commitment_kind')=='committed' and item.get('acceptance_evidence') else '建議／待確認，不代表有人已承接或正在處理'
        records.append(dict(source_id=ident,logical_id=ident,text=item['text'],locator=dict(
            provenance='sqlite_series_memory',source_id=ident,historical_context=meta,
            origin_session_id=item.get('origin_session_id') or item['source_refs'][0].get('session_id'),
            source_refs=item['source_refs'],series_id=context['series_id'],
            series_revision=context['revision'],provider_final=False,quality='derived')))
    for item in state['conclusions']:
        add(item,'series_conclusion',{'active'})
    for item in state['open_tasks']:
        add(item,'series_task',{'open','deferred'})
    for item in state['changes']:
        if not isinstance(item,dict):
            raise ValueError('Invalid series change')
        if item.get('id') not in seen:
            add(item,'series_change',{'completed','cancelled','superseded'})
    return identity,records


class MeetingStoreMinutesReader:
    """Read one source/series snapshot; standalone legacy history remains readable."""
    def __init__(self, store):
        self.store = store

    async def __call__(self, meeting_id):
        result = await _await(self.store.read(meeting_id))
        if (not isinstance(result,dict) or result.get('meeting_id')!=meeting_id
                or not isinstance(result.get('sources'),list)
                or type(result.get('version')) is not int or result['version']<0
                or type(result.get('finalized')) is not bool
                or result.get('completeness') not in {'complete','partial','unavailable'}):
            raise ValueError('MeetingStore returned an invalid scoped source contract')
        records = []
        for source in result['sources']:
            ident = source.get('event_id',source.get('provisional_id'))
            if not isinstance(ident,str) or not isinstance(source.get('text'),str):
                raise ValueError('MeetingStore returned an invalid source')
            logical = ['source',ident]
            if (source.get('source_state')=='application_segment' and isinstance(source.get('source_event_id'),str)
                    and type(source.get('part_index')) is int):
                logical = ['segment',source.get('capture_id'),source['source_event_id'],source['part_index']]
            locator = {key:source[key] for key in ('event_id','revision','source_state','provider_final','quality',
                'timestamp','source_timestamp','source_event_id','source_revision','capture_id','receipt_sequence','part_index','ordering')
                if key in source}
            records.append({'source_id':source['id'],'logical_id':json.dumps(logical,separators=(',',':')),
                'text':source['text'],'locator':{'provenance':'sqlite_source','origin_session_id':meeting_id,
                    'source_id':source['id'],**locator}})
        series_identity,series_records = (
            _series_records(result['series_context']) if 'series_context' in result else (None,None))
        prior = {} if series_records is not None else result.get('prior_context') or {}
        if not isinstance(prior,dict):
            raise ValueError('MeetingStore returned invalid prior context')
        if prior.get('available') is True:
            from .meeting_store import canonical_json
            historical = prior.get('summary')
            if not isinstance(historical,dict) or not isinstance(prior.get('session_id'),str):
                raise ValueError('MeetingStore returned invalid historical context')
            for section in ('重點','決策','待辦','未決問題'):
                for index,item in enumerate(historical.get(section,[])):
                    if not isinstance(item,dict) or not isinstance(item.get('text'),str):
                        raise ValueError('MeetingStore returned invalid historical item')
                    ident = 'history:'+hashlib.sha256(canonical_json([prior['session_id'],section,index,item]).encode()).hexdigest()[:32]
                    records.append({'source_id':ident,'logical_id':ident,'text':item['text'],
                        'locator':{'provenance':'sqlite_derived_summary','source_id':ident,
                            'historical_context':{'kind':'historical_summary','section':section,
                                'occurred_at':prior.get('occurred_at'),
                                **{key:value for key,value in item.items() if key != 'text'}},
                            'origin_session_id':prior['session_id'],'summary_revision':prior.get('revision'),
                            'source_refs':item.get('sources',[]),'provider_final':False,'quality':'derived',
                            'capture_status':prior.get('capture_status'),'capture_error_code':prior.get('capture_error_code'),
                            'end_reason':prior.get('end_reason')}})
        for task in prior.get('open_tasks',[]):
            if not isinstance(task,dict) or not isinstance(task.get('id'),str):
                raise ValueError('MeetingStore returned invalid task memory')
            ident = 'task:'+task['id']
            records.append({'source_id':ident,'logical_id':ident,'text':task['text'],
                'locator':{'provenance':'sqlite_task_memory','source_id':ident,
                    'historical_context':{'kind':'series_task',
                        **{key:task[key] for key in ('owner','date','status')}},
                    'origin_session_id':task['origin_session_id'],'source_refs':task['source_refs'],
                    'provider_final':False,'quality':'operator_managed_memory'}})
        history_status = {
            'summary_available': prior.get('available') is True,
            'summary_item_counts': {section:len(prior.get('summary',{}).get(section,[]))
                if prior.get('available') is True else 0
                for section in ('重點','決策','待辦','未決問題')},
            'series_open_task_count': len(prior.get('open_tasks',[])),
            'previous_transcript_included': False,
            'truncated': bool(prior.get('truncated',False))}
        if series_records is not None:
            records.extend(series_records)
            state = result['series_context']['state']
            history_status.update(summary_available=True,
                summary_item_counts={'重點':0,'決策':len(state['conclusions']),
                    '待辦':len(state['open_tasks']),'未決問題':0},
                series_open_task_count=len(state['open_tasks']))
        screen_observations = result.get('screen_observations', [])
        if not isinstance(screen_observations, list):
            raise ValueError('MeetingStore returned invalid screen observations')
        return {**({'series_snapshot':series_identity} if series_identity is not None else {}),
            **({'screen_observations':screen_observations} if screen_observations else {}),
            'meeting_id':meeting_id,'records':records,'application_segments':result.get('segments',[]),
            'version':result['version'],'finalized':result['finalized'],'completeness':result['completeness'],
            'transcript_state':result['transcript_state'],'history_truncated':bool(prior.get('truncated',False)),
            'history_status':history_status}


class MeetingMinutesTool:
    """One host-bound read_meeting_context(query), with exact source locators."""
    name = 'read_meeting_context'

    def __init__(self, meeting_id, reader):
        if not isinstance(meeting_id,str) or not meeting_id:
            raise ValueError('Invalid context tool scope')
        self.meeting_id,self.reader = meeting_id,reader

    async def dispatch(self, name, arguments):
        if (name!=self.name or not isinstance(arguments,dict) or set(arguments)!={'query'}
                or not isinstance(arguments['query'],str) or not arguments['query'].strip()):
            raise ValueError('Only the bound meeting context read tool is allowed')
        result = await _await(self.reader(self.meeting_id))
        if not isinstance(result,dict) or result.get('meeting_id')!=self.meeting_id:
            raise ValueError('Context reader returned an unscoped or different meeting')
        records = result.get('records')
        if not isinstance(records,list):
            raise ValueError('Context reader requires structured records')
        lines,locators = [],{}
        identities = set()
        for number,record in enumerate(records,1):
            if (not isinstance(record,dict) or not isinstance(record.get('source_id'),str)
                    or not isinstance(record.get('logical_id'),str) or not isinstance(record.get('text'),str)
                    or not isinstance(record.get('locator'),dict) or record['source_id'] in identities):
                raise ValueError('Invalid structured context record')
            identities.add(record['source_id'])
            lines.append((number,record['text']))
            locators[number] = record['locator']
        query, literals, terms = _query_terms(arguments["query"])
        segments = result.get('application_segments', [])
        boundary_match = _receipt_boundary_match(segments,
            [query, *sorted(literals, key=lambda t: (-len(t), t)),
             *sorted(terms, key=lambda t: (-len(t), t))], self.meeting_id)
        selected = [{'record_index':number,'text':text} for number,text in lines]
        if boundary_match:
            by_event = {locator.get('event_id'):number for number,locator in locators.items()}
            for ref in boundary_match['sources']:
                number = by_event.get(ref['source_id'])
                if number is None:
                    raise ValueError('Stored boundary source has no matching SQLite record')
                position = {'record_index':number,'char_start':ref['char_start'],'char_end':ref['char_end']}
                ref['record_ranges'] = [position]
            boundary_match['context_complete'] = True
        query_hits = sum(any(term in row["text"].casefold() for term in terms) for row in selected) + bool(boundary_match)
        literal_hits = sum(any(needle in row["text"].casefold() for needle in literals) for row in selected)
        literal_hits += bool(boundary_match and boundary_match['needle'] in literals)
        screen_observations = result.get('screen_observations', [])
        if not isinstance(screen_observations, list):
            raise ValueError('Context reader returned invalid screen observations')
        history_truncated = bool(result.get('history_truncated'))
        partial = not query_hits or history_truncated
        value = {"meeting_id": self.meeting_id, "available": bool(selected) or bool(screen_observations),
                "context": selected, "truncated": history_truncated,
                "coverage": "partial" if partial else "complete",
                "query_hit_count": query_hits, "query_match_kind":
                    "term_and_receipt_boundary_matches" if boundary_match else "term_matches",
                "literal_hit_count": literal_hits,
                "boundary_hit_count": int(bool(boundary_match)),
                "boundary_matches": [boundary_match] if boundary_match else [],
                "source_locators":[{"record_index":row["record_index"],**locators[row["record_index"]]}
                    for row in selected if row["record_index"] in locators],
                "version":result.get("version"), "finalized":result.get("finalized"),
                "completeness":result.get("completeness", "unknown")}
        if screen_observations:
            value['screen_observations'] = list(screen_observations)
        if 'series_snapshot' in result:
            value['series_snapshot'] = dict(result['series_snapshot'])
        if "transcript_state" in result:
            if not isinstance(result["transcript_state"], dict):
                raise ValueError("Minutes reader returned invalid transcript state")
            value["transcript_state"] = result["transcript_state"]
        if 'history_status' in result:
            history = result['history_status']
            fields = {'summary_available','summary_item_counts','series_open_task_count',
                'previous_transcript_included','truncated'}
            if (not isinstance(history,dict) or set(history)!=fields
                    or any(type(history[key]) is not bool for key in
                        ('summary_available','previous_transcript_included','truncated'))
                    or type(history['series_open_task_count']) is not int or history['series_open_task_count']<0
                    or not isinstance(history['summary_item_counts'],dict)
                    or set(history['summary_item_counts'])!={'重點','決策','待辦','未決問題'}
                    or any(type(count) is not int or count<0 for count in history['summary_item_counts'].values())):
                raise ValueError('Minutes reader returned invalid history status')
            value['history_status'] = {**history,'summary_item_counts':dict(history['summary_item_counts'])}
        return value

