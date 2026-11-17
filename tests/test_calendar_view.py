import asyncio
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from teams_transcription.assistant_store import AssistantStore, AssistantStoreError
from teams_transcription.storage import Session
from summary_history_fixture import legacy_job
from teams_transcription.summary import SECTIONS, source_quality


class CalendarViewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.store = AssistantStore(self.root)
        self.calendar = self.store.save_calendar(dict(account_id='fixture',
            calendar_id='fixture@example.invalid',name='Calendar',enabled=True,
            recording_authorized=True,speaking_authorized=True))

    def occurrence(self, ident, start, end, **changes):
        event = dict(event_id=ident,title='Meeting',start_at=start,end_at=end,
            status='confirmed',link_status='valid',all_day=False,
            join_url='https://teams.microsoft.com/meet/12345')
        event.update(changes)
        return self.store.sync_occurrences(self.calendar['id'],[event])[0]

    def capture(self, occurrence, text='Stored source'):
        lease = self.store.claim_run(occurrence['id'],'fixture')
        session = Session(self.root,lease['session_id'],lease=lease).create(dict(
            provider='codex',source='meeting',prior_context=self.store.get_run(lease['session_id'])['prior_context']))
        if text:
            session.append_text(text,0)
        return session, lease

    def publish(self, occurrence, text):
        session, capture = self.capture(occurrence,text)
        self.store.finish_run(capture,'stopped','operator_stop')
        legacy_job(self.store,session.ident)
        lease = self.store.claim_summary('summary-fixture')
        publisher = Session(self.root,session.ident,lease=lease)
        source = publisher.summary_source()
        value = {key:[] for key in SECTIONS}
        item = dict(text=text,owner='',date='',sources=[source['entries'][0]['id']])
        value['決策'] = [item]
        value['待辦'] = [item]
        value.update(model='controlled-test-codex',source_quality=source_quality(source['entries']))
        publisher.publish_summary(value,expected_version=source['version'],
            expected_source_sha256=source['source_sha256'],generation_mode='codex')
        self.store.finish_summary(lease)
        return session

    def test_local_day_overlap_has_exclusive_end_and_no_wall_clock_completion(self):
        crossing = self.occurrence('crossing','2026-10-27T15:30:00Z','2026-10-27T17:00:00Z')
        self.occurrence('ended-at-start','2026-10-27T15:00:00Z','2026-10-27T16:00:00Z')
        self.occurrence('starts-at-end','2026-10-28T16:00:00Z','2026-10-28T17:00:00Z')
        view = self.store.calendar_view('2026-10-28','2026-10-29','Asia/Taipei')
        self.assertEqual([item['id'] for item in view['meetings']],[crossing['id']])
        meeting = view['meetings'][0]
        self.assertEqual((meeting['start_date'],meeting['end_date']),('2026-10-27','2026-10-28'))
        self.assertEqual((meeting['start_time'],meeting['end_time']),('23:30','01:00'))
        self.assertEqual(meeting['state'],'pending')
        self.assertFalse(meeting['is_live'])
        self.assertIsNone(meeting['session_id'])
        self.assertEqual(self.store.calendar_view('2026-10-28','2026-10-29','UTC')['meetings'][0]['start_time'],'16:00')

    def test_previous_is_chronological_then_frozen_but_tasks_are_fresh(self):
        def recurring(ident, day):
            start = f'2026-10-{day:02d}T01:00:00+00:00'
            return self.occurrence(ident,start,f'2026-10-{day:02d}T02:00:00+00:00',
                recurring_event_id='weekly',original_start=start)

        first, middle, later = recurring('first',1),recurring('middle',8),recurring('later',22)
        target = recurring('target',15)
        middle_session = self.publish(middle,'Earlier decision')
        self.publish(first,'Published later but chronologically older')
        self.publish(later,'Future decision must not become previous')
        detail = self.store.meeting_view(target['id'])
        self.assertEqual(detail['previous']['session_id'],middle_session.ident)
        self.assertEqual(detail['previous']['conclusions'][0]['text'],'Earlier decision')
        self.assertEqual(detail['previous']['date'],'2026-10-08')
        current, lease = self.capture(target,'Current only')
        self.store.finish_run(lease,'stopped','operator_stop')
        # Supply a synthetic historical failed job; no service enqueue occurs.
        legacy_job(self.store,current.ident)
        pending = self.store.claim_summary('summary-fixture')
        self.store.finish_summary(pending,'controlled_summary_failure')
        closer = recurring('closer',14)
        self.publish(closer,'Newly published closer decision')
        task = next(item for item in self.store.list_tasks(target['series_id'])
            if item['origin_session_id']==middle_session.ident)
        self.store.update_task(task['id'],'completed')
        # Calendar metadata may move after execution; it cannot rewrite the
        # historical date or conclusions frozen into this run.
        self.occurrence('middle','2026-10-29T01:00:00+00:00','2026-10-29T02:00:00+00:00',
            recurring_event_id='weekly',original_start='2026-10-08T01:00:00+00:00')
        detail = self.store.meeting_view(target['id'])
        self.assertEqual(detail['previous']['session_id'],middle_session.ident)
        self.assertEqual(detail['previous']['date'],'2026-10-08')
        self.assertEqual(next(item['status'] for item in detail['tasks'] if item['id']==task['id']),'completed')
        self.assertEqual([item['text'] for item in detail['transcript']],['Current only'])
        self.assertEqual(detail['session_id'],current.ident)
        self.assertEqual(detail['qa'],[])
        self.assertFalse(detail['controls']['retry_summary'])

    def test_standalone_is_isolated_and_recurring_identity_survives_narrow_range(self):
        earlier = self.occurrence('weekly-one','2026-10-01T01:00:00Z','2026-10-01T02:00:00Z',
            recurring_event_id='weekly',original_start='2026-10-01T01:00:00Z')
        weekly = self.occurrence('weekly-two','2026-10-28T01:00:00Z','2026-10-28T02:00:00Z',
            recurring_event_id='weekly',original_start='2026-10-28T01:00:00Z')
        single = self.occurrence('single','2026-10-28T01:00:00Z','2026-10-28T02:00:00Z')
        self.publish(earlier,'Series-only source')
        listing = self.store.calendar_view('2026-10-28','2026-10-29')
        meetings = {item['id']:item for item in listing['meetings']}
        self.assertEqual(meetings[weekly['id']]['kind'],'series')
        self.assertEqual(meetings[single['id']]['kind'],'single')
        detail = self.store.meeting_view(single['id'])
        self.assertIsNone(detail['previous'])
        self.assertEqual(detail['tasks'],[])
        self.assertEqual(detail['series_meetings'],[])
        self.assertEqual(detail['transcript'],[])
        self.assertEqual(detail['qa'],[])
        self.assertIsNone(detail['summary'])
        self.assertIsNone(detail['source_version'])

    def test_live_failed_empty_and_unstarted_controls_follow_real_state(self):
        now = datetime.now(timezone.utc)
        start = (now-timedelta(minutes=1)).isoformat()
        end = (now+timedelta(minutes=30)).isoformat()
        occurrence = self.occurrence('current',start,end)
        pending = self.store.meeting_view(occurrence['id'])
        self.assertTrue(pending['controls']['join'])
        self.assertTrue(pending['controls']['skip'])
        self.assertFalse(pending['controls']['stop'])
        self.assertIsNone(pending['run_state'])
        calendar = self.store.get_calendar(self.calendar['id'])
        disabled = self.store.save_calendar({**calendar,'recording_authorized':False})
        self.assertFalse(self.store.meeting_view(occurrence['id'])['controls']['join'])
        self.store.save_calendar({**disabled,'recording_authorized':True})
        session, lease = self.capture(occurrence,text=None)
        for state in ('starting','joining','waiting_for_admission'):
            with self.subTest(state=state):
                session.update(state=state)
                preparing = self.store.meeting_view(occurrence['id'])
                self.assertEqual(preparing['meeting']['state'],state)
                self.assertFalse(preparing['meeting']['is_live'])
                self.assertTrue(preparing['controls']['stop'])
                self.assertFalse(preparing['controls']['join'])
                self.assertFalse(preparing['controls']['output'])
        self.assertEqual(preparing['meeting']['state_label'],'等待准入')
        session.update(state='recording',response_status=dict(state='sleeping',
            output_enabled=False,output_muted=True,error_code=None,
            question_activity=dict(state='unavailable',error_code='question_activity_write_failed')))
        detail = self.store.meeting_view(occurrence['id'])
        self.assertEqual(detail['meeting']['state'],'recording')
        self.assertTrue(detail['meeting']['is_live'])
        self.assertTrue(detail['controls']['stop'])
        self.assertTrue(detail['controls']['output'])
        self.assertEqual(detail['response']['question_activity'],
            dict(state='unavailable',error_code='question_activity_write_failed'))
        self.assertFalse(detail['controls']['join'])
        self.assertFalse(detail['controls']['skip'])
        self.assertEqual(detail['transcript'],[])
        self.assertEqual(detail['completeness'],'unavailable')
        self.assertEqual(detail['updated_at'],session.status()['updated_at'])
        self.assertEqual(detail['qa'],[])
        self.store.request_stop(session.ident)
        stopping = self.store.meeting_view(occurrence['id'])
        self.assertFalse(stopping['controls']['stop'])
        self.assertFalse(stopping['controls']['output'])
        self.store.finish_run(lease,'failed','preparation_error','meeting_join_failed')
        failed = self.store.meeting_view(occurrence['id'])
        self.assertEqual(failed['meeting']['state'],'failed')
        self.assertEqual(failed['run_state'],'failed')
        self.assertFalse(failed['meeting']['is_live'])
        self.assertFalse(any(failed['controls'].values()))
        self.assertIsNone(failed['summary_job'])
        self.assertEqual(failed['completeness'],'unavailable')

    def test_authorization_busy_slot_and_cleanup_interlock_hide_join(self):
        now = datetime.now(timezone.utc)
        start, end = (now-timedelta(minutes=1)).isoformat(),(now+timedelta(minutes=20)).isoformat()
        first = self.occurrence('first',start,end)
        second = self.occurrence('second',start,end)
        session, lease = self.capture(first,text=None)
        self.assertFalse(self.store.meeting_view(second['id'])['controls']['join'])
        self.store.recover_expired(now_boottime=lease['expires_boottime']+1)
        interrupted = self.store.meeting_view(first['id'])
        self.assertEqual(interrupted['meeting']['state'],'interrupted')
        self.assertTrue(interrupted['cleanup_review_required'])
        self.assertFalse(interrupted['controls']['stop'])
        self.assertFalse(self.store.meeting_view(second['id'])['controls']['join'])
        calendar = self.store.get_calendar(self.calendar['id'])
        self.store.save_calendar({**calendar,'recording_authorized':False})
        self.assertFalse(self.store.meeting_view(second['id'])['controls']['join'])

    def test_manual_grouping_uses_stored_identity_not_visible_count_or_url(self):
        values = dict(title='Same name',join_url='https://teams.microsoft.com/meet/12345',
            start_at='2026-10-01T01:00:00Z',end_at='2026-10-01T02:00:00Z')
        first = self.store.create_manual_occurrence(values)
        grouped = self.store.create_manual_occurrence({**values,'series_id':first['series_id'],
            'start_at':'2026-10-28T01:00:00Z','end_at':'2026-10-28T02:00:00Z'})
        single = self.store.create_manual_occurrence({**values,
            'start_at':'2026-10-28T01:00:00Z','end_at':'2026-10-28T02:00:00Z'})
        listing = self.store.calendar_view('2026-10-28','2026-10-29')
        meetings = {item['id']:item for item in listing['meetings']}
        self.assertEqual(meetings[grouped['id']]['kind'],'series')
        self.assertEqual(meetings[single['id']]['kind'],'single')
        related = self.store.meeting_view(grouped['id'])['series_meetings']
        self.assertEqual({item['id'] for item in related},{first['id'],grouped['id']})

    def test_calendar_range_is_uncapped_and_reports_undated_records_separately(self):
        events = [dict(event_id=f'event-{index}',title='Meeting',status='confirmed',
            start_at='2026-10-28T01:00:00Z',end_at='2026-10-28T02:00:00Z',
            all_day=False,link_status='valid',join_url='https://teams.microsoft.com/meet/12345')
            for index in range(1005)]
        self.store.sync_occurrences(self.calendar['id'],events)
        undated = self.occurrence('undated',None,None,all_day=True)
        listing = self.store.calendar_view('2026-10-28','2026-10-29')
        self.assertEqual(len(listing['meetings']),1005)
        self.assertEqual(listing['undated_count'],1)
        detail = self.store.meeting_view(undated['id'])
        self.assertIsNone(detail['meeting']['start_at'])
        self.assertEqual(detail['meeting']['state'],'needs_review')
        self.assertFalse(detail['controls']['join'])
        self.assertTrue(detail['controls']['skip'])

    def test_dst_range_uses_local_midnights_and_validates_inputs(self):
        first = self.occurrence('first','2026-11-01T04:00:00Z','2026-11-01T04:30:00Z')
        late = self.occurrence('late','2026-11-02T04:30:00Z','2026-11-02T04:45:00Z')
        self.occurrence('next-day','2026-11-02T05:00:00Z','2026-11-02T06:00:00Z')
        listing = self.store.calendar_view('2026-11-01','2026-11-02','America/New_York')
        self.assertEqual([item['id'] for item in listing['meetings']],[first['id'],late['id']])
        self.assertEqual(listing['meetings'][1]['start_time'],'23:30')
        for start, end in [('2026-10-28','2026-10-28'),('2026-10-29','2026-10-28'),
                ('2026-02-30','2026-03-01'),('20261028','2026-10-29')]:
            with self.subTest(start=start,end=end), self.assertRaisesRegex(AssistantStoreError,'invalid_calendar_range'):
                self.store.calendar_view(start,end)
        with self.assertRaisesRegex(AssistantStoreError,'invalid_timezone'):
            self.store.calendar_view('2026-10-28','2026-10-29','not/a-timezone')
        with self.assertRaisesRegex(AssistantStoreError,'record_not_found'):
            self.store.meeting_view('missing')

    def test_empty_frozen_prior_is_not_backfilled_and_source_quality_is_preserved(self):
        earlier = self.occurrence('earlier','2026-10-01T01:00:00Z','2026-10-01T02:00:00Z',
            recurring_event_id='weekly',original_start='2026-10-01T01:00:00Z')
        current = self.occurrence('current','2026-10-08T01:00:00Z','2026-10-08T02:00:00Z',
            recurring_event_id='weekly',original_start='2026-10-08T01:00:00Z')
        session, lease = self.capture(current,text=None)
        session.record_increment(dict(capture_id='a'*32,receipt_sequence=1,part_index=0,
            text='Application text',received_at='2026-10-08T01:01:00Z'))
        self.store.finish_run(lease,'stopped','operator_stop')
        legacy_job(self.store,session.ident)
        job = self.store.claim_summary('summary-fixture')
        self.store.finish_summary(job,'controlled_summary_failure')
        self.publish(earlier,'Published after this run started')
        detail = self.store.meeting_view(current['id'])
        self.assertIsNone(detail['previous'])
        self.assertEqual(detail['meeting']['state'],'stopped')
        self.assertEqual(detail['transcript'][0]['source_state'],'application_segment')
        self.assertEqual(detail['transcript'][0]['quality'],'unfinalized')
        self.assertEqual(detail['transcript'][0]['revision'],0)
        self.assertEqual(detail['completeness'],'partial')
        self.assertFalse(detail['meeting']['is_live'])

    def test_question_journal_is_scoped_and_does_not_become_transcript_source(self):
        from teams_transcription.qa_activity import QuestionActivityRecorder
        occurrence = self.occurrence('current','2026-10-28T01:00:00Z','2026-10-28T02:00:00Z')
        unrelated = self.occurrence('unrelated','2026-10-28T01:00:00Z','2026-10-28T02:00:00Z')
        session, lease = self.capture(occurrence,'Only A source')
        before = self.store.meeting_view(occurrence['id'])

        async def record():
            recorder = QuestionActivityRecorder(session.store,session.ident)
            try:
                self.assertTrue(await recorder.question('fixture-thread','request-one','Stored question?'))
                self.assertTrue(await recorder.transcript('fixture-thread','thread/realtime/transcript/done',
                    dict(threadId='fixture-thread',role='assistant',text='Observed answer.',turnId='turn-one')))
            finally:
                await recorder.close()

        asyncio.run(record())
        detail = self.store.meeting_view(occurrence['id'])
        self.assertEqual([item['text'] for item in detail['qa']],['Stored question?','Observed answer.'])
        self.assertEqual([item['state'] for item in detail['qa']],['admitted','final_unassociated'])
        self.assertIsNone(detail['qa'][1]['request_id'])
        self.assertEqual(detail['qa'][1]['turn_id'],'turn-one')
        self.assertEqual(detail['transcript'],before['transcript'])
        self.assertEqual(detail['source_version'],before['source_version'])
        self.assertEqual(detail['updated_at'],detail['qa'][-1]['updated_at'])
        self.assertEqual(self.store.meeting_view(unrelated['id'])['qa'],[])
        self.store.finish_run(lease,'stopped','operator_stop')
        self.assertEqual(self.store.meeting_view(occurrence['id'])['qa'],detail['qa'])
