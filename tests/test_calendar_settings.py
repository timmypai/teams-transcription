"""Authenticated same-origin settings form; no Calendar/OAuth/provider reads."""
import json
import unittest
import test_admin_web as fixture
from teams_transcription.assistant_store import AssistantStoreError


class CalendarSettingsTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdminWebTests.asyncSetUp
    login = fixture.AdminWebTests.login

    async def test_readonly_primary_form_creates_read_only_calendar_without_grant_or_poll(self):
        csrf = await self.login()
        response = await self.client.get('/settings')
        self.assertIn('id="calendar-settings"',response.text)
        self.assertIn('action="/actions/calendar-readonly"',response.text)
        self.assertEqual(self.daemon.store.list_calendars(),[])
        response = await self.client.post('/actions/calendar-readonly',data=dict(csrf=csrf,
            calendar_ref='primary',revision='0',enabled='on'),headers={'Origin':fixture.ORIGIN})
        self.assertEqual(response.status_code,303,response.text)
        rows = self.daemon.store.list_calendars()
        self.assertEqual(len(rows),1)
        self.assertEqual((rows[0]['calendar_id'],rows[0]['account_id']),('primary','default'))
        self.assertTrue(rows[0]['enabled'])
        self.assertFalse(rows[0]['recording_authorized'] or rows[0]['speaking_authorized'])
        self.assertTrue(self.daemon.store.settings()['scheduler_paused'])
        self.assertEqual(self.token_calls,[])
        self.assertIsNone(self.daemon.status()['active_session_id'])

    async def test_exact_revision_no_identity_input_or_media_grant_and_unpaused_refuse(self):
        csrf = await self.login()
        row = self.daemon.store.save_calendar(dict(calendar_id='PRIVATE-calendar-id',name='PRIVATE-name',enabled=False,
            recording_authorized=True,speaking_authorized=False))
        base = dict(csrf=csrf,calendar_ref=row['id'],revision=str(row['revision']),enabled='on')
        for changes in ({'revision':'0'},{'calendar_id':'PRIVATE-other'},{'recording_authorized':'on'}):
            response = await self.client.post('/actions/calendar-readonly',data={**base,**changes},headers={'Origin':fixture.ORIGIN})
            self.assertEqual(response.status_code,409 if 'revision' in changes else 422)
            self.assertEqual(self.daemon.store.get_calendar(row['id']),row)
        response = await self.client.post('/actions/calendar-readonly',data=base,headers={'Origin':fixture.ORIGIN})
        self.assertEqual(response.status_code,303)
        saved = self.daemon.store.get_calendar(row['id'])
        self.assertTrue(saved['enabled'] and saved['recording_authorized'])
        self.assertFalse(saved['speaking_authorized'])
        self.daemon.store.update_settings({'scheduler_paused':False})
        response = await self.client.post('/actions/calendar-readonly',data={**base,'revision':str(saved['revision'])},
            headers={'Origin':fixture.ORIGIN})
        self.assertEqual(response.status_code,422)
        self.assertEqual(self.daemon.store.get_calendar(row['id']),saved)
        self.assertEqual(self.token_calls,[])
