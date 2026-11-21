"""Authenticated schedule action gives a useful refusal without issuing binding."""
import unittest
import time

import test_admin_web as fixture
from test_assistant_daemon import iso


class TargetConsentUITests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixture.AdminWebTests.asyncSetUp
    login=fixture.AdminWebTests.login
    post=fixture.AdminWebTests.post

    async def test_schedule_expired_consent_shows_reason_and_does_not_write_binding(self):
        await self.login()
        now=time.time()
        row=self.daemon.store.create_manual_occurrence(dict(title='Synthetic expired target',
            join_url='https://teams.microsoft.com/meet/12345',start_at=iso(now-3600),end_at=iso(now-1800),
            recording_authorized=True,speaking_authorized=True))
        page=await self.client.get('/schedule')
        self.assertIn('/actions/target-consent',page.text)
        response=await self.post('target-consent',dict(id=row['id'],revision=1,present='on',media='on'))
        self.assertEqual(response.status_code,422)
        self.assertIn('場次不在可入會時段',response.text)
        self.assertIn('occurrence_outside_join_window',response.text)
        self.assertEqual(self.daemon.store.get_occurrence(row['id']),row)
