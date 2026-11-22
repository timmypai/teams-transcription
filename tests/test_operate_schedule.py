"""Observable schedule metadata for the reviewed browser operations."""
import time
import unittest

import test_admin_web as fixture
from test_assistant_daemon import iso


class OperateScheduleTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdminWebTests.asyncSetUp
    login = fixture.AdminWebTests.login

    async def test_schedule_exposes_both_dated_edges_and_timezone(self):
        await self.login()
        row = self.daemon.store.create_manual_occurrence(dict(title='Synthetic private title',
            join_url='https://teams.microsoft.com/meet/12345',
            start_at=iso(time.time()+60), end_at=iso(time.time()+3600),
            recording_authorized=True, speaking_authorized=True))
        response = await self.client.get('/schedule')
        self.assertIn('data-occurrence-id="'+row['id']+'"', response.text)
        self.assertIn('data-state="pending"', response.text)
        self.assertIn('data-timezone="'+self.settings.timezone+'"', response.text)
        self.assertIn('datetime="'+row['start_at']+'"', response.text)
        self.assertIn('datetime="'+row['end_at']+'"', response.text)

    async def test_schedule_reports_failed_sync_without_private_calendar_key(self):
        await self.login()
        self.daemon.calendar_errors['PRIVATE-CALENDAR-ID'] = 'calendar_poll_failed'
        response = await self.client.get('/schedule')
        self.assertIn('日曆同步失敗，請到設定檢查 Google 連線。', response.text)
        self.assertNotIn('PRIVATE-CALENDAR-ID', response.text)


if __name__ == '__main__':
    unittest.main()
