import json
import os
import re
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import httpx

from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials, AdminError
from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.calendar_client import GoogleCalendarClient, READONLY_SCOPE
from teams_transcription.daemon import AssistantDaemon
from teams_transcription.storage import Session

ORIGIN = 'http://127.0.0.1:18437'
PASSWORD = 'controlled-test-password'


def csrf(response):
    return re.search(r'name="csrf" value="([a-f0-9]{64})"',response.text).group(1)


class Forms(HTMLParser):
    """Read native controls rather than matching rendered wording."""
    def __init__(self, text):
        super().__init__()
        self.forms = []
        self.current = None
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag=='form':
            self.current = {'attrs':attrs,'inputs':[],'buttons':[]}
            self.forms.append(self.current)
        elif self.current is not None and tag in {'input','button'}:
            self.current['inputs' if tag=='input' else 'buttons'].append(attrs)

    def handle_endtag(self, tag):
        if tag=='form':
            self.current = None

    def review(self):
        return [form for form in self.forms if form['attrs'].get('action')=='/actions/review-capture-cleanup']


class AdminWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = AssistantSettings(data_root=str(root),port=18437,
            admin_password_file=str(root/'config/password.json'),admin_session_secret_file=str(root/'config/session.key'),
            google_credentials_file=str(root/'config/google.json'),google_client_config_file=str(root/'config/client.json'))
        initialize_admin_credentials(self.settings,PASSWORD)
        Path(self.settings.google_client_config_file).write_text(json.dumps({'web':{'client_id':'fixture-client',
            'client_secret':'fixture-client-secret','redirect_uris':[ORIGIN+'/google/callback']}}))
        os.chmod(self.settings.google_client_config_file,0o600)
        self.token_calls = []
        def provider(request):
            self.token_calls.append(request)
            return httpx.Response(200,json={'access_token':'fixture-access-private','refresh_token':'fixture-refresh-private',
                'expires_in':3600,'scope':READONLY_SCOPE})
        calendar = GoogleCalendarClient(self.settings.google_credentials_file,
            client_config_path=self.settings.google_client_config_file,transport=httpx.MockTransport(provider))
        self.daemon = AssistantDaemon(self.settings,calendar=calendar)
        self.app = create_admin_app(self.daemon,self.settings)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__,None,None,None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url=ORIGIN)
        self.addAsyncCleanup(self.client.aclose)

    async def login(self):
        response = await self.client.get('/login')
        cookie = self.client.cookies.get('assistant_session')
        logged = await self.client.post('/login',data={'csrf':csrf(response),'password':PASSWORD},headers={'Origin':ORIGIN})
        self.assertEqual(logged.status_code,303)
        self.assertNotEqual(cookie,self.client.cookies.get('assistant_session'))
        return csrf(await self.client.get('/'))

    async def post(self,action,values,token=None,origin=ORIGIN):
        token = token or csrf(await self.client.get('/'))
        return await self.client.post('/actions/'+action,data={'csrf':token,**values},headers={'Origin':origin})

    async def test_authenticated_retry_summary_rejects_unknown_and_accepts_sealed_source(self):
        token=await self.login()
        missing=await self.client.post('/actions/retry-summary',
            data={'csrf':token,'id':'a'*32},headers={'Origin':ORIGIN,'Accept':'application/json'})
        self.assertEqual(missing.status_code,404)
        self.assertEqual(missing.json(),{'error':'record_not_found'})
        occ=self.daemon.store.create_manual_occurrence(dict(title='Summary fixture',
            join_url='https://teams.microsoft.com/meet/12345',
            start_at='2026-11-23T01:00:00+00:00',end_at='2026-11-23T02:00:00+00:00',
            recording_authorized=True,speaking_authorized=False))
        lease=self.daemon.store.claim_run(occ['id'],'summary-web-fixture')
        session=Session(self.settings.data_root,lease['session_id'],lease=lease).create(dict(
            provider='mock',source='synthetic',prior_context=self.daemon.store.get_run(lease['session_id'])['prior_context']))
        session.append_text('Synthetic transcript for summary.',0)
        self.daemon.store.finish_run(lease,'stopped','operator_stop')
        response=await self.client.post('/actions/retry-summary',
            data={'csrf':token,'id':session.ident},headers={'Origin':ORIGIN,'Accept':'application/json'})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json()['ok'])
        self.assertEqual(self.daemon.store.list_summary_jobs()[0]['session_id'],session.ident)
        self.assertIn('/actions/retry-summary',(await self.client.get('/history/'+session.ident)).text)

    def interrupted_capture(self, *, signature='12345', recover=True):
        calendar = self.daemon.store.save_calendar(dict(account_id='cleanup-fixture',
            calendar_id='cleanup@example.invalid',name='Cleanup fixture',enabled=True,recording_authorized=True))
        occurrence = self.daemon.store.sync_occurrences(calendar['id'],[dict(event_id='cleanup-fixture',
            title='Interrupted fixture',start_at='2026-10-14T01:00:00+00:00',end_at='2026-10-14T04:00:00+00:00',
            status='confirmed',link_status='valid',all_day=False,join_url='https://teams.microsoft.com/meet/12345')])[0]
        lease = self.daemon.store.claim_run(occurrence['id'],'cleanup-fixture')
        session = Session(self.settings.data_root,lease['session_id'],lease=lease).create(
            dict(provider='mock',source='synthetic',
                prior_context=self.daemon.store.get_run(lease['session_id'])['prior_context']))
        session.update(pid=987654321,process_signature=signature,cleanup_status='unknown',
            private_args='fixture-private-process-arguments',private_status='fixture-private-runtime-status')
        if recover:
            self.daemon.store.recover_expired(now_boottime=lease['expires_boottime']+1)
        return session.ident

    def cleanup_reviews(self):
        return [entry for entry in self.daemon.store.audit() if entry['action']=='capture_cleanup_reviewed']

    async def test_cleanup_review_waits_for_terminal_seal_even_if_worker_is_gone(self):
        ident = self.interrupted_capture(recover=False)
        await self.login()
        with patch('teams_transcription.assistant_store.os.kill',side_effect=ProcessLookupError):
            run = self.daemon.store.get_run(ident)
            self.assertEqual(run['cleanup_review']['worker_status'],'gone')
            page = await self.client.get('/history/'+ident)
        self.assertEqual(Forms(page.text).review(),[])
        self.assertEqual(self.daemon.store.get_run(ident)['state'],'running')
        self.assertTrue(self.daemon.store.capture_interlocks())
        self.assertEqual(self.cleanup_reviews(),[])

    async def test_cleanup_review_requires_authentication_before_read_or_post(self):
        ident = self.interrupted_capture()
        for method,path in (('GET','/history/'+ident),('POST','/actions/review-capture-cleanup')):
            response = await self.client.request(method,path,data={'id':ident,'stamp':'a'*64,
                'resources_stopped':'on','assistant_left':'on','csrf':'b'*64},headers={'Origin':ORIGIN})
            self.assertEqual(response.status_code,303)
            self.assertEqual(response.headers['location'],'/login')
        self.assertTrue(self.daemon.store.capture_interlocks())
        self.assertEqual(self.cleanup_reviews(),[])

    async def test_cleanup_review_origin_csrf_and_explicit_confirmations_fence_real_store(self):
        ident = self.interrupted_capture()
        token = await self.login()
        with patch('teams_transcription.assistant_store.os.kill',side_effect=ProcessLookupError):
            stamp = self.daemon.store.get_run(ident)['cleanup_review']['stamp']
            fields = dict(id=ident,stamp=stamp,resources_stopped='on',assistant_left='on',csrf=token)
            for headers in ({'Origin':'https://evil.invalid','Referer':ORIGIN+'/'},
                    {},{'Referer':'https://evil.invalid/'}):
                response = await self.client.post('/actions/review-capture-cleanup',data=fields,headers=headers)
                self.assertEqual(response.status_code,403)
            for supplied in ('','x'*64,'非 ASCII'):
                response = await self.client.post('/actions/review-capture-cleanup',data={**fields,'csrf':supplied},
                    headers={'Origin':ORIGIN})
                self.assertEqual(response.status_code,403)
            for confirmations in ({},{'resources_stopped':'on'},{'assistant_left':'on'},
                    {'resources_stopped':'false','assistant_left':'false'},
                    {'resources_stopped':'true','assistant_left':'on'}):
                response = await self.post('review-capture-cleanup',
                    dict(id=ident,stamp=stamp,**confirmations),token=token)
                self.assertEqual(response.status_code,422)
            response = await self.post('review-capture-cleanup',{**fields,'stamp':'0'*64},token=token)
            self.assertEqual(response.status_code,422)
            self.assertTrue(self.daemon.store.get_run(ident)['cleanup_review']['required'])
        self.assertTrue(self.daemon.store.capture_interlocks())
        self.assertEqual(self.cleanup_reviews(),[])

    async def test_cleanup_review_native_form_is_unchecked_and_only_explicit_post_changes_guard(self):
        ident = self.interrupted_capture()
        await self.login()
        with patch('teams_transcription.assistant_store.os.kill',side_effect=ProcessLookupError):
            before = self.daemon.store.get_run(ident)
            source = self.daemon.store.read(ident)
            attempted_get = await self.client.get('/actions/review-capture-cleanup',params={
                'id':ident,'stamp':before['cleanup_review']['stamp'],
                'resources_stopped':'on','assistant_left':'on'})
            self.assertEqual(attempted_get.status_code,405)
            for path in ('/','/history'):
                response = await self.client.get(path)
                self.assertEqual(Forms(response.text).review(),[])
            for _ in range(2):
                response = await self.client.get('/history/'+ident)
                forms = Forms(response.text).review()
                self.assertEqual(len(forms),1)
                review = forms[0]
                self.assertEqual(review['attrs']['method'],'post')
                controls = {field['name']:field for field in review['inputs']}
                for name in ('resources_stopped','assistant_left'):
                    self.assertEqual(controls[name]['type'],'checkbox')
                    self.assertNotIn('checked',controls[name])
                for name,value in (('id',ident),('stamp',before['cleanup_review']['stamp'])):
                    self.assertEqual(controls[name]['type'],'hidden')
                    self.assertEqual(controls[name]['value'],value)
                self.assertEqual(len(review['buttons']),1)
                for secret in ('fixture-private-process-arguments','fixture-private-runtime-status','987654321'):
                    self.assertNotIn(secret,response.text)
                self.assertEqual(self.cleanup_reviews(),[])
                self.assertTrue(self.daemon.store.capture_interlocks())
            response = await self.post('review-capture-cleanup',dict(id=ident,
                stamp=controls['stamp']['value'],resources_stopped='on',assistant_left='on'),
                token=controls['csrf']['value'])
            self.assertEqual(response.status_code,303)
            self.assertEqual(response.headers['location'],'/history/'+ident)
            after = self.daemon.store.get_run(ident)
            self.assertFalse(after['cleanup_review']['required'])
            self.assertIsNotNone(after['cleanup_review']['reviewed_at'])
            self.assertEqual(after['cleanup_review']['actor'],'admin')
            for key in ('state','end_reason','error_code','ended_at','status'):
                self.assertEqual(after[key],before[key])
            current_source = self.daemon.store.read(ident)
            for key in ('completeness','transcript_state','sources','summary'):
                self.assertEqual(current_source.get(key),source.get(key))
            self.assertEqual(self.daemon.store.capture_interlocks(),[])
            self.assertFalse(self.daemon.status()['capture_interlock'])
            self.assertTrue(self.daemon.store.settings()['scheduler_paused'])
            self.assertIsNone(self.daemon.status()['active_session_id'])
            self.assertIsNone(self.daemon.status()['response'])
            self.assertEqual(len(self.cleanup_reviews()),1)
            self.assertEqual(Forms((await self.client.get('/history/'+ident)).text).review(),[])

    async def test_cleanup_review_alive_or_unproven_worker_has_no_actionable_form(self):
        ident = self.interrupted_capture()
        await self.login()
        with patch('teams_transcription.assistant_store.os.kill',return_value=None):
            for observed in ('12345',None):
                with patch('teams_transcription.assistant_store.process_signature',return_value=observed):
                    run = self.daemon.store.get_run(ident)
                    self.assertTrue(run['cleanup_review']['required'])
                    self.assertEqual(run['cleanup_review']['worker_status'],'alive' if observed else 'unknown')
                    response = await self.client.get('/history/'+ident)
                    self.assertEqual(Forms(response.text).review(),[])
                    result = await self.post('review-capture-cleanup',dict(id=ident,
                        stamp=run['cleanup_review']['stamp'],resources_stopped='on',assistant_left='on'))
                    self.assertEqual(result.status_code,422)
                    self.assertTrue(self.daemon.store.capture_interlocks())
        self.assertEqual(self.cleanup_reviews(),[])

    async def test_unauthenticated_clients_cannot_read_private_state_or_mutate_policy(self):
        self.daemon.store.save_glossary({'canonical':'Private controlled term'})
        for path in ('/','/glossary','/history','/audit','/api/status'):
            response = await self.client.get(path)
            if path.startswith('/api/'):
                self.assertEqual(response.status_code,401)
                self.assertEqual(response.json()['error'],'admin_login_required')
            else:
                self.assertEqual(response.status_code,303)
                self.assertEqual(response.headers['location'],'/login')
            self.assertNotIn('Private controlled term',response.text)
        result = await self.client.post('/actions/scheduler',data={'paused':'false','csrf':'a'*64},headers={'Origin':ORIGIN})
        self.assertEqual(result.status_code,303)
        self.assertTrue(self.daemon.store.settings()['scheduler_paused'])

    async def test_csrf_and_explicit_foreign_origin_cannot_enable_scheduler(self):
        token = await self.login()
        invalid = await self.post('scheduler',{'paused':'false'},token='非 ASCII')
        self.assertEqual(invalid.status_code,403)
        foreign = await self.client.post('/actions/scheduler',data={'paused':'false','csrf':token},
            headers={'Origin':'https://evil.invalid','Referer':ORIGIN+'/'})
        self.assertEqual(foreign.status_code,403)
        self.assertTrue(self.daemon.store.settings()['scheduler_paused'])

    async def test_logout_revokes_previous_authenticated_cookie(self):
        token = await self.login()
        old = self.client.cookies.get('assistant_session')
        result = await self.client.post('/logout',data={'csrf':token},headers={'Origin':ORIGIN})
        self.assertEqual(result.status_code,303)
        response = await self.client.get('/api/status',headers={'Cookie':'assistant_session='+old})
        self.assertEqual(response.status_code,401)
        self.assertEqual(response.json()['error'],'admin_login_required')

    async def test_session_expiry_rejects_privileged_changes(self):
        token = await self.login()
        now = self.app.state.sessions.clock()
        self.app.state.sessions.clock = lambda:now+4000
        response = await self.post('scheduler',{'paused':'false'},token=token)
        self.assertEqual(response.status_code,303)
        self.assertTrue(self.daemon.store.settings()['scheduler_paused'])

    async def test_glossary_approval_revision_and_html_escaping_are_real_store_effects(self):
        await self.login()
        payload = dict(canonical='<script>controlled</script>',kind='term',aliases='alias one\nalias two',
            pronunciation='',definition='Controlled definition',scope='global',series_id='',status='pending')
        result = await self.post('glossary',payload)
        self.assertEqual(result.status_code,303)
        entry = self.daemon.store.list_glossary()[0]
        response = await self.client.get('/glossary')
        self.assertIn('&lt;script&gt;controlled&lt;/script&gt;',response.text)
        self.assertNotIn('<script>controlled</script>',response.text)
        updated = await self.post('glossary',{**payload,'id':entry['id'],'revision':str(entry['revision']),'status':'active'})
        self.assertEqual(updated.status_code,303)
        self.assertEqual(self.daemon.store.get_glossary(entry['id'])['status'],'active')
        stale = await self.post('glossary',{**payload,'id':entry['id'],'revision':str(entry['revision']),'status':'disabled'})
        self.assertEqual(stale.status_code,409)
        self.assertEqual(self.daemon.store.get_glossary(entry['id'])['status'],'active')

    async def test_oauth_state_is_authenticated_single_use_and_tokens_never_render(self):
        token = await self.login()
        connected = await self.client.post('/google/connect',data={'csrf':token},headers={'Origin':ORIGIN})
        self.assertEqual(connected.status_code,303)
        state = parse_qs(urlsplit(connected.headers['location']).query)['state'][0]
        response = await self.client.get('/google/callback',params={'state':state,'code':'fixture-code'})
        self.assertEqual(response.status_code,303)
        self.assertTrue(self.daemon.calendar.status()['authorized'])
        replay = await self.client.get('/google/callback',params={'state':state,'code':'fixture-code'})
        self.assertEqual(replay.status_code,403)
        self.assertEqual(len(self.token_calls),1)
        for path in ('/','/settings','/api/status','/audit'):
            response = await self.client.get(path)
            for secret in ('fixture-access-private','fixture-refresh-private','fixture-client-secret','fixture-code'):
                self.assertNotIn(secret,response.text)

    async def test_large_body_and_untrusted_host_fail_without_mutation(self):
        token = await self.login()
        response = await self.client.post('/actions/scheduler',content='x'*65537,
            headers={'Origin':ORIGIN,'Content-Type':'application/x-www-form-urlencoded'})
        self.assertEqual(response.status_code,413)
        response = await self.client.get('/api/status',headers={'Host':'evil.invalid'})
        self.assertEqual(response.status_code,400)
        self.assertTrue(self.daemon.store.settings()['scheduler_paused'])

    async def test_login_rate_limit_cannot_be_reset_by_cookie_rotation(self):
        response = await self.client.get('/login')
        for _ in range(10):
            result = await self.client.post('/login',data={'csrf':csrf(response),'password':'wrong-password'},headers={'Origin':ORIGIN})
            self.assertEqual(result.status_code,401)
        self.client.cookies.clear()
        response = await self.client.get('/login')
        denied = await self.client.post('/login',data={'csrf':csrf(response),'password':PASSWORD},headers={'Origin':ORIGIN})
        self.assertEqual(denied.status_code,429)

    async def test_calendar_json_requires_login_and_private_responses_are_not_cached(self):
        for path in ('/api/calendar?start=2026-10-01&end=2026-11-01','/api/meetings/missing'):
            response = await self.client.get(path)
            self.assertEqual(response.status_code,401)
            self.assertEqual(response.json()['error'],'admin_login_required')
        await self.login()
        response = await self.client.get('/api/calendar',params={'start':'2026-10-01','end':'2026-11-01'})
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['meetings'],[])
        self.assertIn('no-store',response.headers['cache-control'])
        missing = await self.client.get('/api/meetings/missing')
        self.assertEqual(missing.status_code,404)
        self.assertEqual(missing.json()['error'],'record_not_found')

    async def test_json_control_keeps_origin_csrf_and_real_policy_mutations(self):
        token = await self.login()
        fields = {'paused':'false','csrf':token}
        forbidden = await self.client.post('/actions/scheduler',data=fields,
            headers={'Origin':'https://evil.invalid','Accept':'application/json'})
        self.assertEqual(forbidden.status_code,403)
        self.assertEqual(forbidden.json()['error'],'admin_origin_rejected')
        self.assertTrue(self.daemon.store.settings()['scheduler_paused'])
        result = await self.client.post('/actions/scheduler',data=fields,
            headers={'Origin':ORIGIN,'Accept':'application/json'})
        self.assertEqual(result.status_code,200)
        self.assertTrue(result.json()['ok'])
        self.assertFalse(self.daemon.store.settings()['scheduler_paused'])

    async def test_local_manual_time_uses_configured_zone_and_rejects_ambiguous_clock(self):
        self.settings.timezone = 'Asia/Taipei'
        token = await self.login()
        values = {'title':'Local-time meeting','join_url':'https://teams.microsoft.com/meet/12345',
            'start_at':'2026-10-28T14:30','end_at':'2026-10-28T15:30'}
        result = await self.post('manual',values,token=token)
        self.assertEqual(result.status_code,303)
        response = await self.client.get('/api/calendar',params={'start':'2026-10-28','end':'2026-10-29'})
        self.assertEqual(response.status_code,200)
        row, = response.json()['meetings']
        self.assertEqual(row['start_time'],'14:30')
        detail = (await self.client.get('/api/meetings/'+row['id'])).json()
        self.assertEqual(detail['meeting']['kind'],'single')
        self.assertIsNone(detail['previous'])
        self.assertEqual(detail['transcript'],[])
        self.assertEqual(detail['qa'],[])
        self.settings.timezone = 'America/New_York'
        for start,end,code in (('2026-11-01T01:30','2026-11-01T03:30','ambiguous_local_time'),
                ('2026-03-08T02:30','2026-03-08T04:30','invalid_local_time')):
            failed = await self.client.post('/actions/manual',data={**values,'start_at':start,'end_at':end,'csrf':token},
                headers={'Origin':ORIGIN,'Accept':'application/json'})
            self.assertEqual(failed.status_code,422)
            self.assertEqual(failed.json()['error'],code)
        self.assertEqual(len(self.daemon.store.list_occurrences()),1)

    def test_bootstrap_never_overwrites_existing_secret_or_accepts_public_password_file(self):
        original = Path(self.settings.admin_password_file).read_bytes()
        with self.assertRaisesRegex(AdminError,'admin_credentials_already_exist'):
            initialize_admin_credentials(self.settings,'another-controlled-password')
        self.assertEqual(Path(self.settings.admin_password_file).read_bytes(),original)
        os.chmod(self.settings.admin_password_file,0o644)
        with self.assertRaisesRegex(AdminError,'private_admin_credentials_required'):
            create_admin_app(self.daemon,self.settings)


if __name__=='__main__':
    unittest.main()
