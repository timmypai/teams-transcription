"""Bounded passive document-header metadata; never a CSP permission decision.

The owned page must register this collector before navigation. No requests,
all-header reads, meta-policy inspection or document/script mutation occur.
Raw headers live only during parsing; URL hashes stay private in RAM.
"""
import asyncio
from collections import OrderedDict
import contextlib
import hashlib

DIRECTIVES = frozenset(('script-src-elem','script-src','default-src'))
FLAGS = frozenset(('blob','self','nonce_present','hash_present','strict_dynamic','unsafe_inline','none'))
MAX_POLICIES = 4
MAX_POLICY_BYTES = 16384
MAX_READS = 4
MAX_FRAMES = 8


def sanitize_document_policy_snapshot(value):
    """Copy only the complete fixed scalar schema; reject extras and partials."""
    unknown={'available':False}
    if not isinstance(value,dict) or value.get('available') is not True:
        return unknown
    if (set(value)!={'available','policy_count','policies'} or type(value['policy_count']) is not int
            or not 0<=value['policy_count']<=MAX_POLICIES or not isinstance(value['policies'],list)
            or len(value['policies'])!=value['policy_count']):
        return unknown
    policies=[]
    for policy in value['policies']:
        if (not isinstance(policy,dict)
                or set(policy)!={'directives','trusted_types_script_required','trusted_types_none'}
                or any(type(policy[k]) is not bool for k in ('trusted_types_script_required','trusted_types_none'))
                or not isinstance(policy['directives'],dict) or not set(policy['directives'])<=DIRECTIVES):
            return unknown
        directives={}
        for name,flags in policy['directives'].items():
            if not isinstance(flags,dict) or set(flags)!=FLAGS or any(type(v) is not bool for v in flags.values()):
                return unknown
            directives[name]=dict(flags)
        policies.append(dict(directives=directives,
            trusted_types_script_required=policy['trusted_types_script_required'],
            trusted_types_none=policy['trusted_types_none']))
    return dict(available=True,policy_count=len(policies),policies=policies)


def _summarize(values):
    if not isinstance(values,list) or len(values)>MAX_POLICIES:
        return {'available':False}
    policies=[]
    for header in values:
        if not isinstance(header,str) or len(header)>MAX_POLICIES*MAX_POLICY_BYTES:
            return {'available':False}
        for raw in header.split(',',MAX_POLICIES):
            if len(policies)>=MAX_POLICIES or len(raw.encode('utf-8'))>MAX_POLICY_BYTES:
                return {'available':False}
            directives={}
            for part in raw.split(';'):
                tokens=part.split()
                if tokens and tokens[0].lower() not in directives:
                    directives[tokens[0].lower()]=[token.lower() for token in tokens[1:]]
            selected={}
            for name in DIRECTIVES:
                if name not in directives:continue
                tokens=directives[name]
                selected[name]=dict(blob='blob:' in tokens,self="'self'" in tokens,
                    nonce_present=any(t.startswith("'nonce-") and t.endswith("'") for t in tokens),
                    hash_present=any(t.startswith(("'sha256-","'sha384-","'sha512-")) and t.endswith("'") for t in tokens),
                    strict_dynamic="'strict-dynamic'" in tokens,unsafe_inline="'unsafe-inline'" in tokens,
                    none="'none'" in tokens)
            policies.append(dict(directives=selected,
                trusted_types_script_required="'script'" in directives.get('require-trusted-types-for',[]),
                trusted_types_none="'none'" in directives.get('trusted-types',[])))
    return sanitize_document_policy_snapshot(dict(available=True,policy_count=len(policies),policies=policies))


def _url_hash(value):
    if not isinstance(value,str) or len(value)>32768:
        return None
    return hashlib.sha256(value.encode('utf-8')).digest()


class DocumentPolicyCollector:
    def __init__(self,page):
        self.page=page
        self.closed=False
        self.records=OrderedDict()
        self.tasks=set()
        self.listener=self._response
        try:page.on('response',self.listener)
        except Exception:self.closed=True

    def _response(self,response):
        if self.closed:return
        try:
            if response.request.resource_type!='document':return
            frame=response.frame
            if frame.page is not self.page or frame.is_detached():return
            digest=_url_hash(response.url)
            if digest is None:return
            record={'digest':digest,'snapshot':{'available':False}}
            self.records.pop(frame,None)
            self.records[frame]=record
            while len(self.records)>MAX_FRAMES:self.records.popitem(last=False)
            # Dropped/overlapping reads invalidate the older cache entry too.
            if len(self.tasks)>=MAX_READS:return
            task=asyncio.create_task(self._read(response,frame,record))
            self.tasks.add(task)
            task.add_done_callback(self._finished)
        except Exception:
            return

    def _finished(self,task):
        self.tasks.discard(task)
        if not task.cancelled():
            with contextlib.suppress(Exception):task.exception()

    async def _read(self,response,frame,record):
        try:
            values=await asyncio.wait_for(response.header_values('content-security-policy'),1)
            snapshot=_summarize(values)
        except Exception:
            snapshot={'available':False}
        if not self.closed and self.records.get(frame) is record:
            record['snapshot']=snapshot

    def snapshot(self,frame):
        if self.closed:return {'available':False}
        try:
            record=self.records.get(frame)
            if (record is None or frame.page is not self.page or frame.is_detached()
                    or _url_hash(frame.url)!=record['digest']):
                return {'available':False}
            return sanitize_document_policy_snapshot(record['snapshot'])
        except Exception:return {'available':False}

    async def close(self):
        """Detach first; cancelled/late reads can never repopulate the cache."""
        self.closed=True
        self.records.clear()
        with contextlib.suppress(Exception):self.page.remove_listener('response',self.listener)
        tasks=tuple(self.tasks)
        for task in tasks:task.cancel()
        if tasks:
            _,pending=await asyncio.wait(tasks,timeout=1)
        else:pending=set()
        self.tasks.clear()
        return not pending
