"""Task lifecycle through the public SQLite seam; synthetic sources only."""
import unittest
import test_postmeeting_store as fixtures


class TaskWorkflowTests(unittest.TestCase):
    # Reuse the public capture/publication fixture, without collecting its
    # existing test methods a second time in this new lifecycle suite.
    setUp = fixtures.PostmeetingPublicationTests.setUp
    capture = fixtures.PostmeetingPublicationTests.capture
    completed = fixtures.PostmeetingPublicationTests.completed
    publish = fixtures.PostmeetingPublicationTests.publish

    def accepted_task(self):
        occurrence,session = self.capture(text='Alice accepts the report.')
        lease = self.store.claim_postmeeting('host')
        source = self.store.postmeeting_input(lease)['source']['entries'][0]
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(owner='Alice',
            acceptance_evidence=dict(source_id=source['id'],quote=source['text']))))
        return occurrence,session,source,self.store.series_state(occurrence['series_id'])['open_tasks'][0]

    def correct(self, session, source_id, content, revision):
        return self.store.add_postmeeting_correction(session,[dict(correction_of=source_id,text=content,
            timestamp=f'2026-11-04T10:0{revision}:00Z',source_revision=revision)],reason='Synthetic correction')

    def test_old_correction_publication_preserves_recent_human_confirmation_time(self):
        from datetime import datetime, timezone
        from unittest.mock import patch

        class ConfirmationClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat('2026-11-20T18:00:00+08:00')

        occurrence,session,source,task = self.accepted_task()
        with patch('teams_transcription.task_workflow.datetime',ConfirmationClock):
            accepted = self.store.apply_task_action(series_id=occurrence['series_id'],task_id=task['id'],
                action='accept',values={key:task[key] for key in ('text','owner','date')},
                expected_revision=1,request_id='a'*32,confirmed=True,note='Checked with Alice today.')
        confirmed = accepted['open_tasks'][0]
        self.assertEqual(confirmed['last_evidenced_at'],'2026-11-20T18:00:00+08:00')
        self.correct(session,source['id'],'Alice did not accept in the original meeting.',1)
        lease = self.store.claim_postmeeting('host')
        correction = self.store.postmeeting_input(lease)['source']['active_correction_ids'][0]
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(id=task['id'],owner='Alice',
            sources=[correction],acceptance_evidence=None)))
        current = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        self.assertEqual(current['last_evidenced_at'],'2026-11-20T18:00:00+08:00')
        self.assertEqual(current['acceptance_evidence'],confirmed['acceptance_evidence'])
        workspace = self.store.task_workspace(occurrence['series_id'],
            now=datetime(2026,11,21,tzinfo=timezone.utc))
        self.assertNotIn('超過 14 天未確認',workspace['tasks'][0]['confirmation_reasons'])
        self.assertEqual(self.store.series_state(occurrence['series_id'],accepted['revision']),accepted)

    def test_later_meeting_evidence_advances_confirmation_time_by_instant_not_date_text(self):
        from datetime import datetime
        from unittest.mock import patch

        class ConfirmationClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime.fromisoformat('2026-11-20T00:30:00+14:00')

        occurrence,_,_,task = self.accepted_task()
        with patch('teams_transcription.task_workflow.datetime',ConfirmationClock):
            accepted = self.store.apply_task_action(series_id=occurrence['series_id'],task_id=task['id'],
                action='accept',values={key:task[key] for key in ('text','owner','date')},
                expected_revision=1,request_id='b'*32,confirmed=True,note='Independently checked.')
        self.capture(19,text='Alice confirms the report is still in progress.',
            start_at='2026-11-19T23:00:00+00:00',end_at='2026-11-20T00:00:00+00:00')
        self.publish(task=dict(id=task['id'],owner='Alice'))
        current = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        self.assertEqual(current['last_evidenced_at'],'2026-11-19T23:00:00+00:00')
        self.assertEqual(current['acceptance_evidence'],accepted['open_tasks'][0]['acceptance_evidence'])

    def test_correction_chain_revokes_current_source_version_and_can_accept_new_valid_quote(self):
        occurrence,session,source,task = self.accepted_task()
        first_correction = self.correct(session,source['id'],'Alice did not accept the report.',1)
        self.publish()  # Omitted task must remain uncommitted.
        self.correct(session,source['id'],'Alice accepts the report after clarification.',2)
        lease = self.store.claim_postmeeting('host')
        current = self.store.postmeeting_input(lease)['source']
        accepted = next(entry for entry in current['entries'] if entry['id'] in current['active_correction_ids'])
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(id=task['id'],owner='Alice',
            sources=[accepted['id']],acceptance_evidence=dict(source_id=accepted['id'],quote=accepted['text']))))
        self.assertEqual(self.store.series_state(occurrence['series_id'])['open_tasks'][0]['commitment_kind'],'committed')
        before_replay = self.store.series_state(occurrence['series_id'])
        self.assertEqual(self.correct(session,source['id'],'Alice did not accept the report.',1),first_correction)
        self.assertEqual(self.store.series_state(occurrence['series_id']),before_replay)
        self.correct(session,source['id'],'Alice never accepted the report.',3)
        current = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        self.assertEqual(current['commitment_kind'],'suggested')
        self.assertEqual(current['acceptance_review']['invalidated_source_ref']['source_id'],accepted['id'])

    def test_unresolved_correction_chain_refreshes_latest_review_without_restoring_commitment(self):
        occurrence,session,source,task = self.accepted_task()
        c1_snapshot = self.correct(session,source['id'],'Alice did not accept, first correction.',1)
        first = self.store.series_state(occurrence['series_id'])
        first_review = first['open_tasks'][0]['acceptance_review']
        self.correct(session,source['id'],'Alice still has not accepted, latest correction.',2)
        latest = self.store.series_state(occurrence['series_id'])
        latest_source = self.store.list_postmeeting_snapshots(session)[-1]
        pending = latest['open_tasks'][0]
        review = pending['acceptance_review']
        self.assertEqual(latest['revision'],first['revision']+1)
        self.assertEqual(review['snapshot_revision'],3)
        self.assertEqual(review['source_sha256'],latest_source['source_sha256'])
        self.assertEqual(review['active_correction_ids'],latest_source['active_correction_ids'])
        self.assertEqual([r['source_id'] for r in review['replacement_sources']],latest_source['active_correction_ids'])
        self.assertEqual(review['initial_invalidation']['source_sha256'],first_review['source_sha256'])
        self.assertEqual(review['invalidated_source_ref'],first_review['invalidated_source_ref'])
        self.assertEqual((pending['id'],pending['status'],pending['commitment_kind']),(task['id'],'open','suggested'))
        self.assertFalse(pending.get('acceptance_evidence'))
        self.assertEqual(self.store.series_state(occurrence['series_id'],first['revision']),first)
        self.assertEqual(self.correct(session,source['id'],'Alice did not accept, first correction.',1),c1_snapshot)
        self.assertEqual(self.store.series_state(occurrence['series_id']),latest)
        projected = self.store.task_workspace(occurrence['series_id'])['tasks'][0]
        self.assertEqual(projected['acceptance_review'],review)
        self.correct(session,source['id'],'Alice remains unassigned, third correction.',3)
        third = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        self.assertEqual(third['acceptance_review']['snapshot_revision'],4)
        self.assertEqual(third['acceptance_review']['initial_invalidation'],review['initial_invalidation'])
        self.assertEqual(third['commitment_kind'],'suggested')
        self.assertEqual(third['status'],'open')

    def test_late_correction_keeps_newer_summary_other_session_evidence_and_b_snapshot_consistent(self):
        import asyncio
        from teams_transcription.b_response import MeetingStoreMinutesReader
        from teams_transcription.response_runtime import _AsyncStore
        occurrence,session,source,task = self.accepted_task()
        _,later_session = self.capture(2,text='Bob accepts a different report.')
        lease = self.store.claim_postmeeting('host')
        other = self.store.postmeeting_input(lease)['source']['entries'][0]
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(text='Different report',owner='Bob',
            acceptance_evidence=dict(source_id=other['id'],quote=other['text']))))
        before = self.store.series_state(occurrence['series_id'])
        self.correct(session,source['id'],'Alice did not accept.',1)
        state = self.store.series_state(occurrence['series_id'])
        self.assertEqual(state['revision'],before['revision']+1)
        self.assertEqual((state['session_id'],state['summary']),(later_session,before['summary']))
        self.assertEqual([item['commitment_kind'] for item in state['open_tasks']],['suggested','committed'])
        projected = self.store.task_workspace(occurrence['series_id'])['tasks']
        self.assertEqual(projected[0]['commitment_label'],'AI 建議／待確認')
        result = asyncio.run(MeetingStoreMinutesReader(_AsyncStore(self.tmp.name))(later_session))
        records = [r['locator']['historical_context'] for r in result['records']
            if r['locator'].get('historical_context',{}).get('kind')=='series_task']
        self.assertEqual([r['commitment_kind'] for r in records],['suggested','committed'])
        self.assertIn('acceptance_review',records[0])
        self.assertEqual(records[0]['acceptance_review']['replacement_sources'][0]['text'],'Alice did not accept.')
        original_review = state['open_tasks'][0]['acceptance_review']
        self.correct(later_session,other['id'],'Bob has not accepted the other report.',1)
        refreshed = self.store.series_state(occurrence['series_id'])
        self.assertEqual(refreshed['open_tasks'][0]['acceptance_review'],original_review)

    def test_corrected_source_acceptance_never_reopens_a_completed_task(self):
        occurrence,session,source,task = self.accepted_task()
        self.capture(2,text='The report was delivered and complete.')
        self.publish(task=dict(id=task['id'],owner='Alice',status='completed'))
        before = self.store.series_state(occurrence['series_id'])
        evidence = before['closed_tasks'][0]['completion_evidence']
        self.correct(session,source['id'],'Alice did not accept at the first meeting.',1)
        after = self.store.series_state(occurrence['series_id'])
        self.assertEqual(after['open_tasks'],[])
        self.assertEqual(after['closed_tasks'][0]['status'],'completed')
        self.assertEqual(after['closed_tasks'][0]['completion_evidence'],evidence)
        self.correct(session,source['id'],'Alice still did not accept in the first meeting.',2)
        refreshed = self.store.series_state(occurrence['series_id'])
        self.assertEqual(refreshed['open_tasks'],[])
        self.assertEqual(refreshed['closed_tasks'][0]['status'],'completed')
        self.assertEqual(refreshed['closed_tasks'][0]['acceptance_review']['snapshot_revision'],3)
        self.assertEqual(refreshed['closed_tasks'][0]['completion_evidence'],evidence)

    def test_correction_to_another_source_in_same_session_keeps_acceptance(self):
        occurrence,session = self.capture(text='Budget approved.',additional_text='Alice accepts the report.')
        lease = self.store.claim_postmeeting('host')
        entries = self.store.postmeeting_input(lease)['source']['entries']
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(owner='Alice',sources=[entries[1]['id']],
            acceptance_evidence=dict(source_id=entries[1]['id'],quote=entries[1]['text']))))
        before = self.store.series_state(occurrence['series_id'])
        self.correct(session,entries[0]['id'],'Budget was not approved.',1)
        after = self.store.series_state(occurrence['series_id'])
        self.assertEqual(after['revision'],before['revision'])
        self.assertEqual(after['open_tasks'][0]['acceptance_evidence'],before['open_tasks'][0]['acceptance_evidence'])

    def test_independent_human_acceptance_wins_over_reinterpreted_source_and_closed_task_stays_closed(self):
        occurrence,session,source,task = self.accepted_task()
        args = dict(series_id=occurrence['series_id'],task_id=task['id'],action='accept',
            values={key:task[key] for key in ('text','owner','date')},expected_revision=1,
            request_id='1'*32,confirmed=True,note='Independently checked with Alice.')
        accepted = self.store.apply_task_action(**args)
        self.correct(session,source['id'],'Alice did not accept in this meeting.',1)
        lease = self.store.claim_postmeeting('host')
        c1 = self.store.postmeeting_input(lease)['source']['active_correction_ids'][0]
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(id=task['id'],owner='Alice',
            sources=[c1],acceptance_evidence=None)))
        current = self.store.series_state(occurrence['series_id'])
        self.assertEqual(current['open_tasks'][0]['acceptance_evidence'],accepted['open_tasks'][0]['acceptance_evidence'])
        self.correct(session,source['id'],'Alice accepts the report after clarification.',2)
        lease = self.store.claim_postmeeting('host')
        data = self.store.postmeeting_input(lease)['source']
        c2 = next(entry for entry in data['entries'] if entry['id'] in data['active_correction_ids'])
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(id=task['id'],owner='Alice',sources=[c2['id']],
            acceptance_evidence=dict(source_id=c2['id'],quote=c2['text']))))
        current = self.store.series_state(occurrence['series_id'])
        self.assertEqual(current['open_tasks'][0]['acceptance_evidence']['request_id'],'1'*32)
        self.store.apply_task_action(**{**args,'action':'complete','expected_revision':current['revision'],'request_id':'2'*32})
        self.correct(session,source['id'],'Alice did not accept in the original meeting.',3)
        self.assertEqual(self.store.series_state(occurrence['series_id'])['open_tasks'],[])
        self.assertEqual(self.store.series_state(occurrence['series_id'])['closed_tasks'][0]['status'],'completed')

    def test_correction_revokes_inherited_acceptance_even_when_task_is_omitted(self):
        for omit in (False,True):
            with self.subTest(omit=omit):
                occurrence,session = self.capture(day=3 if omit else 1,text='Alice accepts the report.')
                lease = self.store.claim_postmeeting('host')
                source = self.store.postmeeting_input(lease)['source']['entries'][0]
                self.store.publish_postmeeting(lease,self.completed(lease,task=dict(owner='Alice',text='Report '+str(omit),
                    acceptance_evidence=dict(source_id=source['id'],quote=source['text']))))
                before = self.store.series_state(occurrence['series_id'])
                task = before['open_tasks'][-1]
                self.store.add_postmeeting_correction(session,[dict(correction_of=source['id'],
                    text='Alice did not accept the report.',timestamp='2026-11-04T10:00:00Z',source_revision=1)],reason='Correction')
                lease = self.store.claim_postmeeting('host')
                correction = self.store.postmeeting_input(lease)['source']['active_correction_ids'][0]
                self.store.publish_postmeeting(lease,self.completed(lease,task=None if omit else dict(
                    id=task['id'],text=task['text'],owner='Alice',sources=[correction],acceptance_evidence=None)))
                current = next(t for t in self.store.series_state(occurrence['series_id'])['open_tasks'] if t['id']==task['id'])
                self.assertEqual(current['commitment_kind'],'suggested')
                self.assertIsNone(current.get('acceptance_evidence'))
                self.assertEqual(current['acceptance_review']['invalidated_source_ref']['source_id'],source['id'])
                historical = self.store.series_state(occurrence['series_id'],before['revision'])
                self.assertEqual(next(t for t in historical['open_tasks'] if t['id']==task['id'])['commitment_kind'],'committed')
    def test_source_status_update_keeps_owner_and_due_when_not_repeated(self):
        occurrence,_ = self.capture(text='Owner accepts report due 2026-11-06.')
        self.publish(task=dict(owner='Owner',date='2026-11-06'))
        task = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        self.capture(2,text='Report complete.')
        self.publish(task=dict(id=task['id'],status='completed'))
        completed = self.store.series_state(occurrence['series_id'])['closed_tasks'][0]
        self.assertEqual((completed['owner'],completed['date']),('Owner','2026-11-06'))

    def test_revised_owner_date_requires_existing_id_and_fabricated_quote_cannot_commit(self):
        occurrence,_ = self.capture()
        self.publish(task={})
        self.capture(2,text='Owner accepts report.')
        lease = self.store.claim_postmeeting('host')
        with self.assertRaisesRegex(ValueError,'existing_item_requires_stable_id'):
            self.store.publish_postmeeting(lease,self.completed(lease,task=dict(owner='Owner',date='2026-11-06')))
        task = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        source = self.store.postmeeting_input(lease)['source']['entries'][0]
        with self.assertRaisesRegex(ValueError,'invalid_task_evidence'):
            self.store.publish_postmeeting(lease,self.completed(lease,task=dict(id=task['id'],owner='Owner',
                acceptance_evidence=dict(source_id=source['id'],quote='Owner accepts imaginary work.'))))
        self.assertEqual(self.store.series_state(occurrence['series_id'])['revision'],1)

    def test_reminders_are_configurable_read_projections_and_owner_change_revokes_acceptance(self):
        from datetime import datetime, timezone
        occurrence, _ = self.capture()
        self.publish(task={})
        series = occurrence['series_id']
        task = self.store.series_state(series)['open_tasks'][0]
        args = dict(series_id=series,task_id=task['id'],action='accept',
            values=dict(text=task['text'],owner='Owner',date='2026-11-01'),
            expected_revision=1,request_id='d'*32,confirmed=True,note='Verified acceptance.')
        accepted = self.store.apply_task_action(**args)
        stamp = datetime.fromisoformat(accepted['published_at'])
        future = stamp.replace(year=stamp.year+1).astimezone(timezone.utc)
        projected = self.store.task_workspace(series,now=future)
        self.assertIn('超過 14 天未確認',projected['tasks'][0]['confirmation_reasons'])
        self.store.update_settings(dict(task_stale_days=0))
        self.assertFalse(any('天未確認' in reason for reason in self.store.task_workspace(series,now=future)['tasks'][0]['confirmation_reasons']))
        self.assertEqual(self.store.series_state(series)['revision'],2)
        changed = self.store.apply_task_action(**{**args,'action':'update','request_id':'e'*32,
            'expected_revision':2,'values':{**args['values'],'owner':'Other owner'}})
        self.assertEqual(changed['open_tasks'][0]['commitment_kind'],'suggested')
        with self.assertRaisesRegex(ValueError,'task_request_conflict'):
            self.store.apply_task_action(**{**args,'note':'Different replay'})
        with self.assertRaisesRegex(ValueError,'task_confirmation_required'):
            self.store.apply_task_action(**{**args,'request_id':'f'*32,'expected_revision':3,'confirmed':False})

    def test_source_completion_without_explicit_quote_stays_pending_and_quoted_acceptance_is_traceable(self):
        occurrence, _ = self.capture(text='Synthetic owner: I accept preparing the report.')
        lease = self.store.claim_postmeeting('host')
        source = self.store.postmeeting_input(lease)['source']['entries'][0]
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(owner='Synthetic owner',
            acceptance_evidence=dict(source_id=source['id'],quote=source['text']))))
        task = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        self.assertEqual(task['commitment_kind'],'committed')
        self.assertEqual(task['acceptance_evidence']['kind'],'meeting_source')
        self.capture(2,text='Report might be complete, ask the owner.')
        lease = self.store.claim_postmeeting('host')
        self.store.publish_postmeeting(lease,self.completed(lease,task=dict(
            id=task['id'],owner='Synthetic owner',status='completed',completion_evidence=None)))
        pending = self.store.series_state(occurrence['series_id'])
        self.assertEqual(pending['closed_tasks'],[])
        self.assertEqual(pending['open_tasks'][0]['status'],'open')
        self.assertIn('completion_proposal',pending['open_tasks'][0])

    def test_human_accept_update_complete_is_fenced_idempotent_and_survives_next_meeting(self):
        from datetime import datetime,timezone
        from unittest.mock import patch
        class WorkflowClock(datetime):
            current=datetime(2026,11,1,12,tzinfo=timezone.utc)
            @classmethod
            def now(cls,tz=None):
                return cls.current.astimezone(tz) if tz is not None else cls.current.replace(tzinfo=None)
        clock=patch('teams_transcription.task_workflow.datetime',WorkflowClock)
        clock.start();self.addCleanup(clock.stop)
        occurrence, _ = self.capture()
        self.publish(task={})
        series = occurrence['series_id']
        task = self.store.series_state(series)['open_tasks'][0]
        self.assertEqual(self.store.task_workspace(series)['tasks'][0]['display_status'],'待確認')
        args = dict(series_id=series, task_id=task['id'], action='accept',
            values=dict(text=task['text'],owner='Synthetic owner',date='2026-11-06'),
            expected_revision=1,request_id='a'*32,confirmed=True,note='Owner accepted in person.')
        first = self.store.apply_task_action(**args)
        self.assertEqual(first['revision'],2)
        self.assertEqual(first['open_tasks'][0]['last_operator_evidence']['request_id'],'a'*32)
        self.assertEqual(self.store.apply_task_action(**args),first)
        self.assertEqual(self.store.task_workspace(series)['tasks'][0]['display_status'],'待處理')
        with self.assertRaisesRegex(ValueError,'series_revision_changed'):
            self.store.apply_task_action(**{**args,'request_id':'b'*32})
        WorkflowClock.current=datetime(2026,11,2,12,tzinfo=timezone.utc)
        self.capture(2,text='Other topic. Task omitted.')
        self.publish()
        carried = self.store.series_state(series)['open_tasks'][0]
        self.assertEqual(carried['id'],task['id'])
        self.assertEqual(carried['acceptance_evidence']['kind'],'human_confirmation')
        self.assertEqual(carried['last_changed_at'],first['open_tasks'][0]['last_changed_at'])
        done = self.store.apply_task_action(**{**args,'action':'complete','expected_revision':3,
            'request_id':'c'*32,'note':'Operator verified delivery.'})
        self.assertEqual(done['open_tasks'],[])
        self.assertEqual(done['closed_tasks'][0]['completion_evidence']['note'],'Operator verified delivery.')
        self.assertEqual(len(self.store.list_postmeeting_notifications()),2)
        self.assertEqual(len(self.store.task_action_history(series,task['id'])),2)
