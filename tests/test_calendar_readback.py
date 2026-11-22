"""Real authenticated management poll/schedule boundary, synthetic Google transport."""
import json
from html.parser import HTMLParser
from pathlib import Path
import time
import unittest
from unittest.mock import patch

import httpx
import test_admin_web as fixture
from test_assistant_daemon import iso
from teams_transcription.calendar_client import READONLY_SCOPE


class Metadata(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.value = None
        self.rows = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if attrs.get('id') == 'calendar-readback':
            self.value = json.loads(attrs['data-readback'])
        if tag == 'tr' and 'data-occurrence-id' in attrs:
            self.rows.append(attrs)


class CalendarReadbackTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AdminWebTests.asyncSetUp
    login = fixture.AdminWebTests.login

    async def poll(self):
        response = await self.client.get('/')
        csrf = fixture.csrf(response) if response.status_code == 200 else await self.login()
        response = await self.client.post('/actions/poll', data={'csrf':csrf}, headers={'Origin':fixture.ORIGIN})
        self.assertEqual(response.status_code, 303)
        response = await self.client.get('/schedule')
        value = Metadata(response.text).value
        self.assertIsNotNone(value, 'poll must expose actual read evidence, not absence of errors')
        self.assertNotRegex(json.dumps(value), 'fixture|PRIVATE|https?://')
        return value

    def enable(self):
        self.daemon.store.save_calendar(dict(calendar_id='PRIVATE-calendar', name='PRIVATE-name', enabled=True,
            recording_authorized=True, speaking_authorized=True))

    def authorize(self, status=200):
        path = Path(self.settings.google_credentials_file)
        path.write_text(json.dumps(dict(access_token='PRIVATE-access',refresh_token='PRIVATE-refresh',
            expires_at=time.time()+3600,scope=READONLY_SCOPE)))
        path.chmod(0o600)
        def provider(request):
            self.token_calls.append(request)
            return httpx.Response(status, json={'items':[]})
        self.addAsyncCleanup(self.daemon.calendar.client.aclose)
        self.daemon.calendar.client = httpx.AsyncClient(transport=httpx.MockTransport(provider))
        self.addAsyncCleanup(self.daemon.calendar.client.aclose)

    async def test_zero_enabled_is_not_attempted_or_ready(self):
        self.authorize()
        value = await self.poll()
        self.assertEqual(value['enabled_calendar_count'], 0)
        self.assertEqual(value['read'], dict(attempted=0,succeeded=0,failed=0,event_count=0,completed=True))
        self.assertFalse(value['ready'])
        self.assertEqual(self.token_calls, [])

    async def test_unconfigured_and_unauthorized_are_distinct(self):
        self.enable()
        value = await self.poll()
        self.assertEqual(value['google'], dict(configured=True, authorized=False,
            reauthorization_required=True,error_code='google_authorization_required'))
        self.assertFalse(value['ready'])
        Path(self.settings.google_client_config_file).unlink()
        value = await self.poll()
        self.assertFalse(value['google']['configured'])
        self.assertEqual(value['google']['error_code'], 'google_oauth_client_required')
        self.assertFalse(value['ready'])

    async def test_successful_empty_read_is_proven_and_failure_is_not_ready(self):
        self.enable()
        self.authorize()
        value = await self.poll()
        self.assertGreaterEqual(len(self.token_calls), 1)
        self.assertEqual(value['read'], dict(attempted=1,succeeded=1,failed=0,event_count=0,completed=True))
        self.assertTrue(value['ready'])
        prior = value['poll_id']
        self.authorize(500)
        value = await self.poll()
        self.assertNotEqual(value['poll_id'], prior)
        self.assertEqual(value['read'], dict(attempted=1,succeeded=0,failed=1,event_count=0,completed=True))
        self.assertFalse(value['ready'])
        self.assertEqual(value['error_codes'], ['calendar_request_failed'])

    async def test_schedule_projects_existing_join_window_without_expired_joinable(self):
        await self.login()
        now = time.time()
        ids = []
        for offset in (7200, -60, -1900, -7200):
            row = self.daemon.store.create_manual_occurrence(dict(title='PRIVATE',
                join_url='https://teams.microsoft.com/meet/12345',start_at=iso(now+offset),
                end_at=iso(now+offset+3600),recording_authorized=True,speaking_authorized=True))
            ids.append(row['id'])
        response = await self.client.get('/schedule')
        rows = {row['data-occurrence-id']:row for row in Metadata(response.text).rows}
        self.assertEqual([rows[i].get('data-joinable') for i in ids], ['false','true','false','false'])
        self.assertEqual([rows[i].get('data-discoverable') for i in ids], ['true','true','false','false'])

    async def test_missing_status_error_code_is_not_explicit_success(self):
        self.enable()
        self.authorize()
        with patch.object(self.daemon.calendar,'status',return_value=dict(configured=True,authorized=True,reauthorization_required=False)):
            value = await self.poll()
        self.assertFalse(value['ready'])
        self.assertEqual(value['google']['error_code'], 'calendar_poll_failed')
