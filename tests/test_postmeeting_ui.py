"""Rendered synthetic postmeeting states; no Google, model or meeting calls."""
import asyncio
import copy
import hashlib
import json
import re
import socket
import tempfile
import unittest
from pathlib import Path

import uvicorn
import httpx
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from teams_transcription.admin_ui import calendar_page
from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials
from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.browser import Browser
from teams_transcription.daemon import AssistantDaemon
from teams_transcription.meeting_store import canonical_json
from teams_transcription.postmeeting_adapter import CompletedPostmeeting
from teams_transcription.storage import Session


class PostmeetingUIHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = AssistantSettings(data_root=str(root),port=18437,
            admin_password_file=str(root/'config/password.json'),
            admin_session_secret_file=str(root/'config/session.key'),
            google_credentials_file=str(root/'config/google.json'))
        initialize_admin_credentials(self.settings,'synthetic-ui-password')
        self.daemon = AssistantDaemon(self.settings)
        self.store = self.daemon.store
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(
            app=create_admin_app(self.daemon,self.settings)),base_url='http://127.0.0.1:18437')
        self.addAsyncCleanup(self.client.aclose)
        self.calendar = self.store.save_calendar(dict(account_id='synthetic-account',
            calendar_id='synthetic-calendar@example.invalid',name='Synthetic calendar',
            enabled=True,recording_authorized=True))

    async def login(self):
        response = await self.client.get('/login')
        token = re.search('name="csrf" value="([a-f0-9]{64})"',response.text).group(1)
        result = await self.client.post('/login',data=dict(csrf=token,password='synthetic-ui-password'),
            headers={'Origin':'http://127.0.0.1:18437'})
        self.assertEqual(result.status_code,303)
        home = await self.client.get('/')
        return re.search('name="csrf" value="([a-f0-9]{64})"',home.text).group(1)

    def capture(self, event, day=2):
        start = f'2026-11-{day:02d}T09:00:00+00:00'
        occurrence = self.store.sync_occurrences(self.calendar['id'],[dict(event_id=event,
            title='Synthetic meeting',status='confirmed',link_status='valid',all_day=False,
            recurring_event_id='synthetic-parent',original_start=start,start_at=start,
            end_at=f'2026-11-{day:02d}T10:00:00+00:00',join_url='https://teams.microsoft.com/meet/12345')])[0]
        lease = self.store.claim_run(occurrence['id'],'synthetic-ui-capture')
        session = Session(self.settings.data_root,lease['session_id'],lease=lease).create(dict(
            provider='codex',source='meeting',prior_context=self.store.get_run(lease['session_id'])['prior_context']))
        session.append_text('Synthetic immutable original',0)
        self.store.finish_run(lease,'stopped','operator_stop')
        return occurrence,session

    def publish(self, *, text='Synthetic current decision', decision=False, task=False, source_id=None):
        lease = self.store.claim_postmeeting('synthetic-ui-adapter')
        data = self.store.postmeeting_input(lease)
        source = source_id or data['source']['entries'][0]['id']
        item = dict(text=text,owner='',date='',sources=[source])
        result = dict(summary={key:([item] if key=='重點' else [])
            for key in ('重點','決策','待辦','未決問題')},
            conclusions=[dict(id=None,text=text,status='active',sources=[source])] if decision else [],
            tasks=[dict(id=None,text='Synthetic stable unfinished work',owner='',date='',status='open',sources=[source])] if task else [])
        completed = CompletedPostmeeting(result,'controlled-ui-test-model','synthetic-thread','synthetic-turn',
            hashlib.sha256(canonical_json(data).encode()).hexdigest())
        self.store.publish_postmeeting(lease,completed)
        return data

    async def test_latest_projection_keeps_selected_meeting_history_and_durable_notice(self):
        first,session = self.capture('synthetic-first')
        self.publish(decision=True,task=True)
        second,later = self.capture('synthetic-second',9)
        self.publish(text='Synthetic later meeting summary')
        await self.login()
        response = await self.client.get('/api/meetings/'+first['id'])
        self.assertEqual(response.status_code,200)
        detail = response.json()
        self.assertEqual(detail['series_state']['revision'],2)
        self.assertEqual(detail['series_state']['session_id'],later.ident)
        self.assertEqual(detail['summary']['重點'][0]['text'],'Synthetic current decision')
        self.assertEqual(detail['summary']['postmeeting_state']['snapshot_revision'],1)
        self.assertEqual(len(detail['series_state']['open_tasks']),1)
        self.assertEqual([item['text'] for item in detail['transcript']],['Synthetic immutable original'])
        self.assertEqual(set(detail['postmeeting'][0]),{
            'id','state','error_code','source_sha256','source_version','snapshot_revision','updated_at'})
        self.assertNotIn('token',json.dumps(detail))
        history = await self.client.get('/history/'+session.ident)
        self.assertIn('Synthetic current decision',history.text)
        self.assertNotIn('Synthetic later meeting summary',history.text)
        self.assertIn('controlled-ui-test-model',history.text)
        latest = await self.client.get('/series/'+first['series_id'])
        self.assertIn('第 2 版',latest.text)
        self.assertIn('Synthetic stable unfinished work',latest.text)
        self.assertNotIn('/actions/task',latest.text)
        notices = await self.client.get('/history')
        self.assertEqual(notices.text.count('/actions/acknowledge-postmeeting'),2)

    async def test_operator_task_form_persists_acceptance_and_recovers_stale_submission(self):
        occurrence,_ = self.capture('synthetic-task')
        self.publish(task=True)
        csrf = await self.login()
        task = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        path = '/tasks/'+occurrence['series_id']+'/'+task['id']
        edit = await self.client.get(path)
        self.assertEqual(edit.status_code,200)
        self.assertIn('確認承接',edit.text)
        request_id = re.search('name="request_id" value="([a-f0-9]{32})"',edit.text).group(1)
        values = dict(csrf=csrf,series_id=occurrence['series_id'],id=task['id'],expected_revision='1',
            request_id=request_id,task_action='accept',text=task['text'],owner='Synthetic owner',
            date='2026-11-06',note='Operator checked acceptance.',confirmed='on')
        denied = await self.client.post('/actions/task-workflow',data=values,headers={'Origin':'https://other.invalid'})
        self.assertEqual(denied.status_code,403)
        accepted = await self.client.post('/actions/task-workflow',data=values,headers={'Origin':str(self.client.base_url).rstrip('/')})
        self.assertEqual(accepted.status_code,303)
        self.assertEqual(self.store.series_state(occurrence['series_id'])['revision'],2)
        replay = await self.client.post('/actions/task-workflow',data=values,headers={'Origin':str(self.client.base_url).rstrip('/')})
        self.assertEqual(replay.status_code,303)
        stale = await self.client.post('/actions/task-workflow',data={**values,'request_id':'f'*32,'owner':'Unsaved owner'},
            headers={'Origin':str(self.client.base_url).rstrip('/')})
        self.assertEqual(stale.status_code,409)
        self.assertIn('Unsaved owner',stale.text)
        self.assertIn('重新載入最新版本',stale.text)
        self.assertIn('name="expected_revision" value="1"',stale.text)

    async def test_series_page_and_task_form_keep_one_revision_across_concurrent_action(self):
        await self.check_snapshot_boundary('series')

    async def test_task_form_revision_and_history_match_visible_fields_during_concurrent_update(self):
        await self.check_snapshot_boundary('task')

    async def check_snapshot_boundary(self, page_name):
        from contextlib import ExitStack
        from unittest.mock import patch
        occurrence,_ = self.capture('snapshot-race')
        self.publish(task=True,decision=True)
        await self.login()
        series = occurrence['series_id']
        task = self.store.series_state(series)['open_tasks'][0]
        # The old app reads state then tasks separately. The new app returns
        # one snapshot; publish a concurrent action immediately after that read.
        for page_name in (page_name,):
            before = self.store.series_state(series)
            revision = before['revision']
            state_read = self.store.series_state
            snapshot_read = getattr(self.store,'task_workspace_snapshot',None)
            fired = []
            def commit_after_read():
                if not fired:
                    fired.append(True)
                    self.store.apply_task_action(series_id=series,task_id=task['id'],
                        action='update' if page_name=='task' else 'complete',
                        values=dict(text=task['text'],owner='New owner',date='2026-11-06'),
                        expected_revision=revision,request_id=('a' if page_name=='task' else 'b')*32,
                        confirmed=True,note='boundary update '+page_name)
            def old_boundary(*args,**kwargs):
                value = state_read(*args,**kwargs)
                if snapshot_read is None:commit_after_read()
                return value
            def snapshot_boundary(*args,**kwargs):
                value = snapshot_read(*args,**kwargs)
                commit_after_read()
                return value
            with ExitStack() as stack:
                stack.enter_context(patch.object(self.store,'series_state',side_effect=old_boundary))
                if snapshot_read is not None:
                    stack.enter_context(patch.object(self.store,'task_workspace_snapshot',side_effect=snapshot_boundary))
                path = '/tasks/'+series+'/'+task['id'] if page_name=='task' else '/series/'+series
                response = await self.client.get(path)
            self.assertTrue(fired)
            self.assertEqual(response.status_code,200)
            if page_name=='task':
                self.assertIn('name="expected_revision" value="1"',response.text)
                self.assertIn('name="owner" value=""',response.text)
                self.assertNotIn('boundary update task',response.text)
                refreshed = await self.client.get(path)
                self.assertIn('name="expected_revision" value="2"',refreshed.text)
                self.assertIn('name="owner" value="New owner"',refreshed.text)
            else:
                self.assertIn('第 '+str(revision)+' 版',response.text)
                open_section = response.text.partition('<h3>未完成待辦</h3>')[2].partition('<details>')[0]
                self.assertIn(task['text'],open_section)
                refreshed = await self.client.get(path)
                self.assertIn('第 '+str(revision+1)+' 版',refreshed.text)
                self.assertIn(task['text'],refreshed.text)
                self.assertIn(task['text']+' · 已完成',refreshed.text)

    async def test_valid_lf_completion_note_is_accepted(self):
        occurrence,_ = self.capture('lf-note')
        self.publish(task=True)
        csrf = await self.login()
        task = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        response = await self.client.post('/actions/task-workflow',data=dict(csrf=csrf,
            series_id=occurrence['series_id'],id=task['id'],expected_revision='1',request_id='8'*32,
            task_action='complete',text=task['text'],owner='',date='',confirmed='on',note='Verified delivery.\nAccepted by owner.'),
            headers={'Origin':str(self.client.base_url).rstrip('/')})
        self.assertEqual(response.status_code,303)
        self.assertEqual(self.store.task_action_history(occurrence['series_id'],task['id'])[-1]['note'],
            'Verified delivery.\nAccepted by owner.')

    async def test_unresolved_corrections_show_latest_evidence_in_http_and_b_snapshot(self):
        from teams_transcription.b_response import MeetingStoreMinutesReader
        from teams_transcription.response_runtime import _AsyncStore
        occurrence,session = self.capture('continuous-corrections')
        lease = self.store.claim_postmeeting('fixture')
        data = self.store.postmeeting_input(lease)
        source = data['source']['entries'][0]
        summary = {key:[] for key in ('重點','決策','待辦','未決問題')}
        result = dict(summary=summary,conclusions=[],tasks=[dict(id=None,text='Synthetic work',owner='Synthetic',
            date='',status='open',sources=[source['id']],acceptance_evidence=dict(source_id=source['id'],quote=source['text']))])
        self.store.publish_postmeeting(lease,CompletedPostmeeting(result,'controlled-ui-test-model','thread','turn',
            hashlib.sha256(canonical_json(data).encode()).hexdigest()))
        task = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        await self.login()
        corrections = []
        for revision,content in [(1,'First pending correction'),(2,'Latest unresolved correction')]:
            correction = dict(correction_of=source['id'],text=content,
                timestamp=f'2026-11-02T11:0{revision}:00Z',source_revision=revision)
            self.store.add_postmeeting_correction(session.ident,[correction],reason='Continuous correction')
            corrections.append(correction)
        state = self.store.series_state(occurrence['series_id'])
        review = state['open_tasks'][0]['acceptance_review']
        response = await self.client.get('/tasks/'+occurrence['series_id']+'/'+task['id'])
        self.assertEqual(response.status_code,200)
        self.assertIn('Latest unresolved correction',response.text)
        self.assertNotIn('First pending correction',response.text)
        self.assertIn('#source-'+review['replacement_sources'][0]['source_id'],response.text)
        projected = (await self.client.get('/api/meetings/'+occurrence['id'])).json()['task_workspace']['tasks'][0]
        self.assertEqual(projected['acceptance_review'],review)
        b = await MeetingStoreMinutesReader(_AsyncStore(self.settings.data_root))(session.ident)
        task_record = next(r for r in b['records'] if r['locator'].get('historical_context',{}).get('kind')=='series_task')
        self.assertEqual(task_record['locator']['historical_context']['acceptance_review'],review)
        self.assertEqual(task_record['locator']['historical_context']['commitment_kind'],'suggested')
        self.store.add_postmeeting_correction(session.ident,[corrections[0]],reason='Continuous correction')
        self.assertEqual(self.store.series_state(occurrence['series_id']),state)

    async def test_multiline_notes_normalize_escape_and_preserve_errors_for_every_action(self):
        occurrence,_ = self.capture('multiline')
        self.publish(task=True)
        csrf = await self.login()
        series = occurrence['series_id']
        origin = {'Origin':str(self.client.base_url).rstrip('/')}
        for index,action in enumerate(('accept','update','complete','cancel')):
            with self.subTest(action=action):
                if action=='cancel':
                    self.capture('new-cycle',9)
                    self.publish(task=True)
                current = self.store.series_state(series)
                task = current['open_tasks'][0]
                values = dict(csrf=csrf,series_id=series,id=task['id'],expected_revision=str(current['revision']),
                    request_id=f'{index+1:032x}',task_action=action,text=task['text'],owner='Named owner',
                    date='2026-11-06',confirmed='on',note='First line\nBad\x00control')
                invalid = await self.client.post('/actions/task-workflow',data=values,headers=origin)
                self.assertEqual(invalid.status_code,400)
                self.assertIn('含不支援的控制字元',invalid.text)
                self.assertNotIn('不能只留空白',invalid.text)
                self.assertIn('name="owner" value="Named owner"',invalid.text)
                self.assertEqual(self.store.series_state(series)['revision'],current['revision'])
                values['note'] = '<b>Checked</b>\r\nSecond line'
                valid = await self.client.post('/actions/task-workflow',data=values,headers=origin)
                self.assertEqual(valid.status_code,303)
                values['note'] = '<b>Checked</b>\nSecond line'
                replay = await self.client.post('/actions/task-workflow',data=values,headers=origin)
                self.assertEqual(replay.status_code,303)
                history = self.store.task_action_history(series,task['id'])
                self.assertEqual(history[-1]['note'],'<b>Checked</b>\nSecond line')
                rendered = await self.client.get('/tasks/'+series+'/'+task['id'])
                self.assertIn('&lt;b&gt;Checked&lt;/b&gt;\nSecond line',rendered.text)
                self.assertNotIn('<b>Checked</b>',rendered.text)

    async def test_note_validation_reports_required_length_and_control_errors(self):
        occurrence,_ = self.capture('note-errors')
        self.publish(task=True)
        csrf = await self.login()
        task = self.store.series_state(occurrence['series_id'])['open_tasks'][0]
        values = dict(csrf=csrf,series_id=occurrence['series_id'],id=task['id'],expected_revision='1',
            request_id='9'*32,task_action='update',text=task['text'],owner='',date='')
        for note,message in [(' \n ','不能只留空白'),('x'*2001,'最多 2000 字元'),('text\tbad','控制字元'),('text\r','控制字元'),('text\x85bad','控制字元')]:
            response = await self.client.post('/actions/task-workflow',data={**values,'note':note},
                headers={'Origin':str(self.client.base_url).rstrip('/')})
            self.assertEqual(response.status_code,400)
            self.assertIn(message,response.text)
        valid = await self.client.post('/actions/task-workflow',data={**values,'note':'x'*1998+'\r\nZ'},
            headers={'Origin':str(self.client.base_url).rstrip('/')})
        self.assertEqual(valid.status_code,303)
        self.assertEqual(len(self.store.task_action_history(occurrence['series_id'],task['id'])[-1]['note']),2000)

    async def test_notification_ack_keeps_origin_csrf_and_is_idempotent(self):
        occurrence,session = self.capture('synthetic-first')
        self.publish()
        notice, = self.store.list_postmeeting_notifications()
        token = await self.login()
        for headers,csrf in [({'Origin':'https://untrusted.invalid'},token),
                ({'Origin':'http://127.0.0.1:18437'},'invalid-csrf')]:
            response = await self.client.post('/actions/acknowledge-postmeeting',
                data=dict(id=notice['id'],csrf=csrf),headers=headers)
            self.assertEqual(response.status_code,403)
            self.assertEqual(len(self.store.list_postmeeting_notifications()),1)
        for _ in range(2):
            response = await self.client.post('/actions/acknowledge-postmeeting',
                data=dict(id=notice['id'],csrf=token),headers={'Origin':'http://127.0.0.1:18437'})
            self.assertEqual(response.status_code,303)
        self.assertEqual(self.store.list_postmeeting_notifications(),[])
        self.assertEqual(len(self.store.list_postmeeting_notifications(unread_only=False)),1)
        self.assertEqual(self.store.list_postmeeting_jobs(session.ident)[0]['state'],'notified')
        self.assertEqual(len(self.store.list_runs()),1)

    async def test_correction_citations_link_to_append_only_source_history(self):
        occurrence,session = self.capture('synthetic-first')
        self.publish()
        base = self.store.list_postmeeting_snapshots(session.ident)[0]['entries'][0]['id']
        self.store.add_postmeeting_correction(session.ident,[dict(correction_of=base,
            text='Synthetic explicit correction',timestamp='2026-11-02T10:02:00Z',source_revision=1)],
            reason='Synthetic source correction')
        snapshot = self.store.list_postmeeting_snapshots(session.ident)[-1]
        corrected = snapshot['entries'][-1]['id']
        self.publish(text='Synthetic correction summary',source_id=corrected)
        await self.login()
        response = await self.client.get('/history/'+session.ident)
        self.assertEqual(response.status_code,200)
        self.assertIn('Synthetic immutable original',response.text)
        self.assertIn('Synthetic explicit correction',response.text)
        self.assertIn('Synthetic correction summary',response.text)
        self.assertIn('href="#source-'+corrected+'"',response.text)
        self.assertEqual(response.text.count('id="source-'+corrected+'"'),1)
        self.assertIn('不是 provider final',response.text)


class PostmeetingUIBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_latest_state_and_pending_handoff_remain_distinct_from_history(self):
        meeting = dict(id='fixture-occurrence',title='Synthetic series meeting',kind='series',
            series_id='fixture-series',series_name='Synthetic series',state='completed',
            state_label='已結束',session_id='fixture-current',is_live=False,
            start_at='2026-11-02T09:00:00+00:00',end_at='2026-11-02T10:00:00+00:00',
            start_date='2026-11-02',end_date='2026-11-02',start_time='09:00',end_time='10:00')
        item = dict(id='decision-one',text='Latest valid conclusion',sources=['S0001'])
        detail = dict(meeting=meeting,session_id='fixture-current',updated_at='2026-11-02T10:01:00Z',
            controls={},response=None,source_version=3,completeness='partial',cleanup_review_required=False,
            previous=dict(date='2026-10-26',title='Older meeting',session_id='fixture-old',
                conclusions=[dict(text='Old conclusion for explicit history',sources=['S0002'])]),
            tasks=[dict(id='legacy-task',text='Legacy task must not duplicate current state',status='open')],
            series_meetings=[meeting],qa=[],summary=None,summary_job=None,
            transcript=[dict(id='S0001',text='Synthetic original transcript',timestamp='2026-11-02T09:01:00Z',
                source_state='application_segment',quality='unfinalized',revision=0)],
            series_state=dict(series_id='fixture-series',revision=4,session_id='fixture-published',
                occurrence_id='fixture-previous',source_sha256='a'*64,source_version=2,
                conclusions=[item],open_tasks=[dict(id='task-stable-one',text='Carry unfinished work',
                    owner='',date='',status='open',origin_session_id='fixture-old',
                    source_refs=[dict(session_id='fixture-old',source_id='S0002')])],
                changes=[dict(text='Existing decision was superseded',sources=['S0001'])],
                summary={key:[] for key in ('重點','決策','待辦','未決問題')},
                published_at='2026-11-02T10:00:00Z'),
            postmeeting=[dict(id='handoff-one',state='ready',error_code='external_dispatch_unavailable',
                source_version=3,source_sha256='b'*64,updated_at='2026-11-02T10:01:00Z')])
        current = copy.deepcopy(detail)
        assets = Path(__file__).resolve().parents[1]/'teams_transcription/static'

        async def index(request):
            return calendar_page(dict(authenticated=True,csrf='synthetic-csrf'),'UTC',
                initial_occurrence_id='fixture-occurrence')

        async def calendar(request):
            return JSONResponse(dict(meetings=[meeting],series=[],undated_count=0,today='2026-11-02',
                scheduler_paused=True,google=dict(authorized=False)))

        async def meeting_detail(request):
            return JSONResponse(current)

        async def asset(request):
            return FileResponse(assets/request.path_params['name'])

        app = Starlette(routes=[Route('/',index),Route('/api/calendar',calendar),
            Route('/api/meetings/fixture-occurrence',meeting_detail),Route('/assets/{name}',asset)])
        sock = socket.socket()
        sock.bind(('127.0.0.1',0))
        sock.listen(128)
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,access_log=False,log_level='critical'))
        serving = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(5):
                while not server.started:
                    if serving.done():
                        await serving
                    await asyncio.sleep(.01)
            async with Browser() as runtime:
                errors = []
                page = await runtime.browser.new_page(viewport=dict(width=1440,height=1000))
                page.on('pageerror',lambda error:errors.append(str(error)))
                await page.goto(f'http://127.0.0.1:{port}/')
                await page.get_by_text('Latest valid conclusion',exact=True).wait_for()
                self.assertEqual(await page.title(),'行事曆 · 小白會議')
                self.assertEqual(await page.locator('#meeting-title').inner_text(),'Synthetic series meeting')
                self.assertIn('第 4 版',await page.locator('#series-revision').inner_text())
                self.assertFalse(await page.get_by_text('Old conclusion for explicit history',exact=True).is_visible())
                self.assertFalse(await page.get_by_text('Legacy task must not duplicate current state',exact=True).is_visible())
                self.assertIn('等待外部整理',await page.locator('#postmeeting-status').inner_text())
                self.assertIn('尚未接通',await page.locator('#postmeeting-status').inner_text())
                self.assertEqual(await page.locator('#task-list select').count(),0)
                self.assertEqual(await page.locator('#task-list a').first.get_attribute('href'),
                    '/history/fixture-old#source-S0002')
                await page.locator('#previous-section > summary').click()
                self.assertTrue(await page.get_by_text('Old conclusion for explicit history',exact=True).is_visible())
                await page.locator('#previous-section > summary').click()
                await page.locator('#tab-summary').click()
                self.assertIn('等待外部整理',await page.locator('#summary-note').inner_text())
                self.assertEqual(await page.locator('#summary-list').inner_text(),'')
                await page.screenshot(path='/tmp/postmeeting-ui-desktop.png',full_page=True)
                current['postmeeting'][0].update(state='running',error_code=None)
                await page.locator('#refresh').click()
                await page.wait_for_function("document.querySelector('#postmeeting-status').textContent.includes('外部整理進行中')")
                await page.set_viewport_size(dict(width=390,height=844))
                await page.locator('#mobile-open').click()
                self.assertEqual(await page.locator('#expand-meeting').get_attribute('aria-expanded'),'true')
                await page.locator('#tab-transcript').click()
                self.assertTrue(await page.get_by_text('Synthetic original transcript',exact=True).is_visible())
                self.assertFalse(await page.evaluate('document.documentElement.scrollWidth > window.innerWidth'))
                await page.screenshot(path='/tmp/postmeeting-ui-mobile.png',full_page=True)
                self.assertEqual(errors,[])
                await page.close()
        finally:
            server.should_exit = True
            done,_ = await asyncio.wait({serving},timeout=5)
            if not done:
                serving.cancel()
                await asyncio.gather(serving,return_exceptions=True)
            sock.close()
