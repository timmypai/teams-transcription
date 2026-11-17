"""Controlled RX-to-SQLite integration, with an injected Live peer.

The page and speech are synthetic. These tests never join Teams or call a model.
"""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from teams_transcription.browser import MeetingBrowser
from teams_transcription.browser import LivePeer
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.storage import Session
from teams_transcription.worker import run_session
from lifecycle_probe import session_events


class InjectedLive:
    instances = []

    def __init__(self, browser, state_dir, on_text, on_event, executable, ice_servers=None, *, media_source=None, on_final=None, lookup_final=None, bind_alias=None, glossary_snapshot=None):
        self.on_final = on_final
        self.media_source = media_source
        self.peer = LivePeer(browser, AsyncMock(), ice_servers, media_source=media_source)
        self.final_emitted = False
        self.error = None
        self.pushes = 0
        self.closed = False
        self.instances.append(self)

    async def start(self):
        await self.peer.offer()

    async def push(self, data):
        self.pushes += 1
        raise AssertionError('native meeting input must never push Python PCM')

    async def ready(self):
        source = (await self.peer.stats()).get('native_source', {})
        if source.get('ready') is not True or source.get('failed') is not False:
            return False
        if not self.final_emitted:
            self.final_emitted = True
            event = {'event_id':'fixture-one', 'revision':0, 'text':'測試結論',
                     'timestamp':'2026-10-09T10:00:00Z', 'sequence':1}
            await self.on_final(event)
            await self.on_final(event)
            await self.on_final({**event, 'revision':1, 'text':'測試更正結論'})
        return True

    async def drain(self, seconds):
        pass

    async def close(self):
        await self.peer.close()
        self.closed = True


ORIGINAL_OPEN = MeetingBrowser.open


async def local_open(self, url=None, storage_state=None):
    await ORIGINAL_OPEN(self)
    await self.context.route('**/*', lambda route: route.abort())
    await self.context.route('https://native-output.invalid/**', lambda route:
        route.fulfill(content_type='text/html',
            body='<button>Leave</button><button>Participants</button>'))
    await self.page.goto('https://native-output.invalid/')
    await self.page.evaluate('''async () => {
      const c=new AudioContext({sampleRate:24000}); await c.resume();
      const o=c.createOscillator(), d=c.createMediaStreamDestination();
      o.connect(d); o.start(); window.fixture={c,o,d};
      await __meeting.attach(d.stream.getAudioTracks()[0]);
    }''')


async def local_summary(session, *args):
    raise AssertionError('automatic summary must not be requested')


class WorkerMinutesTests(unittest.IsolatedAsyncioTestCase):
    def config(self):
        return {'provider':'codex', 'source':'meeting', 'meeting_url':'https://teams.microsoft.com/meet/offline-fixture',
                'codex_bin':'unused-fixture', 'duration':.25, 'name':'Fixture', 'join_timeout':1,
                'audio_timeout':1,'drain_seconds':.01,'save_audio':False}

    async def test_shared_summary_helper_cannot_be_swallowed_as_worker_success(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(directory).create(self.config())
            with self.assertRaisesRegex(AssertionError, 'automatic summary must not be requested'):
                await local_summary(session)

            # Inject the forbidden call before a browser is opened. This checks
            # the real worker's outer error handling without media or a model.
            async def forbidden_call(*args, **kwargs):
                await local_summary(session)
            with patch('teams_transcription.worker.Browser.__aenter__', side_effect=forbidden_call), \
                    patch('teams_transcription.worker.LiveProvider') as live:
                self.assertEqual(await run_session(session, install_signals=False), 1)
            live.assert_not_called()
            self.assertEqual(session.status()['state'], 'failed')
            self.assertEqual(session.status()['summary_state'], 'not_requested')
            result = MeetingStore(directory).read(session.ident)
            self.assertTrue(result['terminal'])
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error_code'], 'browser_open_failed')
            self.assertFalse((session.path / 'summary.md').exists())
            self.assertFalse((session.path / 'summary.json').exists())

    async def test_browser_closed_preserves_safe_type_and_failed_minutes(self):
        from playwright._impl._errors import TargetClosedError
        with tempfile.TemporaryDirectory() as directory:
            session = Session(directory).create(self.config())
            private = 'private-token SDP v=0 https://teams.microsoft.com/meet/private'
            with patch('teams_transcription.worker.Browser.__aenter__',
                       AsyncMock(side_effect=TargetClosedError(private))), \
                    patch('teams_transcription.worker.LiveProvider') as live:
                self.assertEqual(await run_session(session, install_signals=False), 1)
            live.assert_not_called()
            diagnostic = session.status()['diagnostic']
            self.assertEqual(diagnostic['phase'], 'browser_open')
            self.assertEqual(diagnostic['exception_type'], 'TargetClosedError')
            self.assertEqual(diagnostic['error_code'], 'operation_failed')
            self.assertEqual(MeetingStore(directory).read(session.ident)['status'], 'failed')
            diagnostics=json.dumps({'status':session.status(),'events':session_events(session)},ensure_ascii=False)
            self.assertNotIn(private,diagnostics)
            self.assertNotIn('private-token',diagnostics)

    async def test_local_rx_final_revisions_reach_disk_without_audio_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(directory).create(self.config())
            old_summary = '# 既有合成摘要，保留原檔。\n'.encode()
            (session.path / 'summary.md').write_bytes(old_summary)
            with patch('teams_transcription.worker.LiveProvider', InjectedLive), \
                    patch.object(MeetingBrowser, 'open', local_open), \
                    patch('teams_transcription.worker.summary.generate', new_callable=AsyncMock) as generate:
                self.assertEqual(await run_session(session), 0)
            generate.assert_not_called()
            self.assertEqual(session.status()['state'], 'completed')
            self.assertEqual(session.status()['summary_state'], 'not_requested')
            result = MeetingStore(directory).read(session.ident)
            self.assertTrue(result['terminal'])
            self.assertEqual(result['status'], 'completed')
            self.assertIsNone(result['error_code'])
            self.assertEqual(len(result['events']), 1)
            self.assertEqual(result['events'][0]['revision'], 1)
            self.assertEqual(result['events'][0]['text'], '測試更正結論')
            self.assertEqual(result['transcript_state']['provider_final_count'], 1)
            self.assertEqual(session.entries()[0]['text'], '測試更正結論')
            self.assertNotIn('summary_state',result)
            self.assertEqual((session.path / 'summary.md').read_bytes(), old_summary)
            self.assertFalse((session.path / 'summary.json').exists())
            (session.path / 'summary.md').write_text('外部檔案修改不影響終態快照', encoding='utf-8')
            self.assertEqual(MeetingStore(directory).read(session.ident)['summary'], result['summary'])
            self.assertFalse((session.path / 'audio.wav').exists())
            self.assertFalse((session.path / 'source.wav').exists())
            self.assertEqual(InjectedLive.instances[-1].pushes, 0)
            self.assertTrue(InjectedLive.instances[-1].closed)
            logs = json.dumps(session_events(session),ensure_ascii=False)
            self.assertIn('rx_ready', logs)
            self.assertNotIn('offline-fixture', logs)
            self.assertNotIn('測試更正結論', logs)


    async def test_rx_start_failure_persists_safe_phase_and_failed_source(self):
        async def broken_start(self, **kwargs):
            raise RuntimeError('private-token SDP v=0 https://teams.microsoft.com/meet/private')
        with tempfile.TemporaryDirectory() as directory:
            session = Session(directory).create(self.config())
            with patch('teams_transcription.worker.LiveProvider', InjectedLive), \
                    patch.object(MeetingBrowser, 'open', local_open), \
                    patch.object(MeetingBrowser, 'start_capture', broken_start):
                self.assertEqual(await run_session(session), 1)
            diagnostic = session.status()['diagnostic']
            self.assertEqual(diagnostic['phase'], 'rx_start')
            result = MeetingStore(directory).read(session.ident)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(result['error_code'], 'rx_start_failed')
            self.assertEqual(result['events'], [])
            diagnostics=json.dumps({'status':session.status(),'events':session_events(session)},ensure_ascii=False)
            for private in ('private-token','v=0','/meet/private'):
                self.assertNotIn(private,diagnostics)
            self.assertFalse((session.path / 'audio.wav').exists())
            self.assertTrue(InjectedLive.instances[-1].closed)

    async def test_failed_live_delivery_retains_latest_finals_and_no_audio(self):
        class FailedLive(InjectedLive):
            async def drain(self, seconds):
                raise RuntimeError('fixture private provider failure')
        with tempfile.TemporaryDirectory() as directory:
            session = Session(directory).create(self.config())
            with patch('teams_transcription.worker.LiveProvider', FailedLive), \
                    patch.object(MeetingBrowser, 'open', local_open):
                self.assertEqual(await run_session(session), 1)
            result = MeetingStore(directory).read(session.ident)
            self.assertEqual(result['status'], 'failed')
            self.assertEqual(len(result['events']), 1)
            self.assertFalse((session.path / 'audio.wav').exists())

    async def test_stop_before_start_is_terminal_without_provider_or_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Session(directory).create(self.config())
            session.stop()
            with patch('teams_transcription.worker.LiveProvider') as live, \
                    patch('teams_transcription.worker.Browser') as browser:
                self.assertEqual(await run_session(session), 0)
            live.assert_not_called()
            browser.assert_not_called()
            self.assertEqual(MeetingStore(directory).read(session.ident)['status'], 'stopped')
