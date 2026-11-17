"""Pinned RPC transcript input; canonical user roles precede role-free deltas.

This routes text only. Local admitted PCM/quiet still owns question boundaries.
It neither treats a provider final as a question nor guesses text/ID aliases.
"""
from collections import deque
import math
import time


METHODS={
    'thread/realtime/started':'session_started',
    'thread/realtime/item/started':'item_started',
    'thread/realtime/item/transcript/delta':'item_delta',
    'thread/realtime/item/completed':'item_completed',
    'thread/realtime/transcript/delta':'flat_delta',
    'thread/realtime/transcript/done':'flat_done',
}
OUTCOMES={'bound','retired','appended','no_local_activity','invalid_text',
    'foreign_thread','inactive','invalid_item','unknown_item','closed_item',
    'foreign_session','non_user','duplicate_started','flat_mirror',
    'canonical_after_flat','final_observed_only','identity_capacity',
    'session_bound','session_unbound','session_conflict','startup_unverified'}
ITEM_LIMIT=4096  # Bounded identity/tombstone memory, never a text/duration quota.
EVENT_LIMIT=16


class QuestionTranscriptInput:
    def __init__(self,framer,*,clock=time.monotonic):
        self.framer,self.clock=framer,clock
        self.origin=clock()
        self.mode='unselected'
        self.session=None
        self.items={}
        self.counts=dict.fromkeys(sorted(OUTCOMES),0)
        self.events=deque(maxlen=EVENT_LIMIT)

    def _record(self,event,outcome,text=None):
        self.counts[outcome]=min(2147483647,self.counts[outcome]+1)
        elapsed=self.clock()-self.origin
        self.events.append({'event':event,'outcome':outcome,
            'characters':min(2147483647,len(text)) if isinstance(text,str) else 0,
            'received_seconds':min(86400.,max(0.,elapsed)) if math.isfinite(elapsed) else None})

    def _append(self,event,text):
        if not isinstance(text,str) or not text:
            self._record(event,'invalid_text');return
        accepted=self.framer.append_transcript(text)
        self._record(event,'appended' if accepted else 'no_local_activity',text)

    def feed(self,method,params,*,thread_id,enabled,start_issued=False):
        event=METHODS.get(method)
        if event is None:return
        if not thread_id or params.get('threadId')!=thread_id:
            self._record(event,'foreign_thread');return
        if event=='session_started':
            if start_issued is not True:
                self._record(event,'startup_unverified');return
            session=params.get('realtimeSessionId')
            if params.get('version')!='v3' or not isinstance(session,str) or not 1<=len(session)<=256:
                self._record(event,'invalid_item');return
            if self.session is not None and session!=self.session:
                self._record(event,'session_conflict')
                raise RuntimeError('response_input_session_changed')
            self.session=session
            self._record(event,'session_bound');return
        if not enabled:
            self._record(event,'inactive');return
        if event in {'flat_delta','flat_done'}:
            if params.get('role')!='user':
                self._record(event,'non_user');return
            if event=='flat_done':
                self._record(event,'final_observed_only',params.get('text'));return
            if self.mode=='canonical':
                self._record(event,'flat_mirror');return
            if not isinstance(params.get('delta'),str) or not params['delta']:
                self._record(event,'invalid_text');return
            self.mode='flat'
            self._append(event,params.get('delta'));return
        if self.mode=='flat':
            # Explicit legacy fallback: never merge unidentified flat text with
            # a second stream. Pinned canonical-first ordering selects canonical
            # before any corresponding flat mirror in normal production.
            self._record(event,'canonical_after_flat');return
        if self.session is None:
            self._record(event,'session_unbound');return
        if event=='item_delta':
            ident=params.get('itemId')
            if not isinstance(ident,str) or not 1<=len(ident)<=256:
                self._record(event,'invalid_item');return
            item=self.items.get(ident)
            if item is None:
                self._record(event,'unknown_item');return
            if not item['open']:
                self._record(event,'closed_item');return
            if item['role']!='user':
                self._record(event,'non_user');return
            self._append(event,params.get('delta'));return
        value=params.get('item')
        if (not isinstance(value,dict) or value.get('type')!='transcriptSegment'
                or value.get('role') not in ('user','assistant')
                or any(not isinstance(value.get(k),str) or not 1<=len(value[k])<=256
                    for k in ('id','realtimeSessionId'))):
            self._record(event,'invalid_item');return
        if self.session is not None and value['realtimeSessionId']!=self.session:
            if value['id'] in self.items:self.items[value['id']]['open']=False
            self._record(event,'foreign_session');return
        old=self.items.get(value['id'])
        if old is not None and old['role']!=value['role']:
            old['open']=False
            self._record(event,'invalid_item');return
        if event=='item_completed':
            if old is None:
                if len(self.items)>=ITEM_LIMIT:
                    self._record(event,'identity_capacity')
                    raise RuntimeError('response_input_identity_capacity')
                self.items[value['id']]={'role':value['role'],'open':False}
                self._record(event,'unknown_item');return
            old['open']=False
            self._record(event,'retired');return
        if old is not None:
            self._record(event,'duplicate_started');return
        if len(self.items)>=ITEM_LIMIT:
            self._record(event,'identity_capacity')
            raise RuntimeError('response_input_identity_capacity')
        self.items[value['id']]={'role':value['role'],'open':True}
        if value['role']=='user':self.mode='canonical'
        self._record(event,'bound')

    def status(self):
        return {'version':1,'mode':self.mode,'session_bound':self.session is not None,'counts':dict(self.counts),
            'events':[dict(event) for event in self.events],
            'event_limit':EVENT_LIMIT,'identity_count':len(self.items),'identity_limit':ITEM_LIMIT,
            'timing_basis':'host monotonic seconds relative to input router construction'}
