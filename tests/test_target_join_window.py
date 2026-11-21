"""Owner binding admission at the real SQLite public interface; no live Join."""
import unittest
from unittest.mock import patch

from teams_transcription.assistant_store import AssistantStoreError
import test_teams_local_control as fixture
from test_assistant_daemon import iso


class TargetJoinWindowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.LocalControlTests.asyncSetUp
    event=fixture.LocalControlTests.event
    until=fixture.LocalControlTests.until

    async def test_expired_binding_is_refused_without_revision_or_consent_write(self):
        occurrence=self.store.create_manual_occurrence(dict(title='Synthetic expired target',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(self.now-3600),
            end_at=iso(self.now-1800),recording_authorized=True,speaking_authorized=True))
        with self.assertRaisesRegex(AssistantStoreError,'^occurrence_outside_join_window$'):
            self.store.issue_target_consent(occurrence['id'],1,present=True,media=True)
        self.assertEqual(self.store.get_occurrence(occurrence['id']),occurrence)
        self.assertEqual(self.store.list_runs(),[])

    async def test_binding_uses_same_early_late_and_end_boundaries_as_join(self):
        self.now=int(self.now)  # exact second avoids float-to-ISO microsecond rounding
        calendar=self.store.get_calendar(self.calendar_row['id'])
        self.store.save_calendar({**{key:calendar[key] for key in ('id','account_id','calendar_id','name',
            'enabled','recording_authorized','revision')},'speaking_authorized':True,
            'join_early_seconds':120,'max_lateness_seconds':300})
        # start is fixed; early/late endpoints are inclusive, end is exclusive.
        for index,(offset,end_offset,allowed) in enumerate([
                (-121,600,False),(-120,600,True),(0,600,True),(300,600,True),
                (301,600,False),(200,200,False)]):
            with self.subTest(offset=offset,end=end_offset):
                row=self.store.sync_occurrences(calendar['id'],[dict(event_id='window-'+str(index),
                    title='Synthetic time boundary',start_at=iso(self.now),end_at=iso(self.now+end_offset),
                    status='confirmed',all_day=False,link_status='valid',
                    join_url='https://teams.microsoft.com/meet/12345')])[0]
                with patch('teams_transcription.target_consent.time.time',return_value=self.now+offset):
                    if allowed:
                        consent=self.store.issue_target_consent(row['id'],row['revision'],present=True,media=True)
                        self.assertEqual(consent['revision'],row['revision']+1)
                        self.assertEqual(self.store.get_occurrence(row['id'])['target_control_consent']['request_sha256'],
                            consent['request_sha256'])
                    else:
                        with self.assertRaisesRegex(AssistantStoreError,'^occurrence_outside_join_window$'):
                            self.store.issue_target_consent(row['id'],row['revision'],present=True,media=True)
                        self.assertEqual(self.store.get_occurrence(row['id']),row)
                        self.events[row['event_id']]={**self.event(row['event_id'],self.now),
                            'end':{'dateTime':iso(self.now+end_offset)}}
                        self.now+=offset
                        try:
                            with self.assertRaisesRegex(AssistantStoreError,'^occurrence_outside_join_window$'):
                                await self.daemon.join(row['id'])
                        finally:self.now-=offset
        self.assertEqual(self.store.list_runs(),[])
