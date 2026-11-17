import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from teams_transcription.assistant_store import AssistantStore
from teams_transcription.calendar_client import GoogleCalendarClient, CalendarError, READONLY_SCOPE, normalize_event, teams_link

START = '2026-10-14T01:00:00Z'
END = '2026-10-14T04:00:00Z'
CALLBACK = 'http://localhost:8080/google/callback'
LINK = 'https://teams.microsoft.com/meet/12345'


def event(ident='one', **values):
    return {'id':ident,'summary':'Controlled meeting','start':{'dateTime':START},
        'end':{'dateTime':END},'description':LINK,**values}


class CalendarNormalizationTests(unittest.TestCase):
    def test_structured_video_link_wins_and_text_ambiguity_fails_closed(self):
        self.assertEqual(teams_link(event(conferenceData={'entryPoints':[
            {'entryPointType':'video','uri':LINK}]},description='https://teams.microsoft.com/meet/67890')),(LINK,'valid'))
        self.assertEqual(teams_link(event(description=LINK+' https://teams.microsoft.com/meet/67890')),(None,'ambiguous'))
        self.assertEqual(teams_link(event(description='https://teams.microsoft.com/not-a-meeting')),(None,'invalid'))
        self.assertEqual(teams_link(event(description='<a href="'+LINK+'">Join here</a>')),(LINK,'valid'))
        self.assertEqual(teams_link(event(description='https://teams.microsoft.com.evil.invalid/meet/12345')),(None,'missing'))

    def test_recurring_original_identity_and_dst_offsets_are_not_current_start(self):
        first = normalize_event(event(recurringEventId='parent',originalStartTime={
            'dateTime':'2026-11-01T01:30:00-04:00','timeZone':'America/New_York'},
            start={'dateTime':'2026-11-01T01:30:00-05:00'}))
        self.assertEqual(first['original_start'],'2026-11-01T05:30:00+00:00')
        self.assertEqual(first['start_at'],'2026-11-01T06:30:00+00:00')
        naive = normalize_event(event(start={'dateTime':'2026-11-01T01:30:00','timeZone':'America/New_York'}))
        self.assertIsNone(naive['start_at'])
        all_day = normalize_event(event(start={'date':'2026-10-14'},end={'date':'2026-10-15'}))
        self.assertTrue(all_day['all_day'])
        self.assertIsNone(all_day['start_at'])

    def test_minimal_cancellation_keeps_existing_recurring_occurrence_identity(self):
        with tempfile.TemporaryDirectory() as root:
            store = AssistantStore(root)
            calendar = store.save_calendar(dict(calendar_id='primary',name='Fixture'))
            original = normalize_event(event(recurringEventId='parent',originalStartTime={'dateTime':START}))
            occurrence = store.sync_occurrences(calendar['id'],[original])[0]
            cancelled = normalize_event({'id':'one','status':'cancelled'})
            updated = store.sync_occurrences(calendar['id'],[cancelled])[0]
            self.assertEqual((updated['id'],updated['series_id']),(occurrence['id'],occurrence['series_id']))
            self.assertEqual(updated['state'],'cancelled')
            self.assertEqual(updated['original_start'],occurrence['original_start'])


class CalendarProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.config = self.root/'client.json'
        self.credentials = self.root/'credentials.json'
        self.write(self.config,{'web':{'client_id':'fixture-client','client_secret':'fixture-secret','redirect_uris':[CALLBACK]}})

    def write(self,path,value):
        path.write_text(json.dumps(value))
        os.chmod(path,0o600)

    def client(self,handler):
        client = GoogleCalendarClient(self.credentials,client_config_path=self.config,
            transport=httpx.MockTransport(handler),clock=lambda:1000.)
        self.addAsyncCleanup(client.close)
        return client

    def authorize(self,expired=False):
        self.write(self.credentials,dict(access_token='fixture-old',refresh_token='fixture-refresh',
            expires_at=0 if expired else 10000,scope=READONLY_SCOPE))

    async def test_code_exchange_persists_private_offline_authorization(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200,json={'access_token':'fixture-access','refresh_token':'fixture-refresh',
                'expires_in':3600,'scope':READONLY_SCOPE,'token_type':'Bearer'})
        client = self.client(handler)
        auth = parse_qs(urlsplit(client.authorization_url(CALLBACK,'a'*32)).query)
        self.assertEqual(auth['scope'],[READONLY_SCOPE])
        self.assertEqual(auth['access_type'],['offline'])
        with self.assertRaisesRegex(CalendarError,'oauth_redirect_not_registered'):
            client.authorization_url('https://evil.invalid/callback','a'*32)
        state = await client.exchange_code('fixture-code',CALLBACK)
        self.assertTrue(state['authorized'])
        stored = json.loads(self.credentials.read_text())
        self.assertEqual(stored['expires_at'],4600)
        self.assertEqual(self.credentials.stat().st_mode&0o777,0o600)
        self.assertNotIn('fixture-access',json.dumps(state))
        self.assertEqual(str(requests[0].url),'https://oauth2.googleapis.com/token')

    async def test_empty_first_page_and_remaining_page_are_consumed_without_sync_token(self):
        self.authorize()
        requests = []
        def handler(request):
            requests.append(request)
            self.assertEqual(request.url.params['singleEvents'],'true')
            self.assertEqual(request.url.params['showDeleted'],'true')
            self.assertNotIn('syncToken',request.url.params)
            if 'pageToken' not in request.url.params:
                return httpx.Response(200,json={'items':[],'nextPageToken':'page-two'})
            return httpx.Response(200,json={'items':[event('second-page-event')]})
        events = await self.client(handler).list_events('fixture@example.invalid',START,END)
        self.assertEqual([value['event_id'] for value in events],['second-page-event'])
        self.assertEqual(events[0]['join_url'],LINK)
        self.assertEqual(len(requests),2)

    async def test_expired_credentials_refresh_once_for_concurrent_readers(self):
        self.authorize(expired=True)
        refreshes = []
        async def handler(request):
            if request.url.host=='oauth2.googleapis.com':
                refreshes.append(request)
                await asyncio.sleep(0)
                return httpx.Response(200,json={'access_token':'fixture-new','expires_in':3600,'scope':READONLY_SCOPE})
            if request.headers.get('Authorization')!='Bearer fixture-new':
                return httpx.Response(401,json={})
            return httpx.Response(200,json={'items':[event()]})
        client = self.client(handler)
        results = await asyncio.gather(client.list_events('primary',START,END),client.list_events('primary',START,END))
        self.assertEqual([values[0]['event_id'] for values in results],['one','one'])
        self.assertEqual(len(refreshes),1)
        self.assertEqual(json.loads(self.credentials.read_text())['refresh_token'],'fixture-refresh')

    async def test_revoked_refresh_grant_requires_reauthorization_and_stops_reads(self):
        self.authorize(expired=True)
        paths = []
        def handler(request):
            paths.append(request.url.host)
            return httpx.Response(400,json={'error':'invalid_grant','error_description':'private provider text'})
        client = self.client(handler)
        with self.assertRaisesRegex(CalendarError,'google_reauthorization_required'):
            await client.list_events('primary',START,END)
        self.assertFalse(client.status()['authorized'])
        self.assertNotIn('private provider text',json.dumps(client.status()))
        with self.assertRaisesRegex(CalendarError,'google_authorization_required'):
            await client.list_events('primary',START,END)
        self.assertEqual(paths,['oauth2.googleapis.com'])

    async def test_repeated_page_token_rejects_incomplete_data(self):
        self.authorize()
        client = self.client(lambda request:httpx.Response(200,json={'items':[event()],'nextPageToken':'repeated'}))
        with self.assertRaisesRegex(CalendarError,'invalid_calendar_pagination'):
            await client.list_events('primary',START,END)

    async def test_404_before_join_is_a_minimal_cancellation(self):
        self.authorize()
        client = self.client(lambda request:httpx.Response(404,json={}))
        value = await client.get_event('primary','removed-event')
        self.assertEqual((value['event_id'],value['status']),('removed-event','cancelled'))
        self.assertIsNone(value['join_url'])

    async def test_public_or_symlink_credentials_are_rejected_without_request(self):
        self.authorize()
        os.chmod(self.credentials,0o644)
        requests = []
        client = self.client(lambda request:requests.append(request))
        with self.assertRaisesRegex(CalendarError,'private_calendar_configuration_invalid'):
            await client.list_events('primary',START,END)
        self.assertEqual(requests,[])


if __name__=='__main__':
    unittest.main()
