"""Private B observations, separate from A's source ledger and audio control.

Pinned Native RPC transcript notifications do not identify application requests.
Even provider item/turn identity is not proof of a causal host-question mapping.
Anonymous delta runs are tentative display groups, not provider turn identities;
an anonymous final is retained independently rather than guessed onto a draft.
"""
import asyncio
import json
import uuid


class QuestionActivityRecorder:
    def __init__(self, store, meeting_id):
        self.store, self.meeting_id = store, meeting_id
        self._namespace = uuid.uuid4()
        self._thread_id = None
        self._anonymous_draft = None
        # Resource bound, not a text quota. Saturation makes the journal visibly
        # unavailable; it cannot block A/B media or discard already accepted work.
        self._queue = asyncio.Queue(maxsize=64)
        self._worker = None
        self._accepting = True
        self._closed = False
        self._error_code = None

    def status(self):
        return {'state': 'unavailable' if self._error_code else 'closed' if self._closed else 'available',
                'error_code': self._error_code}

    def _identity(self, kind, ident):
        return uuid.uuid5(self._namespace, json.dumps([kind, ident], ensure_ascii=True)).hex

    def _enqueue(self, thread_id, value):
        if (not self._accepting or not isinstance(thread_id, str) or not thread_id
                or self._thread_id is not None and self._thread_id != thread_id):
            return None
        if self._queue.full():
            self._error_code = self._error_code or 'question_activity_queue_full'
            self._accepting = False
            return None
        self._thread_id = thread_id
        future = asyncio.get_running_loop().create_future()
        self._queue.put_nowait((thread_id, value, future))
        if self._worker is None:
            self._worker = asyncio.create_task(self._consume())
        return future

    def _admit_question(self, thread_id, request_id, text):
        if not isinstance(request_id, str) or not request_id or not isinstance(text, str) or not text:
            return None
        return self._enqueue(thread_id, {'kind': 'question', 'request_id': request_id, 'text': text})

    def _admit_transcript(self, thread_id, method, params):
        final = method == 'thread/realtime/transcript/done'
        if (method not in {'thread/realtime/transcript/delta', 'thread/realtime/transcript/done'}
                or params.get('threadId') != thread_id or params.get('role') != 'assistant'):
            return None
        text = params.get('text' if final else 'delta')
        if not isinstance(text, str) or not text:
            return None
        item_id, turn_id = params.get('itemId'), params.get('turnId')
        return self._enqueue(thread_id, {'kind': 'answer', 'text': text, 'final': final,
            'item_id': item_id if isinstance(item_id, str) and item_id else None,
            'turn_id': turn_id if isinstance(turn_id, str) and turn_id else None})

    def submit_question(self, thread_id, request_id, text):
        """Nonblocking production admission; True is queued, not persisted."""
        return self._admit_question(thread_id, request_id, text) is not None

    def submit_transcript(self, thread_id, method, params):
        """Copy only journal fields; never retain a provider envelope or audio."""
        return self._admit_transcript(thread_id, method, params) is not None

    @staticmethod
    async def _settle(future):
        if future is None:
            return False
        # Cancellation releases the caller, not the accepted journal operation.
        # The sole worker owns it until completion, and close tracks that worker.
        return await asyncio.shield(future)

    async def question(self, thread_id, request_id, text):
        """Persist one admitted application question, returning actual success."""
        return await self._settle(self._admit_question(thread_id, request_id, text))

    async def transcript(self, thread_id, method, params):
        """Persist one observed Native transcript notification."""
        return await self._settle(self._admit_transcript(thread_id, method, params))

    async def _consume(self):
        try:
            while not self._queue.empty():
                thread_id, value, future = self._queue.get_nowait()
                try:
                    result = await self._persist(thread_id, value)
                except Exception:
                    self._error_code = self._error_code or 'question_activity_write_failed'
                    self._accepting = False
                    result = False
                future.set_result(result)
                self._queue.task_done()
        finally:
            self._worker = None

    async def _persist(self, thread_id, value):
        kind, text = value['kind'], value['text']
        if kind == 'question':
            # An application boundary splits anonymous display groups without
            # assigning either group's text to the newly admitted question.
            self._anonymous_draft = None
            ident = self._identity('question', value['request_id'])
            request_id, turn_id, state, append = value['request_id'], None, 'admitted', False
        else:
            item_id, turn_id, final = value['item_id'], value['turn_id'], value['final']
            if item_id is not None:
                ident = self._identity('item', item_id)
                self._anonymous_draft = None
            elif turn_id is not None:
                ident = self._identity('turn', turn_id)
                self._anonymous_draft = None
            elif final:
                ident = uuid.uuid4().hex
            else:
                if self._anonymous_draft is None:
                    self._anonymous_draft = uuid.uuid4().hex
                ident = self._anonymous_draft
            if final:
                self._anonymous_draft = None
            request_id, append = None, not final
            state = 'final_unassociated' if final else 'delta_unassociated'
        return await asyncio.to_thread(self.store._save_question_activity, self.meeting_id,
            activity_id=ident, thread_id=thread_id, kind=kind, request_id=request_id,
            turn_id=turn_id, text=text, state=state, append=append)

    async def flush(self, *, timeout=5):
        """Bounded wait; timeout never cancels or loses accepted SQLite work."""
        worker = self._worker
        if worker is None:
            return True
        try:
            done, _ = await asyncio.wait({worker}, timeout=max(0, timeout))
        except asyncio.CancelledError:
            self._error_code = self._error_code or 'question_activity_cleanup_incomplete'
            self._accepting = False
            raise
        if not done:
            self._error_code = self._error_code or 'question_activity_cleanup_incomplete'
            self._accepting = False
            return False
        worker.result()
        return True

    def stop_accepting(self):
        self._accepting = False

    async def close(self, *, timeout=5):
        # Native supplies only the remainder of its existing cleanup window.
        # If disk is blocked, the worker stays owned/tracked and every eventual
        # write still checks the capture lease and sealed state.
        self.stop_accepting()
        try:
            return await self.flush(timeout=timeout)
        finally:
            self._closed = True
