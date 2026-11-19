"""Calendar refresh must preserve same-occurrence consent and persistent denial."""
import unittest
from teams_transcription.assistant_store import AssistantStoreError
import test_teams_local_control as fixture


class TargetCalendarPolicyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.LocalControlTests.asyncSetUp
    event=fixture.LocalControlTests.event
    until=fixture.LocalControlTests.until

    def calendar_target(self):
        row=self.store.get_calendar(self.calendar_row['id'])
        self.store.save_calendar({**{k:row[k] for k in ('id','account_id','calendar_id','name','enabled',
            'recording_authorized','join_early_seconds','max_lateness_seconds','revision')},'speaking_authorized':True})
        self.store.sync_occurrences(row['id'],[dict(event_id='fresh-calendar',title='Synthetic calendar',
            start_at=self.event('fresh-calendar')['start']['dateTime'],end_at=self.event('fresh-calendar')['end']['dateTime'],
            status='confirmed',all_day=False,link_status='valid',join_url='https://teams.microsoft.com/meet/12345')])
        return next(x for x in self.store.list_occurrences() if x['event_id']=='fresh-calendar')

    def refresh(self,target,**changes):
        value={k:v for k,v in target.items() if k not in {'id','calendar_ref','series_id','state','revision',
            'target_control_consent','target_speaking_revoked'}}
        return self.store.sync_occurrences(target['calendar_ref'],[{**value,**changes}])[0]

    async def test_unchanged_calendar_reread_keeps_fresh_binding_and_real_edit_invalidates(self):
        target=self.calendar_target()
        consent=self.store.issue_target_consent(target['id'],target['revision'],present=True,media=True)
        current=self.store.get_occurrence(target['id'])
        refreshed=self.refresh(current)
        self.assertEqual(refreshed['revision'],consent['revision'])
        policy=dict(version='teams-single-v1',request_sha256=consent['request_sha256'],
            maximum_seconds=None,occurrence_revision=consent['revision'])
        self.store.validate_target_consent(target['id'],policy)
        self.refresh(refreshed,title='Real source revision changed')
        with self.assertRaises(AssistantStoreError):self.store.validate_target_consent(target['id'],policy)

    async def test_refresh_never_regrants_persisted_occurrence_output_denial(self):
        target=self.calendar_target()
        consent=self.store.issue_target_consent(target['id'],target['revision'],present=True,media=True)
        lease=self.store.claim_run(target['id'],'synthetic',acceptance=dict(version='teams-single-v1',
            request_sha256=consent['request_sha256'],maximum_seconds=None,occurrence_revision=consent['revision']),target_control=True)
        self.store.revoke_target_output(lease['session_id'],consent['request_sha256'])
        current=self.store.get_occurrence(target['id'])
        refreshed=self.refresh(current)
        self.assertFalse(refreshed['speaking_authorized'])
