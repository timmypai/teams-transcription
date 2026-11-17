"""Synthetic same-origin audit of the real admin app; disposable credentials and data.

Run in the documented sandboxed Docker test harness, never with production mounts.
No Google, Teams, provider or external model execution. JSON lines on stdin drive
the admin DOM and save screenshots/results under /tmp/audit-shots.
"""
import asyncio, hashlib, json, os, socket, tempfile
from pathlib import Path
from urllib.parse import urlsplit
import uvicorn
from playwright.async_api import async_playwright, expect, Error as PlaywrightError
from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.admin_web import create_admin_app,initialize_admin_credentials
from teams_transcription.daemon import AssistantDaemon
from teams_transcription.storage import Session
from teams_transcription.meeting_store import canonical_json
from teams_transcription.postmeeting_adapter import CompletedPostmeeting
from teams_transcription.browser import launch_sandboxed_chromium

PASSWORD='synthetic-audit-password-only'
SHOTS=Path('/tmp/audit-shots')
SHOTS.mkdir()

def seed(store, root):
    calendar=store.save_calendar(dict(account_id='synthetic-audit',calendar_id='audit@example.invalid',
        name='合成驗收行事曆（非真 Google）',enabled=True,recording_authorized=True,speaking_authorized=False))
    def capture(event_id,date,start_hour,text):
        occurrence=store.sync_occurrences(calendar['id'],[dict(event_id=event_id,
            recurring_event_id='synthetic-parent',original_start=f'{date}T{start_hour:02d}:00:00Z',
            start_at=f'{date}T{start_hour:02d}:00:00+00:00',end_at=f'{date}T{start_hour+1:02d}:00:00+00:00',
            title='合成驗收例會（非真會議）',status='confirmed',all_day=False,link_status='valid',
            join_url='https://teams.microsoft.com/meet/12345')])[0]
        lease=store.claim_run(occurrence['id'],'synthetic-audit')
        session=Session(root,lease['session_id'],lease=lease).create(dict(provider='mock',source='synthetic',
            prior_context=store.get_run(lease['session_id'])['prior_context']))
        session.store.record_increment(session.ident,dict(capture_id=hashlib.sha256(event_id.encode()).hexdigest()[:32],
            receipt_sequence=1,part_index=0,text=text,received_at=f'{date}T{start_hour:02d}:30:00Z'))
        store.finish_run(lease,'stopped','operator_stop')
        return occurrence,session.ident
    def publish(decision,tasks):
        lease=store.claim_postmeeting('synthetic-audit-fixture')
        value=store.postmeeting_input(lease)
        source=value['source']['entries'][0]['id']
        summary={key:[] for key in ('重點','決策','待辦','未決問題')}
        summary['重點']=[dict(text='合成驗收資料：本次已確認交付調整，登入流程仍需檢查。',owner='',date='',sources=[source])]
        summary['未決問題']=[dict(text='合成待確認：手機登入的錯誤提示是否足夠清楚？',owner='',date='',sources=[source])]
        state=value['prior']
        result=dict(summary=summary,
            conclusions=[dict(id=state['conclusions'][0]['id'] if state.get('conclusions') else None,
                text=decision,status='active',sources=[source])],tasks=[])
        for item in tasks:
            evidence = dict(source_id=source,quote=value['source']['entries'][0]['text']) if item.get('owner') and not state.get('revision') else None
            result['tasks'].append({**item,'sources':[source],**({'acceptance_evidence':evidence} if evidence else {})})
        completed=CompletedPostmeeting(result,'synthetic-audit-fixture-not-model',
            'synthetic-thread','synthetic-turn',hashlib.sha256(canonical_json(value).encode()).hexdigest())
        store.publish_postmeeting(lease,completed)
    first,session=capture('synthetic-previous','2026-10-26',10,
        '合成逐字稿：原定週五交付設計稿。示例負責人確認承接；預算十萬元。')
    publish('合成結論：預算十萬元，先完成設計稿。',[dict(id=None,text='交付設計稿',owner='示例負責人',
        date='2026-10-30',status='open'),dict(id=None,text='評估新的圖示方向',owner='',date='',status='open')])
    previous=store.series_state(first['series_id'])
    task=next(item for item in previous['open_tasks'] if item['text']=='交付設計稿')
    current,current_session=capture('synthetic-current','2026-11-02',10,
        '合成逐字稿：預算改為十二萬元，交付設計稿的期限改成十一月六日。登入提示仍需驗證。圖示方向尚未承接。')
    publish('合成最新結論：預算十二萬元，交付期限改為 11/6。',[
        dict(id=task['id'],text=task['text'],owner=task['owner'],date='2026-11-06',status='open')])
    pending,pending_session=capture('synthetic-pending','2026-11-02',13,
        '合成逐字稿：這一場來源已保存，但外部整理尚未執行。不得把前場結論當成本場整理完成。')
    empty=store.create_manual_occurrence(dict(title='合成空狀態：尚未開始',
        join_url='https://teams.microsoft.com/meet/12345',start_at='2026-11-03T02:00:00Z',
        end_at='2026-11-03T03:00:00Z',recording_authorized=False,speaking_authorized=False))
    return dict(previous=first,current=current,pending=pending,empty=empty,series_id=first['series_id'],
        current_session=current_session,pending_session=pending_session)

async def main():
    temporary=tempfile.TemporaryDirectory(prefix='synthetic-ui-audit-')
    root=Path(temporary.name)
    sock=socket.socket();sock.bind(('127.0.0.1',0));sock.listen(128)
    port=sock.getsockname()[1]
    settings=AssistantSettings(data_root=str(root),port=port,timezone='Asia/Taipei',
        admin_password_file=str(root/'config/password.json'),admin_session_secret_file=str(root/'config/session.key'),
        google_credentials_file=str(root/'config/google.json'))
    initialize_admin_credentials(settings,PASSWORD)
    daemon=AssistantDaemon(settings)
    fixtures=seed(daemon.store,str(root))
    assert daemon.store.settings()['scheduler_paused'] is True
    app=create_admin_app(daemon,settings)
    server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=port,access_log=False,log_level='critical'))
    serving=asyncio.create_task(server.serve(sockets=[sock]))
    while not server.started:
        if serving.done():await serving
        await asyncio.sleep(.01)
    origin=f'http://127.0.0.1:{port}'
    pw=await async_playwright().start()
    browser=None
    try:
        proxy=None
        proxy_url=os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy')
        if proxy_url:
            parsed=urlsplit(proxy_url)
            proxy=dict(server=f'{parsed.scheme}://{parsed.hostname}:{parsed.port}',bypass='127.0.0.1,localhost,[::1]')
            if parsed.username:proxy['username']=parsed.username
            if parsed.password:proxy['password']=parsed.password
        browser=await launch_sandboxed_chromium(pw.chromium,executable_path='/usr/bin/chromium',proxy=proxy)
        context=await browser.new_context(viewport=dict(width=1440,height=1000),locale='zh-TW')
        await context.route('**/*',lambda route:route.continue_() if route.request.url.startswith(origin+'/') else route.abort())
        page=await context.new_page()
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        await page.goto(origin+'/login')
        async def snapshot_state():
            return dict(url=page.url,title=await page.title(),
                headings=await page.locator('h1,h2,h3').all_text_contents(),
                controls=await page.locator('button,input,select,a').evaluate_all('''els=>els.filter(e=>e.getClientRects().length).map(e=>({tag:e.tagName,id:e.id,name:e.getAttribute('name'),role:e.getAttribute('role'),text:(e.innerText||'').slice(0,100),label:e.getAttribute('aria-label'),href:e.tagName==='A'?e.getAttribute('href'):null}))'''),
                body=(await page.locator('body').inner_text())[:7000],
                metrics=await page.evaluate('''()=>({width:innerWidth,scrollWidth:document.documentElement.scrollWidth,scrollHeight:document.documentElement.scrollHeight,viewportHeight:innerHeight,scrollY:scrollY,focus:document.activeElement?{tag:document.activeElement.tagName,id:document.activeElement.id,text:(document.activeElement.innerText||'').slice(0,100),outline:getComputedStyle(document.activeElement).outlineStyle,outlineWidth:getComputedStyle(document.activeElement).outlineWidth}:null})'''),errors=errors)
        async def state():
            # Enter can begin navigation between the individual DOM reads.
            # Retry observation only; never repeat a submitted action.
            for attempt in range(3):
                try:
                    return {**await snapshot_state(),'observation_retries':attempt}
                except PlaywrightError as error:
                    if 'Execution context was destroyed' not in str(error) or attempt==2:
                        raise
                    await page.wait_for_load_state('domcontentloaded',timeout=10000)
        print(json.dumps(dict(ready=True,origin=origin,fixtures=fixtures,state=await state()),ensure_ascii=False),flush=True)
        while True:
            line=await asyncio.to_thread(__import__('sys').stdin.readline)
            if not line:break
            command=json.loads(line)
            try:
                op=command['op']
                if op=='stop':break
                if op=='goto':
                    path=command['path']
                    if not path.startswith('/') or path.startswith('//'):raise ValueError('same_origin_only')
                    await page.goto(origin+path)
                elif op=='click':
                    locator=page.locator(command['selector'])
                    if await locator.count()!=1:raise ValueError('selector_not_unique')
                    await locator.click()
                elif op=='fill':
                    await page.locator(command['selector']).fill(PASSWORD if command.get('password') else command['value'])
                elif op=='select':await page.locator(command['selector']).select_option(command['value'])
                elif op=='check':
                    locator=page.locator(command.get('selector','body'))
                    if 'text' in command:await expect(locator).to_contain_text(command['text'])
                    if 'not_text' in command:await expect(locator).not_to_contain_text(command['not_text'])
                    if command.get('focused'):await expect(locator).to_be_focused()
                    if 'visible' in command:
                        if command['visible']:await expect(locator).to_be_visible()
                        else:await expect(locator).to_be_hidden()
                elif op=='key':await page.keyboard.press(command['key'])
                elif op=='viewport':await page.set_viewport_size(dict(width=command['width'],height=command['height']))
                elif op=='shot':
                    name=command['name']
                    if Path(name).name!=name or not name.endswith('.png'):raise ValueError('invalid_capture_name')
                    await page.wait_for_load_state('networkidle',timeout=10000)
                    await page.screenshot(path=str(SHOTS/name),full_page=command.get('full_page',True))
                elif op=='state':pass
                elif op in {'correct-fixture-acceptance','correct-fixture-acceptance-again'}:
                    # Dedicated synthetic fixture only; no generic result/SQL import.
                    state_value=daemon.store.series_state(fixtures['series_id'])
                    task=next(item for item in state_value['open_tasks'] if item['text']=='交付設計稿')
                    session_id=task['origin_session_id']
                    original=daemon.store.list_postmeeting_snapshots(session_id)[0]['entries'][0]['id']
                    revision=1 if op=='correct-fixture-acceptance' else 2
                    daemon.store.add_postmeeting_correction(session_id,[dict(correction_of=original,
                        text='合成訂正：示例負責人在原會議沒有承接設計稿。'+str(revision),
                        timestamp=f'2026-11-03T01:0{revision}:00Z',source_revision=revision)],reason='Synthetic acceptance correction')
                else:raise ValueError('unknown_operation')
                await page.wait_for_load_state('networkidle',timeout=10000)
                result=dict(ok=True,operation=command,state=await state())
                with (SHOTS/'results.jsonl').open('a') as journal:journal.write(json.dumps(result,ensure_ascii=False)+'\n')
                print(json.dumps(result,ensure_ascii=False),flush=True)
            except Exception as error:
                result=dict(ok=False,operation=command,error_type=type(error).__name__,error=str(error)[:500])
                with (SHOTS/'results.jsonl').open('a') as journal:journal.write(json.dumps(result,ensure_ascii=False)+'\n')
                print(json.dumps(result,ensure_ascii=False),flush=True)
    finally:
        if browser:await browser.close()
        await pw.stop()
        server.should_exit=True
        await asyncio.wait_for(serving,5)
        sock.close();temporary.cleanup()

asyncio.run(main())
