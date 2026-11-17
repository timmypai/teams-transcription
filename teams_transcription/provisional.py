"""Bounded RPC-only input preview; never infers a final from time or silence."""
import uuid

MAX_PROVISIONAL_BYTES = 65536
MAX_PENDING_PROVISIONALS = 8


class ProvisionalTranscript:
    def __init__(self):
        self.current = None
        self.thread_id = None

    def delta(self, text):
        if not isinstance(text, str) or not text:
            return None
        if self.current is not None and self.current['truncated']:
            return None  # Freeze the retained prefix; never splice across loss.
        old = self.current or {'provisional_id':'provisional:' + uuid.uuid4().hex,
                               'revision':0, 'text':'', 'truncated':False}
        remaining = MAX_PROVISIONAL_BYTES - len(old['text'].encode('utf-8'))
        # Slice characters first: even a huge input cannot allocate an equally
        # huge encoded buffer. Preserve spaces and valid Unicode exactly.
        part = text[:remaining].encode('utf-8')[:remaining].decode('utf-8', errors='ignore')
        return {'provisional_id':old['provisional_id'], 'revision':old['revision']+1,
                'text':old['text']+part, 'truncated':old['truncated'] or len(part)<len(text)}

    def resolved(self, final_event_id):
        if self.current is None:
            return None
        return {**self.current, 'revision':self.current['revision']+1, 'text':'',
                'resolved':True, 'final_event_id':final_event_id}
