"""Disposable UI service using installed application and existing calendar DI.

No real Google, Live, Teams, OAuth, Join or application test-provider switch.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile

import uvicorn
from starlette.routing import Route
from starlette.responses import JSONResponse
from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials
from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.calendar_client import CalendarError, normalize_event
from teams_transcription.daemon import AssistantDaemon


class Calendar:
    def __init__(self):
        self.calls = 0
        self.mode = "normal"
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def status(self):
        return dict(configured=self.mode != 'unconfigured',authorized=self.mode not in {'unconfigured','unauthorized'},
            reauthorization_required=self.mode in {'unconfigured','unauthorized'},
            error_code={'unconfigured':'google_oauth_client_required','unauthorized':'google_authorization_required'}.get(self.mode))

    def authorization_url(self, *args):
        raise CalendarError('google_oauth_client_required')

    async def list_events(self, *args):
        self.calls += 1
        if self.mode in {'unconfigured','unauthorized','failed'}:
            raise CalendarError({'unconfigured':'google_oauth_client_required','unauthorized':'google_authorization_required','failed':'calendar_request_failed'}[self.mode])
        if self.mode == 'empty':
            return []
        if self.calls == 1:
            return []
        now = self.now
        return [normalize_event(dict(id=name, summary='PRIVATE-'+name,
            description='https://teams.microsoft.com/meet/12345',
            start={'dateTime':(now+timedelta(seconds=offset)).isoformat()},
            end={'dateTime':(now+timedelta(seconds=offset+3600)).isoformat()}))
            for name, offset in [('current',-60), ('soon',60), ('expired',-7200), ('late',-600), ('later',7200)]]

    async def close(self):
        pass


def main():
    password = Path(os.environ.get('DEVFLOW_FIXTURE_PASSWORD_FILE', '/tmp/devflow-fixture-password')).read_text().strip()
    with tempfile.TemporaryDirectory(prefix='operate-fixture-') as folder:
        root = Path(folder)
        settings = AssistantSettings(data_root=folder, port=18081, timezone='Asia/Taipei',
            admin_password_file=str(root/'password.json'), admin_session_secret_file=str(root/'session.key'),
            google_credentials_file=str(root/'google.json'), codex_home=str(root/'empty-codex'))
        initialize_admin_credentials(settings, password)
        daemon = AssistantDaemon(settings, calendar=Calendar())
        daemon.store.update_settings({'scheduler_paused':True})
        daemon.store.save_calendar(dict(calendar_id='synthetic', name='PRIVATE-calendar', enabled=True))
        app = create_admin_app(daemon, settings)
        async def fixture_mode(request):
            mode = request.path_params['mode']
            assert mode in {'normal','zero','unconfigured','unauthorized','failed','empty'}
            daemon.calendar.mode = mode
            for calendar in daemon.store.list_calendars():
                calendar['enabled'] = mode != 'zero'
                daemon.store.save_calendar(calendar)
            return JSONResponse({'ok':True})
        app.routes.append(Route('/fixture/{mode}', fixture_mode, methods=['POST']))
        uvicorn.run(app, host='127.0.0.1', port=18081,
            log_level='error', access_log=False)


if __name__ == '__main__':
    main()
