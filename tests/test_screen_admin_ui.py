"""Admin UI screen-context mode switcher and historical screen observation viewer seam tests."""
import hashlib
import os
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from teams_transcription.admin_ui import calendar_page
from teams_transcription.assistant_config import AssistantSettings
from teams_transcription.calendar_view import meeting_view
from teams_transcription.meeting_store import MeetingStore, screen_source_id


def _sample_obs(seq, *, title, summary_text, seed):
    sha = hashlib.sha256(seed).hexdigest()
    return {
        'observation_id': f'screen:{seq:04d}:{sha[:8]}',
        'capture_seq': seq,
        'frame_sha256': sha,
        'dhash': f'{seq:016x}',
        'capture_method': 'webrtc_video_frame',
        'width': 1280,
        'height': 720,
        'captured_at': f'2026-11-24T13:5{seq}:00+00:00',
        'analyzed_at': f'2026-11-24T13:5{seq}:05+00:00',
        'vision_model': 'gpt-5.6-sol',
        'screen_kind': 'slide',
        'title': title,
        'summary_text': summary_text,
        'structured': {
            'screen_kind': 'slide',
            'title': title,
            'summary_text': summary_text,
            'key_items': [f'重點 {seq}'],
            'metrics_or_numbers': [f'{seq * 10000} USD'],
            'visible_text_excerpt': f'Slide {seq}: {title}',
            'uncertain_areas': ['右下角註腳模糊'] if seq == 2 else [],
        },
    }


class ScreenAdminUiTests(unittest.TestCase):
    def test_calendar_page_contains_screen_tab_and_calendar_js_parses(self):
        html = calendar_page({'authenticated': True, 'csrf': 'a' * 64}, 'Asia/Taipei').body.decode('utf-8')
        self.assertIn('id="tab-screen"', html)
        self.assertIn('id="panel-screen"', html)
        self.assertIn('id="screen-count"', html)
        self.assertIn('id="screen-list"', html)

        js_path = Path(__file__).resolve().parent.parent / 'teams_transcription' / 'static' / 'calendar.js'
        res = subprocess.run(['node', '--check', str(js_path)], capture_output=True, text=True)
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_settings_screen_policy_action_persists_to_store_and_propagates_to_daemon_config(self):
        from starlette.testclient import TestClient
        from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials
        from teams_transcription.assistant_store import AssistantStore
        from teams_transcription.daemon import AssistantDaemon

        with tempfile.TemporaryDirectory() as root:
            settings = AssistantSettings(
                data_root=root,
                port=18441,
                admin_password_file=str(Path(root) / 'password.json'),
                admin_session_secret_file=str(Path(root) / 'session.key'),
                google_credentials_file=str(Path(root) / 'google.json'),
                screen_context_mode='off',
                screen_include_in_summary=True,
            )
            initialize_admin_credentials(settings, 'correct-horse-battery-staple')
            store = AssistantStore(root)

            class _FakeDaemon:
                def __init__(self):
                    self.store = store
                    self.settings = settings
                    self.auth_operation_in_progress = False
                    self.closing = types.SimpleNamespace(is_set=lambda: False)
                    import asyncio
                    self._join_lock = asyncio.Lock()

                def status(self):
                    return {
                        'started': True, 'active_session_id': None, 'capture_interlock': False,
                        'background_task_count': 0, 'last_poll_at': None, 'last_error': None,
                        'google': {'authorized': False, 'error_code': None},
                        'calendar_errors': [], 'provider': {'provider_status': 'unchecked'},
                    }

                def google_authorization_url(self, *args, **kwargs):
                    from teams_transcription.calendar_client import CalendarError
                    raise CalendarError('google_oauth_client_required')

                async def start(self):
                    pass

                async def close(self):
                    pass

            daemon = _FakeDaemon()
            app = create_admin_app(daemon, settings)
            with TestClient(app, base_url='http://127.0.0.1:18441') as client:
                login_page = client.get('/login').text
                csrf = login_page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
                resp = client.post(
                    '/login',
                    data={'csrf': csrf, 'password': 'correct-horse-battery-staple'},
                    headers={'origin': 'http://127.0.0.1:18441'},
                    follow_redirects=False,
                )
                self.assertEqual(resp.status_code, 303)

                settings_html = client.get('/settings').text
                self.assertIn('分享畫面理解', settings_html)
                self.assertIn('name="screen_context_mode"', settings_html)
                self.assertIn('on_demand', settings_html)
                self.assertIn('watch_and_cache', settings_html)
                csrf2 = settings_html.split('action="/actions/screen-policy"', 1)[1].split('name="csrf" value="', 1)[1].split('"', 1)[0]

                # Switch to Phase 2 (watch_and_cache) + include in summary unchecked -> False
                save_resp = client.post(
                    '/actions/screen-policy',
                    data={'csrf': csrf2, 'screen_context_mode': 'watch_and_cache'},
                    headers={'origin': 'http://127.0.0.1:18441'},
                    follow_redirects=False,
                )
                self.assertEqual(save_resp.status_code, 303)
                self.assertEqual(save_resp.headers['location'], '/settings')
                stored = store.settings()
                self.assertEqual(stored['screen_context_mode'], 'watch_and_cache')
                self.assertFalse(stored['screen_include_in_summary'])

                # Switch to Phase 1 (on_demand) + include in summary checked -> True
                save_p1 = client.post(
                    '/actions/screen-policy',
                    data={'csrf': csrf2, 'screen_context_mode': 'on_demand', 'screen_include_in_summary': 'on'},
                    headers={'origin': 'http://127.0.0.1:18441'},
                    follow_redirects=False,
                )
                self.assertEqual(save_p1.status_code, 303)
                stored2 = store.settings()
                self.assertEqual(stored2['screen_context_mode'], 'on_demand')
                self.assertTrue(stored2['screen_include_in_summary'])

                # Invalid mode rejected
                bad = client.post(
                    '/actions/screen-policy',
                    data={'csrf': csrf2, 'screen_context_mode': 'bogus_mode'},
                    headers={'origin': 'http://127.0.0.1:18441'},
                )
                self.assertEqual(bad.status_code, 422)

                # Verify real AssistantDaemon._config picks up persisted UI policy
                real_daemon = AssistantDaemon(settings, store=store)
                occ = {'join_url': 'https://teams.microsoft.com/l/meetup-join/test'}
                run = {'session_id': 'a' * 32, 'prior_context': {}, 'glossary_snapshot': {'version': 0, 'entries': [], 'omitted_count': 0}}
                cfg = real_daemon._config(run, occ)
                try:
                    self.assertEqual(cfg['screen_context_mode'], 'on_demand')
                    self.assertTrue(cfg['screen_include_in_summary'])
                finally:
                    Path(cfg['runtime_dir']).rmdir()

    def test_history_detail_and_meeting_view_display_screen_observations_and_citation_anchors(self):
        from starlette.testclient import TestClient
        from teams_transcription.admin_web import create_admin_app, initialize_admin_credentials
        from teams_transcription.assistant_store import AssistantStore

        with tempfile.TemporaryDirectory() as root:
            settings = AssistantSettings(
                data_root=root,
                port=18442,
                admin_password_file=str(Path(root) / 'password.json'),
                admin_session_secret_file=str(Path(root) / 'session.key'),
                google_credentials_file=str(Path(root) / 'google.json'),
            )
            initialize_admin_credentials(settings, 'correct-horse-battery-staple')
            store = AssistantStore(root)
            occ = store.create_manual_occurrence({
                'title': '產品評審會議',
                'join_url': 'https://teams.microsoft.com/l/meetup-join/demo',
                'start_at': '2026-11-24T13:00:00+00:00',
                'end_at': '2026-11-24T14:00:00+00:00',
                'recording_authorized': True,
                'speaking_authorized': True,
            })
            claimed = store.claim_run(occ['id'], 'owner-1', lease_seconds=60)
            session_id = claimed['session_id']
            fenced = MeetingStore(root, lease=claimed)
            fenced.append_final(session_id, {
                'event_id': 'live:1', 'revision': 0, 'text': '這頁是第一季與第二季對比。',
                'timestamp': '2026-11-24T13:50:00+00:00', 'sequence': 1,
            })
            obs1 = _sample_obs(1, title='第一季營收簡報', summary_text='第一季營收為 10,000 USD。', seed=b's1')
            obs2 = _sample_obs(2, title='第二季預測簡報', summary_text='第二季預測為 20,000 USD。', seed=b's2')
            fenced.record_screen_observation(session_id, obs1)
            fenced.record_screen_observation(session_id, obs2)
            fenced.finalize(session_id, 'completed')

            # 1) meeting_view API projection contains screen_observations
            mv = meeting_view(store, occ['id'], 'Asia/Taipei')
            self.assertEqual(len(mv['screen_observations']), 2)
            self.assertEqual(mv['screen_observations'][0]['title'], '第一季營收簡報')
            self.assertEqual(mv['screen_observations'][1]['id'], screen_source_id(obs2['observation_id']))

            # 2) /history/{session_id} renders screen observations with #source-V... anchors
            class _FakeDaemon:
                def __init__(self):
                    self.store = store
                    self.settings = settings
                    self.auth_operation_in_progress = False
                    self.closing = types.SimpleNamespace(is_set=lambda: False)
                    import asyncio
                    self._join_lock = asyncio.Lock()

                def status(self):
                    return {
                        'started': True, 'active_session_id': None, 'capture_interlock': False,
                        'background_task_count': 0, 'last_poll_at': None, 'last_error': None,
                        'google': {'authorized': False, 'error_code': None},
                        'calendar_errors': [], 'provider': {'provider_status': 'unchecked'},
                    }

                async def start(self):
                    pass

                async def close(self):
                    pass

            app = create_admin_app(_FakeDaemon(), settings)
            with TestClient(app, base_url='http://127.0.0.1:18442') as client:
                login_page = client.get('/login').text
                csrf = login_page.split('name="csrf" value="', 1)[1].split('"', 1)[0]
                client.post('/login', data={'csrf': csrf, 'password': 'correct-horse-battery-staple'},
                            headers={'origin': 'http://127.0.0.1:18442'}, follow_redirects=False)

                hist_html = client.get(f'/history/{session_id}').text
                self.assertIn('分享畫面觀察', hist_html)
                self.assertIn('第一季營收簡報', hist_html)
                self.assertIn('第二季預測簡報', hist_html)
                self.assertIn('右下角註腳模糊', hist_html)
                self.assertIn(f'id="source-{screen_source_id(obs1["observation_id"])}"', hist_html)

                api_data = client.get(f'/api/meetings/{occ["id"]}').json()
                self.assertEqual(len(api_data['screen_observations']), 2)


class ScreenCalendarBrowserUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_calendar_workspace_screen_tab_renders_observations_in_sandboxed_chromium(self):
        from teams_transcription.browser import Browser
        exe = os.environ.get('ASSISTANT_CHROMIUM') or os.environ.get('MEETING_CHROMIUM')

        html = calendar_page({'authenticated': True, 'csrf': 'b' * 64}, 'Asia/Taipei',
                             initial_occurrence_id='occ-1').body.decode('utf-8')
        static_dir = Path(__file__).resolve().parent.parent / 'teams_transcription' / 'static'
        css_bytes = (static_dir / 'calendar.css').read_bytes()
        js_bytes = (static_dir / 'calendar.js').read_bytes()

        async with Browser(executable=exe) as runtime:
            assert runtime.browser is not None
            ctx = await runtime.browser.new_context(viewport={'width': 1280, 'height': 800})
            page = await ctx.new_page()

            async def route_handler(route):
                url = route.request.url
                if url.endswith('/assets/calendar.css'):
                    await route.fulfill(status=200, body=css_bytes, content_type='text/css')
                elif url.endswith('/assets/calendar.js'):
                    await route.fulfill(status=200, body=js_bytes, content_type='application/javascript')
                elif '/api/calendar' in url:
                    await route.fulfill(status=200, content_type='application/json', body=
                        '{"meetings":[{"id":"occ-1","title":"設計評審","kind":"single","series_id":"s1","series_name":"s1",'
                        '"state":"completed","state_label":"已結束","session_id":"sess1","is_live":false,'
                        '"start_at":"2026-11-24T13:00:00+08:00","start_date":"2026-11-24","start_time":"13:00",'
                        '"end_at":"2026-11-24T14:00:00+08:00","end_date":"2026-11-24","end_time":"14:00"}],'
                        '"series":[],"undated_count":0,"timezone":"Asia/Taipei","today":"2026-11-24",'
                        '"now":"2026-11-24T14:30:00+08:00","active_session_id":null,"scheduler_paused":true,'
                        '"capture_interlock":false,"google":{"authorized":false,"error_code":null},"last_error":null}')
                elif '/api/meetings/occ-1' in url:
                    await route.fulfill(status=200, content_type='application/json', body=
                        '{"meeting":{"id":"occ-1","title":"設計評審","kind":"single","series_id":"s1","series_name":"s1",'
                        '"state":"completed","state_label":"已結束","session_id":"sess1","is_live":false,'
                        '"start_at":"2026-11-24T13:00:00+08:00","start_date":"2026-11-24","start_time":"13:00",'
                        '"end_at":"2026-11-24T14:00:00+08:00","end_date":"2026-11-24","end_time":"14:00"},'
                        '"session_id":"sess1","run_state":"completed","runs":[],"auto_join":{"label":""},'
                        '"response":{"state":"stopped","output_enabled":false,"output_muted":true,"error_code":null,'
                        '"screen_context":{"mode":"watch_and_cache","observations_saved":2,"share_active":false}},'
                        '"previous":null,"tasks":[],"series_meetings":[],"series_state":null,"task_workspace":null,'
                        '"postmeeting":[],"latest_recorded_meeting":null,"transcript":[],"transcript_paragraphs":[],'
                        '"qa":[],"summary":null,"summary_job":null,"source_version":3,"completeness":"complete",'
                        '"controls":{},"cleanup_review_required":false,"updated_at":"2026-11-24T13:55:00+00:00",'
                        '"screen_observations":['
                        '{"id":"V0001","observation_id":"screen:0001:aaaa1111","capture_seq":1,'
                        '"captured_at":"2026-11-24T13:51:00+00:00","screen_kind":"slide","title":"架構圖 V1",'
                        '"summary_text":"展示 A/B 雙通道與 SQLite。","capture_method":"webrtc_video_frame",'
                        '"width":1280,"height":720,"vision_model":"gpt-5.6-sol",'
                        '"structured":{"key_items":["A 通道錄音","B 通道問答"],"metrics_or_numbers":["42 ms"],"uncertain_areas":[]}},'
                        '{"id":"V0002","observation_id":"screen:0002:bbbb2222","capture_seq":2,'
                        '"captured_at":"2026-11-24T13:54:00+00:00","screen_kind":"spreadsheet","title":"成本試算表",'
                        '"summary_text":"列出各服務月費。","capture_method":"teams_stage_dom",'
                        '"width":1000,"height":560,"vision_model":"gpt-5.6-sol",'
                        '"structured":{"key_items":["GPU 租用"],"metrics_or_numbers":["120,000 USD"],"uncertain_areas":["最底行備註模糊"]}}'
                        ']}')
                elif url == 'http://127.0.0.1:18443/':
                    await route.fulfill(status=200, body=html, content_type='text/html')
                else:
                    await route.abort()

            await page.route('**/*', route_handler)
            errors = []
            page.on('pageerror', lambda exc: errors.append(str(exc)))
            await page.goto('http://127.0.0.1:18443/', wait_until='domcontentloaded')
            await page.wait_for_function("document.getElementById('screen-count').textContent === '2'")

            # Click Screen tab and verify rendered cards + zero JS errors
            await page.click('#tab-screen')
            self.assertFalse(await page.locator('#panel-screen').is_hidden())
            panel_text = await page.locator('#screen-list').inner_text()
            self.assertIn('架構圖 V1', panel_text)
            self.assertIn('成本試算表', panel_text)
            self.assertIn('120,000 USD', panel_text)
            self.assertIn('最底行備註模糊', panel_text)
            self.assertEqual(errors, [])
            await ctx.close()


if __name__ == '__main__':
    unittest.main()
