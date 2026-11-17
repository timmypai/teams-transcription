"""Private B journal behavior; synthetic SQLite and notifications only."""
import asyncio
import tempfile
import threading
import unittest
from unittest.mock import patch

from teams_transcription.meeting_store import MeetingStore
from teams_transcription.qa_activity import QuestionActivityRecorder


class QuestionActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = MeetingStore(self.directory.name)
        self.store.create('one')
        self.store.create('two')
        self.recorder = QuestionActivityRecorder(self.store, 'one')
        self.addAsyncCleanup(self.recorder.close)

    async def notify(self, text, *, final=False, **identity):
        return await self.recorder.transcript('owned',
            'thread/realtime/transcript/done' if final else 'thread/realtime/transcript/delta',
            {'threadId': 'owned', 'role': 'assistant',
             'text' if final else 'delta': text, **identity})

    async def test_restart_keeps_questions_and_identified_final_without_source_changes(self):
        before = self.store.read('one')
        await self.recorder.question('owned', 'question-1', '合成問題？')
        await self.notify('暫', itemId='item-a')
        await self.notify('稿', itemId='item-a')
        await self.notify('合成終稿。', final=True, itemId='item-a', turnId='turn-a')
        await self.notify('晚到增量', itemId='item-a', turnId='turn-a')
        await self.notify('合成終稿。', final=True, itemId='item-b', turnId='turn-b')
        after = MeetingStore(self.directory.name).read('one')
        self.assertEqual(before, after)
        activity = MeetingStore(self.directory.name).list_question_activity('one')
        self.assertEqual([row['text'] for row in activity], ['合成問題？', '合成終稿。', '合成終稿。'])
        self.assertEqual([row['turn_id'] for row in activity], [None, 'turn-a', 'turn-b'])
        self.assertEqual([row['request_id'] for row in activity], ['question-1', None, None])
        self.assertEqual([row['state'] for row in activity], ['admitted', 'final_unassociated', 'final_unassociated'])
        self.assertEqual(len({row['id'] for row in activity}), 3)
        self.assertTrue(all(row['created_at'] <= row['updated_at'] for row in activity))
        self.assertEqual(self.store.list_question_activity('two'), [])

    async def test_anonymous_deltas_form_one_draft_but_late_final_is_not_guessed(self):
        await self.recorder.question('owned', 'question-1', '第一題？')
        await self.notify('未')
        await self.notify('定稿')
        draft = self.store.list_question_activity('one')[-1]
        self.assertEqual(draft['text'], '未定稿')
        await self.recorder.question('owned', 'question-2', '第二題？')
        await self.notify('晚到的第一題或第二題回答', final=True)
        await self.notify('相同回答', final=True)
        await self.notify('相同回答', final=True)
        rows = self.store.list_question_activity('one')
        self.assertEqual(len(rows), 6)
        self.assertEqual(rows[1]['id'], draft['id'])
        self.assertEqual(rows[1]['state'], 'delta_unassociated')
        self.assertTrue(all(row['request_id'] is None for row in rows if row['kind'] == 'answer'))
        self.assertTrue(all(row['turn_id'] is None for row in rows))
        self.assertNotEqual(rows[-1]['id'], rows[-2]['id'])

    async def test_thread_rejection_and_sealed_capture_cannot_add_or_revise_activity(self):
        await self.recorder.question('owned', 'question-1', '合成問題？')
        await self.notify('暫稿', itemId='item')
        self.assertFalse(await self.recorder.question('foreign', 'question-2', '跨執行緒'))
        self.assertFalse(await self.recorder.transcript('owned', 'thread/realtime/transcript/done',
            {'threadId': 'foreign', 'role': 'assistant', 'text': '跨執行緒'}))
        self.assertFalse(await self.recorder.transcript('owned', 'thread/realtime/transcript/done',
            {'threadId': 'owned', 'role': 'user', 'text': '不是回答'}))
        before = self.store.list_question_activity('one')
        self.store.finalize('one', 'stopped')
        self.assertFalse(await self.notify('不應覆寫', final=True, itemId='item'))
        self.assertEqual(self.store.list_question_activity('one'), before)
        self.assertEqual(self.recorder.status(), {'state': 'unavailable', 'error_code': 'question_activity_write_failed'})

    async def test_capture_lease_is_checked_for_each_write(self):
        stale = MeetingStore(self.directory.name, lease={'id': 'missing'})
        recorder = QuestionActivityRecorder(stale, 'one')
        self.assertFalse(await recorder.question('owned', 'question-1', '不應保存'))
        self.assertEqual(self.store.list_question_activity('one'), [])
        self.assertEqual(recorder.status()['state'], 'unavailable')
        await recorder.close()

    async def test_real_capture_lease_blocks_other_meetings_and_writes_after_sealing(self):
        from teams_transcription.assistant_store import AssistantStore
        from teams_transcription.storage import Session
        authority = AssistantStore(self.directory.name)
        calendar = authority.save_calendar(dict(account_id='synthetic', calendar_id='qa@example.invalid',
            name='Fixture', enabled=True, recording_authorized=True))
        occurrence = authority.sync_occurrences(calendar['id'], [dict(event_id='qa-fixture',
            title='Fixture', start_at='2026-10-14T01:00:00+00:00', end_at='2026-10-14T04:00:00+00:00',
            status='confirmed', link_status='valid', all_day=False,
            join_url='https://teams.microsoft.com/meet/12345')])[0]
        lease = authority.claim_run(occurrence['id'], 'qa-owner')
        run = authority.get_run(lease['session_id'])
        session = Session(self.directory.name, lease['session_id'], lease=lease).create(dict(
            provider='codex', source='meeting', prior_context=run['prior_context']))
        recorder = QuestionActivityRecorder(session.store, session.ident)
        self.assertTrue(await recorder.question('owned', 'question-1', '正確場次'))
        foreign = QuestionActivityRecorder(session.store, 'two')
        self.assertFalse(await foreign.question('owned', 'question-1', '其他場次'))
        self.assertEqual(self.store.list_question_activity('two'), [])
        authority.finish_run(lease, 'stopped', 'operator_stop')
        self.assertFalse(await recorder.question('owned', 'question-2', '失效租約'))
        self.assertEqual(len(self.store.list_question_activity(session.ident)), 1)
        await recorder.close()
        await foreign.close()

    async def test_identity_conflicts_do_not_overwrite_another_provider_turn(self):
        await self.notify('保留此段', itemId='item', turnId='first')
        self.assertFalse(await self.notify('不得覆寫', final=True, itemId='item', turnId='second'))
        self.assertEqual(self.store.list_question_activity('one')[0]['text'], '保留此段')
        self.assertEqual(self.recorder.status()['state'], 'unavailable')

    async def test_activity_does_not_add_content_quotas(self):
        question, answer = '合成問題。' * 20000, '合成回答。' * 20000
        self.assertTrue(await self.recorder.question('owned', 'question-1', question))
        self.assertTrue(await self.notify(answer, final=True))
        self.assertEqual([row['text'] for row in self.store.list_question_activity('one')], [question, answer])


    async def test_cancelled_caller_and_close_settle_the_accepted_write(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store._save_question_activity
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(3)
            return original(*args, **kwargs)
        with patch.object(self.store, '_save_question_activity', side_effect=delayed):
            writing = asyncio.create_task(self.recorder.question('owned', 'question-1', '已接受問題'))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            writing.cancel()
            closing = asyncio.create_task(self.recorder.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await writing
            await closing
        self.assertEqual(self.store.list_question_activity('one')[0]['text'], '已接受問題')
        self.assertFalse(await self.recorder.question('owned', 'question-2', '已關閉'))

    async def test_nonblocking_admission_is_bounded_and_close_keeps_all_accepted_work(self):
        accepted = 0
        for index in range(1000):
            if self.recorder.submit_question('owned', 'question-' + str(index), '合成問題'):
                accepted += 1
            else:
                break
        self.assertGreater(accepted, 0)
        self.assertLess(accepted, 1000)
        self.assertEqual(self.recorder.status(),
            {'state': 'unavailable', 'error_code': 'question_activity_queue_full'})
        self.assertFalse(self.recorder.submit_question('owned', 'rejected', '不應保存'))
        await self.recorder.close()
        rows = self.store.list_question_activity('one')
        self.assertEqual(len(rows), accepted)
        self.assertEqual([row['request_id'] for row in rows],
            ['question-' + str(index) for index in range(accepted)])

    async def test_cleanup_deadline_keeps_blocked_write_owned_until_it_finishes(self):
        entered, release = threading.Event(), threading.Event()
        original = self.store._save_question_activity
        def delayed(*args, **kwargs):
            entered.set()
            release.wait(5)
            return original(*args, **kwargs)
        with patch.object(self.store, '_save_question_activity', side_effect=delayed):
            self.assertTrue(self.recorder.submit_question('owned', 'question-1', '已接受問題'))
            self.assertTrue(await asyncio.to_thread(entered.wait, 2))
            try:
                self.assertFalse(await self.recorder.close(timeout=.01))
                self.assertEqual(self.recorder.status(), {'state': 'unavailable',
                    'error_code': 'question_activity_cleanup_incomplete'})
                self.assertFalse(self.recorder.submit_question('owned', 'question-2', '不應接受'))
            finally:
                release.set()
            self.assertTrue(await self.recorder.flush())
        self.assertEqual([row['text'] for row in self.store.list_question_activity('one')], ['已接受問題'])

    async def test_write_failure_closes_admission_without_discarding_already_accepted_work(self):
        original = self.store._save_question_activity
        calls = 0
        def fail_once(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError('synthetic private storage failure')
            return original(*args, **kwargs)
        with patch.object(self.store, '_save_question_activity', side_effect=fail_once):
            self.assertTrue(self.recorder.submit_question('owned', 'question-1', '寫入失敗'))
            self.assertTrue(self.recorder.submit_question('owned', 'question-2', '已接受仍保存'))
            self.assertTrue(await self.recorder.flush())
        self.assertFalse(self.recorder.submit_question('owned', 'question-3', '停止准入'))
        self.assertEqual(self.recorder.status(),
            {'state': 'unavailable', 'error_code': 'question_activity_write_failed'})
        self.assertEqual([row['text'] for row in self.store.list_question_activity('one')], ['已接受仍保存'])
