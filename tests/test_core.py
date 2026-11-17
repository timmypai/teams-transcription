import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from teams_transcription.browser import meeting_platform, browser_join_url
from teams_transcription.live import TranscriptNormalizer
from teams_transcription.storage import Session
from teams_transcription.meeting_store import MeetingStore
from teams_transcription.summary import validate_summary, SECTIONS

ROOT = Path(__file__).resolve().parent.parent


class MeetingLinks(unittest.TestCase):
    def test_teams_link(self):
        for url in ["https://teams.microsoft.com/l/meetup-join/test?context=private", "https://teams.live.com/meet/123", "https://teams.cloud.microsoft/meet/123"]:
            self.assertEqual(meeting_platform(url), "teams")

    def test_zoom_link(self):
        self.assertEqual(meeting_platform("https://us02web.zoom.us/j/123456789?pwd=private"), "zoom")
        self.assertEqual(browser_join_url("https://us02web.zoom.us/j/123456789?pwd=private"), "https://us02web.zoom.us/wc/123456789/join?pwd=private")

    def test_reject_deceptive_hosts_and_nonmeetings(self):
        for url in ["http://teams.microsoft.com/l/meetup-join/a", "https://teams.microsoft.com.attacker.com/l/meetup-join/a", "https://zoom.us.attacker.com/j/1", "https://attacker@teams.microsoft.com/l/meetup-join/a", "https://teams.microsoft.com:8443/meet/1", "https://teams.microsoft.com/", "file:///tmp/x"]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                meeting_platform(url)


class TranscriptTests(unittest.TestCase):
    def test_generated_replies_do_not_become_source(self):
        normal = TranscriptNormalizer()
        self.assertIsNone(normal.source("thread/realtime/transcript/done", {"role": "assistant", "text": "invented answer"}, 1))
        self.assertEqual(normal.source("thread/realtime/transcript/done", {"role": "user", "text": "actual input"}, 1), "actual input")

    def test_canonical_flat_duplicate_and_repeated_speech(self):
        normal = TranscriptNormalizer()
        self.assertEqual(normal.source("thread/realtime/transcript/done", {"role": "user", "text": "yes"}, 1), "yes")
        item = {"id": "a", "role": "user", "type": "transcriptSegment", "text": "yes"}
        self.assertIsNone(normal.source("thread/realtime/item/completed", {"item": item}, 1.1))
        self.assertIsNone(normal.source("thread/realtime/item/completed", {"item": item}, 3))
        item["id"] = "b"
        self.assertEqual(normal.source("thread/realtime/item/completed", {"item": item}, 4), "yes")
        self.assertIsNone(normal.source("thread/realtime/transcript/done", {"role": "user", "text": "yes"}, 4.1))

    def test_nonfinal_and_other_events_ignored(self):
        self.assertIsNone(TranscriptNormalizer().source("thread/realtime/transcript/delta", {"role": "user", "delta": "partial"}, 1))


class CitationTests(unittest.TestCase):
    def test_reject_unknown_or_missing_sources(self):
        for sources in [[], ["S9999"], [None]]:
            value = {s: [] for s in SECTIONS}
            value["重點"] = [{"text": "test", "sources": sources, "owner": "", "date": ""}]
            with self.assertRaises(ValueError):
                validate_summary(value, [{"id": "S0001"}])

    def test_valid_cited_summary(self):
        value = {s: [] for s in SECTIONS}
        value["待辦"] = [{"text": "Review report", "sources": ["S0001"], "owner": "", "date": ""}]
        self.assertEqual(validate_summary(value, [{"id": "S0001"}]), value)


class CLITests(unittest.TestCase):
    def test_admin_bootstrap_stdin_never_overwrites_private_credentials_or_prints_password(self):
        with tempfile.TemporaryDirectory() as root:
            path=Path(root)
            password=path/'config/password.json'
            secret=path/'config/session.key'
            config=path/'runtime.yml'
            config.write_text(json.dumps(dict(data_root=root,admin_password_file=str(password),
                admin_session_secret_file=str(secret),google_credentials_file=str(path/'config/google.json'))))
            command=[sys.executable,'-m','teams_transcription','init-admin','--config',str(config),'--password-stdin']
            first=subprocess.run(command,input='controlled-first-password\n',cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertEqual(first.returncode,0,first.stdout+first.stderr)
            self.assertTrue(json.loads(first.stdout)['admin_initialized'])
            original=(password.read_bytes(),secret.read_bytes())
            second=subprocess.run(command,input='controlled-second-password\n',cwd=ROOT,capture_output=True,text=True,timeout=10)
            self.assertNotEqual(second.returncode,0)
            self.assertEqual((password.read_bytes(),secret.read_bytes()),original)
            self.assertNotIn('controlled-first-password',first.stdout+first.stderr)
            self.assertNotIn('controlled-second-password',second.stdout+second.stderr)
            self.assertEqual(password.stat().st_mode&0o777,0o600)
            self.assertEqual(secret.stat().st_mode&0o777,0o600)

    def test_lost_worker_and_invalid_session_id(self):
        with tempfile.TemporaryDirectory() as root:
            session=Session(root).create({'provider':'codex','source':'meeting'})
            session.update(state='recording',pid=99999999,process_signature='x')
            self.assertEqual(session.observed_status()['state'],'worker_lost')
            with self.assertRaises(ValueError):
                Session(root,'../../etc')
