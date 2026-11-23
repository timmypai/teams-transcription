"""Public SQLite postmeeting seams, entirely synthetic; no model/network access."""
import tempfile
import unittest
from unittest.mock import patch

from teams_transcription.assistant_store import AssistantStore
from teams_transcription.storage import Session


class PostmeetingStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AssistantStore(self.tmp.name)
        self.calendar = self.store.save_calendar(dict(account_id='dot-fixture',
            calendar_id='series@example.invalid', name='Synthetic', enabled=True,
            recording_authorized=True))

    def capture(self, day=1, text='Prepare report; approved budget.', additional_text=None, **extra):
        event = dict(event_id=f'event-{day}', recurring_event_id='parent',
            original_start=f'2026-11-{day:02d}T10:00:00Z',
            start_at=f'2026-11-{day:02d}T10:00:00+00:00',
            end_at=f'2026-11-{day:02d}T11:00:00+00:00',title='Synthetic',
            status='confirmed',link_status='valid',all_day=False,
            join_url='https://teams.microsoft.com/meet/12345')
        event.update(extra)
        occurrence = self.store.sync_occurrences(self.calendar['id'],[event])[0]
        lease = self.store.claim_run(occurrence['id'],'fixture')
        session = Session(self.tmp.name,lease['session_id'],lease=lease).create(
            dict(provider='mock',source='synthetic',prior_context=self.store.get_run(lease['session_id'])['prior_context']))
        if text:
            session.store.record_increment(session.ident,dict(capture_id='a'*32,receipt_sequence=1,part_index=0,
                text=text,received_at='2026-11-02T10:30:00+00:00'))
        if additional_text is not None:
            session.store.record_increment(session.ident,dict(capture_id='a'*32,receipt_sequence=2,part_index=0,
                text=additional_text,received_at='2026-11-02T10:31:00+00:00'))
        self.store.finish_run(lease,'stopped','operator_stop')
        return occurrence, session.ident

    def test_seal_durably_hands_off_unfinalized_source_once_without_old_worker(self):
        occurrence, session = self.capture()
        jobs = self.store.list_postmeeting_jobs(session)
        self.assertEqual(len(jobs),1)
        self.assertEqual(jobs[0]['state'],'ready')
        self.assertEqual(len(self.store.list_summary_jobs()),1)
        self.assertEqual(self.store.list_summary_jobs()[0]['state'],'pending')
        restarted = AssistantStore(self.tmp.name)
        lease = restarted.claim_postmeeting('host')
        value = restarted.postmeeting_input(lease)
        self.assertEqual(value['occurrence_id'],occurrence['id'])
        self.assertEqual(value['source']['entries'][0]['text'],'Prepare report; approved budget.')
        self.assertFalse(value['source']['quality']['all_sources_provider_final'])
        self.assertEqual(value['prior']['revision'],0)
        self.assertIsNone(restarted.claim_postmeeting('other'))
        self.assertEqual(restarted.get_run(session)['state'],'stopped')


if __name__ == '__main__':
    unittest.main()

class PostmeetingPublicationTests(PostmeetingStoreTests):
    def completed(self, lease, *, task=None, decision=None):
        import hashlib
        from teams_transcription.meeting_store import canonical_json
        from teams_transcription.postmeeting_adapter import CompletedPostmeeting
        value = self.store.postmeeting_input(lease)
        source_id = value['source']['entries'][0]['id']
        summary = {key:[] for key in ('重點','決策','待辦','未決問題')}
        summary['重點'] = [dict(text='Synthetic current meeting',owner='',date='',sources=[source_id])]
        tasks = [] if task is None else [{**dict(id=None,text='Prepare report',owner='',date='',status='open',sources=[source_id]),**task}]
        if tasks and tasks[0]['status'] in {'completed','cancelled','superseded'} and 'completion_evidence' not in tasks[0]:
            cited = next((entry for entry in value['source']['entries'] if entry['id']==tasks[0]['sources'][0]),None)
            if cited:
                tasks[0]['completion_evidence'] = dict(source_id=cited['id'],quote=cited['text'])
        decisions = [] if decision is None else [{**dict(id=None,text='Budget approved',status='active',sources=[source_id]),**decision}]
        return CompletedPostmeeting(dict(summary=summary,tasks=tasks,conclusions=decisions),
            'controlled-test-codex','fixture-thread','fixture-turn',
            hashlib.sha256(canonical_json(value).encode()).hexdigest())

    def publish(self, **kwargs):
        lease = self.store.claim_postmeeting('fixture-host')
        result = self.store.publish_postmeeting(lease,self.completed(lease,**kwargs))
        return result

    def test_atomic_publication_carries_omitted_tasks_and_does_not_accumulate_summaries(self):
        occurrence, session = self.capture()
        first = self.publish(task={},decision={})
        state = self.store.series_state(occurrence['series_id'])
        task_id = state['open_tasks'][0]['id']
        self.assertEqual(first['series_revision'],1)
        self.assertEqual(state['open_tasks'][0]['source_refs'][0]['occurrence_id'],occurrence['id'])
        self.assertEqual(state['open_tasks'][0]['origin_session_id'],session)
        self.assertEqual(len(self.store.list_postmeeting_notifications()),1)
        next_occurrence, next_session = self.capture(2,text='Different meeting; task not mentioned.')
        self.publish()
        latest = self.store.series_state(occurrence['series_id'])
        self.assertEqual(latest['revision'],2)
        self.assertEqual([item['id'] for item in latest['open_tasks']],[task_id])
        self.assertEqual(latest['open_tasks'][0]['origin_session_id'],session)
        self.assertEqual(latest['changes'],[])
        self.assertEqual(len(latest['summary']['重點']),1)
        pinned = self.store.series_state(occurrence['series_id'],1)
        self.assertEqual(pinned['session_id'],session)
        self.assertEqual(self.store.series_question_context(next_session)['series_revision'],2)
        self.assertEqual(self.store.series_question_context(next_session,revision=1)['series_revision'],1)
        self.assertIsNone(self.store.claim_postmeeting('retry'))

    def test_completion_requires_current_evidence_and_cannot_reopen(self):
        occurrence, session = self.capture()
        self.publish(task={})
        task = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        self.capture(2,text='Report complete.')
        lease = self.store.claim_postmeeting('host')
        invalid = self.completed(lease,task=dict(id=task['id'],status='completed',sources=['unknown']))
        with self.assertRaisesRegex(ValueError,'invalid_postmeeting_result'):
            self.store.publish_postmeeting(lease,invalid)
        self.assertEqual(len(self.store.list_postmeeting_notifications()),1)
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(id=task['id'],status='completed')))
        self.assertEqual(self.store.series_state(occurrence['series_id'])['open_tasks'],[])
        self.capture(3,text='No explicit new assignment.')
        lease = self.store.claim_postmeeting('host')
        with self.assertRaisesRegex(ValueError,'closed_item_cannot_reopen'):
            self.store.publish_postmeeting(lease,self.completed(lease,task=dict(id=task['id'],status='open')))
        self.store.publish_postmeeting(lease,self.completed(lease))
        self.assertEqual(len(self.store.series_state(occurrence['series_id'])['closed_tasks']),1)

    def test_receipt_for_another_job_cannot_publish_even_with_matching_source_ids(self):
        self.capture()
        lease = self.store.claim_postmeeting('host')
        completed = self.completed(lease)
        self.store.fail_postmeeting(lease,'interrupted')
        # New claim freezes the same input, safe to rerun but old generation fenced.
        replacement = self.store.claim_postmeeting('host')
        with self.assertRaisesRegex(ValueError,'stale_postmeeting_lease'):
            self.store.publish_postmeeting(lease,completed)
        from dataclasses import replace
        with self.assertRaisesRegex(ValueError,'postmeeting_input_changed'):
            self.store.publish_postmeeting(replacement,replace(completed,input_sha256='0'*64))

    def test_lease_recovery_and_heartbeat_use_boot_clock_not_wall_time(self):
        self.capture()
        with patch.object(self.store,'_lease_clock',return_value=('boot',100)):
            first = self.store.claim_postmeeting('first',lease_seconds=10)
        with patch.object(self.store,'_lease_clock',return_value=('boot',105)):
            self.store.heartbeat_postmeeting(first,lease_seconds=10)
        with patch.object(self.store,'_lease_clock',return_value=('boot',114)):
            self.assertIsNone(self.store.claim_postmeeting('second'))
        with patch.object(self.store,'_lease_clock',return_value=('new-boot',1)):
            second = self.store.claim_postmeeting('second')
            self.assertGreater(second['generation'],first['generation'])
            with self.assertRaisesRegex(ValueError,'stale_postmeeting_lease'):
                self.store.postmeeting_input(first)

    def test_correction_is_append_only_idempotent_and_old_results_cannot_replace_newer(self):
        occurrence, session = self.capture()
        self.publish(task={})
        original = self.store.read(session)
        source = self.store.list_postmeeting_snapshots(session)[0]
        correction = dict(correction_of=source['entries'][0]['id'],text='Report remains open.',
            timestamp='2026-11-02T12:00:00Z',source_revision=1)
        first = self.store.add_postmeeting_correction(session,[correction],reason='Late transcript')
        self.assertEqual(self.store.add_postmeeting_correction(session,[correction],reason='Late transcript'),first)
        self.assertEqual(self.store.read(session),original)
        self.assertEqual(len(self.store.list_postmeeting_snapshots(session)),2)
        self.capture(2,text='Newer occurrence.')
        # Correction created first, so publish then newer meeting.
        self.publish()
        self.publish()
        self.assertEqual(self.store.series_state(occurrence['series_id'])['revision'],3)
        correction['text']='A further historical correction.'
        self.store.add_postmeeting_correction(session,[correction],reason='Late transcript')
        published = self.publish()
        self.assertFalse(published['advances_latest'])
        self.assertEqual(self.store.series_state(occurrence['series_id'])['revision'],3)

    def test_notification_ack_is_idempotent_and_never_rebuilds_meeting(self):
        occurrence, session = self.capture()
        self.publish()
        notification = self.store.list_postmeeting_notifications()[0]
        self.store.acknowledge_postmeeting_notification(notification['id'])
        self.store.acknowledge_postmeeting_notification(notification['id'])
        self.assertEqual(self.store.list_postmeeting_notifications(),[])
        self.assertEqual(self.store.list_postmeeting_jobs(session)[0]['state'],'notified')
        self.assertEqual(len(self.store.list_runs()),1)
        with self.assertRaisesRegex(ValueError,'occurrence_not_ready'):
            self.store.claim_run(occurrence['id'],'rejoin')

    def test_unknown_provider_cleanup_blocks_automatic_reclaim(self):
        occurrence, session = self.capture()
        lease = self.store.claim_postmeeting('host')
        self.store.fail_postmeeting(lease,'postmeeting_codex_cleanup_unknown')
        self.assertEqual(self.store.list_postmeeting_jobs(session)[0]['state'],'blocked')
        self.assertIsNone(self.store.claim_postmeeting('retry'))
        self.assertEqual(self.store.list_postmeeting_notifications(),[])

    def test_duplicate_new_item_cannot_recreate_existing_stable_task(self):
        self.capture()
        self.publish(task={})
        self.capture(2,text='Same task still open.')
        lease = self.store.claim_postmeeting('host')
        with self.assertRaisesRegex(ValueError,'existing_item_requires_stable_id'):
            self.store.publish_postmeeting(lease,self.completed(lease,task={}))
        self.store.publish_postmeeting(lease,self.completed(lease))

    def test_empty_capture_does_not_claim_success_or_create_empty_work(self):
        occurrence, session = self.capture(text='')
        self.assertEqual(self.store.list_postmeeting_jobs(session),[])
        self.assertIsNone(self.store.claim_postmeeting('host'))


    def test_separate_corrections_accumulate_and_replay_returns_original_snapshot(self):
        occurrence,session=self.capture(text='Source A.',additional_text='Source B.')
        original=self.store.read(session)
        source_a,source_b=self.store.list_postmeeting_snapshots(session)[0]['entries']
        first=dict(correction_of=source_a['id'],text='Corrected A.',timestamp='2026-11-02T12:00:00Z',source_revision=1)
        second=dict(correction_of=source_b['id'],text='Corrected B.',timestamp='2026-11-02T12:01:00Z',source_revision=1)
        first_snapshot=self.store.add_postmeeting_correction(session,[first],reason='Fix A')
        self.store.add_postmeeting_correction(session,[second],reason='Fix B')
        latest=self.store.list_postmeeting_snapshots(session)[-1]
        self.assertEqual([row['text'] for row in latest['entries']],
            ['Source A.','Source B.','Corrected A.','Corrected B.'])
        self.assertEqual(self.store.add_postmeeting_correction(session,[first],reason='Fix A'),first_snapshot)
        self.assertEqual(len(self.store.list_postmeeting_snapshots(session)),3)
        self.assertEqual(self.store.read(session),original)
        # Earlier work becomes meeting history; the latest input must still contain both corrections.
        self.assertFalse(self.publish()['advances_latest'])
        self.assertFalse(self.publish()['advances_latest'])
        lease=self.store.claim_postmeeting('host')
        corrections=self.store.postmeeting_input(lease)['source']['entries'][2:]
        self.store.publish_postmeeting(lease,self.completed(lease,decision=dict(
            text='Use corrected A and B',sources=[row['id'] for row in corrections])))
        conclusion,=self.store.series_state(occurrence['series_id'])['conclusions']
        self.assertEqual([ref['source_id'] for ref in conclusion['source_refs']],
            [row['id'] for row in corrections])

    def test_repeated_source_correction_explicitly_supersedes_without_losing_other_source(self):
        _,session=self.capture(text='Source A.',additional_text='Source B.')
        source_a,source_b=self.store.list_postmeeting_snapshots(session)[0]['entries']
        first=dict(correction_of=source_a['id'],text='A revision one.',timestamp='2026-11-02T12:00:00Z',source_revision=1)
        second=dict(correction_of=source_b['id'],text='B revision one.',timestamp='2026-11-02T12:01:00Z',source_revision=1)
        third=dict(correction_of=source_a['id'],text='A revision two.',timestamp='2026-11-02T12:02:00Z',source_revision=2)
        self.store.add_postmeeting_correction(session,[first],reason='Fix A')
        self.store.add_postmeeting_correction(session,[second],reason='Fix B')
        self.store.add_postmeeting_correction(session,[third],reason='Revise A')
        snapshots=self.store.list_postmeeting_snapshots(session)
        latest=snapshots[-1]
        a1,b1,a2=latest['entries'][2:]
        self.assertEqual([row['text'] for row in latest['entries']],
            ['Source A.','Source B.','A revision one.','B revision one.','A revision two.'])
        self.assertEqual(a2['supersedes'],[a1['id']])
        self.assertEqual(latest['active_correction_ids'],[b1['id'],a2['id']])
        self.assertEqual([row['text'] for row in snapshots[1]['entries']],
            ['Source A.','Source B.','A revision one.'])
        self.assertEqual(self.store.add_postmeeting_correction(session,[first],reason='Fix A'),snapshots[1]['id'])
        self.assertEqual(len(self.store.list_postmeeting_snapshots(session)),4)

    def test_unknown_cleanup_blocks_correction_and_other_series_admission(self):
        _,session=self.capture()
        lease=self.store.claim_postmeeting('host')
        self.store.fail_postmeeting(lease,'postmeeting_codex_cleanup_unknown')
        source=self.store.list_postmeeting_snapshots(session)[0]['entries'][0]['id']
        self.store.add_postmeeting_correction(session,[dict(correction_of=source,text='Late correction.',
            timestamp='2026-11-02T12:00:00Z',source_revision=1)],reason='Late correction')
        self.assertEqual(self.store.list_postmeeting_jobs(session)[0]['state'],'ready')
        self.assertIsNone(self.store.claim_postmeeting('correction-host'))
        _,other=self.capture(2,recurring_event_id='other-parent')
        self.assertEqual(self.store.list_postmeeting_jobs(other)[0]['state'],'ready')
        self.assertIsNone(self.store.claim_postmeeting('other-series-host'))
        self.assertEqual(self.store.list_postmeeting_notifications(),[])

    def test_old_attempt_can_report_unknown_cleanup_after_takeover_without_result_authority(self):
        _,session=self.capture()
        with patch.object(self.store,'_lease_clock',return_value=('boot',100)):
            first=self.store.claim_postmeeting('first',lease_seconds=10)
            old_result=self.completed(first)
        with patch.object(self.store,'_lease_clock',return_value=('boot',111)):
            second=self.store.claim_postmeeting('second')
            current_result=self.completed(second)
            with self.assertRaisesRegex(ValueError,'stale_postmeeting_lease'):
                self.store.fail_postmeeting(first,'normal_failed')
            self.store.fail_postmeeting(first,'postmeeting_codex_cleanup_unknown')
            self.assertEqual(self.store.list_postmeeting_jobs(session)[0]['state'],'running')
            with self.assertRaisesRegex(ValueError,'stale_postmeeting_lease'):
                self.store.publish_postmeeting(first,old_result)
            with self.assertRaisesRegex(ValueError,'postmeeting_cleanup_interlock'):
                self.store.heartbeat_postmeeting(second)
            with self.assertRaisesRegex(ValueError,'postmeeting_cleanup_interlock'):
                self.store.publish_postmeeting(second,current_result)
            self.store.fail_postmeeting(second,'postmeeting_cancelled')
            self.assertEqual(self.store.list_postmeeting_jobs(session)[0]['state'],'ready')
            # The independent attempt risk survives ordinary updates and restart.
            restarted=AssistantStore(self.tmp.name)
            self.assertIsNone(restarted.claim_postmeeting('third'))
        self.assertEqual(self.store.list_postmeeting_notifications(),[])

    def test_forged_or_already_cleaned_attempt_cannot_create_cleanup_interlock(self):
        _,session=self.capture()
        lease=self.store.claim_postmeeting('host')
        with self.assertRaisesRegex(ValueError,'stale_postmeeting_lease'):
            self.store.fail_postmeeting({**lease,'token':'forged'},'postmeeting_codex_cleanup_unknown')
        self.store.publish_postmeeting(lease,self.completed(lease))
        with self.assertRaisesRegex(ValueError,'stale_postmeeting_lease'):
            self.store.fail_postmeeting(lease,'postmeeting_codex_cleanup_unknown')
        base=self.store.list_postmeeting_snapshots(session)[0]['entries'][0]['id']
        self.store.add_postmeeting_correction(session,[dict(correction_of=base,text='New correction.',
            timestamp='2026-11-02T12:00:00Z',source_revision=1)],reason='New correction')
        next_lease=self.store.claim_postmeeting('next')
        self.assertIsNotNone(next_lease)
        self.store.fail_postmeeting(next_lease,'postmeeting_cancelled')

    def test_later_occurrence_can_explicitly_create_new_cycle_of_completed_work(self):
        occurrence,session=self.capture(text='Prepare the weekly report.')
        self.publish(task={})
        old=self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        self.capture(2,text='The weekly report is complete.')
        self.publish(task=dict(id=old['id'],status='completed'))
        later,later_session=self.capture(3,text='Prepare a new weekly report for this meeting cycle.')
        self.publish(task={})
        latest=self.store.series_state(occurrence['series_id'])
        new,=latest['open_tasks']
        closed,=latest['closed_tasks']
        self.assertNotEqual(new['id'],old['id'])
        self.assertEqual(new['origin_session_id'],later_session)
        self.assertEqual(new['source_refs'][0]['occurrence_id'],later['id'])
        self.assertEqual((closed['id'],closed['status']),(old['id'],'completed'))
        self.assertIsNone(self.store.claim_postmeeting('replay'))
        self.assertEqual(len(self.store.list_postmeeting_notifications()),3)
