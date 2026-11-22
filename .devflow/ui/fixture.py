"""Disposable UI service using installed application and existing calendar DI.

No real Google, Live, Teams, OAuth, Join or application test-provider switch.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import tempfile

import uvicorn
from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials
from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.calendar_client import CalendarError, normalize_event
from teams_transcription.daemon import AssistantDaemon


class Calendar:
    def __init__(self):
        self.calls = 0
        self.now = datetime.now(timezone.utc).replace(microsecond=0)

    def status(self):
        return dict(authorized=True, error_code=None)

    def authorization_url(self, *args):
        raise CalendarError('google_oauth_client_required')

    async def list_events(self, *args):
        self.calls += 1
        if self.calls == 1:
            return []
        now = self.now
        return [normalize_event(dict(id=name, summary='PRIVATE-'+name,
            description='https://teams.microsoft.com/meet/12345',
            start={'dateTime':(now+timedelta(seconds=offset)).isoformat()},
            end={'dateTime':(now+timedelta(seconds=offset+3600)).isoformat()}))
            for name, offset in [('current',60), ('expired',-7200), ('later',7200)]]

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
        daemon.store.save_calendar(dict(calendar_id='synthetic', name='PRIVATE-calendar', enabled=True,
            recording_authorized=True, speaking_authorized=True))
        uvicorn.run(create_admin_app(daemon, settings), host='127.0.0.1', port=18081,
            log_level='error', access_log=False)


if __name__ == '__main__':
    main()
