"""Google readonly OAuth and bounded Calendar polling; no provider fallbacks."""
import asyncio
import html
import json
import math
import os
import re
import stat
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit

import httpx

from .browser import meeting_platform
from .storage import atomic_json

AUTHORIZATION_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
CALENDAR_URL = 'https://www.googleapis.com/calendar/v3/calendars/'
READONLY_SCOPE = 'https://www.googleapis.com/auth/calendar.events.readonly'
TEAMS_HOSTS = {'teams.microsoft.com','teams.cloud.microsoft','teams.live.com'}


class CalendarError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def validate_teams_url(value):
    try:
        if not isinstance(value,str) or not 1<=len(value)<=8192 or '\\' in value:
            raise ValueError()
        parsed = urlsplit(value)
        if parsed.hostname not in TEAMS_HOSTS or '%' in parsed.netloc or meeting_platform(value)!='teams':
            raise ValueError()
        return value
    except (ValueError,TypeError,AttributeError):
        raise CalendarError('invalid_teams_url') from None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links, self.text = [], []

    def handle_starttag(self, tag, attrs):
        if tag=='a':
            self.links.extend(value for key,value in attrs if key=='href' and isinstance(value,str))

    def handle_data(self, data):
        self.text.append(data)


def _select_links(values):
    valid, invalid = {}, False
    for value in values:
        if not isinstance(value,str):
            continue
        value = html.unescape(value).strip()
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.hostname not in TEAMS_HOSTS:
            continue
        try:
            validate_teams_url(value)
            key = (parsed.hostname,unquote(parsed.path),tuple(sorted(parse_qsl(parsed.query,keep_blank_values=True))))
            valid.setdefault(key,value)
        except CalendarError:
            invalid = True
    if invalid:
        return None,'invalid'
    if len(valid)>1:
        return None,'ambiguous'
    return (next(iter(valid.values())),'valid') if valid else (None,'missing')


def teams_link(event):
    conference = event.get('conferenceData') or {}
    entries = conference.get('entryPoints',[]) if isinstance(conference,dict) else []
    structured = [item.get('uri') for item in entries if isinstance(item,dict) and item.get('entryPointType')=='video']
    result = _select_links(structured)
    if result[1]!='missing':
        return result
    parser = _Links()
    for field in ('description','location'):
        value = event.get(field,'')
        if not isinstance(value,str) or len(value.encode())>131072:
            raise CalendarError('calendar_event_too_large')
        parser.feed(value)
    parser.close()
    values = [*parser.links]
    values.extend(match.rstrip('.,;:!?)）]') for match in re.findall(r'https://[^\s<>"\']+',
        html.unescape(' '.join(parser.text)),re.IGNORECASE))
    return _select_links(values)


def _time(value):
    if not isinstance(value,dict) or not isinstance(value.get('dateTime'),str):
        return None
    try:
        parsed = datetime.fromisoformat(value['dateTime'].replace('Z','+00:00'))
        if parsed.tzinfo is None:
            return None  # Never invent an offset/fold for a DST-ambiguous time.
        return parsed.astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def normalize_event(event):
    if not isinstance(event,dict) or not isinstance(event.get('id'),str) or not 1<=len(event['id'])<=1024:
        raise CalendarError('invalid_calendar_event')
    start, end = event.get('start') or {},event.get('end') or {}
    if not isinstance(start,dict) or not isinstance(end,dict):
        raise CalendarError('invalid_calendar_event')
    original = event.get('originalStartTime')
    original_key = None
    if isinstance(original,dict):
        if 'dateTime' in original:
            original_key = _time(original)
        elif isinstance(original.get('date'),str):
            try:
                original_key = 'date:'+datetime.strptime(original['date'],'%Y-%m-%d').date().isoformat()
            except ValueError:
                pass
    title = event.get('summary') or '未命名會議'
    if not isinstance(title,str):
        raise CalendarError('invalid_calendar_event')
    join_url, link_status = teams_link(event)
    status = event.get('status','confirmed')
    if status not in {'confirmed','tentative','cancelled'}:
        raise CalendarError('invalid_calendar_event')
    recurring = event.get('recurringEventId')
    if recurring is not None and (not isinstance(recurring,str) or not original_key):
        raise CalendarError('invalid_recurring_identity')
    return dict(event_id=event['id'],recurring_event_id=recurring,original_start=original_key,
        ical_uid=event.get('iCalUID'),title=' '.join(title.split())[:400] or '未命名會議',
        start_at=_time(start),end_at=_time(end),all_day='date' in start,status=status,
        join_url=join_url,link_status=link_status,updated_at=_time({'dateTime':event.get('updated')}),
        etag=event.get('etag'),timezone=start.get('timeZone'))


def _private_json(path, maximum=65536):
    path = Path(path)
    try:
        if path.absolute()!=path.resolve() or path.is_symlink():
            raise ValueError()
        fd = os.open(path,os.O_RDONLY|os.O_NOFOLLOW)
        with os.fdopen(fd,'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_mode&0o077 or info.st_uid!=os.getuid():
                raise ValueError()
            raw = stream.read(maximum+1)
        value = json.loads(raw)
        if len(raw)>maximum or not isinstance(value,dict):
            raise ValueError()
        return value
    except FileNotFoundError:
        return None
    except (OSError,ValueError,TypeError):
        raise CalendarError('private_calendar_configuration_invalid') from None


class GoogleCalendarClient:
    def __init__(self, credentials_path, *, client_config_path=None, transport=None, clock=None):
        self.credentials_path = Path(credentials_path)
        self.client_config_path = Path(client_config_path) if client_config_path else None
        self.clock = clock or time.time
        self.client = httpx.AsyncClient(transport=transport,timeout=15,follow_redirects=False)
        self.lock = asyncio.Lock()
        self.error_code = None

    def _config(self):
        document = _private_json(self.client_config_path) if self.client_config_path else None
        value = document.get('web') if document else None
        if (not isinstance(value,dict) or not isinstance(value.get('client_id'),str)
                or not value['client_id'] or not isinstance(value.get('client_secret'),str)
                or not value['client_secret'] or not isinstance(value.get('redirect_uris'),list)):
            raise CalendarError('google_oauth_client_required')
        return value

    def status(self):
        try:
            self._config()
            credentials = _private_json(self.credentials_path)
            authorized = bool(credentials and credentials.get('refresh_token') and not credentials.get('reauthorization_required'))
            return {'configured':True,'authorized':authorized,'reauthorization_required':not authorized,
                'error_code':self.error_code if authorized else 'google_authorization_required'}
        except CalendarError as error:
            return {'configured':False,'authorized':False,'reauthorization_required':True,'error_code':error.code}

    def _redirect(self, redirect_uri):
        config = self._config()
        try:
            parsed = urlsplit(redirect_uri)
            if (redirect_uri not in config['redirect_uris'] or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or not parsed.hostname
                    or parsed.scheme not in {'http','https'}
                    or parsed.scheme=='http' and parsed.hostname not in {'127.0.0.1','localhost','::1'}):
                raise ValueError()
        except (ValueError,TypeError):
            raise CalendarError('oauth_redirect_not_registered') from None
        return config

    def authorization_url(self, redirect_uri, state):
        config = self._redirect(redirect_uri)
        if not isinstance(state,str) or not re.fullmatch('[A-Za-z0-9_-]{32,256}',state):
            raise CalendarError('invalid_oauth_state')
        return AUTHORIZATION_URL+'?'+urlencode(dict(client_id=config['client_id'],redirect_uri=redirect_uri,
            response_type='code',scope=READONLY_SCOPE,state=state,access_type='offline',prompt='consent'))

    async def _json_request(self, method, url, **kwargs):
        try:
            async with self.client.stream(method,url,**kwargs) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body)>2097152:
                        raise CalendarError('calendar_response_too_large')
                try:
                    value = json.loads(body)
                except (ValueError,TypeError):
                    value = {}
                if not isinstance(value,dict):
                    raise CalendarError('invalid_google_response')
                return response.status_code,value
        except httpx.HTTPError:
            raise CalendarError('google_transport_failed') from None

    def _persist(self, credentials):
        parent = self.credentials_path.parent
        if parent.is_symlink() or parent.absolute()!=parent.resolve() or self.credentials_path.is_symlink():
            raise CalendarError('private_calendar_configuration_invalid')
        parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if parent.stat().st_mode&0o077:
            raise CalendarError('private_calendar_directory_required')
        atomic_json(self.credentials_path,credentials)

    async def _token(self, values, previous=None):
        status, response = await self._json_request('POST',TOKEN_URL,data=values)
        if status!=200:
            if response.get('error')=='invalid_grant':
                self.error_code = 'google_reauthorization_required'
                if previous:
                    await asyncio.to_thread(self._persist,{**previous,'reauthorization_required':True})
                raise CalendarError(self.error_code)
            raise CalendarError('google_token_exchange_failed')
        access, refresh = response.get('access_token'),response.get('refresh_token') or (previous or {}).get('refresh_token')
        expires = response.get('expires_in')
        scopes = response.get('scope',READONLY_SCOPE)
        token_type = response.get('token_type','Bearer')
        if (not isinstance(access,str) or not access or len(access)>16384
                or not isinstance(refresh,str) or not refresh or len(refresh)>16384
                or not isinstance(token_type,str) or token_type.lower()!='bearer'
                or type(expires) not in (int,float) or not math.isfinite(expires) or not 0<expires<=86400
                or not isinstance(scopes,str) or READONLY_SCOPE not in scopes.split()):
            raise CalendarError('invalid_google_token_response')
        credentials = dict(access_token=access,refresh_token=refresh,expires_at=self.clock()+expires,
            scope=READONLY_SCOPE,reauthorization_required=False)
        await asyncio.to_thread(self._persist,credentials)
        self.error_code = None
        return credentials

    async def exchange_code(self, code, redirect_uri):
        config = self._redirect(redirect_uri)
        if not isinstance(code,str) or not code or len(code)>8192:
            raise CalendarError('invalid_oauth_code')
        async with self.lock:
            await self._token(dict(grant_type='authorization_code',code=code,client_id=config['client_id'],
                client_secret=config['client_secret'],redirect_uri=redirect_uri))
        return self.status()

    async def _access_token(self, force=False):
        async with self.lock:
            credentials = await asyncio.to_thread(_private_json,self.credentials_path)
            if not credentials or credentials.get('reauthorization_required') or not credentials.get('refresh_token'):
                raise CalendarError('google_authorization_required')
            expiry = credentials.get('expires_at')
            if (not force and type(expiry) in (int,float) and math.isfinite(expiry)
                    and expiry>self.clock()+60 and isinstance(credentials.get('access_token'),str)):
                return credentials['access_token']
            config = await asyncio.to_thread(self._config)
            credentials = await self._token(dict(grant_type='refresh_token',refresh_token=credentials['refresh_token'],
                client_id=config['client_id'],client_secret=config['client_secret']),credentials)
            return credentials['access_token']

    async def _get(self, path, params=None):
        token = await self._access_token()
        status, value = await self._json_request('GET',CALENDAR_URL+path,params=params,headers={'Authorization':'Bearer '+token})
        if status==401:
            token = await self._access_token(force=True)
            status,value = await self._json_request('GET',CALENDAR_URL+path,params=params,headers={'Authorization':'Bearer '+token})
        if status==404:
            raise CalendarError('calendar_record_not_found')
        if status!=200:
            raise CalendarError('google_authorization_required' if status in {401,403} else 'calendar_request_failed')
        return value

    async def list_events(self, calendar_id, start_at, end_at):
        start, end = _time({'dateTime':start_at}),_time({'dateTime':end_at})
        if not start or not end or end<=start or not isinstance(calendar_id,str) or not 1<=len(calendar_id)<=1024:
            raise CalendarError('invalid_calendar_window')
        params = dict(timeMin=start,timeMax=end,singleEvents='true',showDeleted='true',maxResults='250',orderBy='startTime')
        events, seen = [],set()
        for _ in range(100):
            page = await self._get(quote(calendar_id,safe='')+'/events',params)
            items = page.get('items',[])
            if not isinstance(items,list):
                raise CalendarError('invalid_google_response')
            events.extend(normalize_event(item) for item in items)
            token = page.get('nextPageToken')
            if token is None:
                return events
            if not isinstance(token,str) or not token or len(token)>8192 or token in seen:
                raise CalendarError('invalid_calendar_pagination')
            seen.add(token)
            params['pageToken'] = token
        raise CalendarError('calendar_window_too_large')

    async def get_event(self, calendar_id, event_id):
        if any(not isinstance(value,str) or not 1<=len(value)<=1024 for value in (calendar_id,event_id)):
            raise CalendarError('invalid_calendar_identity')
        try:
            value = await self._get(quote(calendar_id,safe='')+'/events/'+quote(event_id,safe=''))
        except CalendarError as error:
            if error.code!='calendar_record_not_found':
                raise
            value = {'id':event_id,'status':'cancelled'}
        return normalize_event(value)

    async def close(self):
        await self.client.aclose()
