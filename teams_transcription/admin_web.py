"""Private server-rendered management; no request-owned meeting execution."""
import asyncio
import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import stat
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, PlainTextResponse, Response
from starlette.routing import Route

from .calendar_readback import occurrence_availability
from .assistant_store import AssistantStoreError
from .calendar_client import CalendarError
from .meeting_store import TERMINAL
from .admin_ui import calendar_page, page as management_page
from .codex_auth import AuthError
from .web_authorization import WebChatGPTAuthorization


class AdminError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _private_read(path, maximum):
    path = Path(path)
    try:
        if path.absolute()!=path.resolve() or path.is_symlink():
            raise ValueError()
        fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077:
                raise ValueError()
            value = stream.read(maximum+1)
        if len(value)>maximum:
            raise ValueError()
        return value
    except (OSError,ValueError):
        raise AdminError('private_admin_credentials_required') from None


def password_record(password):
    if not isinstance(password,str) or not 12<=len(password)<=1024:
        raise AdminError('admin_password_length_invalid')
    salt = secrets.token_bytes(32)
    digest = hashlib.scrypt(password.encode(),salt=salt,n=32768,r=8,p=1,dklen=64,maxmem=64*1024*1024)
    return {'algorithm':'scrypt','n':32768,'r':8,'p':1,'salt':base64.b64encode(salt).decode(),
        'hash':base64.b64encode(digest).decode()}


def initialize_admin_credentials(settings, password):
    """Explicit operator bootstrap, never overwrite existing credentials."""
    record = password_record(password)
    paths = [Path(settings.admin_password_file),Path(settings.admin_session_secret_file)]
    for path in paths:
        if path.exists() or path.is_symlink():
            raise AdminError('admin_credentials_already_exist')
        if path.parent.is_symlink() or path.parent.absolute()!=path.parent.resolve():
            raise AdminError('private_admin_directory_required')
        path.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
        if path.parent.stat().st_uid!=os.getuid() or path.parent.stat().st_mode&0o077:
            raise AdminError('private_admin_directory_required')
    created = []
    try:
        for path,value in zip(paths,[(json.dumps(record)+'\n').encode(),base64.b64encode(secrets.token_bytes(32))+b'\n']):
            fd = os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600)
            created.append(path)
            with os.fdopen(fd,'wb') as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
        for parent in {path.parent for path in paths}:
            fd = os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {'admin_initialized':True}


class AdminSessions:
    cookie = 'assistant_session'

    def __init__(self, settings, clock=time.time):
        self.settings,self.clock = settings,clock
        try:
            self.password = json.loads(_private_read(settings.admin_password_file,4096))
            expected = {'algorithm','n','r','p','salt','hash'}
            if (set(self.password)!=expected or self.password['algorithm']!='scrypt'
                    or (self.password['n'],self.password['r'],self.password['p'])!=(32768,8,1)):
                raise ValueError()
            self.salt = base64.b64decode(self.password['salt'],validate=True)
            self.digest = base64.b64decode(self.password['hash'],validate=True)
            self.secret = base64.b64decode(_private_read(settings.admin_session_secret_file,256).strip(),validate=True)
            if (len(self.salt),len(self.digest),len(self.secret))!=(32,64,32):
                raise ValueError()
        except (ValueError,TypeError,KeyError):
            raise AdminError('private_admin_credentials_required') from None
        self.sessions,self.attempts = {},{}

    def _signed(self, nonce):
        return nonce+'.'+hmac.new(self.secret,nonce.encode(),'sha256').hexdigest()

    def lookup(self, token):
        now = self.clock()
        self.sessions = {key:value for key,value in self.sessions.items() if value['expires_at']>now}
        if not isinstance(token,str) or not re.fullmatch(r'[a-f0-9]{64}\.[a-f0-9]{64}',token):
            return None
        nonce = token.split('.',1)[0]
        if not hmac.compare_digest(token,self._signed(nonce)):
            return None
        return self.sessions.get(nonce)

    def create(self, authenticated=False):
        if len(self.sessions)>=1024:
            oldest = min(self.sessions,key=lambda key:self.sessions[key]['expires_at'])
            del self.sessions[oldest]
        nonce = secrets.token_hex(32)
        value = {'nonce':nonce,'authenticated':authenticated,'csrf':secrets.token_hex(32),
            'expires_at':self.clock()+self.settings.admin_session_seconds,'oauth':None}
        self.sessions[nonce] = value
        return value

    def remove(self, session):
        self.sessions.pop(session['nonce'],None)

    def set_cookie(self, response, session):
        response.set_cookie(self.cookie,self._signed(session['nonce']),max_age=self.settings.admin_session_seconds,
            httponly=True,secure=self.settings.cookie_secure,samesite='lax',path='/')

    async def verify_password(self, password, address):
        now = self.clock()
        self.attempts = {key:[stamp for stamp in values if stamp>now-600] for key,values in self.attempts.items()
            if any(stamp>now-600 for stamp in values)}
        if address not in self.attempts and len(self.attempts)>=1024:
            raise AdminError('login_rate_limited')
        attempts = self.attempts.setdefault(address,[])
        if len(attempts)>=10:
            raise AdminError('login_rate_limited')
        attempts.append(now)
        if not isinstance(password,str) or len(password)>1024:
            return False
        digest = await asyncio.to_thread(hashlib.scrypt,password.encode(),salt=self.salt,n=32768,r=8,p=1,
            dklen=64,maxmem=64*1024*1024)
        return hmac.compare_digest(digest,self.digest)




def h(value):
    return html.escape(str(value if value is not None else ''),quote=True)


def hidden(name,value):
    return '<input type="hidden" name="'+h(name)+'" value="'+h(value)+'">'


def input_field(name,label,value='',kind='text'):
    return '<label>'+h(label)+' <input type="'+h(kind)+'" name="'+h(name)+'" value="'+h(value)+'"></label>'


def checkbox(name,label,checked=False):
    return '<label><input type="checkbox" name="'+h(name)+'"'+(' checked' if checked else '')+'>'+h(label)+'</label>'


def select_field(name,label,choices,value=''):
    return '<label>'+h(label)+' <select name="'+h(name)+'">'+''.join('<option value="'+h(key)+'"'+
        (' selected' if str(key)==str(value) else '')+'>'+h(caption)+'</option>' for key,caption in choices)+'</select></label>'


def form(session,action,body,label='保存',path=None):
    target = path or '/actions/'+action
    return '<form method="post" action="'+h(target)+'">'+hidden('csrf',session['csrf'])+body+'<button>'+h(label)+'</button></form>'


def control(session,action,target,label,**values):
    return form(session,action,hidden('id',target)+''.join(hidden(key,value) for key,value in values.items()),label)


def page(title,body,session=None,*,active='settings'):
    return management_page(title,body,session,active=active)


async def bounded_form(request):
    if request.headers.get('content-type','').split(';',1)[0].lower()!='application/x-www-form-urlencoded':
        raise AdminError('unsupported_form_content_type')
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data)>65536:
            raise AdminError('request_too_large')
    try:
        parsed = parse_qs(data.decode('utf-8'),keep_blank_values=True,max_num_fields=64,strict_parsing=True)
        if any(len(values)!=1 for values in parsed.values()):
            raise ValueError()
        return {key:values[0] for key,values in parsed.items()}
    except (ValueError,UnicodeError):
        raise AdminError('invalid_form') from None


def integer(values,key,default=0):
    try:
        return int(values.get(key,str(default)))
    except (ValueError,TypeError):
        raise AdminError('invalid_form_integer') from None


class AdminGuard:
    def __init__(self, app, *, sessions, settings):
        self.app,self.sessions,self.settings = app,sessions,settings

    async def __call__(self, scope, receive, send):
        if scope['type']!='http':
            return await self.app(scope,receive,send)
        request = Request(scope,receive)
        host = request.headers.get('host','')
        try:
            hostname = urlsplit('http://'+host).hostname
        except ValueError:
            hostname = None
        allowed = {'localhost','127.0.0.1','::1'}
        if self.settings.public_base_url:
            allowed.add(urlsplit(self.settings.public_base_url).hostname)
        if hostname not in allowed:
            return await PlainTextResponse('untrusted_host',status_code=400)(scope,receive,send)
        if len(scope.get('query_string',b''))>16384:
            return await PlainTextResponse('request_too_large',status_code=413)(scope,receive,send)
        try:
            if int(request.headers.get('content-length','0'))>65536:
                return await PlainTextResponse('request_too_large',status_code=413)(scope,receive,send)
        except ValueError:
            return await PlainTextResponse('invalid_content_length',status_code=400)(scope,receive,send)
        session = self.sessions.lookup(request.cookies.get(self.sessions.cookie))
        scope.setdefault('state',{})['admin_session'] = session
        async def headers(message):
            if message['type']=='http.response.start':
                message.setdefault('headers',[]).extend([
                    (b'cache-control',b'no-store'),(b'pragma',b'no-cache'),(b'x-content-type-options',b'nosniff'),
                    (b'referrer-policy',b'strict-origin'),(b'x-frame-options',b'DENY'),
                    (b'content-security-policy',b"default-src 'none'; script-src 'self'; connect-src 'self'; style-src 'self' 'unsafe-inline'; form-action 'self' https://accounts.google.com; base-uri 'none'; frame-ancestors 'none'")])
            await send(message)
        await self.app(scope,receive,headers)


def _local_meeting_time(value, timezone_name):
    """Native datetime-local controls use the configured meeting timezone."""
    try:
        parsed = datetime.fromisoformat(value.replace('Z','+00:00'))
        zone = ZoneInfo(timezone_name)
    except (AttributeError, TypeError, ValueError, ZoneInfoNotFoundError):
        raise AdminError('invalid_timestamp') from None
    if parsed.tzinfo is not None:
        return parsed.isoformat()
    candidates = {}
    for fold in (0,1):
        aware = parsed.replace(tzinfo=zone,fold=fold)
        if aware.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None)==parsed:
            candidates[aware.utcoffset()] = aware
    if len(candidates)!=1:
        raise AdminError('ambiguous_local_time' if candidates else 'invalid_local_time')
    return next(iter(candidates.values())).isoformat()


def create_admin_app(daemon, settings, *, local_teams_control=False):
    sessions = AdminSessions(settings)
    chatgpt_auth = WebChatGPTAuthorization(daemon, settings, sessions)

    def readable_time(value):
        try:
            return datetime.fromisoformat(value.replace('Z','+00:00')).astimezone(
                ZoneInfo(settings.timezone)).strftime('%Y-%m-%d %H:%M')+' · '+settings.timezone
        except (ValueError,TypeError,AttributeError):
            return '尚無紀錄'
    from .meeting_store import MeetingStoreError
    from .rx_contract import RxContractError

    def authenticated(request):
        session = request.state.admin_session
        if not session or not session['authenticated']:
            raise AdminError('admin_login_required')
        return session

    async def submitted(request, *, login=False):
        session = request.state.admin_session
        if session is None or not login and not session['authenticated']:
            raise AdminError('admin_login_required')
        origin = settings.public_base_url.rstrip('/') if settings.public_base_url else str(request.base_url).rstrip('/')
        supplied = request.headers.get('origin')
        referer = request.headers.get('referer')
        if supplied is not None and supplied!=origin:
            raise AdminError('admin_origin_rejected')
        if supplied is None:
            parsed = urlsplit(referer or '')
            if not referer or parsed.scheme+'://'+parsed.netloc!=origin:
                raise AdminError('admin_origin_rejected')
        values = await bounded_form(request)
        csrf = values.pop('csrf','')
        if not re.fullmatch('[a-f0-9]{64}',csrf) or not hmac.compare_digest(csrf,session['csrf']):
            raise AdminError('admin_csrf_rejected')
        return session,values

    async def login(request):
        if request.method=='GET':
            session = request.state.admin_session
            if session and session['authenticated']:
                return RedirectResponse('/',status_code=303)
            session = session or sessions.create()
            response = page('管理登入','<section><p>使用操作者初始化的管理密碼。</p>'+form(session,'',
                input_field('password','管理密碼',kind='password'),'登入',path='/login')+'</section>',session)
            sessions.set_cookie(response,session)
            return response
        session,values = await submitted(request,login=True)
        if set(values)!={'password'}:
            raise AdminError('invalid_form')
        address = request.client.host if request.client else 'unknown'
        if not await sessions.verify_password(values['password'],address):
            response = page('登入失敗','<p class="error">密碼不正確，請重新登入。</p><a href="/login">重新登入</a>',session)
            response.status_code = 401
            return response
        sessions.remove(session)
        authenticated_session = sessions.create(True)
        response = RedirectResponse('/',status_code=303)
        sessions.set_cookie(response,authenticated_session)
        await asyncio.to_thread(daemon.store.audit_control,'admin','admin_login','admin')
        return response

    async def logout(request):
        session,_ = await submitted(request)
        sessions.remove(session)
        if session is chatgpt_auth.owner:
            await chatgpt_auth.close()
        response = RedirectResponse('/login',status_code=303)
        response.delete_cookie(sessions.cookie,path='/')
        return response

    async def dashboard(request):
        session = authenticated(request)
        return calendar_page(session,settings.timezone,
            initial_occurrence_id=request.query_params.get('meeting'))

    async def diagnostics(request):
        session = authenticated(request)
        status = daemon.status()
        policy = await asyncio.to_thread(daemon.store.settings)
        jobs = await asyncio.to_thread(daemon.store.list_summary_jobs)
        body = '<section><p>排程：<span class="status">'+('暫停' if policy['scheduler_paused'] else '啟用')+'</span></p>'
        body += '<p>Google：'+h(status['google'].get('error_code') or ('已授權' if status['google'].get('authorized') else '未授權'))+'</p>'
        body += '<p>背景工作：'+h(status['background_task_count'])+'；最後輪詢：'+h(readable_time(status['last_poll_at']))+'</p>'
        body += '<p class="error">'+h(status['last_error'])+'</p>'
        if status['capture_interlock']:
            body += '<p class="error">Capture 清理待確認；禁止新的入會。摘要恢復仍可執行。</p>'
        body += ('<div class="controls">'+control(session,'scheduler','settings','暫停排程',paused='true')+
            control(session,'scheduler','settings','啟用排程',paused='false')+form(session,'poll','','立即輪詢')+'</div></section>')
        if status['active_session_id']:
            run = await asyncio.to_thread(daemon.store.get_run,status['active_session_id'])
            body += '<section><h2>本場執行</h2><p>'+h(run['occurrence']['title'])+'</p><p>'+h(run['status'].get('state'))+'</p>'
            body += '<p>RX ready：'+h(run['status'].get('rx_ready'))+'；清理：'+h(run['status'].get('cleanup_status'))+'</p>'
            response = status.get('response')
            if response is not None:
                visible = {key:response.get(key) for key in
                    ('state','error_code','startup_phase','output_enabled','output_muted',
                     'startup_error','capture_policy_failure','runtime_failure',
                     'sender_selection','progress','screen_context')}
                body += '<h3>語音 B</h3><pre>'+h(json.dumps(visible,ensure_ascii=False,indent=2))+'</pre>'
            body += ('<div class="controls">'+control(session,'stop',run['session_id'],'停止入會')+
                control(session,'output',run['session_id'],'靜音',enabled='false')+
                control(session,'output',run['session_id'],'授權發聲',enabled='true')+'</div>')
            body += '<a href="/history/'+h(run['session_id'])+'">來源與摘要</a></section>'
        body += '<section><h2>會後摘要工作</h2><table><tr><th>來源 session</th><th>狀態</th><th>錯誤</th><th>操作</th></tr>'
        for job in jobs:
            retry_btn = control(session,'retry-summary',job['session_id'],'重試摘要') if job['state']!='running' else ''
            body += ('<tr><td><a href="/history/'+h(job['session_id'])+'">'+h(job['session_id'])+'</a></td><td>'+h(job['state'])+
                '</td><td>'+h(job['error_code'])+'</td><td>'+retry_btn+'</td></tr>')
        return page('進階診斷',body+'</table></section>',session)

    async def settings_page(request):
        session = authenticated(request)
        status = daemon.status()
        policy = await asyncio.to_thread(daemon.store.settings)
        calendars = await asyncio.to_thread(daemon.store.list_calendars)
        rows = calendars+[{'account_id':'default','calendar_id':'primary','name':'','enabled':False,
            'recording_authorized':False,'speaking_authorized':False,'join_early_seconds':300,'max_lateness_seconds':300}]
        body = ('<section><h2>自動加入會議</h2><p>目前'+
            ('已暫停' if policy['scheduler_paused'] else '已啟用')+
            '。只會加入已授權且符合時間的會議；暫停排程不會停止正在進行的會議。</p>'+
            '<div class="controls">'+control(session,'scheduler','settings','暫停排程',paused='true')+
            control(session,'scheduler','settings','啟用排程',paused='false')+
            form(session,'poll','','更新行事曆')+'</div></section>')
        body += '<section><h2>待辦確認提醒</h2><p>超過幾天沒有明確確認時顯示「待確認」。預設 14 天，可自行調整；0 表示停用久未更新提醒。逾期仍會提示，不會自動完成、建立事項或聯絡他人。</p>'
        body += form(session,'task-policy',input_field('task_stale_days','未確認天數（0–365）',policy.get('task_stale_days',14),'number'),'保存提醒設定')+'</section>'
        effective_screen_mode = policy.get('screen_context_mode', settings.screen_context_mode)
        effective_screen_summary = policy.get('screen_include_in_summary', settings.screen_include_in_summary)
        body += ('<section id="screen-context-settings"><h2>分享畫面理解（GPT Live 畫面問答）</h2>'
            '<p>僅在取得會議同意後啟用。原圖只暫存於記憶體（/dev/shm）並於會後清除，SQLite 僅保留衍生文字意義；變更將於下一場會議生效。</p>' +
            form(session, 'screen-policy',
                select_field('screen_context_mode', '畫面理解模式', [
                    ('off', '停用（預設；不擷取 Teams 分享畫面）'),
                    ('on_demand', 'Phase 1：按需擷取（提問時才看畫面，零背景呼叫）'),
                    ('watch_and_cache', 'Phase 2：換頁背景預解析（提問零延遲自動帶入）'),
                ], effective_screen_mode) +
                checkbox('screen_include_in_summary', '將留存於 SQLite 的分享畫面文字意義納入會後自動摘要參考', effective_screen_summary),
                '保存畫面設定') + '</section>')
        if status['capture_interlock']:
            body += '<section><h2>上次離會需要確認</h2><p>請先確認助理已離會，再恢復新的入會。</p><a href="/history">查看受阻會議與清理確認</a></section>'
        notice = session.pop('google_notice',None)
        if notice:
            body += '<p role="status">'+h('Google 授權已保存。行事曆讀取仍需另行確認。' if notice=='authorized' else
                'Google 授權未完成，既有連線未變更。可確認帳號後重新連接。')+'</p>'
        google_error = None
        try:
            daemon.google_authorization_url(redirect_uri(), 'A'*43)
        except CalendarError as error:
            google_error = error.code
        body += ('<section><h2>Google 行事曆</h2><p>'+
            ('已連接' if status['google'].get('authorized') and not status['google'].get('error_code') else '尚未連接，或需要重新授權')+
            '。連接日曆不代表已取得錄音或發聲同意；仍可使用 Teams 連結手動安排會議。</p>'+
            '<p>授權回呼網址：<code>'+h(redirect_uri())+'</code></p>')
        if google_error:
            body += ('<p class="warning">'+h(google_setup_message(google_error))+'</p>')
        else:
            body += form(session,'','','重新授權 Google Calendar' if status['google'].get('authorized') else '連接 Google Calendar',path='/google/connect')
        body += '</section>'
        provider=status.get('provider') or {}
        account={'unchecked':'尚未檢查','blocked':'需要檢查授權或提供者連線',
            'admissible_unverified':'已辨識 ChatGPT 帳號；音訊尚未驗證'}.get(provider.get('provider_status'),'尚未檢查')
        body += ('<section><h2>ChatGPT／GPT Live 授權</h2><p>帳號檢查：'+h(account)+
            '。管理密碼、Google 連線及 Teams 登入都不是 ChatGPT 授權。</p>'+
            '<p>可在此啟動官方裝置授權，再到 OpenAI 網站登入並輸入短效代碼。'+
            '授權保留在同一部署的私有持久目錄；登入成功仍須另外驗證帳號與 GPT Live 音訊收發。</p>'+
            '<a href="/chatgpt">登入或重新授權 ChatGPT</a></section>')
        body += ('<section><h2>媒體出口政策</h2><p>以下為目前有效的 HTTP/WebSocket 精確 host allowlist。空白為合法 [] deny-all；'+
            '不是缺設定，也不自動放寬。僅在排程暫停、無會議及無授權作業時由本人明確修改。'+
            '不接受 URL、萬用字元或 shell；動態 Live SDP 仍須同 thread own-start proof，這不是全域網路防火牆。</p>'+
            form(session,'media-policy','<label>每行一個獲准 host（全小寫；空白拒絕全部）<textarea name="hosts">'+
                h('\n'.join(settings.allowed_egress_hosts))+'</textarea></label>'+
                checkbox('confirmed','我明確核准這份媒體出口政策，不代表會議或 OAuth 同意'),'保存媒體政策')+'</section>')
        body += '<section id="calendar-settings" data-settings="'+h(json.dumps(dict(
            scheduler_paused=policy['scheduler_paused'],calendars=[dict(
                calendar_ref=row['id'],revision=row['revision'],primary=row['calendar_id']=='primary' and row['account_id']=='default',
                **{key:row[key] for key in ('enabled','recording_authorized','speaking_authorized',
                    'join_early_seconds','max_lateness_seconds')}) for row in calendars])))+'"><h2>Calendar 讀取設定（不授權會議）</h2>'
        readonly_rows = [(row['id'],row['revision'],row['enabled']) for row in calendars]
        if not any(row['calendar_id']=='primary' and row['account_id']=='default' for row in calendars):
            readonly_rows.append(('primary',0,False))
        for ref,revision,enabled in readonly_rows:
            body += form(session,'calendar-readonly',hidden('calendar_ref',ref)+hidden('revision',revision)+
                checkbox('enabled','啟用唯讀 Calendar',enabled),'保存唯讀設定')
        body += '</section>'
        for calendar in rows:
            fields = ''.join(hidden(key,calendar[key]) for key in ('id','revision') if key in calendar)
            fields += (input_field('account_id','資料帳號範圍',calendar['account_id'])+
                input_field('calendar_id','Google Calendar ID',calendar['calendar_id'])+input_field('name','名稱',calendar['name']))
            fields += (checkbox('enabled','啟用此 Calendar',calendar['enabled'])+
                checkbox('recording_authorized','已取得加入及錄音授權',calendar['recording_authorized'])+
                checkbox('speaking_authorized','已授權此 Calendar 場次由小白發聲',calendar['speaking_authorized']))
            fields += (input_field('join_early_seconds','提早加入秒數',calendar['join_early_seconds'],'number')+
                input_field('max_lateness_seconds','最晚加入秒數',calendar['max_lateness_seconds'],'number'))
            body += '<section><h2>'+h(calendar.get('name') or '新增 Calendar')+'</h2>'+form(session,'calendar',fields)+'</section>'
        safe = {key:getattr(settings,key) for key in ('display_name','timezone','calendar_poll_seconds','lookahead_hours',
            'max_duration_seconds','join_timeout_seconds','live_model','summary_model','vision_model',
            'screen_context_mode','screen_include_in_summary')}
        body += '<section><details><summary>進階執行設定</summary><p>由部署設定管理；此處不顯示憑證。</p><pre>'+h(
            json.dumps(safe,ensure_ascii=False,indent=2))+'</pre></details></section>'
        body += ('<section><h2>資料與管理</h2><nav><a href="/glossary">人名與術語</a> '
            '<a href="/audit">操作紀錄</a> <a href="/diagnostics">進階診斷</a></nav></section>')
        return page('設定',body,session)

    async def schedule(request):
        session = authenticated(request)
        rows = await asyncio.to_thread(daemon.store.list_occurrences)
        series = await asyncio.to_thread(daemon.store.list_series)
        choices = [('', '獨立單場會議')]+[(row['id'],row['name']) for row in series]
        policy = await asyncio.to_thread(daemon.store.settings)
        now = datetime.now(ZoneInfo(settings.timezone)).replace(second=0,microsecond=0)
        body = ('<section><h2>自動入會排程</h2><p>目前：<strong>'+
            ('已暫停' if policy['scheduler_paused'] else '已啟用')+
            '</strong>。勾選「已取得加入及錄音授權」的場次，於時間到達時將自動加入。</p><div class="controls">'+
            control(session,'scheduler','settings','啟用自動入會',paused='false')+
            control(session,'scheduler','settings','暫停自動入會',paused='true')+'</div></section>')
        body += '<section><h2>使用 Teams 連結安排會議</h2><p>時間以 '+h(settings.timezone)+' 顯示。建立場次不代表已加入；加入與記錄需先取得同意。</p>'
        fields = (input_field('title','會議名稱')+input_field('join_url','Teams 連結',kind='url')+
            input_field('start_at','開始時間',now.strftime('%Y-%m-%dT%H:%M'),kind='datetime-local')+
            input_field('end_at','結束時間',(now+timedelta(hours=1)).strftime('%Y-%m-%dT%H:%M'),kind='datetime-local')+
            select_field('series_id','接續哪一組會議',choices)+checkbox('recording_authorized','已取得加入及錄音授權')+
            checkbox('speaking_authorized','已授權本場小白發聲')+checkbox('join_now','建立後立即加入'))
        body += form(session,'manual',fields,'建立場次')+'</section>'
        sync_failed = bool(daemon.status()['calendar_errors'])
        body += ('<p role="status" id="calendar-sync" data-failed="'+str(sync_failed).lower()+'">'+
            ('日曆同步失敗，請到設定檢查 Google 連線。' if sync_failed else '日曆同步無已知錯誤。')+'</p>')
        readback = session.get('calendar_readback')
        if readback is not None:
            body += ('<p id="calendar-readback" data-readback="'+h(json.dumps(readback))+'">'+
                ('本次已成功讀取啟用的 Google Calendar。' if readback['ready'] else
                 '本次未嘗試讀取 Calendar。' if not readback['read']['attempted'] else '本次 Calendar 前置尚未就緒。')+'</p>')
        body += '<section data-timezone="'+h(settings.timezone)+'"><h2>場次</h2><table><tr><th>會議</th><th>時間／連結</th><th>狀態</th><th>操作</th></tr>'
        for row in rows:
            discoverable, joinable = occurrence_availability(row, daemon.clock())
            controls = ''
            if row['state']=='pending':
                controls += control(session,'join',row['id'],'加入')
                controls += form(session,'target-consent',hidden('id',row['id'])+hidden('revision',row['revision'])+
                    checkbox('present','我現在已在此會議；已通知準備單次加入')+
                    checkbox('media','我確認本次加入、錄音、逐字保存及助理發聲同意'),
                    '建立五分鐘 Hermes 單次控制 binding（不加入）')
            if row['state'] in {'completed','stopped','failed','interrupted','skipped'} and row.get('link_status')=='valid' and not row.get('all_day'):
                controls += control(session,'rejoin',row['id'],'重新加入')
            if row['state'] in {'pending','needs_review'}:
                controls += control(session,'skip',row['id'],'略過')
                controls += form(session,'assign-series',hidden('id',row['id'])+
                    select_field('series_id','指定系列',choices[1:],row['series_id']),'分組')
            if row['state'] != 'running':
                controls += ' <a href="/schedule/'+h(quote(row['id'],safe=''))+'/delete">刪除…</a>'
            times = '<br>'.join('<time data-edge="'+edge+'" datetime="'+h(row.get(edge+'_at'))+'">'+
                h(readable_time(row.get(edge+'_at')))+'</time>' for edge in ('start','end'))
            body += ('<tr data-occurrence-id="'+h(row['id'])+'" data-state="'+h(row['state'])+'" data-discoverable="'+str(discoverable).lower()+'" data-joinable="'+str(joinable).lower()+'"><td>'+h(row['title'])+'<br><a href="/series/'+h(row['series_id'])+'">系列記憶</a></td><td>'+times+
                '<br><span class="small">'+h(row.get('link_status'))+'</span></td><td>'+h(row['state'])+'</td><td>'+controls+'</td></tr>')
        return page('排程與人工入會',body+'</table></section>',session,active='schedule')

    async def schedule_delete_confirm(request):
        session = authenticated(request)
        ident = request.path_params['occurrence_id']
        row = await asyncio.to_thread(daemon.store.get_occurrence,ident)
        if row['state'] == 'deleted':
            raise AssistantStoreError('record_not_found')
        warning = ('此為 Google 行事曆同步場次。確認刪除後將在本機隱藏且下次輪詢不會恢復顯示，不會修改遠端 Google 行事曆。'
            if row.get('calendar_ref') else
            '此為手動建立的會議。確認刪除後將永久移除此場次及所有已封存的逐字稿與摘要紀錄，無法復原。')
        body = '<section><h2>確認刪除會議</h2><p><strong>'+h(row['title'])+'</strong></p>'
        body += '<p>時間：'+h(readable_time(row.get('start_at')))+' – '+h(readable_time(row.get('end_at')))+'</p>'
        body += '<p class="warning">'+h(warning)+'</p>'
        if row['state'] == 'running':
            body += '<p class="error">進行中的會議不能直接刪除，請先讓助理離開。</p>'
        else:
            fields = hidden('id',row['id'])+checkbox('confirm_delete','我已了解上述影響，確認刪除此會議',False)
            body += form(session,'delete',fields,'確認刪除會議')
        body += '<p><a href="/schedule">取消並返回排程</a></p></section>'
        return page('確認刪除會議',body,session,active='schedule')

    async def history(request):
        session = authenticated(request)
        runs = await asyncio.to_thread(daemon.store.list_runs)
        notifications = await asyncio.to_thread(daemon.store.list_postmeeting_notifications)
        body = '<section><h2>整理完成通知</h2>'
        if not notifications:
            body += '<p>沒有未讀的整理完成通知。</p>'
        for notice in notifications:
            body += ('<article><p><a href="/history/'+h(quote(notice['session_id'],safe=''))+'">會後整理已完成</a> · '+
                h(readable_time(notice['created_at']))+'</p>'+control(session,'acknowledge-postmeeting',notice['id'],'標示已讀')+'</article>')
        summary_jobs_by_session = {}
        for sjob in await asyncio.to_thread(daemon.store.list_summary_jobs, 500):
            summary_jobs_by_session.setdefault(sjob['session_id'], sjob)
        body += '</section><section><table><tr><th>會議</th><th>開始</th><th>Capture</th><th>會後摘要</th></tr>'
        for run in runs:
            sjob = summary_jobs_by_session.get(run['session_id'])
            jobs = await asyncio.to_thread(daemon.store.list_postmeeting_jobs,run['session_id'])
            job = jobs[0] if jobs else None
            slabels = {'pending':'摘要等待產生','running':'摘要產生中','published':'摘要已產生','failed':'摘要產生失敗'}
            labels = {'ready':'等待外部整理','running':'外部整理進行中','published':'已發佈','notified':'已發佈，通知已保存'}
            if sjob:
                summary_status = slabels.get(sjob['state'],'狀態待確認')
                if sjob['error_code']:
                    summary_status += '（'+str(sjob['error_code'])+'）'
            elif run['source'].get('summary_state'):
                summary_status = '摘要已保存'
            elif job:
                summary_status = labels.get(job['state'],'狀態待確認')
                if job['error_code']:
                    summary_status += '，有待處理錯誤'
            else:
                summary_status = '尚無摘要記錄'
            body += ('<tr><td><a href="/history/'+h(run['session_id'])+'">'+h(run['occurrence']['title'])+'</a></td><td>'+h(readable_time(run['started_at']))+
                '</td><td>'+h(run['state'])+' / '+h(run['end_reason'])+'</td><td>'+h(summary_status)+'</td></tr>')
        return page('會議歷史',body+'</table></section>',session,active='history')

    async def history_detail(request):
        session = authenticated(request)
        ident = request.path_params['session_id']
        run = await asyncio.to_thread(daemon.store.get_run,ident)
        snapshot = await asyncio.to_thread(daemon.store.read,ident)
        detail = await asyncio.to_thread(daemon.store.meeting_view,run['occurrence']['id'],settings.timezone)
        body = '<section><h2>'+h(run['occurrence']['title'])+'</h2><p>'+h(run['state'])+' / '+h(run['end_reason'])+' / '+h(run['error_code'])+'</p>'
        review = run.get('cleanup_review') or {}
        if review.get('required'):
            terminal = run['state'] in TERMINAL|{'interrupted'} and run.get('ended_at') is not None
            worker = review.get('worker_status')
            worker_label = {'gone':'已確認舊 worker 不在','alive':'舊 worker 仍存活','unknown':'無法確認舊 worker 已停止'}.get(
                worker,'無法確認舊 worker 已停止')
            body += '<section><h2>人工確認中斷後的清理</h2>'
            if terminal:
                body += '<p class="error">此紀錄仍是歷史中斷／清理不確定的失敗，不會改寫為正常停止。</p>'
            else:
                body += '<p class="error">場次尚未完成終態封存；目前不能提交清理審查。</p>'
            body += ('<p>'+h(worker_label)+'</p><p>請先由人員確認舊瀏覽器、容器及相關程序已停止，'
                '且助理已離開會議。此確認僅解除這筆紀錄的 capture 安全阻擋，不代表 provider STOP 回覆或逐字稿最終定稿；'
                '不會重新加入會議、恢復排程或啟用發聲。</p>')
            if terminal and worker=='gone':
                fields = hidden('id',ident)+hidden('stamp',review['stamp'])
                fields += checkbox('resources_stopped','我已親自確認舊瀏覽器、容器及相關程序均已停止')
                fields += checkbox('assistant_left','我已親自確認助理已離開會議')
                body += form(session,'review-capture-cleanup',fields,'確認上述兩項並解除此紀錄的阻擋')
            elif not terminal:
                body += '<p class="error">請等待場次封存後重新載入；舊 worker 不在並不等於來源已封存。</p>'
            else:
                body += '<p class="error">仍存活或無法確認的 worker 不允許提交清理確認；請先人工處理並確認已停止。</p>'
            body += '</section>'
        elif review.get('reviewed_at'):
            body += '<p>人工清理確認時間：'+h(readable_time(review['reviewed_at']))+'；操作者：'+h(review.get('actor'))+'。原始中斷及來源品質維持不變。</p>'
        body += '<p>來源品質：'+h({'partial':'記錄不完整','complete':'已保存'}.get(snapshot['completeness'],'需要確認'))+'</p>'
        body += '<details><summary>來源品質與執行詳情</summary><pre>'+h(json.dumps(snapshot['transcript_state'],ensure_ascii=False,indent=2))+'</pre>'
        response = run['status'].get('response_status')
        if isinstance(response,dict):
            visible = {key:response.get(key) for key in
                ('state','error_code','startup_phase','output_enabled','output_muted',
                 'startup_error','capture_policy_failure','runtime_failure',
                 'sender_selection','progress','screen_context')}
            body += '<h2>語音 B 已保存快照</h2><p>觀測時間：'+h(readable_time(run['status'].get('response_snapshot_at')))+'</p><pre>'+h(
                json.dumps(visible,ensure_ascii=False,indent=2))+'</pre><p>本機送音或 drain 不代表遠端可聽。</p>'
        glossary = run['glossary_snapshot']
        body += '<p>詞庫快照版本：'+h(glossary['version'])+'；筆數：'+h(len(glossary['entries']))+'；省略：'+h(glossary['omitted_count'])+'</p>'
        body += '</details><p><a href="/series/'+h(run['occurrence']['series_id'])+'">系列與待辦</a></p>'
        action_controls = ''
        if run['state'] in TERMINAL|{'interrupted'} and snapshot['sources']:
            action_controls += control(session,'retry-summary',ident,'重新產生摘要' if snapshot.get('summary') else '產生摘要')
        if run['occurrence']['state'] in {'completed','stopped','failed','interrupted','skipped'} and run['occurrence'].get('link_status')=='valid':
            action_controls += control(session,'rejoin',run['occurrence']['id'],'重新加入會議')
        if action_controls:
            body += '<div class="controls">'+action_controls+'</div>'
        body += '</section>'
        jobs = await asyncio.to_thread(daemon.store.list_postmeeting_jobs,ident)
        if jobs:
            labels = {'ready':'等待外部整理','running':'外部整理進行中','published':'本次整理已發佈','notified':'本次整理已發佈，完成通知已保存'}
            job = jobs[0]
            body += '<section><h2>會後整理交接</h2><p>'+h(labels.get(job['state'],'狀態待確認'))+'</p>'
            if job['error_code']:
                body += '<p>尚有待處理錯誤；不能視為整理已完成。</p>'
            body += '</section>'
        summary = snapshot.get('summary') or (detail.get('summary') if detail.get('session_id')==ident else None)
        if summary:
            model = (summary.get('postmeeting_state') or {}).get('model') or summary.get('model')
            body += '<section><h2>衍生摘要</h2><p>模型：'+h(model)+'；來源品質不是 provider 最終定稿的替代。</p>'
            for section in ('重點','決策','待辦','未決問題'):
                body += '<h3>'+h(section)+'</h3><ul>'
                for item in summary[section]:
                    links = ' '.join('<a href="#source-'+h(src)+'">'+h(src)+'</a>' for src in item['sources'])
                    body += '<li>'+h(item['text'])+' '+h(item.get('owner'))+' '+h(item.get('date'))+' '+links+'</li>'
                body += '</ul>'
            body += '</section>'
        from .calendar_view import group_transcript_paragraphs
        paragraphs = group_transcript_paragraphs(snapshot['sources'])
        if paragraphs:
            body += '<section><h2>逐字稿（段落整理）</h2><p>依時間間隔與來源類型分段呈現；原文及 provenance 完整保存於下方來源記錄。</p>'
            for para in paragraphs:
                time_range = readable_time(para['start_timestamp'])
                if para['end_timestamp'] and para['end_timestamp'] != para['start_timestamp']:
                    time_range += ' – ' + readable_time(para['end_timestamp'])
                body += ('<article class="transcript-paragraph"><p class="row-meta">'+h(time_range)+' · '+
                    h(para['source_state'])+' · '+h(para['segment_count'])+' 片段</p><p class="paragraph-text">'+
                    h(para['text'])+'</p></article>')
            body += '</section>'
        screen_observations = snapshot.get('screen_observations') or []
        if screen_observations:
            body += ('<section id="screen-observations-section"><h2>分享畫面觀察（衍生視覺文字）</h2>'
                '<p>此區僅保存分享畫面的衍生視覺文字與結構化重點（不含原始截圖），非與會者口頭發言。</p>')
            for obs in screen_observations:
                structured = obs.get('structured') if isinstance(obs.get('structured'), dict) else {}
                title_label = obs.get('title') or '未命名畫面'
                body += ('<article id="source-'+h(obs['id'])+'" class="screen-observation"><h3>#'+h(obs['capture_seq'])+
                    ' · '+h(title_label)+'（'+h(obs['id'])+'）</h3><p class="row-meta">'+
                    h(readable_time(obs.get('captured_at')))+' · '+h(obs.get('screen_kind'))+' · '+
                    h(obs.get('capture_method'))+' · '+h(obs.get('width'))+'×'+h(obs.get('height'))+' · '+
                    h(obs.get('vision_model'))+' · 衍生視覺觀察</p><p>'+h(obs.get('summary_text'))+'</p>')
                key_items = structured.get('key_items') or []
                if key_items:
                    body += '<ul>'+''.join('<li>'+h(item)+'</li>' for item in key_items)+'</ul>'
                metrics = structured.get('metrics_or_numbers') or []
                if metrics:
                    body += '<p><strong>關鍵數據：</strong>'+h('；'.join(str(x) for x in metrics))+'</p>'
                excerpt = structured.get('visible_text_excerpt') or ''
                if excerpt:
                    body += '<details><summary>可見原文摘錄</summary><pre>'+h(excerpt)+'</pre></details>'
                uncertain = structured.get('uncertain_areas') or []
                if uncertain:
                    body += '<p class="warning">看不清或不確定區域：'+h('；'.join(str(x) for x in uncertain))+'</p>'
                body += '</article>'
            body += '</section>'
        body += '<section><h2>來源記錄</h2><p>原文及 provenance；不以姓名或詞庫覆寫。</p>'
        for source in snapshot['sources']:
            body += ('<article id="source-'+h(source['id'])+'"><h3>'+h(source['id'])+'</h3><p>'+h(source['source_state'])+
                ' / revision '+h(source.get('revision',source.get('source_revision')))+' / '+h(source['quality'])+'</p><pre>'+h(source['text'])+'</pre></article>')
        body += '</section>'
        snapshots = await asyncio.to_thread(daemon.store.list_postmeeting_snapshots,ident)
        displayed = {source['id'] for source in snapshot['sources']}
        for revision in snapshots:
            if revision['kind']!='correction_snapshot':
                continue
            body += ('<section><h2>來源補充修訂 · 第 '+h(revision['snapshot_revision'])+' 版</h2><p>'+
                h(revision['created_at'])+'；原逐字來源保持不變。此修訂不是 provider final。</p>')
            for source in revision['entries']:
                if source['id'] in displayed:
                    continue
                displayed.add(source['id'])
                body += ('<article id="source-'+h(source['id'])+'"><h3>'+h(source['id'])+'</h3><p>'+
                    h(source['source_state'])+' / revision '+h(source.get('source_revision'))+' / '+h(source['quality'])+
                    '</p><p>修訂自 <a href="#source-'+h(source.get('correction_of'))+'">'+h(source.get('correction_of'))+
                    '</a></p><pre>'+h(source['text'])+'</pre></article>')
            body += '</section>'
        return page('來源與摘要',body,session,active='history')

    async def series_page(request):
        session = authenticated(request)
        ident = request.path_params['series_id']
        snapshot = await asyncio.to_thread(daemon.store.task_workspace_snapshot,ident,zone=settings.timezone)
        series,latest,workspace = snapshot['series'],snapshot['state'],snapshot['workspace']
        body = '<section><h2>'+h(series['name'])+'</h2><p>新摘要沒有提及舊待辦，不代表完成。</p>'
        if latest:
            def refs(item):
                return ' '.join('<a href="/history/'+h(quote(ref['session_id'],safe=''))+
                    '#source-'+h(quote(ref['source_id'],safe=''))+'">查看來源</a>'
                    for ref in item.get('source_refs',[]))
            body += '<h3>系列最新狀態 · 第 '+h(latest['revision'])+' 版</h3><p>最新結論所屬會議：'+h(readable_time(latest['occurred_at']))+'</p>'
            body += '<p>最近整理／人工更新：'+h(readable_time(latest['published_at']))+'</p>'
            body += '<h3>目前有效結論</h3>'
            for conclusion in latest['conclusions']:
                body += '<article><p>'+h(conclusion['text'])+'</p>'+refs(conclusion)+'</article>'
            if not latest['conclusions']:
                body += '<p>本版沒有已記錄的有效結論。</p>'
            questions = latest.get('summary',{}).get('未決問題',[])
            if questions:
                body += '<h3>待確認</h3><ul>'+''.join('<li>'+h(item['text'])+'</li>' for item in questions)+'</ul>'
            body += '<h3>未完成待辦</h3>'
            for task in workspace['tasks']:
                body += ('<article><p>'+h(task['text'])+' / '+h(task['owner'])+' / '+h(task['date'])+
                    '</p><p>'+h(task['commitment_label'])+' · '+h(task['display_status'])+'</p><p>'+h('；'.join(task['confirmation_reasons']))+'</p>'+refs(task)+
                    '<p><a href="/tasks/'+h(quote(ident,safe=''))+'/'+h(quote(task['id'],safe=''))+'">確認承接／更新</a></p></article>')
            if not latest['open_tasks']:
                body += '<p>本版沒有未完成待辦。</p>'
            body += '<details><summary>本次必要變更與已結束項目</summary><ul>'
            for change in latest['changes']:
                body += '<li>'+h(change.get('text'))+' / '+h(change.get('status',change.get('kind')))+'</li>'
            body += '</ul>'
            for item in [*latest.get('closed_conclusions',[]),*latest.get('closed_tasks',[])]:
                status_label = {'completed':'已完成','cancelled':'已取消','superseded':'已被取代'}.get(item['status'],'狀態待確認')
                body += '<article><p>'+h(item['text'])+' · '+h(status_label)+'</p>'+refs(item)+'</article>'
            body += '</details>'
            return page('系列記憶與待辦',body+'</section>',session,active='history')
        tasks = snapshot['legacy_tasks']
        body += '<p>尚無新系列狀態；以下是先前保存的待辦記憶，承接與完成依據待核實。首次有效整理會延續其識別，再提供確認操作。</p>'
        for task in tasks:
            body += ('<article><p>'+h(task['text'])+' / '+h(task['owner'])+' / '+h(task['date'])+'</p><p>先前狀態：'+h(task['status'])+'</p></article>')
        return page('系列記憶與待辦',body+'</section>',session,active='history')

    async def task_form(session, series_id, task_id, posted=None, error=None):
        snapshot = await asyncio.to_thread(daemon.store.task_workspace_snapshot,series_id,zone=settings.timezone)
        latest,workspace = snapshot['state'],snapshot['workspace']
        task = next((item for item in [*workspace['tasks'],*workspace['closed_tasks']] if item['id']==task_id),None)
        if task is None:
            raise AssistantStoreError('record_not_found')
        back = '/series/'+quote(series_id,safe='')
        reload_path = '/tasks/'+quote(series_id,safe='')+'/'+quote(task_id,safe='')
        body = '<section><p><a href="'+h(back)+'">← 返回系列最新狀態</a></p><h2>'+h(task['text'])+'</h2>'
        body += '<p>'+h(task['commitment_label'])+' · '+h(task['display_status'])+'</p>'
        if task['confirmation_reasons']:
            body += '<p>'+h('；'.join(task['confirmation_reasons']))+'</p>'
        body += '<p>最近明確確認：'+h(readable_time(task.get('last_evidenced_at')))+'</p>'
        if error:
            messages = {'series_revision_changed':'系列已有新版本。您輸入的內容保留如下，請先查看最新版本再決定如何更新。',
                'task_confirmation_required':'承接、完成或取消必須勾選明確確認。',
                'task_owner_required':'確認承接前，請填寫實際承接的責任人。',
                'invalid_text':'待辦內容、責任人或期限的文字無效；請檢查內容、長度與控制字元。',
                'task_note_required':'請填寫操作依據／說明，不能只留空白。',
                'task_note_too_long':'操作依據／說明最多 2000 字元（換行規範化後）。',
                'task_note_control_character':'操作依據／說明含不支援的控制字元；可使用一般換行。',
                'invalid_task_note':'操作依據／說明必須是文字。',
                'task_not_open':'此事項已結束，不能自動重開。',
                'task_request_conflict':'這次操作識別已用於不同內容，請重新載入後檢查。'}
            body += '<p role="alert" class="warning">'+h(messages.get(error,'無法保存，請檢查欄位並重試。'))+'</p>'
            body += '<p><a href="'+h(reload_path)+'">重新載入最新版本</a>（會清除尚未保存的輸入）</p>'
        if task['status'] not in {'completed','cancelled','superseded'} or posted:
            values = posted or dict(text=task['text'],owner=task['owner'],date=task['date'],
                expected_revision=latest['revision'],request_id=secrets.token_hex(16),task_action='update',note='')
            fields = hidden('series_id',series_id)+hidden('id',task_id)
            fields += hidden('expected_revision',values.get('expected_revision',''))+hidden('request_id',values.get('request_id',''))
            fields += '<fieldset><legend>事項內容</legend>'+input_field('text','待辦內容',values.get('text',''))
            fields += input_field('owner','責任人（填寫不代表已承接）',values.get('owner',''))
            fields += input_field('date','期限（建議 YYYY-MM-DD；不確定可留空）',values.get('date',''))+'</fieldset>'
            fields += '<fieldset><legend>這次確認</legend>'+select_field('task_action','操作',
                [('update','更新／確認近況'),('accept','確認承接'),('complete','確認完成'),('cancel','確認取消')],values.get('task_action','update'))
            display_note = values.get('note','')
            if error=='task_note_control_character':
                import unicodedata
                display_note = ''.join(char if char=='\n' or unicodedata.category(char)!='Cc' else '\ufffd'
                    for char in display_note.replace('\r\n','\n'))
            fields += '<label>依據／說明（必填，可換行，最多 2000 字元）<textarea name="note" required>'+h(display_note)+'</textarea></label>'
            fields += checkbox('confirmed','我已向責任人或可信來源核實這次承接、完成或取消',values.get('confirmed')=='on')+'</fieldset>'
            fields += '<p>更新沿用同一事項 ID；更換責任人或工作內容後需重新承接。未提及、逾期或久未更新都不會自動完成。</p>'
            body += form(session,'task-workflow',fields,'保存本次確認')
        history = [item for item in snapshot['task_actions'] if item['task_id']==task_id]
        body += '<details><summary>操作證據與來源</summary><p>事項 ID：'+h(task_id)+'</p>'
        review = task.get('acceptance_review')
        if review:
            body += '<h3>承接來源訂正（待重新確認）</h3>'
            for replacement in review.get('replacement_sources',[]):
                body += ('<blockquote>'+h(replacement['text'])+'</blockquote><p><a href="/history/'+h(quote(review['session_id'],safe=''))+
                    '#source-'+h(quote(replacement['source_id'],safe=''))+'">查看訂正來源</a> · 來源版本 '+h(replacement.get('source_version'))+
                    ' · 訂正快照 '+h(replacement['snapshot_revision'])+'</p>')
        for key,label in (('acceptance_evidence','明確承接依據'),('completion_evidence','完成／取消依據')):
            evidence = task.get(key)
            if evidence and evidence.get('kind')=='meeting_source':
                body += '<h3>'+label+'（會議引文）</h3><blockquote>'+h(evidence['quote'])+'</blockquote>'
        for ref in task.get('source_refs',[]):
            body += '<p><a href="/history/'+h(quote(ref['session_id'],safe=''))+'#source-'+h(quote(ref['source_id'],safe=''))+'">會議來源</a> · 來源版本 '+h(ref['source_version'])+' · '+h(ref['source_sha256'])+'</p>'
        for item in history:
            label = {'update':'更新近況','accept':'確認承接','complete':'確認完成','cancel':'確認取消'}[item['action']]
            body += '<p>'+h(readable_time(item['created_at']))+' · '+h(item['actor'])+' · '+label+' · <span class="task-action-note">'+h(item['note'])+'</span></p>'
        return page('確認與更新待辦',body+'</details></section>',session,active='history')

    async def task_edit(request):
        return await task_form(authenticated(request),request.path_params['series_id'],request.path_params['task_id'])

    async def glossary_page(request):
        session = authenticated(request)
        rows = await asyncio.to_thread(daemon.store.list_glossary)
        body = '<section><p>僅 active 且適用系列的詞彙於入會凍結。initialItems 不是 ASR hotword API，也不推定 speaker。</p>'
        body += '<a href="/glossary/new">新增詞彙</a><table><tr><th>詞彙</th><th>類別／範圍</th><th>狀態／版本</th><th>衝突</th></tr>'
        for row in rows:
            body += ('<tr><td><a href="/glossary/'+h(row['id'])+'">'+h(row['canonical'])+'</a></td><td>'+h(row['kind'])+' / '+h(row['scope'])+
                '</td><td>'+h(row['status'])+' / '+h(row['revision'])+'</td><td>'+h(', '.join(row['conflicts']))+'</td></tr>')
        return page('人名與術語詞庫',body+'</table></section>',session)

    async def glossary_edit(request):
        session = authenticated(request)
        ident = request.path_params['entry_id']
        row = await asyncio.to_thread(daemon.store.get_glossary,ident) if ident!='new' else {
            'kind':'term','canonical':'','aliases':[],'pronunciation':'','definition':'','scope':'global','series_id':None,'status':'pending'}
        series = await asyncio.to_thread(daemon.store.list_series)
        fields = ''.join(hidden(key,row[key]) for key in ('id','revision') if key in row)
        fields += (input_field('canonical','正式詞彙／人名',row['canonical'])+
            select_field('kind','類別',[('person','人名'),('term','術語'),('abbreviation','縮寫')],row['kind']))
        fields += '<label>別名（每行一個；同音不同人不可合併）<textarea name="aliases">'+h('\n'.join(row['aliases']))+'</textarea></label>'
        fields += (input_field('pronunciation','發音提示',row['pronunciation'])+input_field('definition','定義（單行）',row['definition'])+
            select_field('scope','範圍',[('global','全域'),('series','指定系列')],row['scope'])+
            select_field('series_id','系列',[('', '不指定')]+[(item['id'],item['name']) for item in series],row['series_id'] or '')+
            select_field('status','審核狀態',[('pending','待核准'),('active','已核准使用'),('disabled','停用')],row['status']))
        return page('維護詞庫','<section>'+form(session,'glossary',fields)+'</section>',session)

    async def audit_page(request):
        session = authenticated(request)
        rows = await asyncio.to_thread(daemon.store.audit)
        body = '<section><table><tr><th>時間</th><th>操作者</th><th>動作</th><th>目標 ID</th></tr>'
        for row in rows:
            body += ('<tr><td>'+h(readable_time(row['created_at']))+'</td><td>'+h(row['actor'])+
                '</td><td>'+h(row['action'])+'</td><td>'+h(row['target'])+'</td></tr>')
        return page('操作稽核',body+'</table></section>',session)

    async def action(request):
        session,values = await submitted(request)
        name = request.path_params['action']
        target = values.get('id')
        destination = '/'
        result = None
        if name in {'stop','output'} and 'acceptance_request' in values:
            run=await asyncio.to_thread(daemon.store.get_run,target)
            policy=run.get('acceptance_policy')
            if not policy or values['acceptance_request']!=policy['request_sha256']:
                raise AdminError('teams_acceptance_binding_rejected')
        if name=='scheduler':
            if values.get('paused') not in {'true','false'}:
                raise AdminError('invalid_scheduler_policy')
            async with daemon._join_lock:
                if values['paused']=='false':
                    if daemon.status()['capture_interlock']:
                        raise AdminError('capture_cleanup_review_required')
                    if daemon.auth_operation_in_progress:
                        raise AdminError('codex_auth_operation_in_progress')
                await asyncio.to_thread(daemon.store.update_settings,{'scheduler_paused':values['paused']=='true'})
        elif name=='poll':
            poll = await daemon.poll_calendar()
            session['calendar_readback'] = poll['readback']
            destination = '/schedule'
        elif name=='calendar-readonly':
            if set(values)-{'csrf','calendar_ref','revision','enabled'} or values.get('enabled','on')!='on':
                raise AdminError('invalid_calendar')
            ref = values.get('calendar_ref')
            if not isinstance(ref,str) or (ref!='primary' and not re.fullmatch('[a-f0-9]{32}',ref)):
                raise AdminError('invalid_calendar')
            async with daemon._join_lock:
                if daemon.auth_operation_in_progress or daemon.status()['active_session_id'] is not None or daemon.closing.is_set():
                    raise AdminError('calendar_settings_not_quiescent')
                await asyncio.to_thread(daemon.store.save_calendar,dict(id=ref,revision=integer(values,'revision'),
                    enabled='enabled' in values),read_only=True)
            destination = '/settings'
        elif name=='calendar':
            fields = {key:values[key] for key in ('id','account_id','calendar_id','name') if key in values}
            fields.update(enabled='enabled' in values,recording_authorized='recording_authorized' in values,
                speaking_authorized='speaking_authorized' in values,join_early_seconds=integer(values,'join_early_seconds',300),
                max_lateness_seconds=integer(values,'max_lateness_seconds',300))
            if 'id' in fields:
                fields['revision'] = integer(values,'revision')
            await asyncio.to_thread(daemon.store.save_calendar,fields)
            destination = '/settings'
        elif name=='manual':
            fields = {key:values.get(key,'') for key in ('title','join_url','start_at','end_at')}
            fields['start_at'] = _local_meeting_time(fields['start_at'],settings.timezone)
            fields['end_at'] = _local_meeting_time(fields['end_at'],settings.timezone)
            fields.update(series_id=values.get('series_id') or None,recording_authorized='recording_authorized' in values,
                speaking_authorized='speaking_authorized' in values)
            row = await asyncio.to_thread(daemon.store.create_manual_occurrence,fields)
            if 'join_now' in values:
                await daemon.join(row['id'])
            destination = '/?meeting='+quote(row['id'],safe='')
        elif name=='target-consent':
            if set(values)!={'id','revision','present','media'}:
                raise AdminError('target_owner_confirmation_required')
            async with daemon._join_lock:
                if daemon.auth_operation_in_progress or daemon.status()['active_session_id'] is not None or daemon.closing.is_set():
                    raise AdminError('capture_unavailable')
                result=await asyncio.to_thread(daemon.store.issue_target_consent,target,integer(values,'revision'),
                    present=values.get('present')=='on',media=values.get('media')=='on')
            if 'application/json' not in request.headers.get('accept',''):
                return page('Hermes 單次控制 binding','<p>不代表已入會、OAuth 或遠端聽驗成功。'+
                    '五分鐘內僅供本次 Actions Join；不要把會議連結或登入資料貼到聊天。</p><pre>'+
                    h(json.dumps(result,ensure_ascii=False,indent=2))+'</pre><a href="/schedule">返回場次</a>',session)
        elif name=='media-policy':
            raw=values.get('hosts','')
            if values.get('confirmed')!='on' or len(raw)>4096:
                raise AdminError('media_policy_confirmation_required')
            hosts=raw.splitlines() if raw else []
            async with daemon._join_lock:
                policy=await asyncio.to_thread(daemon.store.settings)
                if (daemon.auth_operation_in_progress or daemon.status()['active_session_id'] is not None
                        or (Path(settings.data_root)/'.deployment-maintenance').exists()
                        or policy.get('scheduler_paused') is not True or daemon.closing.is_set()
                        or await asyncio.to_thread(daemon.store.capture_interlocks)):
                    raise AdminError('capture_unavailable')
                from .private_runtime import save_media_policy
                try:result=await asyncio.to_thread(save_media_policy,settings,hosts)
                except (ValueError,OSError):raise AdminError('media_policy_rejected') from None
                await asyncio.to_thread(daemon.store.audit_control,'admin','media_policy_saved','runtime')
            destination='/settings'
        elif name=='join':
            acceptance=None
            if 'acceptance_request' in values:
                from .teams_acceptance import validate_policy, VERSION
                try:
                    if values.get('acceptance_stop_policy')!='owner_or_meeting_end' or 'acceptance_seconds' in values:
                        raise ValueError('teams_acceptance_policy_rejected')
                    acceptance=validate_policy({'version':VERSION,'request_sha256':values['acceptance_request'],
                        'occurrence_revision':integer(values,'acceptance_revision'),
                        'maximum_seconds':None})
                except ValueError:
                    raise AdminError('teams_acceptance_policy_rejected') from None
            result=await daemon.join(target,**({'acceptance':acceptance} if acceptance is not None else {}))
        elif name=='rejoin':
            result=await daemon.rejoin(target)
            destination = '/?meeting='+quote(target or '',safe='')
        elif name=='delete':
            confirmed = (values.get('confirm_delete') == 'on'
                or ('application/json' in request.headers.get('accept','') and values.get('confirmed') == 'true'))
            if not confirmed:
                raise AdminError('delete_confirmation_required')
            result=await asyncio.to_thread(daemon.store.delete_occurrence,target)
            destination = '/schedule'
        elif name=='skip':
            await asyncio.to_thread(daemon.store.skip_occurrence,target)
            destination = '/schedule'
        elif name=='assign-series':
            await asyncio.to_thread(daemon.store.assign_series,target,values.get('series_id'))
            destination = '/schedule'
        elif name=='stop':
            await daemon.stop(target)
        elif name=='review-capture-cleanup':
            await daemon.review_capture_cleanup(target,values.get('stamp'),
                resources_stopped=values.get('resources_stopped')=='on',
                assistant_left=values.get('assistant_left')=='on',actor='admin')
            destination = '/history/'+quote(target or '',safe='')
        elif name=='output':
            if values.get('enabled') not in {'true','false'}:
                raise AdminError('invalid_output_policy')
            await daemon.set_output(target,values['enabled']=='true')
        elif name=='retry-summary':
            result = await daemon.retry_summary(target)
            destination = '/history/'+quote(target or '',safe='')
        elif name=='task-policy':
            await asyncio.to_thread(daemon.store.update_settings,dict(task_stale_days=integer(values,'task_stale_days')))
            destination = '/settings'
        elif name=='screen-policy':
            if set(values) - {'screen_context_mode', 'screen_include_in_summary'}:
                raise AdminError('invalid_screen_policy')
            mode = values.get('screen_context_mode')
            if mode not in {'off', 'on_demand', 'watch_and_cache'}:
                raise AdminError('invalid_screen_policy')
            if 'screen_include_in_summary' in values and values['screen_include_in_summary'] != 'on':
                raise AdminError('invalid_screen_policy')
            await asyncio.to_thread(daemon.store.update_settings, dict(
                screen_context_mode=mode,
                screen_include_in_summary='screen_include_in_summary' in values,
            ))
            destination = '/settings'
        elif name=='task-workflow':
            try:
                await asyncio.to_thread(daemon.store.apply_task_action,series_id=values.get('series_id'),task_id=target,
                    action=values.get('task_action'),values={key:values.get(key,'') for key in ('text','owner','date')},
                    expected_revision=integer(values,'expected_revision'),request_id=values.get('request_id'),
                    confirmed=values.get('confirmed')=='on',note=values.get('note',''),actor='admin')
            except AssistantStoreError as exc:
                response = await task_form(session,values.get('series_id'),target,values,exc.code)
                response.status_code = 409 if exc.code in {'series_revision_changed','task_request_conflict','task_not_open'} else 400
                return response
            destination = '/tasks/'+quote(values['series_id'],safe='')+'/'+quote(target,safe='')
        elif name=='task':
            raise AdminError('task_workflow_required')
        elif name=='acknowledge-postmeeting':
            await asyncio.to_thread(daemon.store.acknowledge_postmeeting_notification,target)
            destination = '/history'
        elif name=='glossary':
            fields = {key:values.get(key,'') for key in ('kind','canonical','pronunciation','definition','scope','status')}
            fields.update(aliases=[value.strip() for value in values.get('aliases','').splitlines() if value.strip()],
                series_id=values.get('series_id') or None)
            if fields['scope']=='global':
                fields['series_id'] = None
            if target:
                fields.update(id=target,revision=integer(values,'revision'))
            await asyncio.to_thread(daemon.store.save_glossary,fields)
            destination = '/glossary'
        else:
            raise AdminError('unknown_admin_action')
        if 'application/json' in request.headers.get('accept',''):
            return JSONResponse({'ok':True,'destination':destination,**({'result':result} if result is not None else {})})
        return RedirectResponse(destination,status_code=303)

    def redirect_uri():
        base = settings.public_base_url.rstrip('/') if settings.public_base_url else 'http://127.0.0.1:'+str(settings.port)
        return base+'/google/callback'

    def google_setup_message(code):
        return {'google_oauth_client_required':
            '尚未配置 Google OAuth 用戶端。請由部署管理者設定 google_client_config_file，使用私人 Web application client JSON，並在 Google Cloud 登記上述完整回呼網址。此頁不接收 client secret。',
            'oauth_redirect_not_registered':
            'Google OAuth 用戶端尚未登記上述回呼網址。請核對 public_base_url、HTTPS 與 Google Cloud 的 Authorized redirect URIs。',
            'private_calendar_configuration_invalid':
            'Google OAuth 設定檔無法安全讀取。請核對檔案擁有者、私人權限與路徑；此頁不會修改憑證。'
            }.get(code, 'Google OAuth 設定尚未就緒，請由部署管理者檢查私人用戶端設定。')

    async def chatgpt_page(request):
        session = authenticated(request)
        view = chatgpt_auth.view(session)
        state = view['state']
        body = '<section><h2>ChatGPT 裝置授權</h2>'
        if state == 'pending':
            body += ('<p>請僅在自己的瀏覽器開啟官方網址，登入本人 ChatGPT 帳號，並輸入下列短效代碼。請勿分享或截圖代碼。</p>'+
                '<p><a href="'+h(view['verification_url'])+'" target="_blank" rel="noreferrer noopener">開啟 OpenAI 授權網站</a></p>'+
                '<p>一次性代碼：<strong>'+h(view['user_code'])+'</strong></p><p>最多等待 10 分鐘；請在官方網站完成後重新整理此頁查看結果。</p>')
        elif state == 'starting':
            body += '<p role="status">正在檢查部署前置條件並啟動官方裝置授權，請重新整理查看。</p>'
        elif state == 'busy':
            body += '<p>另一個管理工作階段正在授權，或前次授權清理尚未確認。請等待作業結束。</p>'
        else:
            if state == 'completed':
                body += '<p role="status">授權程序已完成。帳號准入與 GPT Live 音訊仍待另外驗證；排程維持目前設定。</p>'
            elif state in {'failed','cancelled','expired'}:
                body += '<p class="warning">授權未確認完成。取消、逾時或中斷仍可能已在官方服務產生授權；請先核對本人帳號，不會自動重試。</p>'
                code = view.get('error_code') or view.get('cleanup_error_code') or 'codex_device_login_failed_outcome_unverified'
                body += '<p>'+h(auth_message(code))+'</p><details><summary>技術詳情</summary><code>'+h(code)+'</code></details>'
                if view.get('cleanup_error_code'):
                    body += '<p class="warning">授權程序清理尚未確認；新的授權與入會持續阻擋。</p><code>'+h(view['cleanup_error_code'])+'</code>'
            body += ('<p>先暫停排程並結束會議。需要同部署的私人、持久可寫 codex_home 及固定版本 Codex；此頁不建立或匯入憑證、不接收 ChatGPT 密碼或 token。裝置授權若被帳戶或 workspace 停用，請本人查閱官方設定。</p>'+
                form(session,'',checkbox('confirm_account','我本人同意將 ChatGPT 授權給此部署',False)+
                    checkbox('reauthorize','我已核對既有授權，同意重新授權（已登入時必選）',False),
                    '開始 ChatGPT 授權',path='/chatgpt/connect'))
        if state in {'starting','pending'}:
            body += form(session,'','','取消此次授權',path='/chatgpt/cancel')
        body += '<p><a href="/chatgpt">重新整理授權狀態</a> · <a href="/settings">返回連線與設定</a></p></section>'
        return page('ChatGPT 授權',body,session)

    def auth_message(code):
        if code == 'explicit_reauthorization_required':
            return '此部署已有授權檔；請先核對帳號，再明確勾選重新授權。'
        if code == 'pause_scheduler_and_finish_meeting_before_login':
            return '請先暫停排程、結束會議，並完成任何離會清理確認。'
        if code.startswith('codex_home') or code.startswith('codex_auth_private'):
            return '請由部署管理者配置同一部署的私人持久可寫 codex_home，核對目錄 0700、授權檔 0600 及執行 UID/GID；網頁不會自動建立或搬移。'
        return '請檢查部署前置條件、官方裝置授權設定與網路，再決定是否重新授權。'

    async def chatgpt_connect(request):
        session,values = await submitted(request)
        if set(values) - {'confirm_account','reauthorize'}:
            raise AdminError('invalid_form')
        await chatgpt_auth.begin(session,confirmed=values.get('confirm_account')=='on',
            reauthorize=values.get('reauthorize')=='on')
        return RedirectResponse('/chatgpt',status_code=303)

    async def chatgpt_cancel(request):
        session,values = await submitted(request)
        if values:
            raise AdminError('invalid_form')
        await chatgpt_auth.cancel(session)
        return RedirectResponse('/chatgpt',status_code=303)

    async def google_connect(request):
        session,values = await submitted(request)
        if values:
            raise AdminError('invalid_form')
        state = secrets.token_urlsafe(32)
        uri = redirect_uri()
        url = daemon.google_authorization_url(uri,state)
        session['oauth'] = {'state':state,'redirect_uri':uri,'expires_at':time.time()+300}
        return RedirectResponse(url,status_code=303)

    async def google_callback(request):
        session = authenticated(request)
        pending = session.get('oauth')
        states,codes = request.query_params.getlist('state'),request.query_params.getlist('code')
        if (not pending or pending['expires_at']<=time.time() or len(states)!=1
                or not re.fullmatch('[A-Za-z0-9_-]{32,256}',states[0])
                or not hmac.compare_digest(states[0],pending['state'])):
            raise AdminError('google_oauth_state_rejected')
        session['oauth'] = None
        errors = request.query_params.getlist('error')
        if len(errors)==1 and errors[0] and not codes:
            session['google_notice'] = 'denied'
            return RedirectResponse('/settings',status_code=303)
        if errors or len(codes)!=1:
            raise AdminError('google_oauth_state_rejected')
        await daemon.google_exchange_code(codes[0],pending['redirect_uri'])
        session['google_notice'] = 'authorized'
        await asyncio.to_thread(daemon.store.audit_control,'admin','google_authorized','google')
        return RedirectResponse('/settings',status_code=303)

    async def api_status(request):
        authenticated(request)
        return JSONResponse(daemon.status())

    async def api_calendar(request):
        authenticated(request)
        data = await asyncio.to_thread(daemon.store.calendar_view,
            request.query_params.get('start'),request.query_params.get('end'),settings.timezone)
        status = daemon.status()
        policy = await asyncio.to_thread(daemon.store.settings)
        now = datetime.now(ZoneInfo(settings.timezone))
        return JSONResponse({**data,'timezone':settings.timezone,'today':now.date().isoformat(),
            'now':now.isoformat(),'active_session_id':status.get('active_session_id'),
            'scheduler_paused':policy['scheduler_paused'],'capture_interlock':status['capture_interlock'],
            'google':{'authorized':bool(status['google'].get('authorized')),
                'error_code':status['google'].get('error_code')},'last_error':status.get('last_error')})

    async def api_meeting(request):
        authenticated(request)
        if request.query_params.get('view')=='acceptance':
            from .teams_acceptance import meeting_view
            view=await asyncio.to_thread(meeting_view,daemon.store,request.path_params['occurrence_id'])
            allowed=sorted(set(settings.allowed_egress_hosts))
            view['egress_policy_sha256']=hashlib.sha256(json.dumps(allowed,separators=(',',':')).encode()).hexdigest()
            view['egress_allows_target_host']=view['join_host'] in allowed
            status=daemon.status()
            response=status.get('response') or {}
            view['response_ready']=bool(view.get('session_id') and view['session_id']==status.get('active_session_id')
                and response.get('state') in {'sleeping','listening','thinking','answering'})
            return JSONResponse(view)
        data = await asyncio.to_thread(daemon.store.meeting_view,
            request.path_params['occurrence_id'],settings.timezone)
        if data.get('session_id'):
            def acceptance_status():
                from .teams_acceptance import receipt
                with daemon.store._locked() as db:
                    row=db.execute('SELECT status_json FROM session_runtime WHERE meeting_id=?',(data['session_id'],)).fetchone()
                    return receipt(db,data['session_id'],json.loads(row[0]) if row else {})
            data['acceptance']=await asyncio.to_thread(acceptance_status)
        status = daemon.status()
        active = bool(data['session_id'] and data['session_id']==status.get('active_session_id'))
        controls = data['controls']
        controls['stop'] = controls['stop'] and active
        controls['output'] = controls['output'] and active
        can_start_capture = bool(status.get('started')) and not (
            status.get('active_session_id') or status.get('capture_interlock') or status.get('closing'))
        controls['join'] = controls['join'] and can_start_capture
        controls['rejoin'] = controls['rejoin'] and can_start_capture
        controls['delete'] = controls['delete'] and not active
        if active and isinstance(status.get('response'),dict):
            data['response'] = {key:status['response'].get(key) for key in
                ('state','output_enabled','output_muted','error_code','question_activity','screen_context')}
        return JSONResponse(data)

    async def asset(request):
        filename = request.path_params['filename']
        if filename not in {'calendar.css','calendar.js'}:
            return PlainTextResponse('not_found',status_code=404)
        path = Path(__file__).with_name('static') / filename
        content = await asyncio.to_thread(path.read_bytes)
        return Response(content,media_type='text/css' if filename.endswith('.css') else 'application/javascript')

    async def health(request):
        return JSONResponse({'status':'ok' if daemon.status()['started'] else 'starting'})

    async def fixed_error(request,error):
        code = getattr(error,'code','admin_request_failed')
        if not isinstance(code,str) or not re.fullmatch('[a-z0-9_]{1,80}',code):
            code = 'admin_request_failed'
        json_request = request.url.path.startswith('/api/') or 'application/json' in request.headers.get('accept','')
        if code=='admin_login_required':
            if json_request:
                return JSONResponse({'error':code},status_code=401)
            return RedirectResponse('/login',status_code=303)
        status = 403 if code in {'admin_csrf_rejected','admin_origin_rejected','google_oauth_state_rejected','authorization_owner_required'} else \
            409 if code=='revision_conflict' else 429 if code=='login_rate_limited' else 413 if code=='request_too_large' else 422
        if json_request:
            return JSONResponse({'error':code},status_code=404 if code=='record_not_found' else status)
        messages = {'admin_login_failed':'密碼不正確，請重新登入。',
            'admin_csrf_rejected':'頁面已過期，請重新載入後再操作。',
            'admin_origin_rejected':'無法確認操作來源，請從管理頁重新操作。',
            'explicit_account_authorization_required':'請本人先勾選同意，才會開始 ChatGPT 授權。',
            'pause_scheduler_and_finish_meeting_before_login':'請先暫停排程、結束會議，並完成任何離會清理確認。',
            'codex_auth_operation_in_progress':'已有 ChatGPT 授權作業，請完成或取消後再操作。',
            'authorization_owner_required':'只有發起授權的管理工作階段可以取消此作業。',
            'google_oauth_client_required':google_setup_message('google_oauth_client_required'),
            'oauth_redirect_not_registered':google_setup_message('oauth_redirect_not_registered'),
            'revision_conflict':'資料已被更新，請重新載入後再修改。',
            'target_owner_confirmation_required':'請本人確認現在在場及本次媒體同意；此操作不會加入或授權其他場次。',
            'occurrence_outside_join_window':'場次不在可入會時段；請核對日期、開始／結束時間及允許提早／遲到時段，不會建立 binding 或加入。',
            'target_occurrence_not_authorized':'場次授權或版本已變動；請重新載入既有合法待加入場次，確認暫停排程及清理狀態。',
            'media_policy_confirmation_required':'保存前必須明確核准這份媒體出口政策；空白保持拒絕全部。',
            'media_policy_rejected':'媒體政策未保存：請確認精確小寫 hosts、私有 runtime.yml，且沒有環境覆寫。',
            'capture_cleanup_review_required':'上次離會狀態尚未確認，請先完成清理確認。',
            'occurrence_not_rejoinable':'這場會議目前狀態不允許重新加入。',
            'meeting_running_cannot_delete':'進行中的會議不能直接刪除，請先讓助理離開。',
            'delete_confirmation_required':'請先勾選明確確認，才會刪除此會議。',
            'summary_source_unavailable':'此場次尚無可摘要的逐字來源。',
            'summary_already_running':'此場次摘要正在產生中，請稍候。',
            'postmeeting_active_cannot_delete':'會後整理仍在執行或受阻，暫時無法刪除。',
            'invalid_screen_policy':'分享畫面模式設定無效，請重新選擇。',
            'record_not_found':'找不到這筆會議或資料。'}
        response = page('操作未完成','<p class="error">'+h(messages.get(code,'操作未完成，請確認目前狀態後再試。'))+
            '</p><details><summary>技術詳情</summary><code>'+h(code)+'</code></details><a href="/">返回行事曆</a>',request.state.admin_session)
        response.status_code = status
        return response

    @asynccontextmanager
    async def lifespan(app):
        from .teams_local_control import control_lifespan
        await daemon.start()
        try:
            async with control_lifespan(daemon,settings,local_teams_control):
                yield
        finally:
            await chatgpt_auth.close()
            await daemon.close()

    routes = [Route('/login',login,methods=['GET','POST']),Route('/logout',logout,methods=['POST']),
        Route('/',dashboard),Route('/settings',settings_page),Route('/schedule',schedule),
        Route('/schedule/{occurrence_id}/delete',schedule_delete_confirm),Route('/diagnostics',diagnostics),
        Route('/history',history),Route('/history/{session_id}',history_detail),Route('/series/{series_id}',series_page),
        Route('/tasks/{series_id}/{task_id}',task_edit),
        Route('/glossary',glossary_page),Route('/glossary/{entry_id}',glossary_edit),Route('/audit',audit_page),
        Route('/actions/{action}',action,methods=['POST']),Route('/google/connect',google_connect,methods=['POST']),
        Route('/google/callback',google_callback),Route('/api/status',api_status),
        Route('/chatgpt',chatgpt_page),Route('/chatgpt/connect',chatgpt_connect,methods=['POST']),
        Route('/chatgpt/cancel',chatgpt_cancel,methods=['POST']),
        Route('/api/calendar',api_calendar),Route('/api/meetings/{occurrence_id}',api_meeting),
        Route('/assets/{filename}',asset),Route('/healthz',health)]
    app = Starlette(routes=routes,lifespan=lifespan,exception_handlers={AdminError:fixed_error,
        AssistantStoreError:fixed_error,CalendarError:fixed_error,AuthError:fixed_error,MeetingStoreError:fixed_error,RxContractError:fixed_error})
    app.add_middleware(AdminGuard,sessions=sessions,settings=settings)
    app.state.daemon,app.state.sessions = daemon,sessions
    app.state.chatgpt_auth = chatgpt_auth
    return app
